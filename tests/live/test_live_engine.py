"""The live show as a whole: source -> platforms, effects, bass; recording; a programme of a session."""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pytest

from fourdvr_fx.dmx import CaptureOutput
from fourdvr_fx.embed import read_effects
from fourdvr_fx.preflight import default_patch
from fourdvr_live import sim
from fourdvr_live.config import LiveConfig, LiveConfigError, load_live_config, parse_live_config
from fourdvr_live.convert import ConvertOptions, convert_session, effects_from_session, events_from_session, trimmed
from fourdvr_live.engine import LiveEngine
from fourdvr_live.safety import RUNNING, STALLED
from fourdvr_live.sample import LiveSample
from fourdvr_live.session import Session, SessionError, SessionRecorder
from fourdvr_live.sources.replay import DemoSource, ReplaySource
from fourdvr_motion import motionfile
from fourdvr_motion.runtime import Phase
from live_helpers import PROFILE, ROOT, ManualSource, Stepper


def play_lap(engine: LiveEngine, st: Stepper, drive: sim.Drive, seconds: float = sim.LAP_S, record: list | None = None) -> None:
    """Three quiet seconds, then the drive at 50 samples a second (every second control period)."""
    st.step(n=300)
    for k in range(int(seconds * 100)):
        if k % 2 == 0:
            engine.guard.offer(drive.at(k / 100.0).sample, st.now)
        st.step()
        if record is not None:
            record.append([rt.pipeline.pose.copy() for rt in engine.runtimes])


def test_a_drive_moves_the_platform_within_its_limits(make_engine, drive: sim.Drive) -> None:  # type: ignore[no-untyped-def]
    engine, st = make_engine()
    rt = engine.runtimes[0]
    poses: list = []
    play_lap(engine, st, drive, record=poses)
    assert rt.phase == Phase.PLAYING and rt.fault_reason == "" and engine.guard.state == RUNNING
    p = np.array(poses)[:, 0, :]
    lim, neutral = rt.cfg.limits, rt.cfg.calibration.neutral_pose
    assert np.all(p <= lim.position_max + 1e-9) and np.all(p >= lim.position_min - 1e-9)
    v = np.diff(p, axis=0) * 100.0
    a = np.diff(v, axis=0) * 100.0
    assert np.all(np.abs(v) <= lim.velocity * 1.001) and np.all(np.abs(a) <= lim.acceleration * 1.001)
    moved = np.max(np.abs(p - neutral), axis=0)
    assert moved[0] > 0.02 and moved[1] > 0.03 and moved[3] > 0.05 and moved[4] > 0.04       # it really moved
    # speeding up tips the nose up, the left bend leans the seat right side down
    assert p[500, 4] < neutral[4] - 0.02 and p[1050, 3] > neutral[3] + 0.03
    counters = rt.pipeline.counters.as_dict()
    assert counters["position_clamps"] == 0 and counters["jerk_clamps"] < 40                 # only at the collision
    # the source stops: back to neutral, and it stays the platform's own decision how fast
    st.step(n=250)
    assert engine.guard.state == STALLED and engine.guard.envelope == 0.0
    st.step(n=300)
    assert np.allclose(rt.pipeline.pose, neutral, atol=2e-3) and rt.phase == Phase.PLAYING


def test_every_platform_gets_the_same_cue_and_a_fault_waits_for_the_operator(make_engine, drive: sim.Drive) -> None:  # type: ignore[no-untyped-def]
    engine, st = make_engine(platforms=2)
    a, b = engine.runtimes
    play_lap(engine, st, drive, 12.0)
    assert np.allclose(a.pipeline.pose, b.pipeline.pose, atol=1e-9) and a.following and b.following
    b._enter_fault("test: board reported a fault")
    st.step(lambda: drive.at(12.0).sample, n=300)
    assert b.phase == Phase.FAULT and not b.following and a.following
    status = {p["id"]: p for p in engine.status()["platforms"]}
    assert status["PLATFORM-02"]["fault"] and status["PLATFORM-01"]["fault"] is None
    states = engine._states(st.now)
    assert states["PLATFORM-01"].active and not states["PLATFORM-02"].active and states["theatre"].active
    engine.engage()                               # the operator's "go on"
    st.step(lambda: drive.at(12.0).sample, n=500)
    assert b.phase == Phase.PLAYING and b.following


