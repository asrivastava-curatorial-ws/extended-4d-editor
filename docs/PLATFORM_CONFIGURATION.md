# Platform and Controller Configuration

> **Names.** Since version 2.0 the *Motion Controller* and the *Motion Extractor* are parts of one program,
> Extended 4D Editor: "Motion Controller" below is its show service (`fourdvr controller …`), "Motion
> Extractor" its motion extraction (the Editor and `fourdvr extract …`). The former commands
> `fourdvr-motion` and `fourdvr-extract` used in the examples still work unchanged.

Two kinds of TOML files:

* the **controller configuration** (`controller.toml`): server connection, control rate, and the list
  of platforms this process runs;
* one **platform profile** per platform *type* (`platforms/*.toml`): geometry, actuators, limits,
  calibration and behaviour. The same profile file is used by the Motion Extractor to validate
  content, so keep exactly one file per platform type and give it to the content team.

Validate with `fourdvr-motion check-config -c controller.toml` and
`fourdvr-motion check-platform profile.toml` (the latter also prints the reachable workspace).
Every profile has a SHA-256 (`profile_sha256` in the platform STATUS and in `.motion` validation
records) so you can see which profile a platform runs and which one content was checked against.

## 1. Controller configuration

```toml
[controller]
server_url = "ws://192.168.10.2:8080/ws/v1/device"
control_rate_hz = 100
cache_dir = "/var/lib/fourdvr-motion/cache"
status_interval_s = 0.5
time_sync_interval_s = 2.0
server_loss_timeout_s = 10.0
latency_compensation_s = 0.0
log_level = "INFO"
log_format = "text"

[[platforms]]
config = "platforms/my-hexapod.toml"
id = "PLATFORM-01"
link_platform_id = 1
address = "udp://192.168.10.101:47000"
device_key_file = "keys/PLATFORM-01.key"
```

| Key | Default | Meaning |
|---|---|---|
| `server_url` | (required) | Control Server device endpoint, `ws://` or `wss://` |
| `control_rate_hz` | 100 | control loop rate, 50–200 Hz. Independent of video frame rate and `.motion` sample rate |
| `cache_dir` | `./cache` | downloaded `.motion` files (relative paths are relative to this file) |
| `status_interval_s` | 0.5 | STATUS reports to the server |
| `time_sync_interval_s` | 2.0 | clock synchronisation exchanges (a burst of 8 at connection) |
| `server_loss_timeout_s` | 10.0 | without the server the platform keeps following the timeline this long, then returns to neutral |
| `latency_compensation_s` | 0.0 | sample the trajectory this far ahead (0–0.2 s) to cover actuator/drive latency measured during commissioning |
| `log_level`, `log_format` | INFO, text | `text` or `json` (journald) |

`[[platforms]]` (one per platform, 1–20 per process; ids must be unique):

| Key | Meaning |
|---|---|
| `config` | platform profile file (relative to this file) |
| `id` | device id on the server (`PLATFORM-01` …); overrides the profile's `platform.id` |
| `link_platform_id` | platform id in the binary packets (1–250); must equal the bridge's `FDVR_PLATFORM_ID` |
| `address` | bridge address (`udp://host:47000`, `serial:///dev/ttyUSB0?baud=921600`); overrides the profile |
| `driver` | optional override (`servo_electric`, `simulation`) |
| `name` | optional display name |
| `device_key_file` or `device_key` | device key from `fourdvr-server device-key <id>`; prefer a file with mode 600 |

## 2. Platform profile

### `[platform]`

| Key | Meaning |
|---|---|
| `id` | default device id (normally overridden per `[[platforms]]` entry) |
| `name` | description |
| `driver` | `servo_electric` (ESP32 bridge) or `simulation`. **`hydraulic` and `pneumatic` are not implemented and are rejected** |
| `address` | default bridge address |
| `link_platform_id` | 1–250 |
| `link_timeout_ms` | no STATUS from the bridge for this long ⇒ link fault (default 250) |

### `[geometry]` (metres)

