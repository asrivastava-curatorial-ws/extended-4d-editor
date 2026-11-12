"""The stream server: files with byte ranges, the timeline (one clock), viewers, the operator's commands."""

from __future__ import annotations

import http.client
import json
import shutil
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from fourdvr_stream import probe
from fourdvr_stream.library import Library
from fourdvr_stream.server import PAUSED, PLAYING, STOPPED, ControlError, StreamServer, Timeline, parse_range

KEY = "operator-key"


@pytest.fixture(scope="module")
def files(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Files that only have to be served: their content is a pattern that can be checked byte by byte."""
    folder = tmp_path_factory.mktemp("served")
    (folder / "ride.mkv").write_bytes(bytes(range(256)) * 4096 * 3)          # 3 MiB
    (folder / "short.mkv").write_bytes(b"0123456789" * 100)
    (folder / "stream.toml").write_text('[video."ride.mkv"]\ntitle = "The ride"\nprojection = "360"\nduration_s = 30.0\n'
                                        '[video."short.mkv"]\nduration_s = 0.6\nprogramme_id = 7\n')
    (folder / "notes.txt").write_text("not a video")
    return folder


@pytest.fixture
def server(files: Path) -> Iterator[StreamServer]:
    library = Library()
    library.add_path(files)
    srv = StreamServer(library, "127.0.0.1", 0, KEY, max_viewers=3, start_lead_s=0.4)
    srv.start()
    yield srv
    srv.stop()


def get(server: StreamServer, path: str, headers: dict[str, str] | None = None, method: str = "GET",
        body: Any = None) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    try:
        data = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=data, headers={**(headers or {}), **({"Content-Type": "application/json"} if data else {})})
        response = conn.getresponse()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
    finally:
        conn.close()


def control(server: StreamServer, **body: Any) -> tuple[int, dict[str, Any]]:
    status, _headers, raw = get(server, f"/api/control?key={KEY}", method="POST", body=body)
    return status, json.loads(raw)


def test_byte_ranges() -> None:
    assert parse_range("bytes=0-99", 1000) == (0, 99)
    assert parse_range("bytes=500-", 1000) == (500, 999)
    assert parse_range("bytes=-100", 1000) == (900, 999)
    assert parse_range("bytes=990-2000", 1000) == (990, 999)                  # the end is cut to the file's
    assert parse_range("bytes=0-0, 5-9", 1000) == (0, 0)                      # of a list, the first
    for bad in ("bytes=1000-", "bytes=5-2", "bytes=-0", "bytes=a-b", "items=0-5", "bytes=", "bytes"):
        assert parse_range(bad, 1000) is None, bad
    assert parse_range("bytes=0-", 0) is None


def test_pages_list_and_files(server: StreamServer, files: Path) -> None:
    status, headers, page = get(server, "/")
    assert status == 200 and headers["content-type"].startswith("text/html") and b"<video" in page and b"/api/events" in page
    assert get(server, "/library")[0] == 200
    status, _h, raw = get(server, "/api/library")
    listing = json.loads(raw)
    assert status == 200 and [v["id"] for v in listing["videos"]] == ["ride.mkv", "short.mkv"] and listing["on_demand"] is True
    ride = listing["videos"][0]
    assert ride["title"] == "The ride" and ride["projection"] == "360" and ride["url"] == "/media/ride.mkv" and ride["browsers"] == "no"
    assert ride["bitrate"] == round(3 * 1024 * 1024 * 8 / 30.0)
    whole = (files / "ride.mkv").read_bytes()
    # the whole file, a part, the end, one byte
    status, headers, body = get(server, "/media/ride.mkv")
    assert status == 200 and body == whole and headers["accept-ranges"] == "bytes" and headers["content-type"] == "video/x-matroska"
    status, headers, body = get(server, "/media/ride.mkv", {"Range": "bytes=1000000-2099999"})
    assert status == 206 and body == whole[1000000:2100000] and headers["content-range"] == f"bytes 1000000-2099999/{len(whole)}"
    status, headers, body = get(server, "/media/ride.mkv", {"Range": "bytes=-5"})
    assert status == 206 and body == whole[-5:]
    status, headers, body = get(server, "/media/ride.mkv", {"Range": f"bytes={len(whole)}-"})
    assert status == 416 and headers["content-range"] == f"bytes */{len(whole)}" and body == b""
    status, headers, body = get(server, "/media/ride.mkv", method="HEAD")
    assert status == 200 and body == b"" and int(headers["content-length"]) == len(whole)
    # nothing but the listed videos is served
    for path in ("/media/notes.txt", "/media/../stream.toml", "/media/stream.toml", "/media/", "/etc/passwd", "/media/ride.mkv/../../x"):
        assert get(server, path)[0] == 404, path
    assert server.status()["bytes_sent"] >= len(whole) + 1100000 + 5 and server.status()["media_requests"] == 3


def test_the_operators_page_and_commands_need_the_key(server: StreamServer) -> None:
    assert get(server, "/control")[0] == 403 and get(server, "/control?key=wrong")[0] == 403
    status, _h, page = get(server, f"/control?key={KEY}")
    assert status == 200 and b"/api/control" in page
    assert get(server, "/api/status")[0] == 403 and get(server, f"/api/status?key={KEY}")[0] == 200
    assert get(server, "/api/status", {"X-Stream-Key": KEY})[0] == 200
    assert get(server, "/api/control", method="POST", body={"action": "stop"})[0] == 403
    assert server.state()["state"] == STOPPED


def test_one_timeline_for_everybody(server: StreamServer) -> None:
    now = server.clock.now()
    status, tl = control(server, action="play", video="ride.mkv")
    assert status == 200 and tl["state"] == PLAYING and tl["video"] == "ride.mkv" and tl["duration_s"] == 30.0
    assert tl["start_at"] == pytest.approx(now + 0.4, abs=0.1) and tl["epoch"] == pytest.approx(tl["start_at"])
    assert tl["position_s"] == pytest.approx(-0.4, abs=0.1)                  # negative: it starts in that many seconds
    time.sleep(0.7)
    assert server.state()["position_s"] == pytest.approx(0.3, abs=0.12)
    # pause holds the position; resume goes on from it, a little later
    status, tl = control(server, action="pause")
    held = tl["position_s"]
    assert tl["state"] == PAUSED and held == pytest.approx(0.3, abs=0.15)
    time.sleep(0.2)
    assert server.state()["position_s"] == held
    status, tl = control(server, action="resume")
    assert tl["state"] == PLAYING and tl["start_at"] - tl["epoch"] == pytest.approx(held, abs=1e-3)
    assert tl["start_at"] > tl["server_time"] + 1.0                           # everybody gets ready again
    # seek: while playing, and while paused
    status, tl = control(server, action="seek", position_s=12.5)
    assert tl["state"] == PLAYING and tl["start_at"] - tl["epoch"] == pytest.approx(12.5, abs=1e-3)
    control(server, action="pause")
    status, tl = control(server, action="seek", position_s=20.0)
    assert tl["state"] == PAUSED and tl["position_s"] == 20.0
    # play again from the pause point (no position given), then from the start of another video
    status, tl = control(server, action="play")
    assert tl["start_at"] - tl["epoch"] == pytest.approx(20.0, abs=1e-3)
    status, tl = control(server, action="load", video="short.mkv")
    assert tl["state"] == PAUSED and tl["video"] == "short.mkv" and tl["position_s"] == 0.0 and tl["duration_s"] == 0.6
    status, tl = control(server, action="stop")
    assert tl["state"] == STOPPED and tl["position_s"] == 0.0
    seq = tl["seq"]
    # what is refused, and how it is said
    for body, message in (({"action": "play", "video": "nothing.mp4"}, "there is no video"),
                          ({"action": "seek", "position_s": 99.0}, "beyond the end"),
                          ({"action": "seek"}, "seek needs"), ({"action": "loop"}, "loop needs"),
                          ({"action": "play", "video": "ride.mkv", "position_s": -1}, "must be 0 or more"),
                          ({"action": "play", "video": "ride.mkv", "position_s": "soon"}, "could not convert"),
                          ({"action": "dance"}, "unknown action")):
        status, answer = control(server, **body)
        assert status == 400 and message in answer["error"], (body, answer)
    assert server.state()["seq"] == seq                                       # nothing of it changed anything


def test_a_video_ends_or_goes_round(server: StreamServer) -> None:
    control(server, action="play", video="short.mkv", lead_s=0.0)             # 0.6 s long
    time.sleep(0.3)
    assert server.state()["state"] == PLAYING
    time.sleep(1.0)
    tl = server.state()
    assert tl["state"] == STOPPED and tl["note"] == "the video has ended"
    control(server, action="play", video="short.mkv", lead_s=0.0, loop=True)
    time.sleep(1.4)
    tl = server.state()
    assert tl["state"] == PLAYING and tl["loop"] is True and 0.0 <= tl["position_s"] < 0.6        # in its third round
    position = tl["position_s"]
    _status, tl = control(server, action="loop", loop=False)                   # "not again": this round is played to its end
    assert tl["position_s"] == pytest.approx(position, abs=0.1) and tl["loop"] is False
    time.sleep(1.1)
    assert server.state()["state"] == STOPPED
    line = Timeline(state=PLAYING, epoch=100.0, duration_s=10.0, loop=True)
    assert line.position_at(125.5) == pytest.approx(5.5) and line.position_at(97.0) == -3.0
    line.loop = False
    assert line.position_at(125.5) == 10.0


def events(server: StreamServer, query: str = "") -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    conn.request("GET", "/api/events" + query)
    return conn, conn.getresponse()


def next_event(response: http.client.HTTPResponse) -> dict[str, Any]:
    while True:
        line = response.readline()
        assert line, "the stream of events ended"
        if line.startswith(b"data: "):
            return dict(json.loads(line[6:]))


def test_viewers_are_told_at_once_and_counted(server: StreamServer) -> None:
    conn, response = events(server, "?id=phone-1")
    assert response.status == 200 and response.getheader("Content-Type") == "text/event-stream"
    assert next_event(response)["state"] == STOPPED                           # where things stand, as the first thing
    control(server, action="play", video="ride.mkv")
    told = next_event(response)
    assert told["state"] == PLAYING and told["video"] == "ride.mkv" and told["start_at"] > told["server_time"]
    st = server.status()
    assert st["watching"] == 1 and st["viewers"][0]["id"] == "phone-1" and st["viewers"][0]["kind"] == "show"
    # what the viewer says about itself reaches the operator
    assert get(server, "/api/report", method="POST", body={"id": "phone-1", "drift_ms": -12.5, "playing": True, "stalled": False,
                                                           "stalls": 0, "video": "ride.mkv", "junk": "left out"})[0] == 200
    st = server.status()
    assert st["viewers"][0]["drift_ms"] == -12.5 and "junk" not in st["viewers"][0]
    assert st["in_step"] == 1 and st["worst_drift_ms"] == 12.5 and st["stalled"] == 0
    assert get(server, "/api/report", method="POST", body={"id": "nobody", "drift_ms": 1})[0] == 200
    # anybody on the network can send anything as a report: the operator's page does not stumble over it
    for junk in ({"drift_ms": "<b>far</b>", "playing": "yes", "stalls": -5, "video": "x" * 5000, "view": {"a": 1}},
                 {"drift_ms": 1e300, "stalled": 1, "stalls": 10**12, "position_s": [1, 2]}, {"drift_ms": True, "stalls": 2.5}):
        assert get(server, "/api/report", method="POST", body={"id": "phone-1", **junk})[0] == 200
        st = server.status()
        said = st["viewers"][0]
        assert said.get("drift_ms") is None and st["in_step"] == 0 and st["worst_drift_ms"] is None
        assert (len(said.get("video", "")) <= 80 and "view" not in said) or isinstance(said["view"], str)
        assert said.get("stalls", 0) in (0, 1_000_000) and said.get("playing", False) is False
    assert get(server, "/api/report", method="POST", body={"id": "phone-1", "drift_ms": -12.5, "playing": True})[0] == 200
    for raw in (b"[1, 2]", b"not json", b'"text"'):
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        conn.request("POST", "/api/report", body=raw, headers={"Content-Type": "application/json"})
        assert conn.getresponse().status == 400
        conn.close()
    # two more, then the server is full (it is set to three); the operator is not a viewer
    more = [events(server, f"?id=tablet-{k}") for k in range(2)]
    assert all(r.status == 200 for _c, r in more)
    full_conn, full = events(server, "?id=one-too-many")
    assert full.status == 503 and b"3 viewers" in full.read()
    full_conn.close()
    assert get(server, "/media/ride.mkv", {"Range": "bytes=0-9"})[0] == 206          # (a file is still given to anybody)
    op_conn, op = events(server, "?id=desk&kind=operator")
    assert op.status == 200 and server.status()["watching"] == 3 and server.status()["refused"] == 1
    # a viewer that goes is noticed when it is next told something
    for c, r in ((conn, response), *more, (op_conn, op)):
        r.close()
        c.close()
    deadline = time.monotonic() + 5.0
    while server.status()["viewers"] and time.monotonic() < deadline:
        server.broadcast()
        time.sleep(0.05)
    assert server.status()["viewers"] == []


def test_fifty_at_once_each_gets_its_bytes(server: StreamServer, files: Path) -> None:
    whole = (files / "ride.mkv").read_bytes()
    wrong: list[str] = []

    def viewer(k: int) -> None:
        first = (k * 37_337) % (len(whole) - 600_000)
        status, _h, body = get(server, "/media/ride.mkv", {"Range": f"bytes={first}-{first + 599_999}"})
        if status != 206 or body != whole[first : first + 600_000]:
            wrong.append(f"viewer {k}: {status}, {len(body)} bytes")

    threads = [threading.Thread(target=viewer, args=(k,)) for k in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30.0)
    assert not wrong and server.status()["bytes_sent"] == 50 * 600_000
    # the same with the tool for it: 20 simulated viewers as fast as it comes, then at a video's pace
    url = f"http://127.0.0.1:{server.port}/media/ride.mkv"
    assert probe.head(url) == len(whole)
    result = probe.run(url, viewers=20, seconds=1.0, flat_out=True)
    assert result["answered"] == 20 and not result["errors"] and result["total_bits_per_second"] > 20e6
    paced = probe.run(url, viewers=10, seconds=1.5, bitrate=800_000.0)        # the file is "30 s long": 0.8 Mbit/s
    assert paced["answered"] == 10 and paced["would_have_stalled"] == 0
    assert 0.5e6 < paced["median_bits_per_second"] < 6e6                      # a few seconds ahead, then at its pace
    refused = probe.run(f"http://127.0.0.1:{server.port}/media/none.mkv", viewers=2, seconds=0.5, flat_out=True)
    assert refused["answered"] == 0 and refused["errors"] == ["the server answers 404"]


def test_the_timeline_can_be_set_from_outside_and_then_the_operator_cannot(server: StreamServer) -> None:
    conn, response = events(server, "?id=v")
    next_event(response)
    server.follow("ride.mkv", PLAYING, epoch=1000.0, duration_s=30.0, loop=True, start_at=1000.0)
    told = next_event(response)
    assert told["source"] == "theatre" and told["epoch"] == 1000.0 and told["loop"] is True
    seq = server.state()["seq"]
    server.follow("ride.mkv", PLAYING, epoch=1000.004, duration_s=30.0, loop=True, start_at=1000.0)     # the same, to a few ms
    assert server.state()["seq"] == seq
    server.follow("ride.mkv", PLAYING, epoch=1000.2, duration_s=30.0, loop=True, start_at=1000.0)       # really another instant
    assert server.state()["seq"] == seq + 1 and next_event(response)["epoch"] == 1000.2
    with pytest.raises(ControlError, match="follows the theatre"):
        server.control("pause")
    status, answer = control(server, action="stop")
    assert status == 400 and "Control Server" in answer["error"]
    server.follow(None, STOPPED, black=True, note="x")
    assert next_event(response)["black"] is True
    response.close()
    conn.close()


def test_a_server_that_only_shows_the_show(files: Path) -> None:
    library = Library()
    library.add(files / "short.mkv")
    srv = StreamServer(library, "127.0.0.1", 0, on_demand=False, title="Dome")
    srv.start()
    try:
        assert len(srv.key) >= 10                                             # a key of its own when none is given
        assert get(srv, "/library")[0] == 404 and get(srv, "/")[0] == 200
        listing = json.loads(get(srv, "/api/library")[2])
        assert listing["on_demand"] is False and listing["title"] == "Dome"
        assert srv.urls() == [f"http://127.0.0.1:{srv.port}"]
    finally:
        srv.stop()


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl program")
def test_https_with_a_certificate_of_ones_own(files: Path, tmp_path: Path) -> None:
    from fourdvr_stream.cli import cert_command

    done = subprocess.run(cert_command(tmp_path, ["127.0.0.1", "localhost"]), capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    library = Library()
    library.add(files / "ride.mkv")
    srv = StreamServer(library, "127.0.0.1", 0, KEY, cert=str(tmp_path / "stream.crt"), cert_key=str(tmp_path / "stream.key"))
    srv.start()
    try:
        assert srv.urls()[0].startswith("https://")
        trusting = ssl.create_default_context(cafile=str(tmp_path / "stream.crt"))            # the certificate is its own issuer
        with urllib.request.urlopen(f"https://127.0.0.1:{srv.port}/api/state", context=trusting, timeout=10) as response:
            assert json.loads(response.read())["state"] == STOPPED
        request = urllib.request.Request(f"https://127.0.0.1:{srv.port}/media/ride.mkv", headers={"Range": "bytes=70000-1069999"})
        with urllib.request.urlopen(request, context=trusting, timeout=10) as response:
            assert response.status == 206 and response.read() == (files / "ride.mkv").read_bytes()[70000:1070000]
        with pytest.raises(urllib.error.URLError):                                             # ... and nobody else's
            urllib.request.urlopen(f"https://127.0.0.1:{srv.port}/api/state", timeout=10)
        # somebody who speaks plain http to it gets nothing, and the server goes on
        with pytest.raises((OSError, http.client.HTTPException)):
            get(srv, "/api/state")
        result = probe.run(f"https://127.0.0.1:{srv.port}/media/ride.mkv", viewers=5, seconds=0.8, flat_out=True, insecure=True)
        assert result["answered"] == 5
    finally:
        srv.stop()
