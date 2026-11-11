"""Venue patch, safety limiter, engine rendering and the effects service."""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from fourdvr_fx import Cue, EffectsDocument
from fourdvr_fx.dmx import CaptureOutput, DmxOutput
from fourdvr_fx.engine import THEATRE, EffectsEngine, Limiter, ShowState
from fourdvr_fx.patch import Limits, PatchError, load_patch, patch_from_dict
from fourdvr_fx.service import EffectsService

ROOT = Path(__file__).resolve().parents[2]
PLATFORMS = ["PLATFORM-01", "PLATFORM-02"]


def example_patch():  # type: ignore[no-untyped-def]
    return load_patch(ROOT / "config" / "effects" / "patch.example.toml")


def doc_all() -> EffectsDocument:
    doc = EffectsDocument(100.0)
    doc.new_track("wind").points = [(0.0, 0.0), (10.0, 1.0), (100.0, 1.0)]
    doc.new_track("water").cues = [Cue(20.0, 0.5), Cue(20.8, 0.5), Cue(30.0, 10.0)]
    doc.new_track("air").cues = [Cue(5.0, 0.25)]
    doc.new_track("fog").cues = [Cue(40.0, 30.0)]
    doc.new_track("strobe").cues = [Cue(80.0, 2.0, params={"rate_hz": 10.0})]
    doc.new_track("scent").cues = [Cue(60.0, 3.0, params={"slot": 2.0})]
    raw = doc.new_track("dmx")
    raw.universe, raw.channel, raw.points = 5, 7, [(0.0, 0.2), (100.0, 0.2)]
    return doc


def show(doc: EffectsDocument, pos: float, active: bool = True, run: int = 1, loop: bool = False) -> ShowState:
    return ShowState(active, doc.compile(), pos, doc.duration_s, loop, run)


def test_example_patch_loads_and_addresses_platforms() -> None:
    patch = example_patch()
    assert patch.output.protocol == "sacn" and not patch.problems(PLATFORMS) and not patch.warnings()
    inst = {i.key: (i.universe, i.address) for i in patch.instances(PLATFORMS)}
    assert inst["fog-main"] == (1, 1) and inst["wind@PLATFORM-01"] == (2, 1) and inst["wind@PLATFORM-02"] == (2, 9)
    assert inst["water@PLATFORM-02"] == (2, 10) and len(inst) == 4 + 3 * 2
    fog = next(f for f in patch.fixtures if f.id == "fog-main")
    assert (fog.limits.max_on_s, fog.limits.min_off_s, fog.limits.max_duty) == (4.0, 10.0, 0.25)   # override + type defaults
    assert "on ≤ 4 s" in fog.limits.describe()
    # a controller that hosts only the second platform keeps that platform's addresses
    wind = next(f for f in patch.fixtures if f.id == "wind")
    wind.platforms = PLATFORMS
    assert {i.key: i.address for i in patch.instances(["PLATFORM-02"]) if i.fixture.id == "wind"} == {"wind@PLATFORM-02": 9}


@pytest.mark.parametrize(
    "data, message",
    [
        ({"output": {"protocol": "dmx512"}}, "protocol"),
        ({"fixtures": [{"id": "a", "effect": "dmx"}]}, "effect"),
        ({"fixtures": [{"id": "a", "effect": "fog", "address": 600}]}, "address"),
        ({"fixtures": [{"id": "a", "effect": "fog", "colour": 1}]}, "unknown key"),
        ({"fixtures": [{"id": "a", "effect": "fog", "channels": [{"role": "pan"}]}]}, "role"),
        ({"fixtures": [{"id": "a", "effect": "fog", "limits": {"max_duty": 2}}]}, "max_duty"),
        ({"fixtures": [{"id": "a", "effect": "fog"}, {"id": "a", "effect": "wind"}]}, "unique"),
        ({"fixtures": [{"id": "a", "effect": "strobe", "limits": {"max_rate_hz": 40}}]}, "max_rate_hz"),
        ({"audio": {"channels": 0}}, "channels"),
        ({"shows": {}}, "unknown key"),
    ],
)
def test_bad_patches_are_rejected(data, message) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(PatchError, match=message):
        patch_from_dict(data)


