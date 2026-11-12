"""What is known about a video without decoding it, the tools around the server, and following the theatre's show."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from fourdvr_stream import cli
from fourdvr_stream.library import Library, LibraryError, MediaItem, describe, guess_format, read_mp4
from fourdvr_stream.media import MediaError, ffmpeg_command, prepare, test_video
from fourdvr_stream.server import PAUSED, PLAYING, STOPPED, StreamServer
from fourdvr_stream.theatre import TheatreFollower

make_test_video = test_video        # (not a test: pytest would collect the name)
del test_video


@pytest.fixture(scope="module")
def clips(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Real videos, two seconds each: H.264 with AAC in MP4, the same with its index at the end, VP8 in WebM (360)."""
    import av

    folder = tmp_path_factory.mktemp("clips")
    made = make_test_video(folder / "clip.mp4", seconds=2.0, width=320, height=180, fps=25)
    assert made["width"] == 320 and (folder / "clip.mp4").stat().st_size == made["size"]
    make_test_video(folder / "ball_360.webm", seconds=2.0, width=360, height=180, fps=25, projection="360")
    with av.open(str(folder / "clip.mp4")) as inp, av.open(str(folder / "index-at-the-end.mp4"), "w", format="mp4") as out:
        streams = {s.index: out.add_stream_from_template(s) for s in inp.streams}
        for packet in inp.demux():
            if packet.dts is not None:
                packet.stream = streams[packet.stream.index]
                out.mux(packet)
    return folder


def test_an_mp4_is_described_from_its_own_boxes(clips: Path) -> None:
    info = read_mp4(clips / "clip.mp4")
    assert info["faststart"] is True and info["video_codec"] == "h264" and info["audio_codec"] == "aac"
    assert (info["width"], info["height"]) == (320, 180) and info["duration_s"] == pytest.approx(2.0, abs=0.1)
    item = describe(clips / "clip.mp4")
    assert item.id == "clip" and item.mime == "video/mp4" and item.projection == "flat" and item.stereo == "mono"
    assert item.browsers()[0] == "yes" and "every browser" in item.browsers()[1] and item.notes == []
    assert item.bitrate == pytest.approx(item.size * 8 / item.duration_s) and item.to_dict()["browsers"] == "yes"
    late = describe(clips / "index-at-the-end.mp4")
    assert late.faststart is False and late.browsers()[0] == "yes" and "index is at the end" in late.notes[0]
    web = describe(clips / "ball_360.webm")                                   # not an MP4: PyAV is asked
    assert web.video_codec == "vp8" and web.audio_codec == "opus" and web.duration_s == pytest.approx(2.0, abs=0.15)
    assert (web.projection, web.stereo) == ("360", "mono") and web.browsers()[0] == "some"
    (clips.parent / "not-a-video.mp4").write_bytes(b"this is text, whatever its name says " * 40)
    with pytest.raises(LibraryError, match="not an MP4"):
        read_mp4(clips.parent / "not-a-video.mp4")
    broken = describe(clips.parent / "not-a-video.mp4")
    assert broken.duration_s == 0.0 and broken.bitrate == 0.0 and broken.notes and broken.browsers()[0] == "some"
    with pytest.raises(LibraryError, match="is not a file"):
        describe(clips / "nothing.mp4")


def test_what_plays_in_a_browser() -> None:
    def item(name: str, video: str, audio: str = "aac", width: int = 1920, height: int = 1080) -> tuple[str, str]:
        return MediaItem("x", Path(name), "x", 1, "video/mp4", video_codec=video, audio_codec=audio, width=width, height=height).browsers()

    assert item("a.mp4", "h264")[0] == "yes"
    assert item("a.mp4", "h264", "")[0] == "yes" and "no sound" in item("a.mp4", "h264", "")[1]
    big = item("a.mp4", "h264", width=5760, height=2880)
    assert big[0] == "some" and "3840-wide" in big[1]
    assert item("a.mp4", "hevc")[0] == "some" and "Firefox" in item("a.mp4", "hevc")[1]
    assert item("a.mov", "prores")[0] == "no" and "VLC" in item("a.mov", "prores")[1]
    assert item("a.mp4", "h264", "ac3")[0] == "some" and "AAC" in item("a.mp4", "h264", "ac3")[1]
    assert item("a.webm", "vp9", "opus")[0] == "some"
    assert item("a.mkv", "h264")[0] == "no" and item("a.avi", "mpeg4")[0] == "no"


