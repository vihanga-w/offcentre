// system-audio-tap: exposes the Mac's system mix as an input device, without BlackHole.
//
// Creates a Core Audio process tap (macOS 14.2+) over every process except the ones passed with
// --exclude-pid, set to mute the original sound while the tap is being read, and wraps it in a
// public aggregate device that other processes (PortAudio in offcentre.py) can open as a
// 2-channel input. The aggregate is clocked by the current default output device.
//
// Prints "READY <device name> ... bits=N int|float rate=R" on stdout once the device exists, the
// last part being the output's physical format so the caller knows when to dither. Removes the tap and the device
// on SIGINT/SIGTERM or when stdin closes (i.e. the parent process went away).
//
// Build: swiftc -O system_audio_tap.swift -o system-audio-tap

import CoreAudio
import Foundation

let deviceName = "Offcentre Tap"
let deviceUID = "offcentre.system-tap"

func fail(_ message: String, _ code: Int32 = 1) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    exit(code)
}

func systemAddress(_ selector: AudioObjectPropertySelector) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(
        mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
}

func processObject(for pid: pid_t) -> AudioObjectID {
    var address = systemAddress(kAudioHardwarePropertyTranslatePIDToProcessObject)
    var qualifier = pid
    var object = AudioObjectID(kAudioObjectUnknown)
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    let status = AudioObjectGetPropertyData(
        AudioObjectID(kAudioObjectSystemObject), &address,
        UInt32(MemoryLayout<pid_t>.size), &qualifier, &size, &object)
    return status == noErr ? object : AudioObjectID(kAudioObjectUnknown)
}

func device(forUID uid: String) -> AudioObjectID {
    var address = systemAddress(kAudioHardwarePropertyTranslateUIDToDevice)
    var qualifier = uid as CFString
    var device = AudioObjectID(kAudioObjectUnknown)
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    let status = withUnsafeMutablePointer(to: &qualifier) {
        AudioObjectGetPropertyData(
            AudioObjectID(kAudioObjectSystemObject), &address,
            UInt32(MemoryLayout<CFString>.size), $0, &size, &device)
    }
    return status == noErr ? device : AudioObjectID(kAudioObjectUnknown)
}

func defaultOutputUID() -> String {
    var address = systemAddress(kAudioHardwarePropertyDefaultOutputDevice)
    var device = AudioObjectID(kAudioObjectUnknown)
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    guard AudioObjectGetPropertyData(
        AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size, &device) == noErr,
        device != kAudioObjectUnknown
    else { fail("No default output device.") }

    // An aggregate (including a Multi-Output Device) can't be nested inside our aggregate.
    address = systemAddress(kAudioDevicePropertyTransportType)
    var transport: UInt32 = 0
    size = UInt32(MemoryLayout<UInt32>.size)
    if AudioObjectGetPropertyData(device, &address, 0, nil, &size, &transport) == noErr,
        transport == kAudioDeviceTransportTypeAggregate
    {
        fail("The system output is a Multi-Output or aggregate device, which can't be tapped. "
            + "Choose the speakers themselves (e.g. your HomePods) in Sound > Output.", 4)
    }

    address = systemAddress(kAudioDevicePropertyDeviceUID)
    var uid: Unmanaged<CFString>?
    size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    guard AudioObjectGetPropertyData(device, &address, 0, nil, &size, &uid) == noErr,
        let value = uid?.takeRetainedValue()
    else { fail("Could not read the default output device's UID.") }
    return value as String
}

/// Bit depth and rate the default output device actually runs at (its first output stream's
/// physical format). AirPlay reports 16-bit integer at 44.1 kHz: ALAC, CD-quality lossless.
func defaultOutputPhysicalFormat() -> (bits: UInt32, integer: Bool, rate: Double) {
    var address = systemAddress(kAudioHardwarePropertyDefaultOutputDevice)
    var device = AudioObjectID(kAudioObjectUnknown)
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size, &device)

    address = AudioObjectPropertyAddress(
        mSelector: kAudioDevicePropertyStreams, mScope: kAudioObjectPropertyScopeOutput,
        mElement: kAudioObjectPropertyElementMain)
    var stream = AudioObjectID(kAudioObjectUnknown)
    size = UInt32(MemoryLayout<AudioObjectID>.size)  // first stream is enough
    guard AudioObjectGetPropertyData(device, &address, 0, nil, &size, &stream) == noErr,
        stream != kAudioObjectUnknown
    else { return (0, false, 0) }

    address = systemAddress(kAudioStreamPropertyPhysicalFormat)
    var format = AudioStreamBasicDescription()
    size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
    guard AudioObjectGetPropertyData(stream, &address, 0, nil, &size, &format) == noErr
    else { return (0, false, 0) }
    let integer = format.mFormatFlags & kAudioFormatFlagIsFloat == 0
    return (format.mBitsPerChannel, integer, format.mSampleRate)
}

