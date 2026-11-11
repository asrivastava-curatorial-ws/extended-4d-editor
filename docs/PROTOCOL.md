# 4D-VR-THEATRE/1 — Control Protocol 1.0.0

This document specifies how the **Control Server**, the **VR Players** (VR-01 … VR-20) and the
**Motion Controller** platforms (PLATFORM-01 … PLATFORM-20) communicate. An identical copy ships in the
Control Server, VR Player and Motion Controller packages. The machine-readable JSON Schema is
`protocol/4d-vr-theatre-protocol-1.0.0.schema.json`. It is generated from the reference implementation
(`fourdvr-server schema`).

| Item | Value |
|---|---|
| Protocol identifier | `4D-VR-THEATRE/1` (sent in every message) |
| Protocol version | `1.0.0` (semantic versioning) |
| Transport | WebSocket text frames, one JSON object per frame, UTF-8 |
| Device endpoint | `ws://<server>:8080/ws/v1/device` (`wss://` when TLS is configured) |
| Operator endpoint | `ws://<server>:8080/ws/v1/operator?token=<session token>` (dashboard, read-only) |
| Content endpoint | `GET http://<server>:8080/content/v1/{video\|motion}/{sha256}` |
| REST API | `http://<server>:8080/api/v1/…` (see API.md in the Control Server package) |
| Clock | one master clock (server), seconds as a float |
| Network | dedicated theatre LAN; no Internet or cloud service is used at run time |

## 1. Versioning and compatibility

* Peers are compatible when the **major** version matches (`1.x.y`). The server rejects a
  `REGISTER` whose `protocol_version` has a different major version (`UNSUPPORTED_PROTOCOL`, close
  code 4400). Devices refuse a server whose challenge carries an incompatible version.
* A minor version may add optional payload fields and new message types. Receivers **ignore unknown
  payload fields**. A peer that receives an unknown message *type* answers `ERROR BAD_MESSAGE`.
* The `protocol` field must be exactly `4D-VR-THEATRE/1`. Messages with another value are rejected.

## 2. Envelope

```json
{"protocol": "4D-VR-THEATRE/1", "type": "PLAY", "id": "4f1c…", "ts": 1762098765.123456,
 "request_id": "9a0e…", "payload": { … }}
```

| Field | Type | Meaning |
|---|---|---|
| `protocol` | string | always `4D-VR-THEATRE/1` |
| `type` | string | message type (section 6) |
| `id` | string (1–64) | unique message id (128-bit random hex in all implementations) |
| `ts` | number | sender clock when sent, in seconds (server: master clock; devices: local monotonic clock). Informational only |
| `request_id` | string or null | id of the operator command that caused this message (audit trail). Devices echo it in `READY` and report the last one in `STATUS` |
| `payload` | object | type-specific body |

Floats are sent with full precision. Master-clock times are about 1.7·10⁹ s and need microseconds.

## 3. Connection, authentication and supervision

```
device                                   server
  | -- REGISTER {device_id, device_type, ...} -->|
  | <-- AUTHENTICATE {stage: challenge, nonce} --|
  | -- AUTHENTICATE {stage: response, mac} ----->|   mac = HMAC-SHA256(device_key, "4D-VR-THEATRE/1|<device_id>|<nonce>")
  | <-- AUTHENTICATE {stage: result, content_token, heartbeat_interval_s, offline_timeout_s, server_time}
  | <-- MANIFEST, then PREPARE (programme active) or SYNC (otherwise)
  | -- TIME_SYNC burst, HEARTBEAT, STATUS ... -->|
```

* The first message must be `REGISTER`, within 10 s of connecting. The challenge must be answered
  within 10 s.
* **Device key.** Each device has its own key, derived on the server from a 256-bit master secret
  (`<data_dir>/secret.key`): `device_key = hex(HMAC-SHA256(secret, "device-key|<device_id>"))[0:40]`.
  Administrators print it with `fourdvr-server device-key VR-03`, or with
  `GET /api/v1/devices/VR-03/key` (ADMIN). It is stored in the device configuration
  (`config.json` on headsets, `[[platforms]] device_key` for motion controllers). Rotating
  `secret.key` invalidates every key.
* `mac` is lowercase hexadecimal. The nonce is 32 hex characters and is never reused.
* After a successful result the device is registered in the server database (auto-registration
  can be disabled). It receives a **content token**, valid for this session only, to download media.
* A second connection with the same `device_id` replaces the first. The old socket is closed with
  code 4409.