def test_patch_problems_and_warnings() -> None:
    patch = patch_from_dict({
        "output": {"protocol": "enttec-pro", "enttec": {"port": "/dev/ttyUSB0", "universe": 1}},
        "audio": {"enabled": True, "channels": 2, "bass_channels": [3]},
        "fixtures": [
            {"id": "a", "effect": "fog", "address": 5, "channels": [{"role": "level"}, {"role": "static", "offset": 1, "value": 9}],
             "limits": {"max_on_s": 0, "duty_window_s": 0}},
            {"id": "b", "effect": "wind", "address": 6},
            {"id": "c", "effect": "water", "scope": "platform", "universe": 2, "address": 1},
            {"id": "s", "effect": "strobe", "address": 100, "limits": {"max_rate_hz": 8}},
        ],
    })
    problems = patch.problems(PLATFORMS)
    assert any("channel 6 already used by a" in p for p in problems)
    assert any("address_step" in p for p in problems) and any("one universe" in p for p in problems)
    assert any("[audio] channel 3" in p for p in problems)
    warnings = patch.warnings()
    assert any("photosensitivity" in w for w in warnings) and any("no activation limit" in w for w in warnings)
    fast = {"id": "s", "effect": "strobe", "flash": "software", "limits": {"max_rate_hz": 12}}
    assert any("uneven at 40 DMX frames" in w for w in patch_from_dict({"fixtures": [fast]}).warnings())
    assert not any("uneven" in w for w in patch_from_dict({"fixtures": [fast | {"flash": "fixture"}]}).warnings())
    with pytest.raises(ValueError, match="effects patch"):
        EffectsEngine(patch, PLATFORMS)


def run_limiter(lim: Limits, request, seconds: float, dt: float = 0.025) -> list[float]:  # type: ignore[no-untyped-def]
    limiter, out, t = Limiter(lim), [], 0.0
    while t < seconds - 1e-9:
        out.append(limiter.step(request(t), t, dt))
        t += dt
    return out


def on_time(levels: list[float], dt: float = 0.025) -> float:
    return sum(dt for v in levels if v > 0.01)


def test_limiter_max_on_cooldown_and_release() -> None:
    always = lambda _t: 1.0  # noqa: E731
    out = run_limiter(Limits(max_on_s=2.0, min_off_s=3.0), always, 12.0)
    assert on_time(out[:80]) == pytest.approx(2.0, abs=0.03) and on_time(out[80:200]) == 0.0   # 2 s on, 3 s off
    assert on_time(out) == pytest.approx(6.0, abs=0.1)                                         # 2 on / 3 off cycle
    held = run_limiter(Limits(max_on_s=2.0), always, 12.0)                                     # no cool-down configured:
    assert on_time(held) == pytest.approx(2.0, abs=0.03)                                       # stays off until released
    again = run_limiter(Limits(max_on_s=2.0), lambda t: 0.0 if 5.0 <= t < 5.1 else 1.0, 12.0)
    assert on_time(again) == pytest.approx(4.0, abs=0.06)
    # min_off also separates two short cues
    two = run_limiter(Limits(min_off_s=1.0), lambda t: 1.0 if (0 <= t < 0.5 or 0.8 <= t < 1.3) else 0.0, 3.0)
    assert on_time(two) == pytest.approx(0.5, abs=0.03)