// --- arguments ---------------------------------------------------------------------------------

var excludedPIDs: [pid_t] = []
var args = CommandLine.arguments.dropFirst()
while let arg = args.popFirst() {
    switch arg {
    case "--exclude-pid":
        guard let value = args.popFirst(), let pid = pid_t(value) else { fail("--exclude-pid needs a PID") }
        excludedPIDs.append(pid)
    default:
        fail("usage: system-audio-tap [--exclude-pid PID]...")
    }
}

// Every excluded process must already be a Core Audio client. If the caller's own output were
// tapped, it would be fed back into itself, so refuse rather than run without the exclusion.
var excluded: [AudioObjectID] = []
for pid in excludedPIDs {
    let object = processObject(for: pid)
    if object == kAudioObjectUnknown {
        fail("Process \(pid) is not a Core Audio client yet; cannot exclude it from the tap.", 3)
    }
    excluded.append(object)
}

// A previous run that was killed with SIGKILL can leave the device behind.
let stale = device(forUID: deviceUID)
if stale != kAudioObjectUnknown {
    AudioHardwareDestroyAggregateDevice(stale)
}

// --- tap + aggregate device --------------------------------------------------------------------

let description = CATapDescription(stereoGlobalTapButExcludeProcesses: excluded)
description.name = deviceName
description.muteBehavior = .mutedWhenTapped
description.isPrivate = false

var tapID = AudioObjectID(kAudioObjectUnknown)
var status = AudioHardwareCreateProcessTap(description, &tapID)
if status != noErr {
    fail("Creating the process tap failed (OSStatus \(status)). macOS 14.2 or later is required.")
}

let outputUID = defaultOutputUID()
let composition: [String: Any] = [
    kAudioAggregateDeviceNameKey: deviceName,
    kAudioAggregateDeviceUIDKey: deviceUID,
    kAudioAggregateDeviceMainSubDeviceKey: outputUID,
    kAudioAggregateDeviceIsPrivateKey: false,
    kAudioAggregateDeviceIsStackedKey: false,
    kAudioAggregateDeviceTapAutoStartKey: true,
    kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: outputUID]],
    kAudioAggregateDeviceTapListKey: [
        [kAudioSubTapDriftCompensationKey: true, kAudioSubTapUIDKey: description.uuid.uuidString]
    ],
]

var aggregateID = AudioObjectID(kAudioObjectUnknown)
status = AudioHardwareCreateAggregateDevice(composition as CFDictionary, &aggregateID)
if status != noErr {
    AudioHardwareDestroyProcessTap(tapID)
    fail("Creating the aggregate device failed (OSStatus \(status)).")
}

func cleanUpAndExit() -> Never {
    AudioHardwareDestroyAggregateDevice(aggregateID)
    AudioHardwareDestroyProcessTap(tapID)
    exit(0)
}

var signalSources: [DispatchSourceSignal] = []
for sig in [SIGINT, SIGTERM, SIGHUP] {
    signal(sig, SIG_IGN)
    let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    source.setEventHandler { cleanUpAndExit() }
    source.resume()
    signalSources.append(source)
}

// stdin closing means the parent is gone; clean up rather than leave a muted system behind.
Thread {
    while FileHandle.standardInput.availableData.count > 0 {}
    DispatchQueue.main.async { cleanUpAndExit() }
}.start()

let physical = defaultOutputPhysicalFormat()
print("READY \(deviceName) (output clock: \(outputUID)) "
    + "bits=\(physical.bits) \(physical.integer ? "int" : "float") rate=\(Int(physical.rate))")
fflush(stdout)
dispatchMain()
