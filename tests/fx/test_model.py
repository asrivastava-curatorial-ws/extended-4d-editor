"""Effects document: evaluation, serialisation, validation, embedding in .motion files."""

from __future__ import annotations

import json
import random

import numpy as np
import pytest

from fourdvr_fx import BassTrack, Cue, EffectsDocument, EffectsError, Track, effect_type, embed
from fourdvr_motion import motionfile


def sample_doc() -> EffectsDocument:
    doc = EffectsDocument(60.0, meta={"title": "test"})
    wind = doc.new_track("wind")
    wind.points = [(0.0, 0.0), (10.0, 0.5), (20.0, 0.5), (30.0, 0.0), (60.0, 0.0)]
    water = doc.new_track("water")
    water.cues = [Cue(5.0, 0.5), Cue(12.0, 1.0, 0.6, fade_in=0.2, fade_out=0.3)]
    strobe = doc.new_track("strobe")
    strobe.cues = [Cue(40.0, 2.0, params={"rate_hz": 8.0})]
    scent = doc.new_track("scent", params={"slot": 2.0})
    scent.cues = [Cue(50.0, 3.0), Cue(55.0, 2.0, params={"slot": 3.0})]
    raw = doc.new_track("dmx")
    raw.universe, raw.channel = 3, 17
    raw.points = [(0.0, 0.0), (60.0, 1.0)]
    return doc


def test_cue_envelope_and_track_levels() -> None:
    c = Cue(12.0, 1.0, 0.6, fade_in=0.2, fade_out=0.3)
    assert c.value_at(11.99) == 0.0 and c.value_at(13.0) == 0.0          # half-open [t, t + d)
    assert c.value_at(12.1) == pytest.approx(0.3) and c.value_at(12.5) == pytest.approx(0.6)
    assert c.value_at(12.85) == pytest.approx(0.3)
    doc = sample_doc()
    wind = doc.track("wind")
    assert wind is not None
    assert wind.value_at(5.0) == pytest.approx(0.25) and wind.value_at(15.0) == pytest.approx(0.5)
    assert wind.value_at(-1.0) == 0.0 and wind.value_at(99.0) == 0.0     # holds the end values
    wind.cues.append(Cue(4.0, 2.0, 0.9))                                  # a cue above the curve wins
    assert wind.value_at(5.0) == pytest.approx(0.9)
    scent = doc.track("scent")
    assert scent is not None
    assert scent.params_at(51.0) == {"slot": 2.0} and scent.params_at(56.0) == {"slot": 3.0}
    assert effect_type("bubbles").label == "Bubbles" and effect_type("wind").editing == "curve"
    assert doc.new_track("wind").id == "wind-2"


def test_compiled_matches_reference_on_random_documents() -> None:
    rng = random.Random(4)
    for _ in range(40):
        doc = EffectsDocument(30.0)
        for k in range(rng.randint(1, 4)):
            tr = doc.new_track(rng.choice(["wind", "water", "fog", "strobe"]))
            times = sorted(rng.uniform(0, 30) for _ in range(rng.randint(0, 8)))
            if times and rng.random() < 0.3:
                times.append(times[-1])                                   # a step (two points at one time)
            tr.points = [(t, rng.random()) for t in times]
            tr.cues = [
                Cue(rng.uniform(0, 28), d := rng.uniform(0.05, 4), rng.random(), rng.uniform(0, d / 2), rng.uniform(0, d / 2),
                    {"rate_hz": float(rng.randint(1, 9))} if rng.random() < 0.5 else {})
                for _ in range(rng.randint(0, 12))
            ]
            tr.sort()
            assert k >= 0
        comp = doc.compile()
        for _ in range(200):
            t = rng.uniform(-1, 31)
            for tr in doc.tracks:
                ct = next(c for c in comp.by_type[tr.type] if c.id == tr.id)
                assert ct.value_at(t) == pytest.approx(tr.value_at(t), abs=1e-12)
                assert ct.params_at(t) == tr.params_at(t)
            for typ in doc.types():
                assert comp.level(typ, t) == pytest.approx(max(tr.value_at(t) for tr in doc.tracks if tr.type == typ))
    doc = sample_doc()
    comp = doc.compile()
    assert comp.level("scent", 51.0, {"slot": 2.0}) == 1.0 and comp.level("scent", 51.0, {"slot": 3.0}) == 0.0
    assert comp.level("scent", 56.0, {"slot": 3.0}) == 1.0 and comp.level("scent", 56.0, {"slot": 2.0}) == 0.0
    assert comp.params("strobe", 40.5) == {"rate_hz": 8.0} and comp.level("bubbles", 1.0) == 0.0
    doc.tracks[0].enabled = False
    assert doc.compile().level("wind", 15.0) == 0.0                       # disabled tracks are not played


