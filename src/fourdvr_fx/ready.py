"""Is the venue ready? One report on what a show needs.

    patch          it loads, no address is used twice, the warnings a person should have read
    DMX output     the socket or port opens; Art-Net nodes that answer a poll
    ESP32 boards   every board of the patch answers, how quickly, how many answers were lost, what it
                   has against what the patch expects of it, emergency stop, its error counters - and
                   that it goes dark by itself when the signal stops
    sound          the output of the bass track exists
    Control Server it answers; which headsets and platform controllers are connected, and their state
    programme      what the venue will do with a programme's effects (missing devices, limits)

Nothing here switches a device on. The signal-loss test sends the frame of a venue at rest (every
effect off) for half a second and then nothing, and it is left out while something else is sending.
"""

from __future__ import annotations

import contextlib
import json
import socket
import ssl
import statistics
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .bridge import (
    DEFAULT_PORT,
    NODE_DMX_TIMEOUT_S,
    STATE_DMX_ACTIVE,
    TYPE_INFO,
    BridgeOutput,
    LinkError,
    NodeInfo,
    build_dmx,
    build_hello,
    create_output,
    decode,
)
from .dmx import DMX_CHANNELS
from .patch import Patch, PatchError, load_patch

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"
MARKS = {OK: "ok  ", WARN: "WARN", FAIL: "FAIL", INFO: "    "}
ARTNET_PORT = 6454
SLOW_MS = 20.0          # a board on a cable answers in a few milliseconds; more than this is worth a look
Progress = Callable[[str], None]


@dataclass
class Item:
    status: str
    area: str
    text: str


@dataclass
class Report:
    items: list[Item] = field(default_factory=list)
    when: str = field(default_factory=lambda: datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z").strip())
    title: str = ""

    def add(self, status: str, area: str, text: str) -> None:
        self.items.append(Item(status, area, text))

    def count(self, status: str) -> int:
        return sum(1 for i in self.items if i.status == status)

    @property
    def ready(self) -> bool:
        return self.count(FAIL) == 0

    def verdict(self) -> str:
        fails, warns = self.count(FAIL), self.count(WARN)
        if fails:
            return f"NOT READY: {fails} problem(s)" + (f", {warns} warning(s)" if warns else "")
        return "READY" + (f" - with {warns} warning(s) to read" if warns else "")

    def lines(self) -> list[str]:
        out = [f"Venue check {self.when}" + (f" - {self.title}" if self.title else ""), ""]
        area = ""
        for item in self.items:
            if item.area != area:
                area = item.area
                out.append(f"{area}")
            out.append(f"  {MARKS[item.status]}  {item.text}")
        out += ["", self.verdict()]
        return out

    def to_dict(self) -> dict[str, Any]:
        return {"when": self.when, "title": self.title, "ready": self.ready, "verdict": self.verdict(),
                "problems": self.count(FAIL), "warnings": self.count(WARN),
                "items": [{"status": i.status, "area": i.area, "text": i.text} for i in self.items]}


# ---------------------------------------------------------------------------------------------- ESP32 boards
@dataclass
class Probe:
    host: str
    info: NodeInfo | None = None
    node_id: int = 0
    sent: int = 0
    rtts_ms: list[float] = field(default_factory=list)

    @property
    def lost(self) -> int:
        return max(0, self.sent - len(self.rtts_ms))

    @property
    def median_ms(self) -> float:
        return statistics.median(self.rtts_ms) if self.rtts_ms else 0.0

    def has(self) -> str:
        if self.info is None:
            return ""
        parts = [{"platform": "a platform", "dmx": f"{self.info.dmx_ports} DMX line(s)", "audio": "a sound output"}[w]
                 for w in self.info.has()]
        return ", ".join(parts) or "nothing to drive"


def local_addresses() -> list[str]:
    found: set[str] = set()
    with contextlib.suppress(OSError):
        found.update(socket.gethostbyname_ex(socket.gethostname())[2])
    with contextlib.suppress(OSError), socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("192.0.2.1", 9))                    # nothing is sent: only the route is looked up
        found.add(s.getsockname()[0])
    return sorted(a for a in found if not a.startswith("127."))


