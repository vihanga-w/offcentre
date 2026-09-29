#!/usr/bin/env python3
"""
offcentre - real-time phantom-centre correction for an asymmetric listening position.

Signal path (macOS), default "tap" source:

    apps -> [Core Audio process tap, originals muted] -> this script -> system output (AirPlay/HomePods)

The tap (macOS 14.2+) captures the system mix while the system output stays on the real speakers,
which matters for AirPlay: macOS only creates the AirPlay device while it is the selected output,
so it cannot be reached from behind BlackHole. The "blackhole" source is kept for wired speakers:

    apps -> system output "BlackHole 2ch" -> this script -> speakers / DAC

The speaker nearer to the listener is delayed so both wavefronts arrive together, and
attenuated by the inverse-distance law so both arrive at the same level:

    delay  = (far - near) / c            (3 m / 343 m/s = 8.746 ms)
    gain   = near / far                  (1 m / 4 m = 0.25 = -12.04 dB)

Sound *intensity* falls as 1/r^2, so pressure *amplitude* falls as 1/r; the 0.25 amplitude
ratio and the -12.04 dB figure are the same thing. We attenuate the near side rather than
boost the far side, so the output can never clip.

Architecture: the input device (BlackHole) and the output device (AirPlay) run on independent
clocks with independent buffer sizes, so they are opened as two separate streams joined by a
lock-free FIFO. The input callback does the DSP (delay line + gain) and pushes into the FIFO;
the output callback pulls from it and slips a single frame now and then to absorb clock drift,
which is inaudible, instead of letting the FIFO run dry and click every few minutes.

Run `offcentre --help` (or `python -m offcentre --help`) for options.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import math
import os
import select
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from dataclasses import asdict, dataclass

from pathlib import Path

import numpy as np

__version__ = "0.1.0"

# sounddevice finds PortAudio with ctypes, which doesn't search Homebrew's lib directories.
# Its pip wheels bundle PortAudio, but a from-source install (Homebrew) uses the system one.
if sys.platform == "darwin":
    _fallback = os.environ.get("DYLD_FALLBACK_LIBRARY_PATH", "")
    _extra = ":".join(p for p in ("/opt/homebrew/lib", "/usr/local/lib") if p not in _fallback)
    if _extra:
        os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = ":".join(filter(None, (_fallback, _extra)))

try:
    import sounddevice as sd
except ImportError:  # pragma: no cover - environment problem, not a code path
    sys.exit("sounddevice is not installed. Reinstall offcentre, or run:  pip install sounddevice")
except OSError as exc:  # pragma: no cover - PortAudio shared library missing
    sys.exit(f"sounddevice could not load PortAudio ({exc}). Try:  brew install portaudio")

SPEED_OF_SOUND = 343.0  # m/s, dry air at ~20 C
CHANNELS = 2
LEFT, RIGHT = 0, 1

# Output devices we must never send to: BlackHole is our own input (feedback loop), and the
# conferencing apps' virtual devices are never what anyone wants.
OUTPUT_BLOCKLIST = ("blackhole", "offcentre tap", "zoomaudiodevice", "microsoft teams audio")
# Preferred outputs when --output is not given. macOS exposes AirPlay targets as a CoreAudio
# device named "AirPlay" (or after the speaker, e.g. "Living Room").
OUTPUT_PREFERENCES = ("airplay", "homepod")

HERE = Path(__file__).resolve().parent
# Per-user state lives outside the install, which may be read-only (Homebrew) or replaced on
# upgrade. OFFCENTRE_HOME overrides it (tests, multiple setups).
STATE_DIR = Path(os.environ.get("OFFCENTRE_HOME")
                 or Path.home() / "Library" / "Application Support" / "offcentre")
TAP_SOURCE = HERE / "macos" / "system_audio_tap.swift"
PREBUILT_TAP = HERE / "bin" / "system-audio-tap"  # compiled at install time by the Homebrew formula
TAP_BINARY = STATE_DIR / "bin" / "system-audio-tap"  # otherwise compiled here on first run
TAP_DEVICE_NAME = "Offcentre Tap"
WEB_DIR = HERE / "web"
WEB_PAGE = WEB_DIR / "index.html"
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}
SETTINGS_FILE = STATE_DIR / "settings.json"
LOCK_FILE = STATE_DIR / "instance.lock"
LEGACY_SETTINGS = HERE.parent / "settings.json"  # where a git checkout kept it before 0.1.0


def migrate_legacy_settings() -> None:
    """Copy settings saved by pre-0.1.0 checkouts (next to the script) into STATE_DIR once."""
    if SETTINGS_FILE.exists() or not LEGACY_SETTINGS.is_file():
        return
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(LEGACY_SETTINGS.read_text())
        print(f"Moved saved settings to {SETTINGS_FILE}")
    except OSError:
        pass


def acquire_instance_lock(port: int):
    """Hold an exclusive lock for this process's lifetime (the OS drops it on exit or crash).

    Two instances would fight over the one tap device (the helper replaces a leftover device
    with the same UID), so a second launch must stop before touching anything.
    """
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK_FILE, "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.seek(0)
        running_port = handle.read().strip() or "8765"
        handle.close()
        raise SetupError(
            "offcentre is already running. Use its controls at "
            f"http://127.0.0.1:{running_port}/ or stop it with Ctrl+C first."
        ) from None
    handle.truncate(0)
    handle.write(str(port))
    handle.flush()
    return handle


class SetupError(Exception):
    """A problem the user can fix (wrong device, bad argument). Printed without a traceback."""


# ---------------------------------------------------------------------------------------------
# Maths
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Correction:
    near_channel: int  # LEFT or RIGHT: the channel that gets delayed and attenuated
    delay_seconds: float
    delay_samples: int
    gain: float  # linear amplitude multiplier for the near channel
    samplerate: int

    @property
    def gain_db(self) -> float:
        return 20.0 * math.log10(self.gain) if self.gain > 0 else float("-inf")

    @property
    def rounding_error_us(self) -> float:
        """Time error introduced by rounding the delay to a whole sample, in microseconds."""
        return (self.delay_samples / self.samplerate - self.delay_seconds) * 1e6

    def describe(self) -> str:
        side = "LEFT" if self.near_channel == LEFT else "RIGHT"
        return (
            f"{side} channel: delay {self.delay_seconds * 1e3:.3f} ms = {self.delay_samples} samples "
            f"@ {self.samplerate} Hz (rounding error {self.rounding_error_us:+.1f} us), "
            f"gain x{self.gain:.4f} ({self.gain_db:+.2f} dB)"
        )


def compute_correction(
    near_m: float,
    far_m: float,
    samplerate: int,
    near_channel: int = LEFT,
    speed_of_sound: float = SPEED_OF_SOUND,
    attenuation_db: float | None = None,
) -> Correction:
    """Delay and gain for the near speaker so both speakers arrive in time and at equal level.

    attenuation_db overrides the inverse-distance gain (positive number = dB of cut). In a real
    room reflections narrow the level difference, so a smaller cut than 12 dB may sound better.
    """
    if near_m <= 0 or far_m <= 0:
        raise SetupError("Speaker distances must be positive.")
    if far_m < near_m:
        raise SetupError(f"--far ({far_m} m) must be >= --near ({near_m} m).")
    if samplerate <= 0:
        raise SetupError(f"Invalid sample rate {samplerate}.")
    if speed_of_sound <= 0:
        raise SetupError("Speed of sound must be positive.")

    delay_seconds = (far_m - near_m) / speed_of_sound
    delay_samples = int(round(delay_seconds * samplerate))

    if attenuation_db is None:
        gain = near_m / far_m
    else:
        if attenuation_db < 0:
            raise SetupError("--attenuation-db is a cut; give a positive number (e.g. 6).")
        gain = 10.0 ** (-attenuation_db / 20.0)

    return Correction(near_channel, delay_seconds, delay_samples, gain, int(samplerate))


# ---------------------------------------------------------------------------------------------
# DSP building blocks (allocation-free on the audio thread)
# ---------------------------------------------------------------------------------------------


MAX_DELAY_MS = 50.0  # upper bound for live tweaking; 50 ms is ~17 m of path difference
BUFFER_MS_RANGE = (10.0, 300.0)  # safety buffer between capture and playback
ROOM_LIMIT_M = 30.0  # room-plan coordinates are clamped to +/- this many metres
MIN_GAIN_DB = -60.0
SOLO_MODES = ("none", "left", "right")


@dataclass(frozen=True)
class Settings:
    """Everything that can be changed while running. Immutable: the control thread publishes a
    new instance and the audio thread picks it up at the next block boundary."""

    bypass: bool = False  # straight through (master volume and solo still apply), for A/B
    solo: str = "none"  # "left" / "right": play one channel only, to identify the speakers
    left_delay_ms: float = 0.0
    right_delay_ms: float = 0.0
    left_gain_db: float = 0.0
    right_gain_db: float = 0.0
    master_db: float = 0.0
    # Cushion between the capture and playback callbacks. It is the only latency this script
    # adds that apps can't compensate for; too small and the output runs dry (clicks).
    buffer_ms: float = 60.0
    # Room plan for the control page, in metres: x across the speaker wall from the left
    # HomePod, y into the room. The defaults put the seat 1 m from the left HomePod and 4 m from
    # the right one. With follow_room on, the page derives delays and levels from these.
    left_x: float = 0.0
    left_y: float = 0.0
    right_x: float = 3.2
    right_y: float = 0.0
    seat_x: float = -0.74
    seat_y: float = 0.67
    follow_room: bool = True
    # TPDF dither on channels whose level is changed, when the output is integer PCM (AirPlay:
    # 16-bit). Untouched channels are never dithered, so they stay bit-perfect.
    dither: bool = True
    level_law: float = 1.0  # fraction of the inverse-distance level cut to apply (1 = full)

    @classmethod
    def from_correction(cls, c: Correction, master_db: float = 0.0) -> "Settings":
        delay_ms = c.delay_samples * 1000.0 / c.samplerate
        near = {"delay_ms": delay_ms, "gain_db": max(c.gain_db, MIN_GAIN_DB)}
        side = "left" if c.near_channel == LEFT else "right"
        return cls(master_db=master_db, **{f"{side}_{k}": v for k, v in near.items()})

    def updated(self, changes: dict) -> "Settings":
        """Return a copy with `changes` applied, validated and clamped. Raises ValueError."""
        values = asdict(self)
        for key, value in changes.items():
            if key not in values:
                raise ValueError(f"unknown setting {key!r}")
            if key in ("bypass", "follow_room", "dither"):
                if not isinstance(value, bool):
                    raise ValueError(f"{key} must be true or false")
            elif key == "solo":
                if value not in SOLO_MODES:
                    raise ValueError(f"solo must be one of {SOLO_MODES}")
            else:
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError(f"{key} must be a number")
                if key.endswith("delay_ms"):
                    lo, hi = 0.0, MAX_DELAY_MS
                elif key == "buffer_ms":
                    lo, hi = BUFFER_MS_RANGE
                elif key.endswith(("_x", "_y")):
                    lo, hi = -ROOM_LIMIT_M, ROOM_LIMIT_M
                elif key == "level_law":
                    lo, hi = 0.0, 1.0
                else:
                    lo, hi = MIN_GAIN_DB, 0.0
                value = min(max(float(value), lo), hi)
            values[key] = value
        return Settings(**values)

    def channel_params(self, samplerate: int) -> tuple[tuple[int, float], tuple[int, float]]:
        """(delay in samples, linear gain) for the left and right output channels."""
        master = 10.0 ** (self.master_db / 20.0)
        params = []
        for side in ("left", "right"):
            if self.solo not in ("none", side):
                gain = 0.0
            elif self.bypass:
                gain = master
            else:
                gain = master * 10.0 ** (getattr(self, f"{side}_gain_db") / 20.0)
            delay_ms = 0.0 if self.bypass else getattr(self, f"{side}_delay_ms")
            params.append((int(round(delay_ms * samplerate / 1000.0)), gain))
        return params[0], params[1]


class DelayLine:
    """Circular buffer that can be read at any delay up to `max_delay` samples.

    State carries over between blocks, so the output is one continuous signal no matter how it is
    chopped up: no gaps, no pops at block boundaries. Per block: write(), read() one or more
    times, then advance().
    """

    def __init__(self, max_delay: int, max_block: int):
        if max_delay < 0 or max_block <= 0:
            raise ValueError("max_delay must be >= 0 and max_block > 0")
        self.max_delay = max_delay
        self.max_block = max_block
        # max_delay + max_block slots is exactly enough: a block never overwrites a sample that
        # the same block still needs to read.
        self._size = max_delay + max_block
        self._buf = np.zeros(self._size, dtype=np.float32)
        self._pos = 0

    def write(self, x: np.ndarray) -> None:
        if len(x) > self.max_block:
            raise ValueError(f"block of {len(x)} frames exceeds max_block {self.max_block}")
        self._copy_in(self._pos, x)

    def read(self, delay: int, out: np.ndarray) -> None:
        """Read len(out) samples delayed by `delay`, aligned with the block just written."""
        if not 0 <= delay <= self.max_delay:
            raise ValueError(f"delay {delay} outside 0..{self.max_delay}")
        self._copy_out((self._pos - delay) % self._size, out)

    def advance(self, n: int) -> None:
        self._pos = (self._pos + n) % self._size

    def process(self, x: np.ndarray, out: np.ndarray, delay: int | None = None) -> None:
        """write + read + advance in one call, for a fixed delay (default: max_delay)."""
        self.write(x)
        self.read(self.max_delay if delay is None else delay, out[: len(x)])
        self.advance(len(x))

    def _copy_in(self, pos: int, x: np.ndarray) -> None:
        first = min(len(x), self._size - pos)
        self._buf[pos : pos + first] = x[:first]
        self._buf[: len(x) - first] = x[first:]

    def _copy_out(self, pos: int, out: np.ndarray) -> None:
        first = min(len(out), self._size - pos)
        out[:first] = self._buf[pos : pos + first]
        out[first:] = self._buf[: len(out) - first]


class StereoBalancer:
    """Applies Settings to stereo blocks. Assign `settings` from any thread to change it live;
    each change is crossfaded over one block (~12 ms), so slider moves never click."""

    def __init__(self, settings: Settings, samplerate: int, max_block: int):
        self.samplerate = samplerate
        self.settings = settings
        max_delay = int(math.ceil(MAX_DELAY_MS * samplerate / 1000.0))
        self._lines = [DelayLine(max_delay, max_block), DelayLine(max_delay, max_block)]
        self._new = np.zeros(max_block, dtype=np.float32)
        self._old = np.zeros(max_block, dtype=np.float32)
        self._ramps: dict[int, np.ndarray] = {}
        # One LSB of the output's integer format (1/32768 for 16-bit); 0 = float output, no dither.
        self.dither_lsb = 0.0
        self._rng = np.random.default_rng()
        self._noise_a = np.zeros(max_block, dtype=np.float32)
        self._noise_b = np.zeros(max_block, dtype=np.float32)
        self._seen = settings
        self._active = settings.channel_params(samplerate)

    def _ramp(self, n: int) -> np.ndarray:
        ramp = self._ramps.get(n)
        if ramp is None:  # allocated once per distinct block size
            ramp = self._ramps[n] = (np.arange(1, n + 1, dtype=np.float32) / n)
        return ramp

    @staticmethod
    def _needs_dither(g_old: float, g_new: float) -> bool:
        # Unity gain (a pure delay) and silence both land exactly on the integer grid already.
        return any(g not in (0.0, 1.0) for g in (g_old, g_new))

    def _add_tpdf(self, channel: np.ndarray, n: int) -> None:
        """Triangular dither of +/-1 LSB: turns the requantisation error of a gain change into
        benign constant noise instead of distortion that follows the music."""
        a, b = self._noise_a[:n], self._noise_b[:n]
        self._rng.random(dtype=np.float32, out=a)
        self._rng.random(dtype=np.float32, out=b)
        a -= b
        a *= self.dither_lsb
        channel += a

    def process(self, indata: np.ndarray, outdata: np.ndarray) -> None:
        n = len(indata)
        settings = self.settings
        target = self._active if settings is self._seen else settings.channel_params(self.samplerate)
        for ch in (LEFT, RIGHT):
            line = self._lines[ch]
            line.write(indata[:, ch])
            (d_new, g_new), (d_old, g_old) = target[ch], self._active[ch]
            new = self._new[:n]
            line.read(d_new, new)
            if (d_new, g_new) == (d_old, g_old):
                np.multiply(new, g_new, out=outdata[:, ch])
            else:
                old, ramp = self._old[:n], self._ramp(n)
                line.read(d_old, old)
                old *= g_old
                new *= g_new
                # out = old + (new - old) * ramp
                np.subtract(new, old, out=new)
                np.multiply(new, ramp, out=new)
                np.add(old, new, out=outdata[:, ch])
            if self.dither_lsb and settings.dither and self._needs_dither(g_old, g_new):
                self._add_tpdf(outdata[:, ch], n)
            line.advance(n)
        self._seen, self._active = settings, target


class FrameFifo:
    """Single-producer / single-consumer FIFO of stereo frames between two audio callbacks.

    Only the producer moves `_written` and only the consumer moves `_read`; each counter is
    published after the data it covers is copied, so no lock is needed.
    """

    def __init__(self, capacity: int, channels: int = CHANNELS):
        self._buf = np.zeros((capacity, channels), dtype=np.float32)
        self._cap = capacity
        self._written = 0
        self._read = 0

    @property
    def fill(self) -> int:
        return self._written - self._read

    def write(self, frames: np.ndarray) -> int:
        """Append frames; returns how many were dropped because the FIFO was full."""
        n = min(len(frames), self._cap - self.fill)
        pos = self._written % self._cap
        first = min(n, self._cap - pos)
        self._buf[pos : pos + first] = frames[:first]
        self._buf[: n - first] = frames[first:n]
        self._written += n
        return len(frames) - n

    def skip(self, n: int) -> int:
        """Discard up to n frames (consumer side); returns how many were discarded."""
        n = min(n, self.fill)
        self._read += n
        return n

    def read(self, out: np.ndarray) -> int:
        """Fill `out` from the FIFO; returns how many frames were actually available."""
        n = min(len(out), self.fill)
        pos = self._read % self._cap
        first = min(n, self._cap - pos)
        out[:first] = self._buf[pos : pos + first]
        out[first:n] = self._buf[: n - first]
        self._read += n
        return n


class DriftCompensatedReader:
    """Pulls from a FrameFifo, holding its fill level near `target` frames.

    The two devices' clocks differ by tens of ppm, so the FIFO slowly fills or drains. When the
    smoothed fill wanders more than `tolerance` frames from target, one frame per callback is
    dropped or repeated: a one-sample slip is inaudible, a buffer underrun is a click.
    """

    def __init__(self, fifo: FrameFifo, target: int, tolerance: int, max_block: int):
        self.fifo = fifo
        self.target = target
        self.tolerance = tolerance
        self._scratch = np.zeros((max_block + 1, CHANNELS), dtype=np.float32)
        self._avg_fill = float(target)
        self._new_target: int | None = None
        self.primed = False
        self.underruns = 0
        self.slips = 0

    def retarget(self, target: int) -> None:
        """Change the target fill from another thread; applied at the next callback."""
        self._new_target = target

    def read(self, out: np.ndarray) -> None:
        frames = len(out)
        if self._new_target is not None:
            # A deliberate jump, heard as one small skip or gap. Drifting there at one frame per
            # callback would take minutes.
            self.target, self._new_target = self._new_target, None
            excess = self.fifo.fill - self.target
            if excess > 0:
                self.fifo.skip(excess)
            else:
                self.primed = False  # play silence until the FIFO has refilled to the new target
            self._avg_fill = float(self.target)
        fill = self.fifo.fill

        if not self.primed:
            if fill < self.target:
                out.fill(0)
                return
            self.primed = True
            self._avg_fill = float(fill)

        self._avg_fill += 0.01 * (fill - self._avg_fill)  # ~1 s time constant at 512-frame blocks
        error = self._avg_fill - self.target

        if error > self.tolerance and fill > frames + 1:
            # Running ahead: consume one extra frame, dropping it from the middle of the block.
            got = self.fifo.read(self._scratch[: frames + 1])
            mid = frames // 2
            out[:mid] = self._scratch[:mid]
            out[mid:] = self._scratch[mid + 1 : got]
            self.slips += 1
            self._avg_fill -= 1
        elif error < -self.tolerance and frames > 1:
            # Running behind: consume one frame fewer, repeating the middle frame.
            got = self.fifo.read(self._scratch[: frames - 1])
            if got == frames - 1:
                mid = frames // 2
                out[:mid] = self._scratch[:mid]
                out[mid] = self._scratch[mid - 1] if mid else self._scratch[0]
                out[mid + 1 :] = self._scratch[mid : frames - 1]
                self.slips += 1
                self._avg_fill += 1
            else:
                self._underrun(out, got)
        else:
            got = self.fifo.read(out)
            if got < frames:
                self._underrun(out, got)

    def _underrun(self, out: np.ndarray, got: int) -> None:
        out[got:] = 0
        self.underruns += 1
        self.primed = False  # wait for the FIFO to refill to target before playing again


# ---------------------------------------------------------------------------------------------
# Device selection
# ---------------------------------------------------------------------------------------------


def _devices() -> list[dict]:
    try:
        return [dict(d, index=i) for i, d in enumerate(sd.query_devices())]
    except sd.PortAudioError as exc:
        raise SetupError(f"Could not query audio devices: {exc}") from exc


def _describe(dev: dict) -> str:
    return (
        f"[{dev['index']}] {dev['name']}  "
        f"(in {dev['max_input_channels']}, out {dev['max_output_channels']}, "
        f"{dev['default_samplerate']:.0f} Hz)"
    )


def list_devices() -> None:
    for dev in _devices():
        print(_describe(dev))


def _candidates(kind: str) -> list[dict]:
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    devs = [d for d in _devices() if d[key] >= CHANNELS]
    if kind == "output":
        devs = [d for d in devs if not any(b in d["name"].lower() for b in OUTPUT_BLOCKLIST)]
    return devs


def _match(query: str, devs: list[dict], kind: str) -> dict:
    if query.isdigit():
        for d in devs:
            if d["index"] == int(query):
                return d
        raise SetupError(f"No usable stereo {kind} device with index {query}.")
    hits = [d for d in devs if query.lower() in d["name"].lower()]
    if not hits:
        names = "\n  ".join(_describe(d) for d in devs) or "(none)"
        raise SetupError(f"No stereo {kind} device matches '{query}'. Available:\n  {names}")
    return hits[0]


def _prompt(devs: list[dict], kind: str, interactive: bool, hint: str) -> dict:
    if not devs:
        raise SetupError(f"No stereo {kind} devices available. {hint}")
    if not interactive or not sys.stdin.isatty():
        names = "\n  ".join(_describe(d) for d in devs)
        raise SetupError(f"Could not pick a {kind} device automatically. {hint}\nAvailable:\n  {names}")
    print(f"\nSelect the {kind} device ({hint}):")
    for d in devs:
        print("  " + _describe(d))
    while True:
        choice = input(f"{kind} device index: ").strip()
        try:
            return _match(choice, devs, kind)
        except SetupError as exc:
            print(exc)


def choose_input(query: str | None, interactive: bool) -> dict:
    devs = _candidates("input")
    if query:
        return _match(query, devs, "input")
    for d in devs:
        if "blackhole" in d["name"].lower():
            return d
    return _prompt(devs, "input", interactive, "BlackHole not found; see README for installing it")


def choose_output(query: str | None, interactive: bool, prefer_system_default: bool = False) -> dict:
    devs = _candidates("output")
    if query:
        if "blackhole" in query.lower():
            raise SetupError("Output cannot be BlackHole: that is the input, it would loop back.")
        return _match(query, devs, "output")
    if prefer_system_default:
        default = sd.default.device[1]
        for d in devs:
            if d["index"] == default:
                return d
        if default >= 0 and "blackhole" in sd.query_devices(default)["name"].lower():
            raise SetupError(
                "The system output is BlackHole, but the tap source plays to the system output. "
                "Switch Sound > Output back to your speakers (e.g. the HomePods), or pass "
                "--source blackhole to use the BlackHole route."
            )
    for pref in OUTPUT_PREFERENCES:
        for d in devs:
            if pref in d["name"].lower():
                return d
    return _prompt(
        devs, "output", interactive,
        "no AirPlay device found; pick your HomePods/speakers or pass --output",
    )


# ---------------------------------------------------------------------------------------------
# System audio tap (macOS 14.2+)
# ---------------------------------------------------------------------------------------------


class SystemTap:
    """Runs the Swift helper that exposes the system mix as the input device "Offcentre Tap".

    While the tap is read, every other app's audio is muted at the output and only this script's
    corrected copy is heard. The helper removes the tap when it exits, including when this
    process dies (it watches its stdin), so a crash never leaves the Mac silent.
    """

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.output_format: dict | None = None  # {"bits", "integer", "rate"} from the helper

    @staticmethod
    def build() -> Path:
        source_mtime = TAP_SOURCE.stat().st_mtime
        for candidate in (PREBUILT_TAP, TAP_BINARY):
            if candidate.exists() and candidate.stat().st_mtime >= source_mtime:
                return candidate
        print("Compiling the system audio tap helper (one-off)...")
        TAP_BINARY.parent.mkdir(parents=True, exist_ok=True)
        try:
            result = subprocess.run(
                ["swiftc", "-O", str(TAP_SOURCE), "-o", str(TAP_BINARY)],
                capture_output=True, text=True,
            )
        except FileNotFoundError as exc:
            raise SetupError("swiftc not found. Install the command line tools:  xcode-select --install") from exc
        if result.returncode != 0:
            raise SetupError(f"Compiling {TAP_SOURCE.name} failed:\n{result.stderr.strip()}")
        return TAP_BINARY

    def start(self, timeout: float = 10.0) -> None:
        binary = self.build()
        # Our own output must be excluded from the tap or it would feed back into itself. The
        # helper can only exclude processes Core Audio already knows, which PortAudio's
        # initialisation (already done by the device queries) guarantees.
        self.proc = subprocess.Popen(
            [str(binary), "--exclude-pid", str(os.getpid())],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        ready, _, _ = select.select([self.proc.stdout], [], [], timeout)
        line = self.proc.stdout.readline().strip() if ready else ""
        if not line.startswith("READY"):
            self.stop()
            err = self.proc.stderr.read().strip() if self.proc.stderr else ""
            raise SetupError(f"System audio tap failed to start. {err or 'No response from helper.'}")
        fields = dict(part.split("=", 1) for part in line.split() if "=" in part)
        try:
            self.output_format = {
                "bits": int(fields["bits"]),
                "integer": " int " in f" {line} ",
                "rate": int(fields["rate"]),
            }
        except (KeyError, ValueError):
            self.output_format = None  # older helper; dithering stays off
        self._wait_for_device()

    @staticmethod
    def _wait_for_device(timeout: float = 5.0) -> None:
        """The helper creates the device before printing READY, but this process's view of the
        device list is updated asynchronously (notably right after a previous tap was torn
        down), so a single immediate look can miss it. PortAudio also snapshots the list at
        initialisation, so reload it on each attempt."""
        start = time.monotonic()
        deadline = start + timeout
        told = False
        while True:
            sd._terminate()
            sd._initialize()
            if any(d["name"] == TAP_DEVICE_NAME and d["max_input_channels"] >= CHANNELS
                   for d in sd.query_devices()):
                return
            now = time.monotonic()
            if now >= deadline:
                return  # the caller reports the missing device with advice
            if not told and now - start > 1.0:
                print("Waiting for the audio tap to appear...")
                told = True
            time.sleep(0.2)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.close()
                self.proc.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                self.proc.kill()


# ---------------------------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------------------------


class Engine:
    def __init__(self, in_dev: dict, out_dev: dict, correction: Correction, settings: Settings,
                 args: argparse.Namespace, direct: bool = False,
                 output_format: dict | None = None):
        self.in_dev, self.out_dev, self.correction = in_dev, out_dev, correction
        self.output_format = output_format
        # direct: one duplex stream on the tap's aggregate device, which also contains the
        # output. One clock, one callback: no FIFO, no drift, no safety buffer.
        self.direct = direct
        self.source = args.source
        # The tap device is an aggregate: the clock device's own inputs (if any) come first and
        # the tapped system mix is the last two channels. For BlackHole these are channels 1-2.
        self.in_channels = in_dev["max_input_channels"] if args.source == "tap" else CHANNELS
        self.blocksize = args.blocksize
        # PortAudio may hand us blocks larger than requested on some devices; leave headroom.
        max_block = max(8192, 4 * self.blocksize)
        self.balancer = StereoBalancer(settings, correction.samplerate, max_block)
        if output_format and output_format["integer"] and output_format["bits"] in (16, 24):
            self.balancer.dither_lsb = 1.0 / 2 ** (output_format["bits"] - 1)
        self.settings_path: Path | None = None if args.no_save else SETTINGS_FILE
        self._settings_lock = threading.Lock()
        self._processed = np.zeros((max_block, CHANNELS), dtype=np.float32)
        self.samplerate = correction.samplerate
        max_buffer = self._ms_to_frames(BUFFER_MS_RANGE[1])
        self.fifo = FrameFifo(capacity=2 * max_buffer + 2 * max_block)
        self.reader = DriftCompensatedReader(
            self.fifo, target=self._ms_to_frames(settings.buffer_ms),
            # The fill is sampled once per output callback while input arrives a block at a
            # time, so it swings by up to a block around its true mean. A tolerance narrower
            # than that "corrects" the swing itself and slips constantly.
            tolerance=self.blocksize, max_block=max_block,
        )
        self.output_latency_s: float | None = None
        self.in_peak = 0.0
        self.dropped = 0
        self.status_flags = 0
        # Per-channel peaks for the web meters, reset each time the page polls.
        self.meter_in = np.zeros(CHANNELS, dtype=np.float32)
        self.meter_out = np.zeros(CHANNELS, dtype=np.float32)

    # --- audio thread callbacks: no printing, no allocation beyond numpy views ---

    def _on_input(self, indata, frames, _time, status) -> None:
        if status:
            self.status_flags += 1
        out = self._processed[:frames]
        stereo = indata[:, -CHANNELS:]
        self.balancer.process(stereo, out)
        self.in_peak = max(self.in_peak, float(np.abs(stereo).max(initial=0.0)))
        if frames:
            np.maximum(self.meter_in, np.abs(stereo).max(axis=0), out=self.meter_in)
            np.maximum(self.meter_out, np.abs(out).max(axis=0), out=self.meter_out)
        self.dropped += self.fifo.write(out)

    def _on_duplex(self, indata, outdata, frames, _time, status) -> None:
        if status:
            self.status_flags += 1
        stereo = indata[:, -CHANNELS:]
        self.balancer.process(stereo, outdata)
        peak = float(np.abs(stereo).max(initial=0.0))
        self.in_peak = max(self.in_peak, peak)
        if frames:
            np.maximum(self.meter_in, np.abs(stereo).max(axis=0), out=self.meter_in)
            np.maximum(self.meter_out, np.abs(outdata).max(axis=0), out=self.meter_out)

    def _on_output(self, outdata, frames, _time, status) -> None:
        if status:
            self.status_flags += 1
        self.reader.read(outdata)

    # --- control thread ---

    def _ms_to_frames(self, ms: float) -> int:
        return int(round(ms * self.samplerate / 1000.0))

    def update_settings(self, changes: dict) -> Settings:
        with self._settings_lock:
            previous = self.balancer.settings
            settings = previous.updated(changes)
            self.balancer.settings = settings  # picked up at the next block boundary
            if settings.buffer_ms != previous.buffer_ms:
                self.reader.retarget(self._ms_to_frames(settings.buffer_ms))
            if self.settings_path:
                try:
                    self.settings_path.write_text(json.dumps(asdict(settings), indent=2) + "\n")
                except OSError as exc:
                    print(f"\nCould not save settings: {exc}")
        return settings

    def _quality(self, settings: Settings) -> dict:
        """Per-channel account of what happens to the samples, for the control page."""
        fmt = self.output_format
        channels = []
        for (delay, gain) in settings.channel_params(self.correction.samplerate):
            if gain == 0.0:
                status = "muted"
            elif gain == 1.0:
                status = "bit-perfect" if self.direct else "bit-perfect except drift slips"
            elif self.balancer.dither_lsb and settings.dither:
                status = "level changed, dithered"
            else:
                status = "level changed"
            channels.append({"status": status, "delay_samples": delay, "gain": gain})
        return {
            "output": fmt,  # None when unknown (BlackHole route / older helper)
            "dither_available": bool(self.balancer.dither_lsb),
            "mode": "direct" if self.direct else "buffered",
            "channels": channels,
        }

    def state(self) -> dict:
        def db(peaks: np.ndarray) -> list[float]:
            return [round(20 * math.log10(v), 1) if v > 1e-5 else -99.0 for v in peaks.tolist()]

        meter_in, meter_out = db(self.meter_in), db(self.meter_out)
        self.meter_in[:] = 0
        self.meter_out[:] = 0
        settings = self.balancer.settings
        (dl, _), (dr, _) = settings.channel_params(self.correction.samplerate)
        return {
            "settings": asdict(settings),
            "samplerate": self.correction.samplerate,
            "delay_samples": [dl, dr],
            "limits": {"max_delay_ms": MAX_DELAY_MS, "min_gain_db": MIN_GAIN_DB},
            "devices": {"input": self.in_dev["name"], "output": self.out_dev["name"]},
            # AirPlay devices are named after the room ("Living Room"), so go by their
            # ~2 s latency rather than the name.
            "airplay": "airplay" in self.out_dev["name"].lower()
            or (self.output_latency_s or 0) > 1.0,
            "latency": {
                "mode": "direct" if self.direct else "buffered",
                # What the script adds: one block in and out, plus the FIFO when buffered. Apps
                # can't see this part, so it is what shifts lip-sync.
                "added_ms": round(
                    ((0 if self.direct else self.fifo.fill) + 2 * self.blocksize)
                    * 1000 / self.samplerate
                ),
                # The output device's own latency (AirPlay: 2 s), which apps do compensate for.
                "output_ms": None if self.output_latency_s is None
                else round(self.output_latency_s * 1000),
            },
            "meters": {"in": meter_in, "out": meter_out},
            "quality": self._quality(settings),
            "stats": {
                "buffer": self.fifo.fill, "slips": self.reader.slips,
                "underruns": self.reader.underruns, "dropped": self.dropped,
                "xruns": self.status_flags,
            },
        }

    def _silence_hint(self) -> str:
        if self.source == "tap":
            return (
                "No signal from the system tap for 10 s. Is something playing? Has your terminal "
                "app been allowed under System Settings > Privacy & Security > Screen & System "
                "Audio Recording (the 'System Audio Recording Only' list)? Restart the terminal "
                "after granting it."
            )
        return (
            "No signal from BlackHole for 10 s. Is System Settings > Sound > Output set to "
            "BlackHole, is something playing, and has your terminal been granted Microphone "
            "access (System Settings > Privacy & Security)?"
        )

    def run(self, web_port: int | None = None, open_browser: bool = True) -> None:
        sr = self.correction.samplerate
        common = dict(samplerate=sr, dtype="float32", blocksize=self.blocksize)
        try:
            if self.direct:
                duplex = sd.Stream(
                    device=(self.in_dev["index"], self.in_dev["index"]),
                    channels=(self.in_channels, CHANNELS), latency="low",
                    callback=self._on_duplex, **common,
                )
                streams, output_latency = [duplex], duplex.latency[1]
            else:
                in_stream = sd.InputStream(
                    device=self.in_dev["index"], channels=self.in_channels, latency="low",
                    callback=self._on_input, **common,
                )
                out_stream = sd.OutputStream(
                    device=self.out_dev["index"], channels=CHANNELS, latency="low",
                    callback=self._on_output, **common,
                )
                streams, output_latency = [in_stream, out_stream], out_stream.latency
        except sd.PortAudioError as exc:
            raise SetupError(
                f"Could not open audio streams at {sr} Hz: {exc}\n"
                f"Both devices must run at the same rate. With --source blackhole, set BlackHole "
                f"to {sr} Hz in Audio MIDI Setup, or pass --samplerate with a rate both support."
            ) from exc

        self._loop(streams, web_port, open_browser, output_latency)

    def _loop(self, streams: list, web_port: int | None, open_browser: bool,
              output_latency_s: float) -> None:
        server = ControlServer(self, web_port) if web_port else None
        self.output_latency_s = output_latency_s
        with contextlib.ExitStack() as stack:
            for stream in streams:
                stack.enter_context(stream)
            print(f"Running: {self.in_dev['name']} -> {self.out_dev['name']}. Ctrl+C to stop.")
            if server:
                server.start()
                print(f"Live controls: {server.url}")
                if open_browser:
                    webbrowser.open(server.url)
            print()
            silent_since = time.monotonic()
            warned_silent = False
            while True:
                time.sleep(0.5)
                peak, self.in_peak = self.in_peak, 0.0
                now = time.monotonic()
                if peak > 1e-5:
                    silent_since, warned_silent = now, False
                elif now - silent_since > 10 and not warned_silent:
                    warned_silent = True
                    print("\n" + self._silence_hint())
                level = 20 * math.log10(peak) if peak > 1e-5 else -99.0
                # Kept under 60 columns: a wrapped line can't be overwritten with \r.
                if self.direct:
                    line = f"\rin {level:5.1f}dB  direct  xrun {self.status_flags} "
                else:
                    line = (f"\rin {level:5.1f}dB  buf {self.fifo.fill:5d}  slip {self.reader.slips}  "
                            f"under {self.reader.underruns}  drop {self.dropped}  xrun {self.status_flags} ")
                print(line, end="", flush=True)


# ---------------------------------------------------------------------------------------------
# Live control web page
# ---------------------------------------------------------------------------------------------


class ControlServer:
    """Serves web/index.html and a tiny JSON API on 127.0.0.1 only.

    GET  /api/state     settings, meters and stream stats
    POST /api/settings  JSON object of settings to change; returns the new state
    """

    def __init__(self, engine: Engine, port: int):
        self.engine = engine
        self.port = port
        self.url = f"http://127.0.0.1:{port}/"
        try:
            self.httpd = ThreadingHTTPServer(("127.0.0.1", port), self._handler())
        except OSError as exc:
            raise SetupError(f"Could not start the control page on port {port}: {exc}. "
                             f"Pass --port with another number, or --no-web.") from exc
        self.httpd.daemon_threads = True

    def start(self) -> None:
        threading.Thread(target=self.httpd.serve_forever, name="control-server", daemon=True).start()

    def _handler(self):
        engine, port = self.engine, self.port
        allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):  # keep the terminal status line clean
                pass

            def _send(self, code: int, body: bytes, ctype: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code: int, obj: dict) -> None:
                self._send(code, json.dumps(obj).encode(), "application/json")

            def _host_ok(self) -> bool:
                # Blocks DNS-rebinding: a web page can't reach us under some other hostname.
                if self.headers.get("Host") in allowed_hosts:
                    return True
                self._json(403, {"error": "forbidden host"})
                return False

            def do_GET(self) -> None:
                if not self._host_ok():
                    return
                if self.path in ("/", "/index.html"):
                    try:
                        self._send(200, WEB_PAGE.read_bytes(), "text/html; charset=utf-8")
                    except OSError:
                        self._json(500, {"error": f"missing {WEB_PAGE}"})
                elif self.path == "/api/state":
                    self._json(200, engine.state())
                else:
                    self._static(self.path)

            def _static(self, url_path: str) -> None:
                # Only plain files directly inside web/, with a known type: no traversal.
                name = url_path.split("?", 1)[0].lstrip("/")
                target = (WEB_DIR / name).resolve()
                ctype = STATIC_TYPES.get(target.suffix)
                if "/" in name or target.parent != WEB_DIR.resolve() or not ctype or not target.is_file():
                    return self._json(404, {"error": "not found"})
                self._send(200, target.read_bytes(), ctype)

            def do_POST(self) -> None:
                if not self._host_ok():
                    return
                if self.path != "/api/settings":
                    return self._json(404, {"error": "not found"})
                # Requiring a JSON content type forces a CORS preflight for cross-site requests,
                # which we never approve, so other websites can't change the settings.
                if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                    return self._json(415, {"error": "expected application/json"})
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 4096:
                        raise ValueError("bad body length")
                    changes = json.loads(self.rfile.read(length))
                    if not isinstance(changes, dict):
                        raise ValueError("expected a JSON object")
                    engine.update_settings(changes)
                except (ValueError, json.JSONDecodeError) as exc:
                    return self._json(400, {"error": str(exc)})
                self._json(200, engine.state())

        return Handler


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Time-align and level-match an asymmetric stereo pair in real time.",
    )
    p.add_argument("--near", type=float, default=1.0, help="distance to the nearer speaker, m (default 1)")
    p.add_argument("--far", type=float, default=4.0, help="distance to the farther speaker, m (default 4)")
    p.add_argument("--near-side", choices=("left", "right"), default="left",
                   help="which speaker is nearer (default left)")
    p.add_argument("--attenuation-db", type=float, default=None,
                   help="override the inverse-distance cut on the near side (e.g. 6)")
    p.add_argument("--volume-db", type=float, default=0.0,
                   help="master volume in dB applied to both channels (default 0)")
    p.add_argument("--speed-of-sound", type=float, default=SPEED_OF_SOUND, help="m/s (default 343)")
    p.add_argument("--source", choices=("tap", "blackhole"), default="tap",
                   help="tap: capture system audio with a Core Audio tap and play to the current "
                        "system output, e.g. HomePods (default, macOS 14.2+). blackhole: read "
                        "BlackHole, which must be set as the system output")
    p.add_argument("--input", help="input device name substring or index (--source blackhole only)")
    p.add_argument("--output", help="output device name substring or index "
                                    "(default: system output for tap, AirPlay for blackhole)")
    p.add_argument("--samplerate", type=int, default=None,
                   help="sample rate in Hz (default: the output device's native rate)")
    p.add_argument("--blocksize", type=int, default=512, help="frames per callback (default 512)")
    p.add_argument("--no-prompt", action="store_true", help="fail instead of asking for devices")
    p.add_argument("--list-devices", action="store_true", help="list audio devices and exit")
    p.add_argument("--dry-run", action="store_true", help="print the correction and exit")
    p.add_argument("--version", action="version", version=f"offcentre {__version__}")
    p.add_argument("--port", type=int, default=8765, help="live control page port (default 8765)")
    p.add_argument("--no-web", action="store_true", help="don't serve the live control page")
    p.add_argument("--no-browser", action="store_true", help="don't open the control page")
    p.add_argument("--reset", action="store_true",
                   help=f"ignore {SETTINGS_FILE.name} and start from --near/--far/--near-side")
    p.add_argument("--no-save", action="store_true",
                   help=f"don't write changes made on the control page to {SETTINGS_FILE.name}")
    args = p.parse_args(argv)
    if not 16 <= args.blocksize <= 8192:
        p.error("--blocksize must be between 16 and 8192")
    if args.volume_db > 0:
        p.error("--volume-db must be <= 0 (positive gain could clip)")
    if not 1 <= args.port <= 65535:
        p.error("--port must be between 1 and 65535")
    return args


def _interrupt(_signum, _frame):
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # Closing the terminal (SIGHUP) or `kill` (SIGTERM) should clean up like Ctrl+C does.
    for sig in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(sig, _interrupt)
    near_channel = LEFT if args.near_side == "left" else RIGHT
    migrate_legacy_settings()
    tap = SystemTap()
    lock = None
    try:
        if args.list_devices:
            list_devices()
            return 0
        if args.dry_run and args.samplerate:
            print(compute_correction(args.near, args.far, args.samplerate, near_channel,
                                     args.speed_of_sound, args.attenuation_db).describe())
            return 0

        if args.source == "tap":
            if sys.platform != "darwin":
                raise SetupError("--source tap needs macOS 14.2+; use --source blackhole.")
            out_dev = choose_output(args.output, not args.no_prompt, prefer_system_default=True)
            if args.dry_run:
                in_dev = {"index": -1, "name": TAP_DEVICE_NAME, "max_input_channels": CHANNELS,
                          "max_output_channels": 0, "default_samplerate": out_dev["default_samplerate"]}
            else:
                lock = acquire_instance_lock(args.port)
                tap.start()
                try:
                    in_dev = _match(TAP_DEVICE_NAME, _candidates("input"), "input")
                except SetupError:
                    raise SetupError(
                        f"The helper started but '{TAP_DEVICE_NAME}' didn't appear as an input. "
                        f"Check that Sound > Output is a real device (e.g. your HomePods), not a "
                        f"Multi-Output or aggregate device, then try again."
                    ) from None
                out_dev = _match(out_dev["name"], _candidates("output"), "output")  # indices moved
        else:
            if not args.dry_run:
                lock = acquire_instance_lock(args.port)
            in_dev = choose_input(args.input, not args.no_prompt)
            out_dev = choose_output(args.output, not args.no_prompt)
        samplerate = args.samplerate or int(out_dev["default_samplerate"])
        correction = compute_correction(args.near, args.far, samplerate, near_channel,
                                        args.speed_of_sound, args.attenuation_db)

        settings = Settings.from_correction(correction, args.volume_db)
        if not args.reset and SETTINGS_FILE.exists():
            try:
                settings = Settings().updated(json.loads(SETTINGS_FILE.read_text()))
                print(f"Loaded saved settings from {SETTINGS_FILE.name} (--reset to ignore).")
            except (OSError, ValueError) as exc:
                print(f"Ignoring unreadable {SETTINGS_FILE.name}: {exc}")

        print(f"Input:  {_describe(in_dev)}")
        print(f"Output: {_describe(out_dev)}")
        print(f"Calculated: {correction.describe()}")
        if args.dry_run:
            return 0
        # The tap's aggregate device is built around the system output, so when that is where
        # we're playing, use it for both directions.
        direct = (args.source == "tap" and args.output is None
                  and in_dev["max_output_channels"] >= CHANNELS)
        if direct:
            print(f"Mode: direct (one stream on {TAP_DEVICE_NAME}, clocked by {out_dev['name']})")
        Engine(in_dev, out_dev, correction, settings, args, direct=direct,
               output_format=tap.output_format).run(
            web_port=None if args.no_web else args.port, open_browser=not args.no_browser,
        )
    except SetupError as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0
    finally:
        tap.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