def test_round_trip_and_bass_track() -> None:
    doc = sample_doc()
    t = np.arange(0, 60.0, 1 / 2000.0)
    doc.bass = BassTrack.from_float(0.5 * np.sin(2 * np.pi * 40 * t), 2000, cutoff_hz=120.0, source="unit test")
    data = json.loads(json.dumps(doc.to_dict()))
    back = EffectsDocument.from_dict(data)
    assert back.to_dict() == doc.to_dict()
    assert back.bass is not None and np.array_equal(back.bass.samples, doc.bass.samples)
    assert back.bass.duration_s == pytest.approx(60.0) and not back.problems()
    assert len(json.dumps(data["bass"])) < 0.5 * 2 * len(t)               # compressed well below raw size
    copy = doc.copy()
    copy.tracks[1].cues[0].level = 0.1
    assert doc.tracks[1].cues[0].level == 1.0 and copy.bass is doc.bass
    data["bass"]["data"] = data["bass"]["data"][:-8] + "AAAAAAA="
    with pytest.raises(EffectsError):
        EffectsDocument.from_dict(data)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(format="other"),
        lambda d: d.update(version="2.0.0"),
        lambda d: d.update(duration_s=0),
        lambda d: d["tracks"][0].update(id="Bad Id"),
        lambda d: d["tracks"][0].update(points=[[5, 0.2], [1, 0.3]]),
        lambda d: d["tracks"][0].update(points=[[1, 1.5]]),
        lambda d: d["tracks"][1]["cues"][0].update(d=0),
        lambda d: d["tracks"][1]["cues"][1].update(fade_in=0.9, fade_out=0.9),
        lambda d: d["tracks"][1]["cues"][0].update(level=float("nan")),
        lambda d: d["tracks"][2]["cues"][0].update(params={"rate_hz": "fast"}),
        lambda d: d["tracks"][4].update(channel=600),
        lambda d: d["tracks"].append(dict(d["tracks"][0])),
    ],
)
def test_malformed_documents_are_rejected(mutate) -> None:  # type: ignore[no-untyped-def]
    data = sample_doc().to_dict()
    mutate(data)
    with pytest.raises(EffectsError):
        EffectsDocument.from_dict(data)


def test_problems_are_warnings() -> None:
    doc = sample_doc()
    doc.tracks[1].cues.append(Cue(59.8, 1.0))
    dup = doc.new_track("dmx")
    dup.universe, dup.channel = 3, 17
    assert len(doc.problems()) == 2
    assert EffectsDocument.from_dict(doc.to_dict()).problems() == doc.problems()


def _motion(tmp_path, effects: EffectsDocument | None = None):  # type: ignore[no-untyped-def]
    samples = np.random.default_rng(1).normal(0, 0.01, (6001, 6)).astype(np.float32).astype(np.float64)
    header = motionfile.build_header(programme_id=3, duration_s=60.0, sample_rate_hz=100.0,
                                     video={"sha256": "ab" * 32, "filename": "v.mp4", "duration_s": 60.0}, generation={})
    path = tmp_path / "show.motion"
    motionfile.write_motion(path, embed.attach_effects(header, effects), samples)
    return path, samples


def test_embedding_in_motion_file_is_optional_and_lossless(tmp_path) -> None:  # type: ignore[no-untyped-def]
    plain, samples = _motion(tmp_path)
    assert embed.read_effects(plain) is None                              # motion-only programme
    plain_bytes = plain.read_bytes()
    doc = sample_doc()
    sha = embed.embed_into_motion(plain, doc, tmp_path / "fx.motion")
    with_fx = motionfile.read_motion(tmp_path / "fx.motion")              # the unchanged reader accepts it
    assert np.array_equal(with_fx.samples, samples) and with_fx.header["data_sha256"] == motionfile.read_header(plain)["data_sha256"]
    assert len(sha) == 64 and embed.read_effects(tmp_path / "fx.motion").to_dict() == doc.to_dict()
    embed.embed_into_motion(tmp_path / "fx.motion", None, tmp_path / "back.motion")      # removing restores the file
    assert (tmp_path / "back.motion").read_bytes() == plain_bytes
    embed.write_effects(tmp_path / "show.fx.json", doc)
    assert embed.read_effects(tmp_path / "show.fx.json").to_dict() == doc.to_dict()
    (tmp_path / "junk.fx.json").write_text("not json")
    with pytest.raises(EffectsError):
        embed.read_effects(tmp_path / "junk.fx.json")
    empty = EffectsDocument(60.0)
    empty.new_track("wind")
    assert "effects" not in embed.attach_effects({"a": 1}, empty)         # nothing to play: not embedded
    big = EffectsDocument(60.0, bass=BassTrack(8000, np.random.default_rng(0).integers(-30000, 30000, 8000 * 1500).astype("<i2")))
    with pytest.raises(EffectsError, match="too large"):
        embed.attach_effects({}, big)


def test_track_bookkeeping() -> None:
    tr = Track("x", "fog", cues=[Cue(5, 1), Cue(1, 1)], points=[(3.0, 0.1), (1.0, 0.2)])
    tr.sort()
    assert [c.t for c in tr.cues] == [1, 5] and tr.points[0][0] == 1.0 and not tr.is_empty()
    assert Track("y", "fog", points=[(0.0, 0.0), (9.0, 0.0)]).is_empty()


def test_bass_track_is_unpacked_only_as_far_as_declared() -> None:
    """A damaged or hostile bass section must not make a reader allocate more than the header declares."""
    import base64
    import hashlib
    import zlib

    good = BassTrack.from_float(0.5 * np.sin(np.arange(4000) / 9.0), 2000).to_dict()
    assert np.array_equal(BassTrack.from_dict(good).samples, BassTrack.from_dict(json.loads(json.dumps(good))).samples)
    bomb = zlib.compress(bytes(50_000_000), 9)                    # 50 MB of silence in ~50 kB
    for change in (
        {"sample_count": 3999},                                   # more data than declared
        {"sample_count": 4001},                                   # less data than declared
        {"data": base64.b64encode(bomb).decode()},                # expands far beyond the declared size
        {"data": good["data"][:-12]},                             # truncated
        {"data": "not base64 !!"},
        {"sample_count": 2000 * 5 * 3600},                        # longer than any reader accepts
        {"sha256": hashlib.sha256(b"other").hexdigest()},
        {"encoding": "mp3"},
    ):
        with pytest.raises(EffectsError):
            BassTrack.from_dict({**good, **change})
