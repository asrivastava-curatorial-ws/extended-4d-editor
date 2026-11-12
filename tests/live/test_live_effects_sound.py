"""Effects made from a live source, and the sound for the shakers."""

from __future__ import annotations

import numpy as np
import pytest

from fourdvr_fx.audio import AudioState, BassRenderer
from fourdvr_fx.engine import THEATRE, EffectsEngine, ShowState
from fourdvr_fx.patch import AudioConfig
from fourdvr_fx.preflight import default_patch
from fourdvr_live.effects import LiveDocument, LiveRules, RuleEngine
from fourdvr_live.sample import LiveSample
from fourdvr_live.shaker import CaptureFeed, MixFeed, ShakerSettings, SynthFeed
from fourdvr_live.sources.sound import BassBand


def settle(rules: RuleEngine, sample: LiveSample, seconds: float = 4.0, envelope: float = 1.0) -> dict[str, float]:
    levels: dict[str, float] = {}
    for k in range(int(seconds * 100)):
        levels = rules.step(sample, envelope, 10.0 + k / 100.0, 0.01)
    return levels


# --------------------------------------------------------------------------------------------- rules
def test_wind_follows_speed_and_vibration_follows_engine_and_road() -> None:
    r = RuleEngine()
    assert settle(r, LiveSample(speed=2.0)) == {}
    assert settle(r, LiveSample(speed=29.0))["wind"] == pytest.approx(0.5, abs=0.01)
    assert settle(r, LiveSample(speed=120.0))["wind"] == pytest.approx(1.0, abs=0.005)
    idle = settle(RuleEngine(), LiveSample(rpm=900.0, rpm_max=7000.0))["vibration"]
    flat_out = settle(RuleEngine(), LiveSample(rpm=7000.0, rpm_max=7000.0, throttle=1.0))["vibration"]
    assert 0.0 < idle < 0.05 and flat_out == pytest.approx(0.35, abs=0.01)
    assert settle(RuleEngine(), LiveSample(rumble=1.0))["vibration"] == pytest.approx(0.8, abs=0.01)
    assert settle(RuleEngine(), LiveSample(speed=120.0, rumble=1.0), envelope=0.5) == {
        "wind": pytest.approx(0.5, abs=0.01), "vibration": pytest.approx(0.4, abs=0.01)}
    assert settle(RuleEngine(), LiveSample(speed=120.0), envelope=0.0) == {}
    assert RuleEngine().step(None, 1.0, 0.0, 0.01) == {}


def test_a_hit_fires_the_air_blast_for_a_moment() -> None:
    r = RuleEngine()
    assert "air" not in r.step(LiveSample(impact=0.2), 1.0, 5.0, 0.01)        # too weak
    assert r.step(LiveSample(impact=0.6), 1.0, 5.01, 0.01)["air"] == 1.0
    assert r.step(LiveSample(), 1.0, 5.2, 0.01)["air"] == 1.0                 # still within the blast
    assert "air" not in r.step(LiveSample(), 1.0, 5.4, 0.01)
    off = RuleEngine(LiveRules(air_on_impact=False))
    assert "air" not in off.step(LiveSample(impact=1.0), 1.0, 0.0, 0.01)


def test_what_a_source_states_itself_passes_but_not_everything() -> None:
    stated = LiveSample(effects={"rain": 0.6, "fog": 0.3, "strobe": 1.0, "dmx": 1.0, "splash": 0.5, "laser": 0.4},
                        params={"scent": {"slot": 2}})
    levels = settle(RuleEngine(), stated, 0.1)
    assert levels["rain"] == 0.6 and levels["fog"] == 0.3 and levels["water"] == 0.5
    assert levels["laser"] == 0.4                                # a venue's own effect type
    assert "strobe" not in levels and "dmx" not in levels and "splash" not in levels
    allowed = RuleEngine(LiveRules(strobe=True))
    assert settle(allowed, stated, 0.1)["strobe"] == 1.0 and allowed.params["strobe"] == {"rate_hz": 3.0}
    assert "rain" not in settle(RuleEngine(LiveRules(stated=False)), stated, 0.1)
    with pytest.raises(ValueError, match="unknown effects rule"):
        LiveRules.from_dict({"wind_ful_m_s": 40})
    with pytest.raises(ValueError, match="above wind_start"):
        LiveRules.from_dict({"wind_full_m_s": 2.0})