def broadcast_addresses() -> list[str]:
    """Where a question to "every board on this network" goes: the general broadcast address and the
    last address of each network this PC is in (taken as 255 addresses wide, the usual size)."""
    return list(dict.fromkeys(["255.255.255.255", *(a.rsplit(".", 1)[0] + ".255" for a in local_addresses())]))


def _address(host: str) -> str:
    try:
        return socket.gethostbyname(host)
    except OSError:
        return host


def probe_nodes(hosts: list[str], port: int = DEFAULT_PORT, count: int = 20, interval_s: float = 0.05, wait_s: float = 0.4,
                everyone: bool = False) -> dict[str, Probe]:
    """Ask each board `count` times what it has (HELLO) and time every answer (INFO).

    `everyone`: the hosts are broadcast addresses - whoever answers is in the result, by its address."""
    probes: dict[str, Probe] = {} if everyone else {h: Probe(h) for h in hosts}
    by_address = {} if everyone else {_address(h): probes[h] for h in hosts}
    targets = [(h, _address(h)) for h in hosts]
    sent_at: dict[int, float] = {}
    answered: set[tuple[str, int]] = set()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        if everyone:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("", 0))

        def listen(until: float) -> None:
            while (left := until - time.monotonic()) > 0:
                sock.settimeout(max(left, 0.001))
                try:
                    packet, (address, _p) = sock.recvfrom(2048)
                except TimeoutError:
                    return
                except OSError:
                    continue                           # a closed port answered with an error: not a board
                now = time.monotonic()
                try:
                    ptype, node_id, _seq, payload = decode(packet)
                    if ptype != TYPE_INFO:
                        continue
                    info = NodeInfo.unpack(payload)
                except LinkError:
                    continue
                probe = by_address.get(address)
                if probe is None:
                    if not everyone:
                        continue
                    probe = by_address[address] = probes[address] = Probe(address)
                probe.info, probe.node_id = info, node_id
                started = sent_at.get(info.echo_us)
                if started is not None and (address, info.echo_us) not in answered:
                    answered.add((address, info.echo_us))
                    probe.rtts_ms.append((now - started) * 1000.0)

        stamp = int(time.monotonic() * 1e6)
        for k in range(count):
            for host, address in targets:
                stamp += 1
                sent_at[stamp] = time.monotonic()
                with contextlib.suppress(OSError):
                    sock.sendto(build_hello(0, k + 1, stamp), (address, port))
                if not everyone:
                    probes[host].sent += 1
            listen(time.monotonic() + interval_s)
        end = time.monotonic() + wait_s
        while time.monotonic() < end and (everyone or any(p.lost for p in probes.values())):
            listen(min(end, time.monotonic() + 0.05))
    finally:
        sock.close()
    return probes


def find_nodes(port: int = DEFAULT_PORT, seconds: float = 1.5, targets: list[str] | None = None) -> dict[str, Probe]:
    """The ESP32 boards that answer on this network: address -> what it says."""
    rounds = max(2, round(seconds / 0.5))
    return probe_nodes(targets or broadcast_addresses(), port, count=rounds, interval_s=seconds / rounds, wait_s=0.2, everyone=True)


def signal_loss_test(host: str, port: int, frames: dict[int, bytes], node_id: int = 0, send_s: float = 0.5,
                     limit_s: float = 3.0) -> tuple[str, str]:
    """Send a board the frame of a venue at rest, stop, and see that its DMX line goes dark by itself.
    `frames`: DMX port of the board -> that frame. Returns (status, what was seen)."""
    def ask() -> NodeInfo | None:
        return probe_nodes([host], port, count=1, wait_s=0.3)[host].info

    before = ask()
    if before is None:
        return FAIL, "signal loss: the board does not answer"
    if before.state & STATE_DMX_ACTIVE:
        return INFO, "signal loss: something is sending to it now - the test was left out (the counters above say what happened so far)"
    address = _address(host)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        end, k = time.monotonic() + send_s, 0
        while time.monotonic() < end:
            for dmx_port, data in frames.items():
                k += 1
                with contextlib.suppress(OSError):
                    sock.sendto(build_dmx(node_id, k, dmx_port, 1, bytes(data[:DMX_CHANNELS]) or b"\x00"), (address, port))
            time.sleep(0.025)
    finally:
        sock.close()
    stopped = time.monotonic()
    during = ask()
    if during is None or not during.state & STATE_DMX_ACTIVE:
        return WARN, "signal loss: the board did not show its DMX line as active while frames were sent - not tested"
    while time.monotonic() - stopped < limit_s:
        time.sleep(0.1)
        info = ask()
        if info is not None and not info.state & STATE_DMX_ACTIVE:
            took = time.monotonic() - stopped
            counted = "" if info.dmx_timeouts != before.dmx_timeouts else " (its time-out counter did not move)"
            return OK, f"signal loss: its DMX line went dark by itself {took:.1f} s after the frames stopped{counted}"
    return FAIL, (f"signal loss: its DMX line is still active {limit_s:g} s after the frames stopped - "
                  f"it must go dark after {NODE_DMX_TIMEOUT_S:g} s")