* **Supervision.** Devices send `HEARTBEAT` every `heartbeat_interval_s` (default 1 s). The server
  answers each one with `HEARTBEAT {server_time}`. If the server hears nothing from a device for
  `offline_timeout_s` (default 5 s), it marks the device offline and closes the socket with code 4408.
  If a device hears nothing for `offline_timeout_s + heartbeat_interval_s`, it reconnects.
  Reconnection uses exponential back-off of 1, 2, 4, 8, then 10 s, and 15 s after an
  authentication failure.

WebSocket close codes:

| Code | Meaning |
|---|---|
| 4400 | protocol error (malformed handshake, incompatible version) |
| 4401 | authentication failed (wrong key) or operator token invalid |
| 4403 | device disabled by an administrator, or unknown device with auto-registration off |
| 4408 | handshake timeout or heartbeat timeout |
| 4409 | replaced by a newer connection with the same device id |

## 4. Master clock and time synchronisation

The server clock is `wall-clock anchor (taken at start-up) + monotonic elapsed time`. It never
jumps when the operating system adjusts its clock.

Each device estimates the master clock from `TIME_SYNC` exchanges, using NTP-style four
timestamps:

```
device: t0 = local send time     -> TIME_SYNC {t0}
server: t1 = receive time, t2 = send time (master clock) -> TIME_SYNC {t0, t1, t2}
device: t3 = local receive time
offset = ((t1 - t0) + (t2 - t3)) / 2          rtt = (t3 - t0) - (t2 - t1)
```

Devices keep the last 64 samples. The offset estimate is the median of the 30 % of samples with the
lowest round-trip time, which rejects samples delayed by Wi-Fi or by the operating system. A
least-squares fit over those samples gives the drift between the local and master oscillators.
The fit is used once the samples span at least 5 s and is limited to ±500 ppm. The result is
`master_time(local) = local + offset + drift·(local − ref)`. On connection a device sends a burst of 8
requests 50 ms apart, then one every `time_sync_interval_s` (default 2 s). An operator **SYNC ALL**
triggers another burst. The `AUTHENTICATE` result (which carries `server_time`) is used as a first
coarse sample. The estimate survives disconnections, so playback continues on it while offline.

## 5. The shared timeline

There is exactly one programme timeline for the whole theatre. Video and motion use the same one.

| Field | Type | Meaning |
|---|---|---|
| `timeline_id` | int | increases by one with every state change (PLAY, PAUSE, RESUME, STOP, RESTART). Devices ignore commands with a lower id than the newest they have accepted |
| `state` | string | `IDLE`, `PREPARING`, `PLAYING`, `PAUSED`, `STOPPED` |
| `programme_id` | int or null | the programme |
| `epoch` | number or null | master time at which programme position 0 occurs (PLAYING) |
| `position_s` | number | frozen position (PAUSED, STOPPED, PREPARING) |
| `duration_s` | number | programme duration (the video duration) |
| `loop` | bool | restart at the end |
| `start_at` | number or null | master time at which playback (re)starts |
| `server_time` | number | master time when the timeline was sent |

Programme position at master time `T` while `PLAYING`:

```
p = T - epoch
p < 0           -> scheduled, not started yet (cued at position start_at - epoch)
loop            -> p mod duration_s
not loop        -> min(p, duration_s)
```

Commands never mean "do this now". They carry a master-clock instant (`start_at`, `pause_at`,
`resume_at`, `stop_at`) a little in the future (server defaults: start lead 3 s, pause/stop lead
0.5 s/0.1 s, resume lead 1 s), so all 40 devices act at the same moment regardless of network
latency. A device that receives a command late simply joins the timeline at the correct position.

## 6. Messages

### Device → server

| Type | Payload | Notes |
|---|---|---|
| `REGISTER` | `device_id` (pattern `^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$`), `device_type` (`vr_player` \| `motion_controller`), `app_version`, `protocol_version`, `capabilities` {}, `hardware` {} | first message |
| `AUTHENTICATE` | `stage: "response"`, `device_id`, `nonce`, `mac` | answer to the challenge |
| `HEARTBEAT` | `state`, `programme_id` (int\|null), `uptime_s` | every `heartbeat_interval_s` |
| `TIME_SYNC` | `t0` | local monotonic seconds |
| `READY` | `programme_id`, `timeline_id`, `ok` (bool), `detail` (≤ 500 chars), `video_sha256`?, `motion_sha256`? | answer to `PREPARE`. `request_id` is copied from the PREPARE |
| `SYNC` | `reason` | asks the server for the current timeline (reply: `SYNC`) |
| `STATUS` | see section 8 | every 0.5–1 s |
| `ERROR` | `code`, `message`, `fatal` | device-side problem report (shown on the dashboard) |

### Server → device