def test_limiter_duty_total_level_and_ramps() -> None:
    always = lambda _t: 1.0  # noqa: E731
    duty = run_limiter(Limits(duty_window_s=20.0, max_duty=0.25), always, 120.0)
    for start in range(0, 4000, 200):                                           # any 20 s window: <= 25 % (+ one frame)
        assert on_time(duty[start : start + 800]) <= 5.0 + 0.03
    changes = sum(1 for a, b in itertools.pairwise(duty) if (a > 0.01) != (b > 0.01))
    assert changes < 30                                                         # no chattering at the window edge
    total = run_limiter(Limits(max_total_on_s=3.0), lambda t: 1.0 if int(t) % 2 == 0 else 0.0, 20.0)
    assert on_time(total) == pytest.approx(3.0, abs=0.03)
    assert max(run_limiter(Limits(max_level=0.4), always, 1.0)) == pytest.approx(0.4)
    ramp = run_limiter(Limits(ramp_up_s=1.0, ramp_down_s=2.0), lambda t: 1.0 if t < 2.0 else 0.0, 5.0)
    assert ramp[19] == pytest.approx(0.5, abs=0.03) and ramp[60] == pytest.approx(1.0)
    assert ramp[119] == pytest.approx(0.5, abs=0.03) and ramp[-1] == 0.0
    lim = Limiter(Limits(max_total_on_s=1.0))
    for k in range(80):
        lim.step(1.0, k * 0.025, 0.025)
    assert lim.out == 0.0 and lim.reason == "show total reached"
    lim.reset_show()
    assert lim.step(1.0, 2.1, 0.025) == 1.0


def render_at(engine: EffectsEngine, doc: EffectsDocument, pos: float, now: float, platforms=PLATFORMS, **kw):  # type: ignore[no-untyped-def]
    shows = {THEATRE: show(doc, pos, **kw)} | {p: show(doc, pos, **kw) for p in platforms}
    return engine.render(shows, now)


def test_engine_renders_fixtures_leads_and_raw_dmx() -> None:
    engine = EffectsEngine(example_patch(), PLATFORMS)
    doc = doc_all()
    frames = render_at(engine, doc, 4.6, 0.0)
    assert set(frames) == {1, 2, 5} and all(len(f) == 512 for f in frames.values())
    # wind: lead 0.4 s -> level at 5.0 s = 0.5, but the 0.5 s soft start allows only one frame's worth
    assert frames[2][0] == 40 + round(215 * 0.05) and frames[2][8] == frames[2][0]
    assert frames[5][6] == 51                                                # raw DMX 5/7 at 20 %
    for k in range(1, 41):                                                   # one second later the ramp has caught up
        frames = render_at(engine, doc, 4.6 + k * 0.025, k * 0.025)
    assert frames[2][0] == 40 + round(215 * 0.6) and frames[2][2] == 0       # air blast (5.0-5.25 s) is over
    frames = render_at(engine, doc, 5.1, 2.0)
    assert frames[2][2] == 255 and frames[2][10] == 255                      # air on both platforms
    frames = render_at(engine, doc, 39.2, 3.0)
    assert frames[1][0] == 255                                               # fog 1.0 s early (lead)
    frames = render_at(engine, doc, 58.5, 4.0)
    assert frames[1][20] == 255 and frames[1][19] == 0                       # scent slot 2 only (lead 2 s)
    status = engine.status()
    assert {f["key"] for f in status["fixtures"]} >= {"fog-main", "wind@PLATFORM-02"} and status["types"]["water"]["enabled"]
    assert engine.type_levels["scent"] == 1.0


def test_engine_gates_platforms_and_blacks_out() -> None:
    engine = EffectsEngine(example_patch(), PLATFORMS)
    doc = doc_all()
    shows = {THEATRE: show(doc, 20.1), "PLATFORM-01": show(doc, 20.1), "PLATFORM-02": show(doc, 20.1, active=False)}
    frames = engine.render(shows, 0.0)
    assert frames[2][1] == 255 and frames[2][9] == 0                         # water only on the playing platform
    assert frames[2][8] == 0                                                 # ... and no wind there either
    frames = engine.render({THEATRE: show(doc, 20.2, active=False)}, 0.025)  # show paused / stopped: everything off
    assert frames[2][1] == 0 and frames[5][6] == 0 and max(frames[1]) == 0
    assert engine.render({}, 0.05)[2][1] == 0                                # no show at all
    engine.render(shows, 0.1)
    dark = engine.blackout()
    assert all(max(f) == 0 for f in dark.values()) and set(dark) == {1, 2, 5}