def test_live_document_is_a_programme_to_the_effects_engine() -> None:
    doc = LiveDocument()
    doc.set({"scent": 1.0, "strobe": 0.8}, {"scent": {"slot": 2.0}, "strobe": {"rate_hz": 4.0}}, 12.0)
    assert doc.level("scent", 99.0) == 1.0 and doc.level("scent", 0.0, {"slot": 2.0}) == 1.0
    assert doc.level("scent", 0.0, {"slot": 3.0}) == 0.0 and doc.level("wind", 0.0) == 0.0
    assert doc.params("strobe", 0.0) == {"rate_hz": 4.0} and doc.onset("strobe", 0.0) == 12.0
    doc.set({"strobe": 0.8}, {}, 15.0)
    assert doc.onset("strobe", 0.0) == 12.0                      # the same cue goes on: flashes keep counting
    doc.set({}, {}, 16.0)
    doc.set({"strobe": 0.5}, {}, 20.0)
    assert doc.onset("strobe", 0.0) == 20.0 and doc.raw_dmx() == [] and doc.bass is None


def test_fixture_limits_hold_for_live_levels() -> None:
    """A game that says "rain" for a minute gets what the rain fixture's limits allow: 5 s on, 5 s off."""
    patch = default_patch(["rain", "wind", "air"])
    now = [0.0]
    engine = EffectsEngine(patch, [], lambda: now[0])
    doc = LiveDocument()
    rules = RuleEngine()
    on = 0
    wind = []
    for k in range(40 * 40):
        now[0] = k / 40.0
        levels = rules.step(LiveSample(speed=120.0, effects={"rain": 1.0}), 1.0, now[0], 0.025)
        doc.set(levels, rules.params, now[0])
        engine.render({THEATRE: ShowState(True, doc, now[0], 0.0, False, 0)}, now[0])
        by_effect = {f["effect"]: f for f in engine.status()["fixtures"]}
        on += by_effect["rain"]["level"] > 0.0
        wind.append(by_effect["wind"]["level"])
    assert on / 40.0 == pytest.approx(20.0, abs=0.2)             # half of the 40 seconds
    assert wind[10] < 0.6 and wind[-1] == pytest.approx(1.0, abs=0.005)   # the fixture's own ramp on top of the rule's


# --------------------------------------------------------------------------------------------- shaker sound
def tone_hz(x: np.ndarray, rate: int) -> float:
    spectrum = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return float(np.fft.rfftfreq(len(x), 1.0 / rate)[int(np.argmax(spectrum))])


def render(feed, seconds: float, rate: int = 8000, block: int = 160, key: int = 1) -> np.ndarray:  # type: ignore[no-untyped-def]
    return np.concatenate([feed.read(key, block, rate, 0.0) for _ in range(int(seconds * rate / block))])


def test_engine_tone_follows_the_revs_and_is_silent_at_rest() -> None:
    feed = SynthFeed(ShakerSettings(road=False, impacts=False))
    assert not np.any(render(feed, 0.2))
    feed.update(LiveSample(rpm=900.0, rpm_max=7000.0, rpm_idle=900.0, throttle=0.5), 1.0)
    low = render(feed, 2.0)
    feed.update(LiveSample(rpm=7000.0, rpm_max=7000.0, rpm_idle=900.0, throttle=1.0), 1.0)
    between = render(feed, 0.5)
    high = render(feed, 2.0)
    assert tone_hz(low[4000:], 8000) == pytest.approx(28.0, abs=1.0)
    assert tone_hz(high[4000:], 8000) == pytest.approx(85.0, abs=1.0)
    assert np.abs(high).max() <= 0.55 and np.abs(low).max() < np.abs(high).max()
    assert np.abs(np.diff(np.concatenate([low, between, high]))).max() < 0.08   # no click where the tone changes
    feed.update(None, 0.0)
    render(feed, 0.2)
    assert np.abs(render(feed, 0.2)).max() < 1e-3


def test_road_rumble_is_low_and_a_hit_is_a_thump_that_dies_away() -> None:
    feed = SynthFeed(ShakerSettings(engine=False, impacts=False))
    feed.update(LiveSample(rumble=1.0), 1.0)
    x = render(feed, 6.0, rate=8000)[8000:]
    spectrum = np.abs(np.fft.rfft(x)) ** 2
    freqs = np.fft.rfftfreq(len(x), 1 / 8000)
    assert spectrum[freqs < 150].sum() > 0.97 * spectrum.sum() and 0.05 < np.sqrt(np.mean(x * x)) < 0.6
    thump = SynthFeed(ShakerSettings(engine=False, road=False))
    assert not np.any(render(thump, 0.02))                       # the output is playing (silence) when it happens
    thump.update(LiveSample(impact=1.0), 1.0)
    thump.update(LiveSample(), 1.0)                              # the hit lasts one sample; the thump rings on
    y = render(thump, 1.0)
    assert 0.5 < np.abs(y[:800]).max() <= 0.9 and np.abs(y[6400:]).max() < 0.01
    assert tone_hz(y[:4000], 8000) == pytest.approx(42.0, abs=4.0)