| Type | Payload | Device behaviour |
|---|---|---|
| `AUTHENTICATE` | challenge: `stage: "challenge"`, `nonce`, `algorithm: "HMAC-SHA256"`, `server_version`, `protocol_version`<br>result: `stage: "result"`, `result: "ok"`, `session_id`, `content_token`, `heartbeat_interval_s`, `offline_timeout_s`, `server_version`, `protocol_version`, `server_time` | handshake |
| `HEARTBEAT` | `server_time` | link supervision |
| `TIME_SYNC` | `t0`, `t1`, `t2` | clock sample |
| `MANIFEST` | `programmes`: [ProgrammeAsset] | every published programme. Devices pre-download and cache the assets and may delete cached files that are no longer listed |
| `PREPARE` | `programme` (ProgrammeAsset), `timeline_id`, `deadline_s` | make the programme ready (download, verify, load), then answer `READY` within `deadline_s` |
| `PLAY` | `programme_id`, `timeline_id`, `start_at`, `position_s` (0), `duration_s`, `loop` | cue at `position_s`, start at `start_at`. `epoch = start_at − position_s` |
| `RESTART` | `programme_id`, `timeline_id`, `start_at`, `duration_s`, `loop` | same as PLAY from position 0 |
| `PAUSE` | `programme_id`, `timeline_id`, `pause_at`, `position_s` | keep playing until `pause_at`, then hold exactly at `position_s` (devices re-align to it) |
| `RESUME` | `programme_id`, `timeline_id`, `resume_at`, `position_s` | cue at `position_s`, continue at `resume_at` (`epoch = resume_at − position_s`) |
| `STOP` | `programme_id` (int\|null), `timeline_id`, `stop_at` | stop at `stop_at`. VR: black picture, audio off. Platform: controlled return to neutral or park |
| `SYNC` | `timeline` (Timeline), `force` (bool), `black_screen` (bool) | authoritative state, sent after connection/READY, on request and on operator **SYNC ALL** (`force: true`: re-align immediately and refresh the clock estimate) |
| `BLACK_SCREEN` | `enabled` | VR players only. Picture black (and audio muted by default) while the programme keeps running on the timeline. The application keeps running |
| `ERROR` | `code`, `message`, `fatal` | `fatal: true` precedes a close |

**ProgrammeAsset**

```json
{"programme_id": 1, "name": "Ocean Ride", "revision": 3, "duration_s": 312.48,
 "projection": "360", "stereo": "mono", "loop": true,
 "video": {"url": "/content/v1/video/<sha256>", "sha256": "<64 hex>", "size": 1834567123,
           "filename": "ocean.mp4", "width": 5760, "height": 2880, "container": "mp4", "frame_rate": 30.0},
 "motion": {"url": "/content/v1/motion/<sha256>", "sha256": "<64 hex>", "size": 412345,
            "filename": "ocean.motion", "sample_rate_hz": 100.0, "duration_s": 312.48}}
```

`projection` is one of `flat`, `180`, `360`. `stereo` is one of `mono`, `sbs`, `ou`. `motion` may be
`null`, in which case the platforms stay neutral. `revision` increases whenever the video or motion
file is replaced. Because a new file has a new checksum, devices treat it as new content.

## 7. Command sequences

**PLAY n** (operator):

1. If a *different* programme is playing or paused, the server broadcasts `STOP` at now+0.1 s and
   waits `transition_gap_s` (3 s). During the gap the platforms return to neutral and the headsets
   go black.
2. The server broadcasts `PREPARE` and waits until every connected device has answered `READY`,
   or until `prepare_timeout_s` (120 s) has passed.
3. Start policy `ready_devices` (default): start with the devices that are ready, and report the
   others as late or failed. Policy `all_ready`: abort unless every device is ready.
4. The server broadcasts `PLAY` with `start_at = now + start_lead_s`. Devices that become ready
   later receive `SYNC` and join at the correct position.

**PAUSE / RESUME / RESTART / STOP** are broadcast immediately with their scheduled instant.
**SYNC ALL** broadcasts the current timeline with `force: true`.
**BLACK SCREEN** toggles or sets the flag on all VR players. The flag is also carried in every
`SYNC`.

**Reconnection.** After a network loss a device keeps playing on its local timeline and clock
estimate. Looping continues. When it reconnects, it authenticates, receives `MANIFEST` and then
`PREPARE` for the running programme. It answers `READY` immediately, because the programme is
already loaded, without restarting playback. It then receives `SYNC` and corrects any drift
smoothly.

**Late join.** A device that connects while a programme is running receives `PREPARE` (or `SYNC`),
loads the programme and starts at the current timeline position.

## 8. STATUS

All devices report:

| Field | Meaning |
|---|---|
| `state` | device state (≤ 32 chars) |
| `programme_id`, `timeline_id` | what the device is following |
| `position_s` | measured programme position (video decoder / motion playback) |
| `expected_position_s` | timeline position from the master clock |
| `drift_s` | `position − expected` (circular on looping programmes); null when not playing |
| `clock_offset_s`, `rtt_s` | clock estimate and round-trip time |
| `last_request_id` | last operator command received |
| `app_version` | device software version |
| `errors` | recent problems (strings) |

VR players add the following fields: `playback_rate`, `resync_count`, `loop_count`, `black_screen`,
`backend` (`compositor`/`builtin`), `video_filename`, `video_sha256`, `cache` {status, entries,
bytes_used, queued, failed, active}, `free_storage_bytes`, `clock_synced`, `xr`, and `reconnects`.
VR player states are `IDLE`, `PREPARING`, `READY`, `SCHEDULED`, `PLAYING`, `PAUSED`, `STOPPED`,
`ENDED` and `ERROR`.

Motion controllers add `platform` {phase, driver, link, faults, actuator lengths, limits use …}.
The server maps their phases to the dashboard states READY, RUNNING, STOPPED, DISCONNECTED and
ERROR.

## 9. Content distribution

`GET /content/v1/video/<sha256>` and `GET /content/v1/motion/<sha256>`:

* Authentication: `Authorization: Bearer <content_token>` (or `?token=`). Staff tokens with role
  TECHNICIAN or higher are also accepted. Anything else gets 401.
* `Range: bytes=<from>-` is supported (206 Partial Content), so devices resume interrupted
  downloads. Responses carry `ETag: "<sha256>"` and `X-Content-SHA256`.
* Devices **must** verify the SHA-256 of every downloaded file against the manifest before use, and
  check free storage first. They store files by checksum, so a new revision never overwrites a file
  that is still in use.

## 10. Error codes

| Code | Meaning |
|---|---|
| `BAD_MESSAGE` | malformed JSON, unknown type, missing or ill-typed fields |
| `UNSUPPORTED_PROTOCOL` | wrong protocol id or incompatible major version |
| `AUTH_FAILED` | wrong HMAC, wrong nonce, unknown device |
| `NOT_AUTHENTICATED` | message before authentication, handshake timeout |
| `DEVICE_DISABLED` | administratively disabled |
| `DUPLICATE_SESSION` | reserved |
| `CONTENT_ERROR` | programme not playable on the device (format, download, deadline) |
| `CHECKSUM_MISMATCH` | downloaded or cached file corrupted |
| `STORAGE_FULL` | not enough free space |
| `PLAYBACK_ERROR` | decoder or player failure |
| `MOTION_FAULT` | motion platform fault (limits, actuator, driver) |
| `LINK_FAULT` | Motion Controller ↔ ESP32 link fault |
| `INTERNAL` | unexpected internal error |

## 11. Example session (VR-03)

```
→ {"protocol":"4D-VR-THEATRE/1","type":"REGISTER","id":"a1","ts":12.5,"request_id":null,
   "payload":{"device_id":"VR-03","device_type":"vr_player","app_version":"1.0.0","protocol_version":"1.0.0",
              "capabilities":{"backends":["builtin","compositor"]},"hardware":{"model":"Quest 3"}}}
← {"type":"AUTHENTICATE","payload":{"stage":"challenge","nonce":"5be0…","algorithm":"HMAC-SHA256",…}}
→ {"type":"AUTHENTICATE","payload":{"stage":"response","device_id":"VR-03","nonce":"5be0…","mac":"91a7…"}}
← {"type":"AUTHENTICATE","payload":{"stage":"result","result":"ok","content_token":"…","server_time":1762098700.25,…}}
← {"type":"MANIFEST","payload":{"programmes":[…]}}
← {"type":"SYNC","payload":{"timeline":{"timeline_id":0,"state":"IDLE",…},"force":false,"black_screen":false}}
→ {"type":"TIME_SYNC","payload":{"t0":12.61}}   ← {"type":"TIME_SYNC","payload":{"t0":12.61,"t1":1762098700.362,"t2":1762098700.3621}}
   … operator presses PLAY 1 …
← {"type":"PREPARE","request_id":"c9…","payload":{"programme":{…},"timeline_id":1,"deadline_s":120}}
→ {"type":"READY","request_id":"c9…","payload":{"programme_id":1,"timeline_id":1,"ok":true,"detail":"","video_sha256":"…"}}
← {"type":"PLAY","request_id":"c9…","payload":{"programme_id":1,"timeline_id":1,"start_at":1762098731.0,
   "position_s":0.0,"duration_s":312.48,"loop":true}}
→ {"type":"STATUS","payload":{"state":"PLAYING","programme_id":1,"position_s":12.004,"expected_position_s":12.0,
   "drift_s":0.004,"clock_offset_s":1762098687.75,"rtt_s":0.003,…}}
```