def test_engine_applies_limits_masters_and_switches() -> None:
    engine = EffectsEngine(example_patch(), PLATFORMS)
    doc = doc_all()
    water_on = 0.0
    for k in range(0, 800):                                                  # 20 s .. 40 s of the show
        frames = render_at(engine, doc, 20.0 + k * 0.025, k * 0.025)
        water_on += 0.025 if frames[2][1] else 0.0
    # cue 1 (0.5 s), cue 2 blocked by the 1 s pause, long cue: 2 s on / 1 s off cycles
    assert 0.5 + 6.0 <= water_on <= 0.5 + 8.2
    assert "water@PLATFORM-01" not in engine.limited()
    fog_on = 0.0
    for k in range(0, 1300):                                                 # 39 s .. 71.5 s: 30 s fog cue
        frames = render_at(engine, doc, 39.0 + k * 0.025, 100.0 + k * 0.025)
        fog_on += 0.025 if frames[1][0] else 0.0
    assert fog_on <= 15.0 + 0.1 and fog_on >= 8.0                            # 4 s on / 10 s off and <= 25 % duty
    engine.set_master("wind", 0.5)
    for k in range(60):
        frames = render_at(engine, doc, 50.0, 200.0 + k * 0.025)
    assert frames[2][0] == 40 + round(215 * 0.5)
    engine.set_enabled("wind", False)
    for k in range(60):
        frames = render_at(engine, doc, 50.0, 202.0 + k * 0.025)
    assert frames[2][0] == 0
    engine.set_enabled("dmx", False)
    assert render_at(engine, doc, 50.0, 204.0)[5][6] == 0


def test_strobe_rate_is_capped_and_software_flash_is_deterministic() -> None:
    engine = EffectsEngine(example_patch(), PLATFORMS)
    doc = doc_all()                                                          # cue asks for 10 Hz, patch allows 3 Hz
    states = []
    for k in range(80):                                                      # 80.0 .. 82.0 s
        states.append(render_at(engine, doc, 80.0 + k * 0.025, k * 0.025)[1][9] > 0)
    flashes = sum(1 for a, b in zip([False, *states], states, strict=False) if b and not a)
    assert flashes == 6 and engine.instances[1].rate_hz == 3.0               # 3 flashes per second for 2 s
    assert sum(states) <= 6 * 2                                              # short flashes (30 ms -> 2 frames at most)
    patch = example_patch()
    strobe = next(f for f in patch.fixtures if f.id == "strobe-front")
    strobe.flash = "fixture"
    strobe.channels = patch_from_dict({"fixtures": [{"id": "x", "effect": "strobe", "profile": "level-rate"}]}).fixtures[0].channels
    engine2 = EffectsEngine(patch, PLATFORMS)
    frames = render_at(engine2, doc, 80.5, 0.0)
    assert frames[1][9] == 255 and frames[1][10] == round(255 * (3.0 - 1.0) / 19.0)   # level + capped rate channel


def test_fade_tails_do_not_leak_through_a_cool_down() -> None:
    """The end of a fade-out is a level between 0 and 1 %: it must not light a channel that is being held off."""
    engine = EffectsEngine(example_patch(), PLATFORMS)
    doc = EffectsDocument(60.0)
    doc.new_track("fog").cues = [Cue(10.0, 8.0, fade_in=0.5, fade_out=1.0)]      # venue: fog on for 4 s at most
    values = [render_at(engine, doc, 8.0 + k * 0.025, k * 0.025)[1][0] for k in range(480)]   # 8 .. 20 s
    lit = [8.0 + k * 0.025 for k, v in enumerate(values) if v > 0]
    assert lit[0] == pytest.approx(9.0, abs=0.03) and lit[-1] == pytest.approx(13.0, abs=0.03)   # lead 1 s; cut after 4 s
    assert all(b - a < 0.03 for a, b in itertools.pairwise(lit))                 # one activation, nothing later
    assert Limiter(Limits()).step(0.009, 0.0, 0.025) == 0.0 and Limiter(Limits()).step(0.02, 0.0, 0.025) == 0.02