def test_hold_gain_and_the_source_coming_back(make_engine, drive: sim.Drive) -> None:  # type: ignore[no-untyped-def]
    engine, st = make_engine()
    rt = engine.runtimes[0]
    neutral = rt.cfg.calibration.neutral_pose
    play_lap(engine, st, drive, 5.0)
    assert np.abs(rt.pipeline.pose - neutral).max() > 0.02
    engine.hold()
    st.step(lambda: drive.at(5.0).sample, n=400)
    assert engine.guard.envelope == 0.0 and np.allclose(rt.pipeline.pose, neutral, atol=2e-3)
    assert not engine._states(st.now)["theatre"].active
    engine.engage()
    engine.set_gain(0.0)
    st.step(lambda: drive.at(5.0).sample, n=600)
    assert engine.guard.envelope == 1.0 and np.allclose(rt.pipeline.pose, neutral, atol=1e-3)     # strength zero
    assert engine.set_gain(5.0) == 2.0 and engine.set_gain(-1.0) == 0.0


def test_effects_and_bass_go_through_the_venue_patch(profile, drive: sim.Drive) -> None:  # type: ignore[no-untyped-def]
    patch = default_patch(["wind", "rain", "air"])
    out = CaptureOutput()
    cfg = LiveConfig(platforms=[profile], patch=patch)
    engine = LiveEngine(cfg, source=ManualSource(), effects_output=out, audio_backend="null")
    st = Stepper(engine)
    svc = engine.service
    assert svc is not None and engine.doc.feed is not None
    address = {f.effect: f.address for f in patch.fixtures}
    seen: dict[str, list[int]] = {"wind": [], "rain": [], "air": []}
    st.step(n=300)
    for k in range(int(sim.LAP_S * 100)):
        if k % 2 == 0:
            engine.guard.offer(drive.at(k / 100.0).sample, st.now)
        st.step()
        if k % 4 == 0:                           # the effects service's own 25 frames a second
            svc.tick(st.now)
            for name in seen:
                seen[name].append(out.channel(1, address[name]))
    wind, rain, air = (np.array(seen[k]) for k in ("wind", "rain", "air"))
    t = np.arange(len(wind)) / 25.0
    assert wind[t < 1.5].max() == 0 and wind[(t > 18) & (t < 20)].min() > 40          # wind with the speed
    assert rain[(t > 25) & (t < 27)].min() > 100 and rain[t < 23].max() == 0           # the rain the source states
    assert air[(t > 29.0) & (t < 29.4)].max() == 255 and air[t < 28.5].max() == 0      # a blast at the collision
    status = engine.status()
    assert status["effects"]["running"] is False and "vibration" in status["levels"]      # the engine idles at the end
    # the shakers' sound is asked for as long as the show runs
    audio = svc._state_for(engine._states(st.now)["theatre"], svc.renderer, 0.0, st.now)  # type: ignore[arg-type]
    assert audio.active and audio.feed is engine.doc.feed
    engine.hold()
    for _ in range(200):
        st.step(lambda: drive.at(20.0).sample)
        svc.tick(st.now)
    assert out.channel(1, address["wind"]) == 0


def test_theatre_effects_rest_when_no_platform_follows(make_engine, drive: sim.Drive) -> None:  # type: ignore[no-untyped-def]
    engine, st = make_engine()
    play_lap(engine, st, drive, 4.0)
    assert engine._states(st.now)["theatre"].active
    engine.runtimes[0]._enter_fault("test")
    st.step(lambda: drive.at(4.0).sample, n=50)
    assert not engine._states(st.now)["theatre"].active
    none, st2 = make_engine(platforms=0)          # a venue without platforms: effects only
    st2.step(lambda: drive.at(4.0).sample, n=400)
    assert none._states(st2.now)["theatre"].active and none.runtimes == []


def test_show_totals_start_again_after_show_minutes(make_engine) -> None:  # type: ignore[no-untyped-def]
    engine, st = make_engine(platforms=0)
    engine.cfg.rules.show_minutes = 1.0
    st.step(LiveSample(speed=10.0), n=5)
    t0 = st.now
    assert engine._states(t0 + 10.0)["theatre"].run_id == 0 and engine._states(t0 + 61.0)["theatre"].run_id == 1


# --------------------------------------------------------------------------------------------- recording
@pytest.fixture(scope="module")
def session(tmp_path_factory: pytest.TempPathFactory) -> Session:
    from dataclasses import replace

    from fourdvr_motion.platform_config import load_platform_config

    cfg = LiveConfig(platforms=[replace(load_platform_config(PROFILE), driver="simulation")], sound=False)
    engine = LiveEngine(cfg, source=ManualSource())
    st = Stepper(engine)
    engine.start_recording()
    play_lap(engine, st, sim.Drive())
    path = engine.stop_recording(tmp_path_factory.mktemp("live") / "lap")
    assert path is not None and path.suffix == ".4dlive" and engine.recorder is None
    return Session.load(path)