# ---------------------------------------------------------------------------------------------- Art-Net
def _ascii(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("ascii", "replace").strip()


def artnet_poll(targets: list[str], port: int = ARTNET_PORT, seconds: float = 1.5, bind_port: int | None = None) -> list[dict[str, Any]]:
    """Art-Net nodes that answer a poll: [{address, name, long_name, ports}]. A node answers to port 6454,
    which is where the poll comes from - OSError if another program on this PC holds that port."""
    poll = b"Art-Net\x00" + bytes([0x00, 0x20, 0, 14, 0, 0])
    found: dict[str, dict[str, Any]] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("", port if bind_port is None else bind_port))
        end = time.monotonic() + seconds
        next_poll = 0.0
        while (now := time.monotonic()) < end:
            if now >= next_poll:
                next_poll = now + 0.5
                for target in targets:
                    with contextlib.suppress(OSError):
                        sock.sendto(poll, (_address(target), port))
            sock.settimeout(max(0.001, min(end, next_poll) - now))
            try:
                packet, (address, _p) = sock.recvfrom(1024)
            except TimeoutError:
                continue
            except OSError:
                continue
            if len(packet) < 108 or packet[:8] != b"Art-Net\x00" or packet[8:10] != b"\x00\x21":
                continue
            found[address] = {"address": address, "name": _ascii(packet[26:44]), "long_name": _ascii(packet[44:108]),
                              "ports": packet[173] if len(packet) > 173 else 0}
    finally:
        sock.close()
    return [found[a] for a in sorted(found)]


# ---------------------------------------------------------------------------------------------- Control Server
class ServerError(Exception):
    pass