def test_a_strobe_cue_flashes_from_its_own_start() -> None:
    """A lightning cue flashes when it starts, wherever it sits on the timeline; a short cue is one flash."""
    for at in (12.0, 12.11, 12.27, 47.333):
        engine = EffectsEngine(example_patch(), PLATFORMS)
        doc = EffectsDocument(60.0)
        doc.new_track("strobe").cues = [Cue(at, 0.15), Cue(at + 5.0, 1.0)]
        lit = []
        for k in range(280):                                                 # at-0.5 .. at+6.5 s
            pos = at - 0.5 + k * 0.025
            if render_at(engine, doc, pos, k * 0.025)[1][9] > 0:
                lit.append(pos)
        starts = [t for i, t in enumerate(lit) if i == 0 or t - lit[i - 1] > 0.1]
        assert len(starts) == 1 + 3                                          # one flash, then three in the 1 s cue
        assert starts[0] == pytest.approx(at, abs=0.026) and starts[1] == pytest.approx(at + 5.0, abs=0.026)
        assert starts[2] - starts[1] == pytest.approx(1 / 3, abs=0.03)


def test_no_flash_is_lost_when_frames_come_late() -> None:
    """A loaded PC sends some frames late: every flash must still appear (a frame later at worst), none twice."""
    import random

    rng = random.Random(4)
    for _trial in range(20):
        engine = EffectsEngine(example_patch(), PLATFORMS)
        doc = EffectsDocument(60.0)
        doc.new_track("strobe").cues = [Cue(10.0, 0.15), Cue(12.0, 2.0)]      # 1 flash, then 6 at the venue's 3 per second
        pos, now, lit = 9.5, 0.0, []
        while pos < 15.0:
            lit.append((pos, render_at(engine, doc, pos, now)[1][9] > 0))
            step = 0.025 + rng.choice([0.0, 0.0, 0.004, 0.009, 0.02])        # up to 20 ms late
            pos, now = pos + step, now + step
        starts = flash_starts(lit)
        assert len(starts) == 7, starts
        assert starts[0] == pytest.approx(10.0, abs=0.05) and all(b - a > 0.29 for a, b in itertools.pairwise(starts[1:]))
        assert all(t - (12.0 + k / 3) < 0.075 for k, t in enumerate(starts[1:])), starts   # late by three frames at most


def flash_starts(lit: list[tuple[float, bool]]) -> list[float]:
    return [t for (t, on), (_t0, before) in zip(lit, [(0.0, False), *lit[:-1]], strict=True) if on and not before]


def test_software_flashes_keep_their_distance_whatever_the_playhead_does() -> None:
    """Scrubbing, seeking and holding in the Editor must not make a strobe flash faster than the venue allows."""
    import random

    frame, gap = 0.025, 1 / 3 - 1.5 * 0.025                                  # example patch: 3 flashes per second
    doc = EffectsDocument(60.0)
    doc.new_track("strobe").cues = [Cue(10.0, 4.0), Cue(20.0, 4.0, params={"rate_hz": 2.0})]
    rng = random.Random(7)
    for jumps in (1, 3, 40):                                                 # a new place every frame ... once a second
        engine = EffectsEngine(example_patch(), PLATFORMS)
        lit, pos = [], 10.0
        for k in range(200):                                                 # 5 s of dragging the playhead about
            if k % jumps == 0:
                pos = rng.choice([10.0, 20.0]) + rng.uniform(0.0, 3.9)
            lit.append((k * frame, render_at(engine, doc, pos, k * frame)[1][9] > 0))
        starts = flash_starts(lit)
        assert len(starts) >= 3, (jumps, starts)                             # it does flash ...
        assert all(b - a >= gap - 1e-9 for a, b in itertools.pairwise(starts)), (jumps, starts)   # ... never faster
        runs = [len(list(g)) for on, g in itertools.groupby(on for _t, on in lit) if on]
        assert max(runs) <= 3, (jumps, runs)                                 # and a flash stays a flash (30 ms -> 3 frames at most)
    # the playhead held at one place: one flash, wherever it stands (in a flash's window or between two)
    for pos in (10.001, 10.2, 21.26):
        engine = EffectsEngine(example_patch(), PLATFORMS)
        lit = [(k * frame, render_at(engine, doc, pos, k * frame)[1][9] > 0) for k in range(120)]
        assert len(flash_starts(lit)) == 1 and sum(on for _t, on in lit) <= 3, pos