def test_a_session_holds_what_the_source_delivered(session: Session, drive: sim.Drive) -> None:
    assert session.rate_hz == 100.0 and session.samples == 4300 and session.duration_s == pytest.approx(42.99)
    assert session.header["format"] == "4D-LIVE-SESSION" and session.header["source"]["kind"] == "car"
    assert set(session.effect_names) == {"wind", "vibration", "air", "rain"}
    i = 300 + 1000                                # ten seconds into the drive: in the left bend
    want = drive.at(10.0).sample
    assert session.accel[i] == pytest.approx(want.accel, abs=1e-4) and session.rates[i, 2] == pytest.approx(want.rates[2])
    assert session.scalars["speed"][i] == pytest.approx(want.speed, abs=1e-3)
    assert not session.active[:300].any() and session.active[700:].all()
    assert np.abs(session.pose).max() > 0.03 and session.level("wind").max() > 0.3
    summary = session.summary()
    assert summary["top_speed_m_s"] == pytest.approx(20.6, abs=0.2) and summary["running_s"] > 36.0


def test_session_files_are_checked(session: Session, tmp_path: Path) -> None:
    copy = session.save(tmp_path / "again.4dlive")
    again = Session.load(copy)
    assert np.allclose(again.accel, session.accel) and again.header == session.header
    assert np.allclose(again.effects, session.effects, atol=1 / 255)
    (tmp_path / "junk.4dlive").write_bytes(b"not a session")
    with pytest.raises(SessionError, match="cannot be read"):
        Session.load(tmp_path / "junk.4dlive")
    np.savez(tmp_path / "other.npz", header=np.array('{"format": "something else"}'))
    with pytest.raises(SessionError, match="not a live session"):
        Session.load(tmp_path / "other.npz")
    with pytest.raises(SessionError):
        Session.load(tmp_path / "missing.4dlive")
    part = trimmed(session, 10.0, 20.0)
    assert part.samples == 1001 and part.duration_s == pytest.approx(10.0) and np.array_equal(part.accel[0], session.accel[1000])


def test_recorder_grows_names_effects_and_stops_at_its_limit() -> None:
    rec = SessionRecorder(100.0, {"source": {"title": "test"}})
    rec.limit = 20000
    for k in range(20010):
        rec.add(LiveSample(speed=float(k)), 1.0, np.zeros(6), {"wind": 0.5} if k > 10000 else {})
    assert len(rec) == 20000 and rec.full
    s = rec.snapshot()
    assert s.samples == 20000 and s.effect_names == ["wind"] and s.level("wind")[10001] == pytest.approx(0.5, abs=0.01)
    assert s.scalars["speed"][19999] == 19999.0 and s.level("fog").max() == 0.0


def test_a_programme_of_a_session(session: Session, tmp_path: Path) -> None:
    out = tmp_path / "lap.motion"
    notes: list[str] = []
    result = convert_session(session, out, ConvertOptions(profile=PROFILE, programme_id=7),
                             progress=lambda _f, text: notes.append(text))
    assert result.passed and result.path == out and notes[-1] == "written: lap.motion"
    mf = motionfile.read_motion(out, verify=True)
    assert mf.programme_id == 7 and mf.duration_s == pytest.approx(session.duration_s) and len(mf.samples) == 4300
    assert mf.header["generation"]["live_session"]["source"] == "Built-in demonstration drive"
    assert mf.header["validation"]["passed"] is True and mf.header["video"]["sha256"] == ""
    motion = np.asarray(mf.samples)
    assert np.allclose(motion[:250], 0.0, atol=2e-3)             # nothing happens before the drive begins
    assert motion[300 + 400, 4] < -0.02 and motion[300 + 1000, 3] > 0.02          # nose up, leaning into the bend
    doc = read_effects(out)
    assert doc is not None and {t.type for t in doc.tracks} == {"wind", "vibration", "air", "rain"}
    by_type = {t.type: t for t in doc.tracks}
    assert len(by_type["air"].cues) == 1 and by_type["air"].cues[0].t == pytest.approx(32.0, abs=0.1)
    rain = by_type["rain"].cues
    assert len(rain) == 1 and rain[0].t == pytest.approx(27.0, abs=0.3) and rain[0].duration == pytest.approx(4.0, abs=0.5)
    assert rain[0].level == pytest.approx(0.6, abs=0.01)
    wind = by_type["wind"]
    assert 4 <= len(wind.points) < 200 and wind.value_at(22.0) > 0.25 and wind.value_at(1.0) < 0.01
    assert doc.bass is not None and doc.bass.duration_s == pytest.approx(session.duration_s, abs=0.01)
    assert doc.problems() == []