def test_two_outputs_play_the_same_feed_at_their_own_rates() -> None:
    feed = SynthFeed(ShakerSettings(road=False, impacts=False))
    feed.update(LiveSample(rpm=4000.0, rpm_max=7000.0, rpm_idle=900.0, throttle=1.0), 1.0)
    a = render(feed, 1.5, rate=48000, block=1920, key=1)
    b = render(feed, 1.5, rate=8000, block=200, key=2)
    assert tone_hz(a[24000:], 48000) == pytest.approx(tone_hz(b[4000:], 8000), abs=1.0)


def test_capture_feed_plays_what_was_written_a_little_later() -> None:
    feed = CaptureFeed(rate=4000, delay_s=0.05)
    t = np.arange(4000 * 3) / 4000.0
    wave = (0.5 * np.sin(2 * np.pi * 40.0 * t)).astype(np.float32)
    out = []
    for k in range(150):                                         # 20 ms at a time, read at 48 kHz
        feed.write(wave[k * 80 : (k + 1) * 80])
        out.append(feed.read(7, 960, 48000, 0.0))
    y = np.concatenate(out)
    assert not np.any(y[:960])                                   # nothing to play in the first block
    steady = y[48000:]
    assert tone_hz(steady, 48000) == pytest.approx(40.0, abs=1.0)
    assert np.abs(steady).max() == pytest.approx(0.5, abs=0.02) and feed.resyncs <= 1
    assert np.abs(np.diff(steady)).max() < 0.01                  # continuous across the blocks
    mixed = MixFeed(feed, None)
    assert mixed.feeds == [feed]


def test_capture_feed_puts_a_reader_back_that_fell_out_of_step() -> None:
    feed = CaptureFeed(rate=4000, delay_s=0.05)
    for _ in range(30):
        feed.write(np.ones(80, dtype=np.float32))
        feed.read(1, 960, 48000, 0.0)
    for _ in range(20):                                          # the reader stops for 0.4 s
        feed.write(np.ones(80, dtype=np.float32))
    before = feed.resyncs
    feed.read(1, 960, 48000, 0.0)
    assert feed.resyncs == before + 1


def test_bass_outputs_play_a_live_feed_through_their_channels() -> None:
    cfg = AudioConfig(enabled=True, sample_rate_hz=8000, channels=2, bass_channels=[1], vibration_channels=[2])
    renderer = BassRenderer(cfg)
    feed = SynthFeed(ShakerSettings(road=False, impacts=False))
    feed.update(LiveSample(rpm=5000.0, rpm_max=7000.0, throttle=1.0), 1.0)
    state = AudioState(active=True, stamp=0.0, feed=feed, vibration=0.0)
    out = np.concatenate([renderer.render(400, state, k * 0.05) for k in range(20)])
    assert np.abs(out[:, 0]).max() > 0.2 and not np.any(out[:, 1])         # the feed goes to the bass channel only
    state.bass_gain = 0.0
    quiet = renderer.render(400, state, 1.0)
    assert not np.any(quiet)
    off = renderer.render(400, AudioState(active=False, feed=feed), 1.1)
    assert not np.any(off)


def test_bass_band_keeps_the_low_end_and_marks_thumps() -> None:
    feed = CaptureFeed(rate=4000, delay_s=0.02)
    band = BassBand(feed, cutoff_hz=120.0, rate_in=48000)
    t = np.arange(48000 * 2) / 48000.0
    sound = 0.4 * np.sin(2 * np.pi * 50.0 * t) + 0.4 * np.sin(2 * np.pi * 1000.0 * t)
    out = []
    for k in range(100):
        band.process(sound[k * 960 : (k + 1) * 960].astype(np.float32))
        out.append(feed.read(1, 80, 4000, 0.0))
    y = np.concatenate(out)[4000:]
    spectrum = np.abs(np.fft.rfft(y))
    freqs = np.fft.rfftfreq(len(y), 1 / 4000)
    assert spectrum[np.argmin(np.abs(freqs - 50.0))] > 300 * spectrum[np.argmin(np.abs(freqs - 1000.0))]
    assert np.abs(y).max() == pytest.approx(0.4, abs=0.05) and 0.5 < band.level <= 1.0
    band.take_thump()
    for _ in range(40):                                          # quiet ...
        band.process(np.zeros(960, dtype=np.float32))
    assert band.take_thump() == 0.0
    band.process((0.8 * np.sin(2 * np.pi * 45.0 * t[:960])).astype(np.float32))   # ... then a bang
    band.process((0.8 * np.sin(2 * np.pi * 45.0 * t[960:1920])).astype(np.float32))
    assert band.take_thump() > 0.3 and band.take_thump() == 0.0
