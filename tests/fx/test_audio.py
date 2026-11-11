"""Bass renderer (deterministic, no sound hardware) and the miniaudio output on its null device."""

from __future__ import annotations

import time

import numpy as np
import pytest

from fourdvr_fx import BassTrack, Cue, EffectsDocument
from fourdvr_fx.audio import AudioOutput, AudioState, BassRenderer, miniaudio_available, resample, soft_clip
from fourdvr_fx.dmx import CaptureOutput
from fourdvr_fx.engine import THEATRE, ShowState
from fourdvr_fx.patch import AudioConfig, patch_from_dict
from fourdvr_fx.service import EffectsService

RATE = 48000


def tone(seconds: float, rate: int, hz: float = 40.0, amp: float = 0.5) -> np.ndarray:
    t = np.arange(int(seconds * rate)) / rate
    return amp * np.sin(2 * np.pi * hz * t) * np.minimum(1.0, t / 0.5) * np.minimum(1.0, (seconds - t) / 0.5)


def test_resample_is_exact_for_bass_content() -> None:
    low = tone(4.0, 2000)
    high = resample(low, 2000, RATE)
    assert len(high) == 4 * RATE
    assert np.max(np.abs(high - tone(4.0, RATE))) < 2e-3                    # 40 Hz tone reproduced at 48 kHz
    assert np.max(np.abs(resample(high, RATE, 2000) - low)) < 2e-3          # and back
    assert len(resample(np.zeros(0), 2000, RATE)) == 0 and np.array_equal(resample(low, 2000, 2000), low.astype(np.float32))
    odd = resample(tone(1.0, 2000), 2000, 44100)                            # non-integer ratio
    assert len(odd) == 44100 and np.max(np.abs(odd - tone(1.0, 44100))) < 2e-3


def renderer(**kw) -> BassRenderer:  # type: ignore[no-untyped-def]
    cfg = AudioConfig(enabled=True, sample_rate_hz=RATE, channels=2, bass_channels=[1], vibration_channels=[2],
                      vibration_gain_db=0.0, **kw)
    return BassRenderer(cfg)


def test_bass_follows_the_show_position_sample_accurately() -> None:
    r = renderer()
    track = BassTrack.from_float(tone(10.0, 2000), 2000)
    tid = r.prepare(track)
    assert r.prepare(track) == tid                                          # prepared once
    ref = resample(track.as_float(), 2000, RATE)
    blocks, now, pos = [], 100.0, 2.0
    for _ in range(50):                                                     # 50 x 10 ms
        blocks.append(r.render(480, AudioState(True, pos, now, tid), now))
        now += 0.01
        pos += 0.01
    out = np.concatenate(blocks)
    start = 2 * RATE
    assert np.allclose(out[720:, 0], ref[start + 720 : start + len(out)], atol=1e-6)   # after the 15 ms fade-in: exact
    assert np.all(out[:, 1] == 0.0) and r.resyncs == 0                      # no vibration requested, no re-sync
    assert abs(out[0, 0]) < 1e-3                                            # starts with a fade, not a click
    # the show clock runs ahead by 50 ms: one re-sync, then exact again
    pos += 0.05
    out2 = np.concatenate([r.render(480, AudioState(True, pos + k * 0.01, now + k * 0.01, tid), now + k * 0.01) for k in range(10)])
    i0 = round(pos * RATE)
    assert r.resyncs == 1 and np.allclose(out2[720:, 0], ref[i0 + 720 : i0 + len(out2)], atol=1e-6)
    # small jitter in the reported position (below the tolerance) does not re-sync
    r.render(480, AudioState(True, pos + 0.1 + 0.004, now + 0.1, tid), now + 0.1)
    assert r.resyncs == 1
    # pause: fades out to silence and stays silent
    tail = r.render(480, AudioState(False, 0.0, now + 0.11, tid), now + 0.11)
    assert np.max(np.abs(tail[-10:, 0])) < 0.02 and np.max(np.abs(np.diff(tail[:, 0]))) < 0.02    # faded, no click
    assert np.max(np.abs(r.render(480, AudioState(False), now + 0.12))) == 0.0


def test_latency_offset_loop_and_gain() -> None:
    r = renderer(latency_offset_s=0.1)
    track = BassTrack.from_float(tone(4.0, 2000), 2000, gain_db=-6.0)
    tid = r.prepare(track)
    ref = resample(track.as_float(), 2000, RATE) * 10 ** (-6 / 20)
    r.render(960, AudioState(True, 1.0, 0.0, tid, bass_gain=0.5), 0.0)
    block = r.render(960, AudioState(True, 1.02, 0.02, tid, bass_gain=0.5), 0.02)
    i0 = round(1.12 * RATE)                                            # plays 100 ms ahead
    assert np.allclose(block[:, 0], 0.5 * ref[i0 : i0 + 960], atol=1e-6)
    loop = renderer()
    tid = loop.prepare(track)
    full = resample(track.as_float(), 2000, RATE) * 10 ** (-6 / 20)
    loop.render(480, AudioState(True, 3.98, 0.0, tid, loop_s=4.0), 0.0)
    wrapped = loop.render(1920, AudioState(True, 3.99, 0.01, tid, loop_s=4.0), 0.01)
    assert loop.resyncs == 0 and np.allclose(wrapped[480:, 0], full[: 1440], atol=1e-6)   # wraps to the start