def test_conversion_options_and_refusals(session: Session, tmp_path: Path) -> None:
    half = convert_session(session, None, ConvertOptions(profile=PROFILE, gain=0.5, effects=False, bass=False))
    full = convert_session(session, None, ConvertOptions(profile=PROFILE))
    assert half.document is None and half.path is None and full.document is not None
    assert np.abs(half.motion).max() < np.abs(full.motion).max()
    part = convert_session(session, None, ConvertOptions(profile=PROFILE, start_s=10.0, end_s=25.0))
    assert part.duration_s == pytest.approx(15.0)
    events = events_from_session(session, 100.0, "aircraft")
    assert events.class_fractions()["flight"] > 0.8 and events.class_fractions()["static"] > 0.05
    assert len(events.impacts) == 1 and events.impacts[0][0] == pytest.approx(32.0, abs=0.05)
    no_bass = effects_from_session(session, bass=False)
    assert no_bass.bass is None and len(no_bass.tracks) == 4
    with pytest.raises(ValueError, match="too short"):
        convert_session(trimmed(session, 1.0, 1.0), tmp_path / "x.motion")


# --------------------------------------------------------------------------------------------- sources without a game
def test_replay_and_demo_sources_deliver_in_real_time(session: Session, tmp_path: Path) -> None:
    short = trimmed(session, 12.0, 12.5)
    path = short.save(tmp_path / "short.4dlive")
    got: list[LiveSample] = []
    src = ReplaySource(str(path))
    src.start(lambda sample, _now: got.append(sample))
    src._thread.join(timeout=3.0)                 # type: ignore[union-attr]
    src.stop()
    assert len(got) == short.samples and got[10].accel == pytest.approx(tuple(short.accel[10]), abs=1e-6)
    assert got[0].effects == {} and got[0].active
    demo: list[LiveSample] = []
    d = DemoSource(seconds=0.5)
    d.start(lambda sample, _now: demo.append(sample))
    d._thread.join(timeout=3.0)                   # type: ignore[union-attr]
    d.stop()
    assert 25 <= len(demo) <= 31 and d.status()["samples"] == len(demo)


# --------------------------------------------------------------------------------------------- configuration
def test_configuration_from_a_file_and_from_the_show_services_own(tmp_path: Path) -> None:
    (tmp_path / "keys").mkdir()
    (tmp_path / "controller.toml").write_text(f'''
[controller]
server_url = "ws://127.0.0.1:8080/ws/v1/device"
control_rate_hz = 125
[[platforms]]
config = "{PROFILE}"
id = "SEAT-A"
driver = "simulation"
device_key = "k"
[effects]
patch = "{ROOT / "config" / "effects" / "patch.example.toml"}"
reference_platform = "SEAT-A"
''')
    (tmp_path / "live.toml").write_text('''
[live]
controller = "controller.toml"
record = "sessions"
hold = true
[source]
type = "f1"
port = 20999
[axes]
yaw = -1
[cueing]
gain = 0.5
axis_gains = { heave = 0.0 }
[rules]
wind_full_m_s = 80
[shaker]
engine = false
[safety]
stall_s = 0.4
''')
    cfg = load_live_config(tmp_path / "live.toml")
    assert [p.platform_id for p in cfg.platforms] == ["SEAT-A"] and cfg.rate_hz == 125.0 and cfg.patch is not None
    assert cfg.reference_platform == "SEAT-A" and cfg.hold and cfg.record_dir == tmp_path / "sessions"
    assert cfg.source == {"type": "f1", "port": 20999} and cfg.axes == {"yaw": -1.0}
    assert cfg.cueing.gain == 0.5 and cfg.cueing.axis_gains["heave"] == 0.0 and cfg.cueing.axis_gains["surge"] == 1.0
    assert cfg.rules.wind_full_m_s == 80.0 and not cfg.shaker.engine and cfg.safety.stall_s == 0.4
    text = "\n".join(cfg.describe())
    assert "EA SPORTS F1 UDP (UDP port 20999)" in text and "SEAT-A" in text and "yaw x -1" in text and "starts held" in text


