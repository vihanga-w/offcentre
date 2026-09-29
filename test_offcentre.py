import numpy as np
import pytest

from offcentre import (
    LEFT, RIGHT, DelayLine, DriftCompensatedReader, FrameFifo, SetupError, Settings,
    StereoBalancer, compute_correction,
)


def test_correction_matches_spec():
    c48 = compute_correction(1.0, 4.0, 48000)
    assert c48.delay_seconds == pytest.approx(3 / 343)  # 8.746 ms
    assert c48.delay_samples == 420  # 419.83 rounded
    assert c48.gain == pytest.approx(0.25)
    assert c48.gain_db == pytest.approx(-12.041, abs=1e-3)
    assert compute_correction(1.0, 4.0, 44100).delay_samples == 386  # 385.71 rounded
    assert abs(c48.rounding_error_us) < 0.5 / 48000 * 1e6


def test_correction_overrides_and_validation():
    assert compute_correction(1, 4, 48000, attenuation_db=6).gain == pytest.approx(0.5012, abs=1e-4)
    with pytest.raises(SetupError):
        compute_correction(4, 1, 48000)
    with pytest.raises(SetupError):
        compute_correction(1, 4, 48000, attenuation_db=-3)


@pytest.mark.parametrize("delay", [0, 1, 420, 2000])
def test_delay_line_is_continuous_across_irregular_blocks(delay):
    rng = np.random.default_rng(0)
    x = rng.standard_normal(20000).astype(np.float32)
    line = DelayLine(max(delay, 1), max_block=1024)
    out, i = [], 0
    for n in rng.integers(1, 1025, size=1000):
        if i >= len(x):
            break
        blk = x[i:i + n]
        buf = np.empty_like(blk)
        line.process(blk, buf, delay)
        out.append(buf)
        i += len(blk)
    got = np.concatenate(out)
    expected = np.concatenate([np.zeros(delay, np.float32), x])[:len(x)]
    np.testing.assert_array_equal(got, expected)


def _run(bal, x, block=512):
    y = np.empty_like(x)
    for i in range(0, len(x), block):
        bal.process(x[i:i + block], y[i:i + block])
    return y


def test_balancer_delays_and_cuts_only_near_channel():
    c = compute_correction(1, 4, 48000)
    bal = StereoBalancer(Settings.from_correction(c), 48000, max_block=512)
    x = np.zeros((1024, 2), np.float32)
    x[0] = 1.0  # impulse on both channels
    y = _run(bal, x)
    assert y[0, RIGHT] == 1.0 and np.count_nonzero(y[:, RIGHT]) == 1
    assert y[420, LEFT] == pytest.approx(0.25) and np.count_nonzero(y[:, LEFT]) == 1


def test_bypass_and_solo():
    s = Settings.from_correction(compute_correction(1, 4, 48000))
    assert s.updated({"bypass": True}).channel_params(48000) == ((0, 1.0), (0, 1.0))
    (_, gl), (_, gr) = s.updated({"solo": "right"}).channel_params(48000)
    assert gl == 0.0 and gr == 1.0


def test_settings_validation_and_clamping():
    s = Settings()
    assert s.updated({"left_delay_ms": 999}).left_delay_ms == 50.0
    assert s.updated({"right_gain_db": 6}).right_gain_db == 0.0
    for bad in ({"nope": 1}, {"bypass": "yes"}, {"solo": "both"}, {"left_gain_db": "x"},
                {"left_delay_ms": float("nan")}, {"master_db": True}):
        with pytest.raises(ValueError):
            s.updated(bad)