def test_projection_and_stereo_are_guessed_from_name_and_shape() -> None:
    assert guess_format(3840, 1920) == ("360", "mono")
    assert guess_format(3840, 3840) == ("360", "ou") and guess_format(1440, 1440) == ("180", "mono")
    assert guess_format(7680, 1920) == ("360", "sbs")
    assert guess_format(1920, 1080) == ("flat", "mono")
    assert guess_format(1920, 1080, "trailer_3dh_sbs") == ("flat", "sbs")
    assert guess_format(3840, 1920, "dive_VR180_LR") == ("180", "sbs")         # the name knows better than the shape
    assert guess_format(3840, 2160, "Coaster 360 TB") == ("360", "ou")
    assert guess_format(3840, 1920, "flat-panorama") == ("flat", "mono")
    assert guess_format(4096, 4096, "ride_360_mono") == ("360", "mono")
    assert guess_format(0, 0, "something") == ("flat", "mono")


def test_a_folder_with_its_settings(clips: Path, tmp_path: Path) -> None:
    library = Library()
    assert [i.id for i in library.add_path(clips)] == ["ball_360.webm", "clip.mp4", "index-at-the-end.mp4"]
    assert library.get("clip.mp4") is not None and library.get("clip") is None
    again = library.add(clips / "clip.mp4")                                    # the same name twice: both stay reachable
    assert again.id == "clip-2.mp4"
    folder = tmp_path / "films"
    folder.mkdir()
    for name in ("ride.mp4", "ride.webm", "talk.mkv"):
        (folder / name).write_bytes(b"x" * 1000)
    (folder / "readme.txt").write_text("no video")
    (folder / "stream.toml").write_text('[video."ride.mp4"]\ntitle = "The big ride"\nprojection = "180"\nstereo = "sbs"\n'
                                        'programme_id = 4\nduration_s = 100\n')
    films = Library()
    films.add_path(folder)
    assert list(films.items) == ["ride.mp4", "ride.webm", "talk.mkv"]
    ride = films.items["ride.mp4"]
    assert (ride.title, ride.projection, ride.stereo, ride.programme_id, ride.duration_s) == ("The big ride", "180", "sbs", 4, 100.0)
    assert ride.bitrate == 80.0 and films.items["ride.webm"].title == "ride.webm"          # same name: told apart
    assert films.for_programme(4) is ride and films.for_programme(9) is None
    assert films.for_programme(9, filename="talk.mkv") is films.items["talk.mkv"]
    from fourdvr_stream.library import file_sha256

    assert films.for_programme(9, sha256=file_sha256(folder / "talk.mkv")) is ride          # (all three hold the same bytes)
    for text, message in (('[video."gone.mp4"]\ntitle = "x"\n', "no such video"), ('[video."ride.mp4"]\ncolour = 1\n', "unknown setting"),
                          ('[video."ride.mp4"]\nprojection = "dome"\n', "projection must be"), ("[videos]\n", "unknown section"),
                          ("video = 3\n", "one table per file"), ("[video\n", "stream.toml")):
        (folder / "stream.toml").write_text(text)
        with pytest.raises(LibraryError, match=message):
            Library().add_path(folder)
    with pytest.raises(LibraryError, match="does not exist"):
        Library().add_path(tmp_path / "nowhere")


def test_prepare_moves_the_index_to_the_front_without_touching_the_picture(clips: Path, tmp_path: Path) -> None:
    import av

    out = tmp_path / "ready.mp4"
    result = prepare(clips / "index-at-the-end.mp4", out)
    assert result["transcoded"] is False and describe(out).faststart is True

    def packets(path: Path) -> list[tuple[int, bytes]]:
        with av.open(str(path)) as container:
            return [(p.stream.index, bytes(p)) for p in container.demux() if p.dts is not None]

    assert packets(out) == packets(clips / "index-at-the-end.mp4")            # the same coded pictures and sound, byte for byte
    # a WebM cannot go into MP4 as it is; re-encoded it can, smaller if asked
    with pytest.raises(MediaError, match="try --transcode"):
        prepare(clips / "ball_360.webm", tmp_path / "ball.mp4")
    assert not (tmp_path / "ball.mp4").exists() and not (tmp_path / "ball.mp4.part").exists()
    result = prepare(clips / "ball_360.webm", tmp_path / "ball.mp4", transcode=True, height=80, bitrate_mbit=0.3)
    ball = describe(tmp_path / "ball.mp4")
    assert (ball.video_codec, ball.audio_codec, ball.width, ball.height) == ("h264", "aac", 160, 80) and ball.faststart is True
    assert ball.duration_s == pytest.approx(2.0, abs=0.2) and ball.browsers()[0] == "yes"
    with pytest.raises(MediaError, match=r"ending in \.mp4"):
        prepare(clips / "clip.mp4", tmp_path / "x.mkv")
    with pytest.raises(MediaError, match="another file"):
        prepare(clips / "clip.mp4", clips / "clip.mp4")
    with pytest.raises(MediaError, match="flat or 360"):
        make_test_video(tmp_path / "t.mp4", projection="180")
    with pytest.raises(MediaError, match=r"\.mp4 or \.webm"):
        make_test_video(tmp_path / "t.avi")
    assert "-c copy -movflags +faststart" in ffmpeg_command(Path("a.mov"), Path("b.mp4"), False)
    command = ffmpeg_command(Path("a.mov"), Path("b.mp4"), True, 1080, 12.0)
    assert "libx264" in command and "scale=-2:1080" in command and "-b:v 12M" in command