def _call(url: str, path: str, token: str = "", body: dict[str, Any] | None = None, timeout_s: float = 4.0,
          insecure: bool = False) -> Any:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url.rstrip("/") + path, data=data, headers=headers, method="POST" if body is not None else "GET")
    context = ssl._create_unverified_context() if insecure and url.lower().startswith("https") else None
    try:
        with urllib.request.urlopen(request, timeout=timeout_s, context=context) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ServerError("the user name or password was not accepted" if path.endswith("/login")
                              else "this account may not read that (a viewer account is enough)") from exc
        raise ServerError(f"answered {exc.code} to {path}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        raise ServerError(f"does not answer ({reason})") from exc


def _some(names: list[str], most: int = 6) -> str:
    return ", ".join(names[:most]) + (f" and {len(names) - most} more" if len(names) > most else "")


def _device_items(report: Report, area: str, what: str, devices: list[dict[str, Any]], wanted: list[str] | None) -> None:
    """`wanted`: the devices this venue cannot do without (platform controllers: the venue's platforms).
    None: every one is wanted and one is needed (headsets - with none, nobody sees the film)."""
    used = [d for d in devices if d.get("enabled", True)]
    here = [d for d in used if d.get("connection") == "CONNECTED"]
    known = {str(d.get("device_id")) for d in devices}
    for name in wanted or []:
        if name not in known:
            report.add(FAIL, area, f"{name} is not known to the server (its {what} never connected, or has another name there)")
    if not used:
        report.add(INFO, area, f"no {what} is expected by the server")
        return
    missing = [str(d.get("device_id")) for d in used if d not in here]
    if wanted is None:
        status = OK if not missing else WARN if here else FAIL
    else:
        status = FAIL if any(m in wanted for m in missing) else WARN if missing else OK
    report.add(status, area, f"{len(here)} of {len(used)} {what}(s) the server expects are connected"
               + (f" - missing: {_some(missing)}" if missing else ""))
    for d in here:
        name, state = str(d.get("device_id")), str(d.get("state") or "")
        errors = [str(e) for e in d.get("errors") or []]
        bad = any(word in state.upper() for word in ("ERROR", "FAULT", "STOP", "FAIL"))
        facts = [state or "connected"]
        for key, unit, limit in (("latency_ms", "ms to the server", 100.0), ("drift_ms", "ms off the timeline", 80.0)):
            value = d.get(key)
            if isinstance(value, (int, float)):
                facts.append(f"{value:.0f} {unit}")
                if abs(value) > limit and not bad:
                    errors.append(f"{key.split('_')[0]} above {limit:g} ms")
        text = f"{name}: {', '.join(facts)}" + (f" - {'; '.join(errors[-3:])}" if errors else "")
        report.add(FAIL if bad else WARN if errors else OK, area, text)


def server_items(report: Report, url: str, user: str = "", password: str = "", insecure: bool = False, timeout_s: float = 4.0,
                 platform_ids: list[str] | None = None) -> None:
    area = "Control Server"
    try:
        health = _call(url, "/api/v1/health", timeout_s=timeout_s, insecure=insecure)
    except ServerError as exc:
        report.add(FAIL, area, f"{url} {exc}")
        return
    state = str(health.get("status", "?"))
    up = float(health.get("uptime_s", 0.0))
    age = f"{up / 3600:.1f} h" if up >= 3600 else f"{up / 60:.0f} min"
    report.add(OK if state == "ok" else FAIL, area, f"{health.get('product', 'Control Server')} {health.get('version', '')} at {url}: "
               f"{state}, running for {age}, {health.get('devices_connected', 0)} device(s) connected")
    if not user:
        report.add(INFO, area, "headsets and platform controllers are listed with a server account (--user, a viewer account will do)")
        return
    try:
        token = str(_call(url, "/api/v1/auth/login", body={"username": user, "password": password}, timeout_s=timeout_s,
                          insecure=insecure)["token"])
        devices = _call(url, "/api/v1/devices", token, timeout_s=timeout_s, insecure=insecure)
        playback = _call(url, "/api/v1/playback/state", token, timeout_s=timeout_s, insecure=insecure)
    except (ServerError, KeyError, TypeError) as exc:
        report.add(FAIL, area, f"{exc}")
        return
    _device_items(report, area, "headset", list(devices.get("vr_players") or []), None)
    _device_items(report, area, "platform controller", list(devices.get("motion_platforms") or []), list(platform_ids or []))
    timeline = playback.get("timeline") or {}
    name = playback.get("programme_name")
    report.add(INFO, area, f"show: {str(timeline.get('state', '?')).lower()}" + (f", programme '{name}'" if name else ""))


# ---------------------------------------------------------------------------------------------- the report
def _sound_items(report: Report, patch: Patch) -> None:
    if not patch.audio.enabled:
        return
    from .audio import list_output_devices, miniaudio_available

    area = "Sound"
    if not miniaudio_available():
        report.add(FAIL, area, "the bass track cannot be played on this PC: the sound library is not installed "
                               "(pip install 'extended-4d-editor[audio]')")
        return
    try:
        names = list_output_devices()
    except Exception as exc:  # noqa: BLE001 - whatever the sound system says
        report.add(FAIL, area, f"the sound outputs cannot be listed: {exc}")
        return
    wanted = patch.audio.device
    if not names:
        report.add(FAIL, area, "this PC has no sound output")
    elif wanted and not any(wanted.lower() in n.lower() for n in names):
        report.add(FAIL, area, f"no sound output is called '{wanted}' (there are: {', '.join(names[:6])})")
    else:
        report.add(OK, area, f"bass track on '{next((n for n in names if wanted.lower() in n.lower()), names[0])}'"
                   if wanted else f"bass track on the default sound output ({len(names)} output(s) found)")


def _board_items(report: Report, patch: Patch, platform_ids: list[str], hosts: dict[str, str] | None, probes: int,
                 signal_loss: bool, sending: bool, progress: Progress) -> None:
    from .engine import EffectsEngine

    area = "ESP32 boards"
    out = create_output(patch, platform_ids, hosts)
    if not isinstance(out, BridgeOutput) or not out.nodes:
        report.add(FAIL, area, "the patch names no ESP32 board ([output.esp32] theatre, theatre_platform, platforms)")
        return
    port = out.udp_port
    progress(f"asking {len(out.nodes)} ESP32 board(s)")
    answers = probe_nodes(list(out.nodes), port, count=probes)
    dark = EffectsEngine(patch, platform_ids).blackout()
    frames: dict[str, dict[int, bytearray]] = {}
    for universe, (host, dmx_port, slots) in out.lines.items():
        data = bytes(dark.get(universe, bytes(DMX_CHANNELS)))[: max(1, min(slots, DMX_CHANNELS))]
        have = frames.setdefault(host, {}).setdefault(dmx_port, bytearray(len(data)))
        if len(have) < len(data):
            have.extend(bytes(len(data) - len(have)))
        for k, value in enumerate(data):
            have[k] = max(have[k], value)
    sound = patch.output.esp32_audio is not None and patch.output.esp32_audio.enabled
    for host, node in out.nodes.items():
        probe = answers[host]
        what = " + ".join(dict.fromkeys(node.lines.values())) or (node.platform or "no DMX line of the patch")
        who = f"{host} ({what})"
        info = probe.info
        if info is None:
            report.add(FAIL, area, f"{who}: no answer on UDP port {port} - its effects stay off")
            continue
        firmware = f"{info.firmware >> 8}.{info.firmware & 0xFF}"
        text = (f"{who}: answers in {probe.median_ms:.1f} ms (longest {max(probe.rtts_ms, default=0.0):.1f}), "
                f"{probe.lost} of {probe.sent} answers lost; has {probe.has()}; firmware {firmware}")
        lost_share = probe.lost / max(probe.sent, 1)
        report.add(FAIL if lost_share >= 0.2 else WARN if probe.lost or probe.median_ms > SLOW_MS else OK, area, text)
        for dmx_port, wired in sorted(node.lines.items()):
            if not info.has_dmx or dmx_port >= info.dmx_ports:
                report.add(FAIL, area, f"{who}: has no DMX line {dmx_port + 1}, where the patch has the devices of {wired}")
        if info.inhibited:
            report.add(FAIL, area, f"{who}: its emergency stop is in - its line is dark and its sound silent")
        if sound and not info.has_audio:
            report.add(INFO, area, f"{who}: no sound output - bass and seat vibration are not played there")
        counters = (f"{info.packets} packets since it started {info.uptime_ms / 60000:.0f} min ago, {info.invalid} invalid, "
                    f"{info.dmx_timeouts} DMX time-out(s), {info.audio_gaps} sound gap(s), {info.audio_underruns} underrun(s)")
        report.add(WARN if info.invalid else INFO, area, f"{who}: {counters}")
        if info.has_dmx and frames.get(host):
            if not signal_loss:
                continue
            if sending:
                report.add(INFO, area, f"{who}: signal loss: the Editor is sending to it (LIVE) - the test was left out")
                continue
            progress(f"signal-loss test of {host}")
            status, seen = signal_loss_test(host, port, {p: bytes(d) for p, d in frames[host].items()}, probe.node_id)
            report.add(status, area, f"{who}: {seen}")


def venue_ready(
    patch: Patch | str | Path,
    platform_ids: list[str],
    *,
    hosts: dict[str, str] | None = None,
    server_url: str = "",
    user: str = "",
    password: str = "",
    insecure: bool = False,
    programme: Any = None,
    signal_loss: bool = True,
    sending: bool = False,
    probes: int = 20,
    progress: Progress | None = None,
) -> Report:
    """The report. `programme`: a .motion / .fx.json path or an EffectsDocument. `sending`: the caller
    itself is sending to the venue's devices now (the signal-loss test is then left out)."""
    say: Progress = progress or (lambda _text: None)
    report = Report()
    area = "Venue patch"
    if not isinstance(patch, Patch):
        report.title = Path(patch).name
        try:
            patch = load_patch(patch)
        except PatchError as exc:
            report.add(FAIL, area, str(exc))
            return report
    else:
        report.title = Path(patch.source).name if patch.source else ""
    problems = patch.problems(platform_ids)
    for text in problems:
        report.add(FAIL, area, text)
    for text in patch.warnings():
        report.add(WARN, area, text)
    instances = patch.instances(platform_ids)
    if not problems:
        report.add(OK if instances else WARN, area, f"{len(instances)} device(s) for {', '.join(patch.effects()) or 'no effect'}, "
                   f"platforms {', '.join(platform_ids)}")
    protocol = patch.output.protocol
    area = "DMX output"
    if problems:
        report.add(INFO, area, "not looked at: the patch has errors")
    elif protocol in ("none", "capture"):
        report.add(WARN if instances else INFO, area, f'the patch sends no DMX (protocol = "{protocol}")')
    elif protocol == "esp32":
        _board_items(report, patch, platform_ids, hosts, probes, signal_loss, sending, say)
    else:
        say(f"opening the {protocol} output")
        output = patch.output.create()
        try:
            output.open()
            output.close()
            report.add(OK, area, {"sacn": "sACN (E1.31): the output opens", "artnet": "Art-Net: the output opens",
                                  "enttec-pro": f"USB DMX on {patch.output.enttec.get('port', '?')}: the port opens"}[protocol])
        except (OSError, ValueError, ImportError) as exc:
            report.add(FAIL, area, f"the {protocol} output cannot be opened: {exc}")
        if protocol == "artnet":
            targets = [str(t) for t in patch.output.artnet.get("targets", ["255.255.255.255"])]
            say("asking for Art-Net nodes")
            try:
                nodes = artnet_poll(targets, int(patch.output.artnet.get("port", ARTNET_PORT)))
                if nodes:
                    report.add(OK, area, f"{len(nodes)} Art-Net node(s) answer: " + ", ".join(
                        f"{n['name'] or n['long_name'] or 'node'} at {n['address']}" for n in nodes[:8]))
                else:
                    report.add(WARN, area, "no Art-Net node answered the poll (a node need not answer one: look at its own display)")
            except OSError as exc:
                report.add(INFO, area, f"Art-Net nodes could not be asked ({exc})")
        elif protocol == "sacn":
            report.add(INFO, area, "sACN receivers cannot be asked whether they listen: look at the receiver's own display")
        report.add(INFO, area, "signal loss: what a DMX node of another make does without a signal is set on the node - "
                               "choose \"blackout\", not \"hold the last look\"")
    if not problems:
        _sound_items(report, patch)
    if server_url:
        say("asking the Control Server")
        server_items(report, server_url, user, password, insecure, platform_ids=platform_ids)
    if programme is not None and not problems:
        _programme_items(report, programme, patch, platform_ids)
    return report


def _programme_items(report: Report, programme: Any, patch: Patch, platform_ids: list[str]) -> None:
    from .embed import read_effects
    from .preflight import preflight

    area = "Programme"
    doc = programme
    if isinstance(programme, (str, Path)):
        try:
            doc = read_effects(programme)
        except (ValueError, OSError) as exc:
            report.add(FAIL, area, f"{programme}: {exc}")
            return
        if doc is None:
            report.add(INFO, area, f"{Path(programme).name}: motion only - no effects to play")
            return
    try:
        result = preflight(doc, patch, platform_ids)
    except ValueError as exc:
        report.add(FAIL, area, str(exc))
        return
    for name in result.unpatched:
        report.add(WARN, area, f"no device for the effect '{name}': its cues are not played at this venue")
    for name in result.disabled:
        report.add(WARN, area, f"the effect '{name}' is switched off in the patch")
    for note in result.notes:
        report.add(WARN, area, note)
    limited = result.limited
    for f in limited[:12]:
        why = ", ".join(f"{k} {v:.1f} s" for k, v in sorted(f.reasons.items(), key=lambda kv: -kv[1]))
        report.add(INFO, area, f"{f.key}: on {f.output_s:.1f} s of the {f.requested_s:.1f} s asked for - held back by its limits ({why})")
    used = sum(1 for f in result.fixtures if f.requested_s > 0)
    report.add(OK, area, f"{used} device(s) play this programme" + (f", {len(limited)} of them shortened by their limits" if limited else ""))