| Key | Meaning |
|---|---|
| `base_joints` | 6 × [x, y, z] of the lower joint centres in the base frame (X forward, Y left, Z up, origin on the floor at the centre) |
| `platform_joints` | 6 × [x, y, z] of the upper joint centres in the platform frame; entry *i* belongs to the same actuator as base joint *i* |
| `neutral_position` | platform-frame origin at the neutral pose, in the base frame |
| `neutral_orientation` | [roll, pitch, yaw] of the platform at neutral (normally zeros) |
| `rotation_centre` | [x, y, z] in the platform frame: the point rotations are made about (near the passengers' heads) |

Measure joint centres on the real platform (CALIBRATION.md §2) or generate an ideal symmetric
geometry with `fourdvr-motion geometry …` and then correct it with measurements.

### `[actuators]`

| Key | Meaning |
|---|---|
| `min_length`, `max_length` | pin-to-pin length fully retracted / fully extended (m); one value or a list of six. Stroke = max − min |
| `max_velocity` | m/s per actuator (use the actuator's rating at the payload, with margin) |
| `max_acceleration` | m/s² per actuator |
| `soft_margin` | m kept clear of both mechanical ends (default 0.005) |

### `[limits.position]`, `[limits.velocity]`, `[limits.acceleration]`, `[limits.jerk]`

Hard software limits of the platform motion relative to neutral, per axis
(`surge, sway, heave` in m, m/s, m/s², m/s³; `roll, pitch, yaw` in rad, rad/s, rad/s², rad/s³).
Positions are `[min, max]` with `min < 0 < max`; the others are positive magnitudes. Every axis is
required. The loader checks that each single-axis position limit is reachable within the actuator
stroke. Combined motion beyond the stroke is scaled down at run time.

### `[calibration]`

| Key | Meaning |
|---|---|
| `zero_offsets` | pose [m, rad × 3] added to every command — levels the platform (CALIBRATION.md §4) |
| `actuator_offsets` | per-actuator stroke correction (m) — compensates mounting/encoder offsets |
| `neutral_pose` | pose held when idle and returned to on STOP |
| `park_pose` | boarding position (e.g. heave −0.08 m); must be inside `limits.position` and the stroke |

Mapping to the bridge: `stroke_i = L_i − min_length_i + actuator_offsets_i`, clipped to
`[0, stroke_i]`, sent in micrometres.

### `[motion]`

| Key | Default | Meaning |
|---|---|---|
| `min_blend_s` | 1.0 | shortest transition between motion sources |
| `park_speed_scale` | 0.35 | fraction of the limits used for stop, park and late-join transitions (0.05–1) |
| `pause_behaviour` | `hold` | `hold`: freeze at the paused pose; `neutral`: return to neutral while paused |
| `fault_recovery` | `manual` | `manual`: a fault is cleared only by an operator STOP or SYNC ALL; `auto`: after 2 s of healthy link |

## 3. Example: 20 platforms

```toml
[controller]
server_url = "ws://192.168.10.2:8080/ws/v1/device"
control_rate_hz = 100
cache_dir = "/var/lib/fourdvr-motion/cache"

[[platforms]]
config = "platforms/my-hexapod.toml"
id = "PLATFORM-01"
link_platform_id = 1
address = "udp://192.168.10.101:47000"
device_key_file = "keys/PLATFORM-01.key"

# ... one block per platform, up to:
[[platforms]]
config = "platforms/my-hexapod.toml"
id = "PLATFORM-20"
link_platform_id = 20
address = "udp://192.168.10.120:47000"
device_key_file = "keys/PLATFORM-20.key"
```

Generate the twenty blocks instead of typing them:

```bash
for i in $(seq 1 20); do n=$(printf %02d "$i"); cat <<EOF
[[platforms]]
config = "platforms/my-hexapod.toml"
id = "PLATFORM-$n"
link_platform_id = $i
address = "udp://192.168.10.$((100 + i)):47000"
device_key_file = "keys/PLATFORM-$n.key"

EOF
done | sudo tee -a /etc/fourdvr-motion/controller.toml >/dev/null
```

Several controller processes (e.g. one per row of seats, each with its own configuration file and
systemd unit instance) may share the work; each platform id must be configured in exactly one
process.