def test_one_strobe_cue_straight_after_another_respects_the_rate() -> None:
    """Two lightning cues 0.2 s apart are 5 flashes per second for a moment: the second one waits."""
    patch = patch_from_dict({"fixtures": [
        {"id": "s", "effect": "strobe", "address": 10, "flash": "software", "limits": {"min_off_s": 0}},
    ]})
    engine = EffectsEngine(patch, PLATFORMS)
    doc = EffectsDocument(60.0)
    doc.new_track("strobe").cues = [Cue(10.0, 0.15), Cue(10.2, 0.15), Cue(12.0, 0.1), Cue(12.15, 0.1)]
    lit = [(9.5 + k * 0.025, render_at(engine, doc, 9.5 + k * 0.025, k * 0.025)[1][9] > 0) for k in range(160)]
    starts = flash_starts(lit)
    assert len(starts) == 3                                                  # the fourth cue is over before it may flash
    assert starts[0] == pytest.approx(10.0, abs=0.026) and starts[1] == pytest.approx(10.3, abs=0.026)
    assert starts[2] == pytest.approx(12.0, abs=0.026)


def test_software_flashes_stay_separate_at_rates_the_output_cannot_follow() -> None:
    """25 flashes per second at 40 DMX frames per second: fewer flashes come out, but never one long one."""
    patch = patch_from_dict({"fixtures": [
        {"id": "s", "effect": "strobe", "address": 10, "flash": "software", "limits": {"max_rate_hz": 25}},
    ]})
    engine = EffectsEngine(patch, PLATFORMS)
    doc = EffectsDocument(60.0)
    doc.new_track("strobe").cues = [Cue(10.0, 2.0, params={"rate_hz": 25.0})]
    lit = [(k * 0.025, render_at(engine, doc, 9.5 + k * 0.025, k * 0.025)[1][9] > 0) for k in range(120)]
    runs = [len(list(g)) for on, g in itertools.groupby(on for _t, on in lit) if on]
    assert max(runs) == 1 and len(runs) == 40, runs                          # 2 s: 50 asked for; lit and dark frames in turn give 40