@pytest.mark.parametrize(("text", "message"), [
    ("[sauce]\ntype = 'forza'", "unknown section"),
    ("[live]\nrat_hz = 100", r"\[live\]: unknown key"),
    ("[live]\nrate_hz = 20", "rate_hz must be"),
    ("[axes]\nspin = 1", "unknown axis"),
    ("[cueing]\ngain = 9", "gain must be"),
    ("[safety]\nstal_s = 1", r"\[safety\]: unknown key"),
    ("[rules]\nwind = 'yes'\nbubbles = true", "unknown effects rule"),
    ("[shaker]\nengine_low_hz = 300", "engine tone"),
    ("[[platforms]]\nid = 'A'", "needs config"),
    ("[effects]\npatch = 'nowhere.toml'", "nowhere.toml"),
])
def test_configuration_mistakes_are_said_plainly(text: str, message: str, tmp_path: Path) -> None:
    import tomllib

    with pytest.raises(LiveConfigError, match=message):
        parse_live_config(tomllib.loads(text), tmp_path)


def test_two_platforms_in_the_file_and_the_example(tmp_path: Path) -> None:
    from fourdvr_live.config import EXAMPLE

    (tmp_path / "live.toml").write_text(f'''
[[platforms]]
config = "{PROFILE}"
id = "A"
driver = "simulation"
[[platforms]]
config = "{PROFILE}"
id = "B"
driver = "simulation"
link_platform_id = 2
[source]
type = "demo"
''')
    cfg = load_live_config(tmp_path / "live.toml")
    assert [p.platform_id for p in cfg.platforms] == ["A", "B"] and cfg.platforms[1].link_platform_id == 2
    (tmp_path / "twice.toml").write_text((tmp_path / "live.toml").read_text().replace('id = "B"', 'id = "A"'))
    with pytest.raises(LiveConfigError, match="same id"):
        load_live_config(tmp_path / "twice.toml")
    import tomllib

    example = tomllib.loads(EXAMPLE)              # the printed example is valid TOML with known sections only
    example["live"].pop("controller")
    parsed = parse_live_config(example, tmp_path)
    assert parsed.source == {"type": "forza", "port": 5300} and parsed.cueing.gain == 0.7


# --------------------------------------------------------------------------------------------- a programme for a video
@pytest.fixture(scope="module")
def turning() -> tuple[Session, float]:
    """A session of 25 s in which the vehicle turns left and right, and the angle it has turned by."""
    rec = SessionRecorder(100.0, {"source": {"title": "turning", "kind": "car"}})
    for k in range(2500):
        t = k / 100.0
        yaw = 0.4 * 2 * np.pi / 7.0 * np.cos(2 * np.pi * t / 7.0) + 0.1 * 2 * np.pi / 2.3 * np.cos(2 * np.pi * t / 2.3)
        rec.add(LiveSample(accel=(0.5, 15.0 * yaw, 0.0), rates=(0.0, 0.0, yaw), speed=15.0), 1.0, np.zeros(6),
                {"wind": 0.25, "rain": 0.7 if 9.0 < t < 11.0 else 0.0})
    return rec.snapshot(), 0.0


def heading(t: float) -> float:
    return 0.4 * np.sin(2 * np.pi * t / 7.0) + 0.1 * np.sin(2 * np.pi * t / 2.3)