# --------------------------------------------------------------------------------------------- the commands
def run(*argv: str) -> int:
    return cli.main(list(argv))


def test_check_says_what_a_network_must_carry(clips: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run("check", str(clips / "clip.mp4"), "--viewers", "50", "--network", "1000") == 0
    out = capsys.readouterr().out
    assert "320 x 180  h264   sound aac" in out and "browsers    yes" in out and "50 viewers take" in out and "of 1000 Mbit/s" in out
    assert run("check", str(clips / "clip.mp4"), "--viewers", "5000", "--network", "1") == 1
    out = capsys.readouterr().out
    assert "more than the network carries" in out and "may have at most" in out
    assert run("check", str(clips), "--json") == 0
    listed = json.loads(capsys.readouterr().out)
    assert [v["id"] for v in listed] == ["ball_360.webm", "clip.mp4", "index-at-the-end.mp4"] and listed[0]["projection"] == "360"
    assert run("check", str(clips / "clip.mp4"), "--projection", "360", "--stereo", "ou") == 0
    assert "shown as    360° over-under" in capsys.readouterr().out
    for argv, message in ((["check", str(clips / "none.mp4")], "does not exist"),
                          (["check", str(clips), "--projection", "360"], "for single files"),
                          (["serve", str(clips / "clip.mp4"), "--cert", "x.crt"], "--cert and --cert-key together"),
                          (["serve", str(clips / "clip.mp4"), "--follow", "http://127.0.0.1:1"], "--follow needs --user"),
                          (["serve", str(clips / "clip.mp4"), "--follow", "http://127.0.0.1:1", "--user", "v"], "password"),
                          (["test-video", "-o", str(clips / "clip.mp4")], "exists"),
                          (["test-video", "-o", str(clips / "new.mp4"), "--size", "big"], "WIDTHxHEIGHT")):
        assert run(*argv) == 1
        assert message in capsys.readouterr().err, argv


def test_prepare_and_cert_commands(clips: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "ready.mp4"
    assert run("prepare", str(clips / "index-at-the-end.mp4"), "-o", str(out), "--quiet") == 0
    said = capsys.readouterr().out
    assert "picture and sound unchanged" in said and "browsers    yes" in said and out.exists()
    assert run("prepare", str(clips / "index-at-the-end.mp4"), "-o", str(out)) == 1 and "exists" in capsys.readouterr().err
    assert run("prepare", str(clips / "ball_360.webm"), "-o", str(tmp_path / "b.mp4"), "--quiet") == 1
    err = capsys.readouterr().err
    assert "try --transcode" in err and "with the ffmpeg program:  ffmpeg -i" in err
    assert run("prepare", "film.mov", "--transcode", "--bitrate", "8", "--print-ffmpeg") == 0
    assert capsys.readouterr().out.startswith('ffmpeg -i "film.mov"')
    assert run("cert", "-o", str(tmp_path), "--print", "192.168.10.5", "show-pc") == 0
    command = capsys.readouterr().out
    assert command.startswith("openssl req -x509") and "subjectAltName=IP:192.168.10.5,DNS:show-pc" in command


def test_serve_and_control_from_the_command_line(clips: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    code: list[int] = []
    thread = threading.Thread(target=lambda: code.append(run("serve", str(clips), "--bind", "127.0.0.1", "--port", str(port), "--key", "k",
                                                             "--play", "clip.mp4", "--loop", "--seconds", "4", "--title", "Hall 2")))
    thread.start()
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if run("control", "status", "--url", url, "--key", "k") == 0:
            break
        time.sleep(0.1)
    started = capsys.readouterr().out             # what the server said when it started
    assert f"viewers       http://127.0.0.1:{port}/" in started and f"operator      http://127.0.0.1:{port}/control?key=k" in started
    assert "playing       clip.mp4, again and again" in started and "plain http" in started
    assert run("control", "status", "--url", url, "--key", "k") == 0
    assert capsys.readouterr().out.startswith("playing  clip.mp4  at 0:0")
    assert run("control", "pause", "--url", url, "--key", "k") == 0 and "paused  clip.mp4" in capsys.readouterr().out
    assert run("control", "play", "ball_360.webm", "--position", "1", "--url", url, "--key", "k") == 0
    assert "playing  ball_360.webm  starts in" in capsys.readouterr().out
    assert run("control", "loop", "off", "--url", url, "--key", "k") == 0
    assert capsys.readouterr().out.startswith("playing  ball_360.webm")
    assert run("control", "status", "--url", url, "--key", "k", "--json") == 0
    status = json.loads(capsys.readouterr().out)
    assert status["timeline"]["video"] == "ball_360.webm" and status["timeline"]["loop"] is False and status["title"] == "Hall 2"
    assert run("control", "play", "nothing.mp4", "--url", url, "--key", "k") == 1 and "there is no video" in capsys.readouterr().err
    assert run("control", "stop", "--url", url, "--key", "wrong") == 1 and "key is missing or wrong" in capsys.readouterr().err
    assert run("probe", f"{url}/media/clip.mp4", "--viewers", "8", "--seconds", "1") == 0
    probed = capsys.readouterr().out
    assert "8 simulated viewers" in probed and "answered      8 of 8" in probed and probed.rstrip().endswith("ok")
    thread.join(10.0)
    assert code == [0]
    assert "requests" in capsys.readouterr().out                             # ... and when it ended
    assert run("control", "status", "--url", url, "--key", "k") == 1 and "does not answer" in capsys.readouterr().err
    with socket.socket() as taken:                # a port that somebody else has
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        assert run("serve", str(clips), "--bind", "127.0.0.1", "--port", str(taken.getsockname()[1])) == 1
        assert "cannot listen" in capsys.readouterr().err


# --------------------------------------------------------------------------------------------- following the theatre
class FakeControlServer:
    """Answers like the Control Server's API, with a clock that is 100 s ahead of this computer's."""

    def __init__(self) -> None:
        self.ahead = 100.0
        self.timeline: dict[str, Any] = {"state": "IDLE", "programme_id": None, "epoch": None, "position_s": 0.0, "duration_s": 0.0,
                                         "loop": True, "start_at": None}
        self.black = False
        self.programmes = [{"programme_id": 7, "name": "The ride", "projection": "360", "stereo": "ou",
                            "video": {"filename": "elsewhere.mp4", "sha256": "", "duration_s": 30.0}}]
        self.logins = 0
        self.token = "token-1"
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                pass

            def answer(self, data: Any, status: int = 200) -> None:
                body = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.logins += 1
                self.answer({"token": outer.token})

            def do_GET(self) -> None:
                if self.path == "/api/v1/time":
                    self.answer({"master_time": outer.now()})
                elif self.headers.get("Authorization") != f"Bearer {outer.token}":
                    self.answer({"detail": "not signed in"}, 401)
                elif self.path == "/api/v1/programmes":
                    self.answer(outer.programmes)
                elif self.path == "/api/v1/playback/state":
                    self.answer({"server_time": outer.now(), "timeline": outer.timeline, "black_screen": outer.black})
                else:
                    self.answer({}, 404)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def now(self) -> float:
        return time.time() + self.ahead

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def theatre(tmp_path: Path) -> Iterator[tuple[FakeControlServer, StreamServer, TheatreFollower]]:
    (tmp_path / "ride.mkv").write_bytes(b"r" * 5000)
    (tmp_path / "stream.toml").write_text('[video."ride.mkv"]\nprogramme_id = 7\n')
    library = Library()
    library.add_path(tmp_path)
    fake = FakeControlServer()
    stream = StreamServer(library, "127.0.0.1", 0)
    follower = TheatreFollower(stream, fake.url, "viewer", "secret", interval_s=0.05, timeout_s=1.0, loss_timeout_s=0.6)
    yield fake, stream, follower
    follower.stop()
    fake.close()
    stream.httpd.server_close()


def test_the_streams_timeline_is_the_theatres(theatre: tuple[FakeControlServer, StreamServer, TheatreFollower]) -> None:
    fake, stream, follower = theatre
    follower.poll()
    assert fake.logins == 1 and follower.offset == pytest.approx(100.0, abs=0.05) and follower.polls == 1
    tl = stream.state()
    assert tl["source"] == "theatre" and tl["state"] == STOPPED and tl["video"] is None
    # PREPARE: the viewers load the video and stand at its start
    fake.timeline = {"state": "PREPARING", "programme_id": 7, "epoch": None, "position_s": 0.0, "duration_s": 30.0, "loop": True,
                     "start_at": None}
    follower.poll()
    tl = stream.state()
    assert tl["video"] == "ride.mkv" and tl["state"] == PAUSED and tl["note"] == "the show is being prepared" and tl["duration_s"] == 30.0
    item = stream.library.items["ride.mkv"]
    assert (item.projection, item.stereo) == ("360", "ou")                    # what the theatre says the video is
    # PLAY, announced three seconds ahead on the theatre's clock: the same instant on ours
    start = fake.now() + 3.0
    fake.timeline = {"state": "PLAYING", "programme_id": 7, "epoch": start, "position_s": 0.0, "duration_s": 30.0, "loop": True,
                     "start_at": start}
    follower.poll()
    tl = stream.state()
    ours = stream.clock.now() + 3.0
    assert tl["state"] == PLAYING and tl["loop"] is True
    assert tl["epoch"] == pytest.approx(ours, abs=0.08) and tl["start_at"] == pytest.approx(tl["epoch"], abs=1e-3)
    seq = tl["seq"]
    for _ in range(5):                                                        # looked at again and again: nothing new to say
        follower.poll()
    assert stream.state()["seq"] == seq
    # PAUSE, the screens blanked, STOP
    fake.timeline = {**fake.timeline, "state": "PAUSED", "position_s": 12.25, "epoch": None, "start_at": None}
    fake.black = True
    follower.poll()
    tl = stream.state()
    assert tl["state"] == PAUSED and tl["position_s"] == 12.25 and tl["black"] is True
    fake.black = False
    fake.timeline = {**fake.timeline, "state": "STOPPED", "position_s": 0.0}
    follower.poll()
    assert stream.state()["state"] == STOPPED and stream.state()["video"] == "ride.mkv" and stream.state()["black"] is False
    # a programme whose video is not here: said, not guessed
    fake.programmes.append({"programme_id": 8, "name": "Deep sea", "projection": "flat", "stereo": "mono",
                            "video": {"filename": "sea.mp4", "sha256": "ab" * 32}})
    fake.timeline = {**fake.timeline, "state": "PLAYING", "programme_id": 8, "epoch": fake.now(), "start_at": None}
    follower.poll()
    tl = stream.state()
    assert tl["state"] == STOPPED and tl["video"] is None and "Deep sea" in tl["note"] and "sea.mp4" in tl["note"]
    # the session ends at the Control Server: signed in again, and on
    fake.token = "token-2"
    with pytest.raises(Exception, match="401"):
        follower.poll()
    follower.poll()
    assert fake.logins == 2 and follower.status()["clock_offset_s"] == pytest.approx(100.0, abs=0.05)


def test_a_control_server_that_goes_away(theatre: tuple[FakeControlServer, StreamServer, TheatreFollower]) -> None:
    fake, stream, follower = theatre
    start = fake.now()
    fake.timeline = {"state": "PLAYING", "programme_id": 7, "epoch": start, "position_s": 0.0, "duration_s": 30.0, "loop": True,
                     "start_at": None}
    follower.start()
    deadline = time.monotonic() + 3.0
    while stream.state()["state"] != PLAYING and time.monotonic() < deadline:
        time.sleep(0.02)
    assert stream.state()["state"] == PLAYING and follower.connected and follower.status()["error"] is None
    fake.close()                                                              # the cable is pulled
    time.sleep(0.4)
    assert stream.state()["state"] == PLAYING                                 # a short silence changes nothing: the show goes on
    deadline = time.monotonic() + 8.0
    while stream.state()["state"] == PLAYING and time.monotonic() < deadline:
        time.sleep(0.05)
    tl = stream.state()
    assert tl["state"] == STOPPED and tl["note"] == "the Control Server does not answer" and not follower.connected
    assert "does not answer" in (follower.status()["error"] or "")