def test_loop_wrap_and_show_totals_reset() -> None:
    patch = example_patch()
    engine = EffectsEngine(patch, PLATFORMS)
    doc = EffectsDocument(10.0)
    doc.new_track("fog").cues = [Cue(0.2, 1.0)]
    doc.new_track("water").cues = [Cue(1.0, 1.0)]
    looping = {THEATRE: ShowState(True, doc.compile(), 9.5, 10.0, True, 1)}
    assert engine.render(looping, 0.0)[1][0] == 255                          # 9.5 s + 1 s lead wraps to 0.5 s
    ending = {THEATRE: ShowState(True, doc.compile(), 9.5, 10.0, False, 1)}
    assert EffectsEngine(patch, PLATFORMS).render(ending, 0.0)[1][0] == 0    # no loop: nothing after the end
    water = next(f for f in patch.fixtures if f.id == "water")
    water.limits = Limits(max_total_on_s=0.5)
    engine = EffectsEngine(patch, ["PLATFORM-01"])
    doc.tracks[1].cues = [Cue(1.0, 1.0), Cue(4.0, 1.0)]

    def run(run_id: int, t0: float, start: float) -> float:
        on = 0.0
        for k in range(80):
            st = ShowState(True, doc.compile(), start + k * 0.025, 10.0, False, run_id)
            on += 0.025 if engine.render({"PLATFORM-01": st}, t0 + k * 0.025)[2][1] else 0.0
        return on

    assert run(1, 0.0, 0.9) == pytest.approx(0.5, abs=0.03)                  # first cue: cut at the show total
    assert run(1, 2.0, 3.9) == 0.0                                           # second cue of the same run: nothing left
    assert run(2, 20.0, 0.9) == pytest.approx(0.5, abs=0.03)                 # a new run starts fresh
    assert run(2, 30.0, 0.9) == pytest.approx(0.5, abs=0.03)                 # ... and so does a loop restart


def test_manual_fire_needs_test_mode_and_respects_limits() -> None:
    engine = EffectsEngine(example_patch(), PLATFORMS, clock=lambda: 0.0)
    assert not engine.fire("fog-main")                                       # not in test mode
    engine.test_enabled = True
    assert not engine.fire("nope") and engine.fire("fog-main", 1.0, 30.0)
    on = sum(0.025 for k in range(400) if engine.render({}, k * 0.025)[1][0])
    assert on == pytest.approx(4.0, abs=0.05)                                # the fixture's 4 s limit still applies
    engine.clock = lambda: 100.0
    engine.fire("water@PLATFORM-02", 1.0, 0.2)
    assert engine.render({}, 100.0)[2][9] == 255 and engine.render({}, 100.3)[2][9] == 0


class FailingOutput(DmxOutput):
    name = "failing"

    def _send(self, universe: int, data: bytes) -> None:
        raise OSError("network is unreachable")


def test_service_isolates_failures_and_goes_dark() -> None:
    doc = doc_all()
    state = {"pos": 20.1, "boom": False}

    def provider(_now: float):  # type: ignore[no-untyped-def]
        if state["boom"]:
            raise RuntimeError("bad programme data")
        return {THEATRE: show(doc, state["pos"]), "PLATFORM-01": show(doc, state["pos"])}

    out = CaptureOutput()
    svc = EffectsService(example_patch(), PLATFORMS, provider, output=out)
    svc.tick(0.0)
    assert out.channel(2, 2) == 255 and svc.errors == 0
    state["boom"] = True
    frames = svc.tick(0.025)
    assert all(max(f) == 0 for f in frames.values()) and svc.errors == 1 and "bad programme" in svc.last_error
    assert out.channel(2, 2) == 0
    state["boom"] = False
    svc.tick(2.0)
    assert out.channel(2, 2) == 255                                          # recovers by itself
    bad = EffectsService(example_patch(), PLATFORMS, provider, output=FailingOutput())
    bad.tick(0.0)
    assert bad.output.errors == 3 and bad.errors == 0 and bad.status()["output"]["last_error"]


def test_service_thread_runs_at_refresh_rate_and_stops_dark() -> None:
    import time

    doc = doc_all()
    out = CaptureOutput(history=10000)
    t0 = time.monotonic()
    svc = EffectsService(example_patch(), PLATFORMS, lambda now: {THEATRE: show(doc, 41.5 + (now - t0))}, output=out)
    svc.start()
    time.sleep(0.6)
    assert svc.running and out.channel(1, 1) == 255                          # fog on
    svc.stop()
    assert not svc.running and out.channel(1, 1) == 0 and not out.is_open
    per_universe = sum(1 for _t, u, _d in out.history if u == 1)
    assert 18 <= per_universe <= 32                                          # ~40 Hz for 0.6 s (+ 3 dark frames)
    st = svc.status()
    assert st["errors"] == 0 and st["refresh_hz"] == 40.0 and "audio" not in st