def test_a_video_of_the_session_is_found_in_it_and_the_programme_is_made_for_that_video(turning, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    import cv2

    from fourdvr_extractor.video import file_sha256
    from fourdvr_live.convert import find_offset, fitted
    from live_helpers import texture, turned

    session, _ = turning
    video = tmp_path / "recording.avi"
    tex = texture(5)
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"MJPG"), 30.0, (640, 360))
    for k in range(360):                          # 12 seconds, beginning at second 4 of the session
        writer.write(turned(tex, heading(4.0 + k / 30.0) - heading(4.0)))
    writer.release()
    offset, confidence = find_offset(session, video, seconds=12.0, projection="flat")
    assert offset == pytest.approx(4.0, abs=0.15) and confidence > 0.7
    # a video of something else is not "found"
    other = tmp_path / "other.avi"
    writer = cv2.VideoWriter(str(other), cv2.VideoWriter_fourcc(*"MJPG"), 30.0, (640, 360))
    for k in range(240):
        writer.write(turned(tex, 0.3 * np.sin(2 * np.pi * k / 30.0 / 1.1)))
    writer.release()
    assert find_offset(session, other, seconds=8.0, projection="flat")[1] < 0.4

    out = tmp_path / "recording.motion"
    result = convert_session(session, out, ConvertOptions(profile=PROFILE, programme_id=4, video=video, video_offset_s=offset,
                                                          projection="flat"))
    mf = motionfile.read_motion(out, verify=True)
    assert result.passed and mf.duration_s == pytest.approx(12.0, abs=0.01) and len(mf.samples) == 1201
    assert mf.video_sha256 == file_sha256(video) and mf.header["video"]["filename"] == "recording.avi"
    assert mf.header["video"]["session_offset_s"] == pytest.approx(offset, abs=1e-3)
    doc = read_effects(out)
    assert doc is not None and doc.duration_s == pytest.approx(12.0, abs=0.01) and doc.problems() == []
    rain = next(t for t in doc.tracks if t.type == "rain").cues
    assert len(rain) == 1 and rain[0].t == pytest.approx(9.0 - offset, abs=0.1)          # on the video's clock
    assert doc.bass is None or doc.bass.duration_s == pytest.approx(12.0, abs=0.01)
    # a video that begins before the recording, and one that runs on after it: rest outside
    early = fitted(session, -3.0, 10.0)
    assert early.samples == 1001 and not early.active[:300].any() and early.active[300:].all()
    late = fitted(session, 20.0, 10.0)
    assert late.active[:500].all() and not late.active[500:].any()
    with pytest.raises(ValueError, match="nothing of the session"):
        convert_session(session, None, ConvertOptions(profile=PROFILE, video=video, video_offset_s=500.0))


def test_the_commands_for_a_video_of_the_session(turning, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:  # type: ignore[no-untyped-def]
    import cv2

    from fourdvr_live import cli
    from live_helpers import texture, turned

    session, _ = turning
    saved = session.save(tmp_path / "lap.4dlive")
    video = tmp_path / "lap.avi"
    tex = texture(5)
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"MJPG"), 30.0, (640, 360))
    for k in range(300):                          # 10 seconds, beginning at second 6 of the session
        writer.write(turned(tex, heading(6.0 + k / 30.0) - heading(6.0)))
    writer.release()
    assert cli.main(["align", str(saved), str(video), "--projection", "flat"]) == 0
    found = re.search(r"the video begins at second ([0-9.]+) of the session", capsys.readouterr().out)
    assert found is not None and float(found.group(1)) == pytest.approx(6.0, abs=0.15)
    out = tmp_path / "lap.motion"
    assert cli.main(["convert", str(saved), "--video", str(video), "--projection", "flat", "--platform", str(PROFILE),
                     "--programme-id", "9", "-o", str(out)]) == 0
    said = capsys.readouterr().out
    assert "the video begins at second" in said and "upload this video and this file to programme 9" in said
    mf = motionfile.read_motion(out, verify=True)
    assert mf.duration_s == pytest.approx(10.0, abs=0.01) and mf.header["video"]["session_offset_s"] == pytest.approx(6.0, abs=0.15)
    # by hand, and the things that cannot be combined or found
    assert cli.main(["convert", str(saved), "--video", str(video), "--offset", "2.5", "--projection", "flat",
                     "--platform", str(PROFILE), "-o", str(out)]) == 0
    assert motionfile.read_motion(out, verify=True).header["video"]["session_offset_s"] == pytest.approx(2.5)
    capsys.readouterr()
    assert cli.main(["convert", str(saved), "--video", str(video), "--start", "2", "-o", str(out)]) == 1
    assert "use --offset" in capsys.readouterr().err
    assert cli.main(["convert", str(saved), "--platform", str(PROFILE), "-o", str(tmp_path / "bare.motion")]) == 0
    assert "none: the Control Server accepts a motion file only together with the video" in capsys.readouterr().out
    still = tmp_path / "still.avi"                # a picture that does not turn at all: not found, and said so
    writer = cv2.VideoWriter(str(still), cv2.VideoWriter_fourcc(*"MJPG"), 30.0, (640, 360))
    for _k in range(180):
        writer.write(turned(tex, 0.0))
    writer.release()
    assert cli.main(["align", str(saved), str(still), "--projection", "flat"]) == 1
    assert "not found" in capsys.readouterr().out
    assert cli.main(["convert", str(saved), "--video", str(still), "--projection", "flat", "-o", str(tmp_path / "x.motion")]) == 1
    assert "give it with --offset" in capsys.readouterr().err and not (tmp_path / "x.motion").exists()
