# `.motion` File Format 1.0

> **Names.** Since version 2.0 the *Motion Controller* and the *Motion Extractor* are parts of one program,
> Extended 4D Editor: "Motion Controller" below is its show service (`fourdvr controller …`), "Motion
> Extractor" its motion extraction (the Editor and `fourdvr extract …`). The former commands
> `fourdvr-motion` and `fourdvr-extract` used in the examples still work unchanged.

A `.motion` file holds the platform motion of one programme: six channels sampled at a fixed rate on
the programme timeline, plus a JSON header that ties the file to one exact video file. The Motion
Extractor writes it, the Control Server validates it, the Motion Controller (and, for optional motion
compensation, the VR Player) read it. The reference implementation is `motionfile.py`
(`fourdvr_motion/motionfile.py` here; the same code is in the Control Server).

| Item | Value |
|---|---|
| Format name | `4D-VR-THEATRE-MOTION` |
| Format version | 1.0.0 (file prefix carries major 1, minor 0) |
| File extension / media type | `.motion` / `application/x-4dvr-motion` |
| Channels | surge, sway, heave (m) · roll, pitch, yaw (rad) |
| Sample rate | 50–200 Hz (extractor default 100 Hz), independent of video frame rate and control rate |
| Byte order | little-endian |

## 1. Binary layout

| Offset | Size | Content |
|---|---|---|
| 0 | 8 | magic `34 44 56 52 4D 4F 54 00` (`"4DVRMOT\0"`) |
| 8 | 2 | u16 format major version = 1 |
| 10 | 2 | u16 format minor version = 0 |
| 12 | 4 | u32 `H` = header length in bytes |
| 16 | H | UTF-8 JSON header (object), padded with spaces so that `16 + H` is a multiple of 8 |
| 16 + H | rest | sample block: N × 6 float32 little-endian, row-major (`s0.surge, s0.sway, …, s0.yaw, s1.surge, …`), raw or zlib-compressed |

A reader must reject a file whose magic or **major** version differs, whose header is not a JSON
object or lacks a required key, whose (decompressed) sample block is not exactly `N × 24` bytes, whose
SHA-256 of the sample block differs from `data_sha256`, or that contains NaN/infinite samples.
Unknown header keys are ignored (minor versions may add keys).

## 2. Timing

Sample *i* is at programme time `t_i = i / sample_rate_hz`; the first sample is at `t = 0`.
`N = floor(duration_s × sample_rate_hz) + 1` (the last sample is at or just before the end).
At run time the Motion Controller evaluates the track at the shared timeline position with a
shape-preserving cubic (PCHIP) interpolation — no overshoot between samples — and loops it with the
programme.

## 3. Coordinate system and units

Values are **offsets from the platform's neutral pose** (all zeros = neutral):

| Channel | Unit | Positive direction |
|---|---|---|
| surge | m | forward (+X) |
| sway | m | left (+Y) |
| heave | m | up (+Z) |
| roll | rad | right side down (rotation about +X) |
| pitch | rad | nose down (rotation about +Y) |
| yaw | rad | nose left, counter-clockwise seen from above (rotation about +Z) |