def test_vibration_synth_level_frequency_and_smoothing() -> None:
    r = renderer(vibration_frequency_hz=50.0)
    blocks = [r.render(480, AudioState(True, k * 0.01, k * 0.01, 0, vibration=0.6), k * 0.01) for k in range(100)]
    out = np.concatenate(blocks)[:, 1]
    steady = out[RATE // 2 :]
    assert np.max(np.abs(steady)) == pytest.approx(0.6, abs=0.01)
    spectrum = np.abs(np.fft.rfft(steady * np.hanning(len(steady))))
    assert np.argmax(spectrum) * RATE / len(steady) == pytest.approx(50.0, abs=2.5)
    assert np.max(np.abs(np.diff(out))) < 0.01                              # no clicks while the level ramps
    assert np.all(np.concatenate(blocks)[:, 0] == 0.0)                      # vibration only on its channel
    off = np.concatenate([r.render(480, AudioState(True, 1.0, 1.0 + k * 0.01), 1.0 + k * 0.01) for k in range(30)])
    assert np.max(np.abs(off[-480:, 1])) < 1e-3                             # decays to silence


def test_soft_clip() -> None:
    x = np.array([[-3.0, -0.5, 0.0, 0.79, 0.9, 5.0]], dtype=np.float32)
    y = soft_clip(x)
    assert np.allclose(y[0, 1:4], x[0, 1:4]) and np.all(np.abs(y) <= 1.0) and y[0, 4] < 0.9 and y[0, 5] > 0.99
    assert np.all(np.diff(soft_clip(np.linspace(-2, 2, 401, dtype=np.float32))) > 0)   # monotonic


def test_service_feeds_bass_and_vibration_from_the_document() -> None:
    doc = EffectsDocument(10.0, bass=BassTrack.from_float(tone(10.0, 2000), 2000))
    doc.new_track("vibration").points = [(0.0, 0.0), (5.0, 1.0), (10.0, 1.0)]
    patch = patch_from_dict({"output": {"protocol": "capture"}, "audio": {"enabled": True, "vibration_channels": [2]},
                             "types": {"vibration": {"master": 0.5}}})
    comp = doc.compile()
    svc = EffectsService(patch, [], lambda now: {THEATRE: ShowState(True, comp, 2.5, 10.0, False, 1)}, output=CaptureOutput())
    assert svc.renderer is not None
    svc.prepare(comp)
    svc.tick(7.0)
    st = svc._audio_state
    assert st.active and st.position_s == 2.5 and st.stamp == 7.0 and st.track_id != 0
    assert st.vibration == pytest.approx(0.25) and st.bass_gain == 1.0     # 0.5 (track) x 0.5 (master)
    svc.engine.set_enabled("bass", False)
    svc.tick(7.1)
    assert svc._audio_state.track_id == 0 and svc._audio_state.vibration == pytest.approx(0.25)
    svc.provider = lambda now: {THEATRE: ShowState(False, comp, 2.5, 10.0)}
    svc.tick(7.2)
    assert not svc._audio_state.active
    assert "audio" in svc.status() and svc.status()["audio"]["running"] is False


@pytest.mark.skipif(not miniaudio_available(), reason="miniaudio not installed")
def test_miniaudio_null_device_pulls_the_renderer_in_real_time() -> None:
    r = renderer()
    tid = r.prepare(BassTrack.from_float(tone(4.0, 2000), 2000))
    t0 = time.monotonic()
    out = AudioOutput(r, lambda: AudioState(True, time.monotonic() - t0, time.monotonic(), tid, vibration=0.3), backend="null")
    assert out.start(), out.error
    try:
        time.sleep(0.6)
        assert out.callbacks >= 5 and out.error == "" and out.running
        cursor_s = r._cursor / RATE
        assert cursor_s == pytest.approx(time.monotonic() - t0, abs=0.12)   # the cursor tracks the show clock
        assert r.resyncs <= 1
    finally:
        out.stop()
    assert not out.running


def test_audio_output_reports_missing_device() -> None:
    r = renderer(device="no-such-sound-card-xyz")
    out = AudioOutput(r, AudioState)
    assert out.start() is False and out.error and not out.running
    out.stop()
    doc = EffectsDocument(5.0)
    doc.new_track("water").cues = [Cue(1.0, 0.5)]
    patch = patch_from_dict({"output": {"protocol": "capture"}, "audio": {"enabled": True, "device": "no-such-sound-card-xyz"},
                             "fixtures": [{"id": "w", "effect": "water", "profile": "switch"}]})
    cap = CaptureOutput()
    svc = EffectsService(patch, [], lambda now: {THEATRE: ShowState(True, doc.compile(), 1.2, 5.0)}, output=cap)
    svc.start()
    time.sleep(0.15)
    try:
        assert cap.channel(1, 1) == 255 and svc.status()["audio"]["error"]  # DMX runs although the audio failed
    finally:
        svc.stop()