def test_live_changes_crossfade_without_jumps():
    sr, n = 48000, 48000
    t = np.arange(n) / sr
    tone = (0.5 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    x = np.stack([tone, tone], axis=1)
    settings = Settings()
    bal = StereoBalancer(settings, sr, max_block=512)
    y = np.empty_like(x)
    changes = [{"left_delay_ms": 8.75}, {"left_gain_db": -12}, {"bypass": True},
               {"bypass": False}, {"left_delay_ms": 30}, {"solo": "left"}, {"solo": "none"}]
    for k, i in enumerate(range(0, n, 512)):
        if k % 10 == 5 and changes:
            settings = settings.updated(changes.pop(0))
            bal.settings = settings
        bal.process(x[i:i + 512], y[i:i + 512])
    assert not changes
    # A 220 Hz sine at 0.5 moves at most ~0.0144 per sample; a hard switch between two delays
    # of it would jump by up to 1.0. Crossfades keep every step small.
    assert np.abs(np.diff(y[:, LEFT])).max() < 0.03
    assert np.abs(np.diff(y[:, RIGHT])).max() < 0.03


def _stream(reader, fifo, produce_per_cb, consume_per_cb, callbacks):
    """Simulate producer/consumer at slightly different rates; return output signal."""
    src = np.arange(1, 12_000_000, dtype=np.float32)  # exact in float32 (< 2**24)
    si, acc, out = 0, 0.0, []
    for _ in range(callbacks):
        acc += produce_per_cb
        n = int(acc); acc -= n
        fifo.write(np.repeat(src[si:si + n, None], 2, axis=1)); si += n
        o = np.empty((consume_per_cb, 2), np.float32)
        reader.read(o)
        out.append(o[:, 0])
    return np.concatenate(out)


@pytest.mark.parametrize("ratio", [1.0, 1.0002, 0.9998])  # +/-200 ppm, far worse than real clocks
def test_drift_reader_never_underruns_and_only_slips_single_frames(ratio):
    fifo = FrameFifo(16 * 512 + 8192)
    reader = DriftCompensatedReader(fifo, target=3 * 512, tolerance=256, max_block=8192)
    y = _stream(reader, fifo, 512 * ratio, 512, callbacks=20000)
    y = y[np.nonzero(y)[0][0]:]  # skip priming silence
    steps = np.diff(y)
    assert reader.underruns == 0
    assert set(np.unique(steps)) <= {0.0, 1.0, 2.0}  # 0 = repeated frame, 2 = dropped frame


def test_retarget_jumps_buffer_level():
    fifo = FrameFifo(20000)
    reader = DriftCompensatedReader(fifo, target=4096, tolerance=256, max_block=8192)
    out = np.empty((512, 2), np.float32)
    for _ in range(20):
        fifo.write(np.ones((512, 2), np.float32))
        reader.read(out)
    assert 4096 - 512 <= fifo.fill <= 4096  # measured just after a 512-frame read
    reader.retarget(1024)
    fifo.write(np.ones((512, 2), np.float32))
    reader.read(out)
    assert fifo.fill <= 1024 and reader.underruns == 0
    assert Settings().updated({"buffer_ms": 1}).buffer_ms == 10.0


def test_no_spurious_slips_at_matched_clocks():
    fifo = FrameFifo(20000)
    reader = DriftCompensatedReader(fifo, target=2646, tolerance=512, max_block=8192)
    _stream(reader, fifo, 512, 512, callbacks=5000)
    assert reader.slips == 0 and reader.underruns == 0


def test_default_room_matches_one_and_four_metres():
    s = Settings()
    d_left = np.hypot(s.seat_x - s.left_x, s.seat_y - s.left_y)
    d_right = np.hypot(s.seat_x - s.right_x, s.seat_y - s.right_y)
    assert d_left == pytest.approx(1.0, abs=0.01) and d_right == pytest.approx(4.0, abs=0.01)
    assert s.updated({"seat_x": 99}).seat_x == 30.0
    with pytest.raises(ValueError):
        s.updated({"follow_room": 1})


def _pcm16(n, seed=0):
    """Random 16-bit PCM as float, the way Core Audio hands it over (value / 32768)."""
    ints = np.random.default_rng(seed).integers(-32768, 32768, size=(n, 2))
    return (ints / 32768.0).astype(np.float32)


def _to_int16(x):
    return np.clip(np.round(x * 32768.0), -32768, 32767).astype(np.int16)


def test_bypass_is_bit_perfect_even_with_dither_enabled():
    x = _pcm16(4096)
    c = compute_correction(1, 4, 44100)
    bal = StereoBalancer(Settings.from_correction(c).updated({"bypass": True}), 44100, 512)
    bal.dither_lsb = 1 / 32768
    y = _run(bal, x)
    np.testing.assert_array_equal(_to_int16(y), _to_int16(x))
    np.testing.assert_array_equal(y, x)  # not just after rounding: identical floats


def test_delay_only_channel_is_bit_perfect_and_untouched_channel_too():
    x = _pcm16(4096)
    s = Settings().updated({"left_delay_ms": 8.75, "left_gain_db": 0.0})
    bal = StereoBalancer(s, 44100, 512)
    bal.dither_lsb = 1 / 32768
    y = _run(bal, x)
    d = round(8.75 * 44100 / 1000)
    np.testing.assert_array_equal(y[d:, LEFT], x[:-d, LEFT])
    np.testing.assert_array_equal(y[:, RIGHT], x[:, RIGHT])


def test_dither_only_on_attenuated_channel_and_is_tpdf():
    n = 200_000
    x = np.zeros((n, 2), np.float32)
    s = Settings().updated({"left_gain_db": -12.0})
    bal = StereoBalancer(s, 44100, 512)
    bal.dither_lsb = 1 / 32768
    y = _run(bal, x)
    assert not y[:, RIGHT].any()                    # untouched channel: exact silence
    noise = y[:, LEFT] * 32768                      # in LSBs
    assert np.abs(noise).max() <= 1.0               # TPDF peaks at +/-1 LSB
    assert abs(noise.mean()) < 0.01
    assert noise.var() == pytest.approx(1 / 6, rel=0.05)  # variance of TPDF with +/-1 LSB peaks


def test_no_dither_when_disabled_or_output_is_float():
    x = _pcm16(2048)
    for lsb, dither in ((1 / 32768, False), (0.0, True)):
        s = Settings().updated({"left_gain_db": -12.0, "dither": dither})
        bal = StereoBalancer(s, 44100, 512)
        bal.dither_lsb = lsb
        y = _run(bal, x)
        np.testing.assert_allclose(y[:, LEFT], x[:, LEFT] * 10 ** (-12 / 20), rtol=1e-6)