Axes follow ISO 8855 (X forward, Y left, Z up). Rotation order is intrinsic Z-Y-X:
`R = Rz(yaw) · Ry(pitch) · Rx(roll)`. Rotations are about the platform's configured rotation centre
(typically near the passenger's head), see STEWART_KINEMATICS.md.

The file describes **perceptual motion cues**, not the camera's real trajectory: it is already
filtered, washed out and scaled to a platform profile by the Motion Extractor. The Motion
Controller still enforces each platform's own limits at run time, whatever the file contains.

## 4. Header

Required keys:

| Key | Type | Meaning |
|---|---|---|
| `format` | string | `"4D-VR-THEATRE-MOTION"` |
| `format_version` | string | `"1.0.0"` |
| `programme_id` | integer | Control Server programme id this file was generated for |
| `video` | object | the source video: `sha256` (of the complete file), `filename`, `duration_s`, `frame_rate`, `width`, `height`, `projection` (`flat`/`180`/`360`), `stereo` (`mono`/`sbs`/`ou`) |
| `duration_s` | number | programme duration (s), normally the video duration |
| `sample_rate_hz` | number | 50–200 |
| `sample_count` | integer | N ≥ 2 |
| `channels` | array | exactly `["surge","sway","heave","roll","pitch","yaw"]` |
| `units` | object | `{"surge":"m", …, "yaw":"rad"}` |
| `coordinate_system` | object | description of §3 (informational) |
| `sample_encoding` | string | `"float32-le"` |
| `compression` | string | `"none"` or `"zlib"` (RFC 1950 zlib stream of the sample block) |
| `data_sha256` | string | SHA-256 (lowercase hex) of the **uncompressed** sample block |
| `generation` | object | how the file was made (generator name/version, creation time, settings, analysis summary, motion classes, detected events, segments, constraint iterations) — informational |

Optional keys written by the extractor: `timestamps` (text description of §2) and `validation`
(`passed`, `profile`, `profile_sha256`, `stroke_utilisation`, `workspace_violations`, `violations[]`,
`disclaimer`) — the result of the numerical check against a platform profile at export time.

**`effects`** (optional, written by 4D VR Theatre Studio 2.0 and by Extended 4D Editor 3.0 and later): the programme's 4D effects
— wind, water, air, fog, strobe, scent, seat vibration, DMX channels and the bass track — as a
`4D-VR-THEATRE-EFFECTS` document ([EFFECTS_FORMAT.md](EFFECTS_FORMAT.md)). A file without this key
is a motion-only programme. The key does not change the format version: readers of `.motion` 1.0
ignore header keys they do not know, so the Control Server 1.0.0 validates, stores and distributes
such files unchanged, and software without effects support plays their motion. A reader that
supports effects and finds a damaged `effects` value ignores the effects and plays the motion.
`generation.manual_edits` records manual motion edits made in the Editor (informational).

Example (abridged):

```json
{"format": "4D-VR-THEATRE-MOTION", "format_version": "1.0.0", "programme_id": 2,
 "video": {"sha256": "3e61dfc4…67aa2", "filename": "programme2_3d_ou.ogv", "duration_s": 8.0,
           "frame_rate": 30.0, "width": 640, "height": 720, "projection": "flat", "stereo": "ou"},
 "duration_s": 8.0, "sample_rate_hz": 100.0, "sample_count": 801,
 "channels": ["surge", "sway", "heave", "roll", "pitch", "yaw"],
 "units": {"surge": "m", "sway": "m", "heave": "m", "roll": "rad", "pitch": "rad", "yaw": "rad"},
 "sample_encoding": "float32-le", "compression": "none", "data_sha256": "4a1822d9…6c7c",
 "generation": {"generator": "Extended-4D-Editor", "generator_version": "3.2.0",
                "created_at": "2026-11-02T17:23:29Z", "segments": [[0.0, 0.21, "rapid"], …], …},
 "validation": {"passed": true, "profile": "Simulated hexapod (testing only)", "workspace_violations": 0,
                "stroke_utilisation": 0.0744, "violations": [],
                "disclaimer": "numerical limit check only; not a passenger-safety certification"}}
```

## 5. Binding to the video

`video.sha256` is the SHA-256 of the exact video file the motion was generated from. The Control
Server compares it with the programme's uploaded video and refuses to publish a programme whose
motion was generated from a *different video version* (re-edit, re-encode, other resolution).
`programme_id` must match the programme, and `duration_s` must be within the server's tolerance
(0.25 s by default) of the video duration.

## 6. Reading and writing (reference implementation)

```python
from fourdvr_motion import motionfile          # or fourdvr_extractor / fourdvr_server
mf = motionfile.read_motion("programme.motion")  # verifies magic, version, header, checksum, finite values
print(mf.programme_id, mf.sample_rate_hz, mf.duration_s, mf.samples.shape)   # samples: (N, 6) float64
header = motionfile.build_header(programme_id=1, duration_s=60.0, sample_rate_hz=100.0,
                                 video={"sha256": "...", "filename": "show.mp4", "duration_s": 60.0},
                                 generation={"generator": "my-tool"})
motionfile.write_motion("out.motion", header, samples, compression="zlib")   # fills count/checksum
```

Inspect a file from the command line: `fourdvr controller motion-info file.motion [--platform profile.toml]`
(motion and effects), `fourdvr fx info file.motion` (effects) or
`fourdvr extract validate file.motion --profile profile.toml`.

## 7. Versioning

Readers accept every 1.x file. A new major version is used only for incompatible changes (layout,
channel set, units); minor versions may add header keys.
