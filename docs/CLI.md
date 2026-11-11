# Command reference

```
fourdvr [FILE]                 the Editor (desktop application); FILE = project (.4dvr), video or .motion
fourdvr extract COMMAND ...    video -> .motion
fourdvr controller COMMAND ... show service and platform tools
fourdvr fx COMMAND ...         effects files, venue patch, live DMX tools
fourdvr version | --version | --help
```

Every part has `--help` (`fourdvr fx --help`, `fourdvr fx play --help`, …). The AppImage is the
`fourdvr` command: `./Extended-4D-Editor-3.0.0-x86_64.AppImage fx info ride.motion`.

Former names: `fourdvr-extract …` = `fourdvr extract …`, `fourdvr-motion …` = `fourdvr controller …`
(also `fourdvr motion …`), `fourdvr-studio` and `fourdvr-extractor-gui` = `fourdvr` (the desktop
launcher is also installed as `extended-4d-editor`).

Exit codes of all commands: `0` success · `1` the check failed (validation, patch errors, strict
preflight, nothing received) · `2` error (unreadable file, invalid configuration, missing device);
the message is printed on stderr as `error: …`.

---

## `fourdvr extract` — motion, vibration and effects from a video

### `info`

```bash
fourdvr extract info ride360.mp4 [--no-hash]
```

Size, frame rate, duration, the guessed VR layout and the SHA-256 of the file.

### `generate`

```bash
fourdvr extract generate VIDEO [-o OUTPUT] [--programme-id N] [--projection flat|180|360]
                         [--stereo mono|sbs|ou] [--hfov DEG] [--rate HZ] [--gain G]
                         [--profile PROFILE.toml] [--settings SETTINGS.json] [--report REPORT.json]
                         [--effects none|suggest] [--no-scenes] [--bass] [--fx EFFECTS]
                         [--allow-violations] [-q]
```

| Option | Default | Meaning |
|---|---|---|
| `-o, --output` | `VIDEO` with `.motion` suffix | output file; the location is checked before the analysis starts |
| `--programme-id` | 1 (or settings file) | Control Server programme number; must match the programme |
| `--projection`, `--stereo` | guessed from the aspect ratio | **set them explicitly** to the programme's layout |
| `--hfov` | 75 | horizontal field of view of flat video (degrees) |
| `--rate` | 100 | motion sample rate, 50–200 Hz |
| `--gain` | 1.0 | global cue gain |
| `--profile` | bundled example profile | the platform profile the result must fit (the file the show service uses) |
| `--settings` | defaults | settings JSON (see `settings`); command-line options override it |
| `--report` | — | also write the validation report as JSON |
| `--effects suggest` | `none` | draft the effect tracks: wind, vibration, air blasts and strobe from the camera motion and the sound; rain, fog, heat, bubbles, snow, water and scent from the scenes in the picture and the sound (USER_GUIDE.md, SCENE_DETECTION.md). What was seen and heard is printed |
| `--no-scenes` | off | with `--effects suggest`: do not look for scenes (a little faster; only the first four kinds are drafted). Without the scene models the result is the same and a note says so |
| `--bass` | off | embed the bass of the video's sound track. A video without sound gets none and a note says so; an installation that cannot read sound at all stops with an error before the analysis |
| `--fx FILE` | — | embed this effects file (`.fx.json`, or a `.motion` file to copy its effects from). Its duration must match the video |
| `--allow-violations` | off | export even if validation fails — **testing only** |
| `-q, --quiet` | off | no progress output |

Runs analysis → generation → processing → validation, prints the validation table and exports only
if validation passes. Without `--effects`, `--bass` and `--fx` the result is a motion-only file with
the same motion samples Motion Extractor 1.1 produced (checked bit for bit on the same analysis).
The scenes are looked for only with `--effects suggest`; the motion is the same with and without.

```
Extended 4D Editor 3.0.0: storm-ride.mp4 -> storm-ride.motion
  3840x1920 30.00 fps 182.40 s, 360/mono, sha256 684571891439e598…
  surge position        84.10 mm  limit   120.00 mm  OK
  …
validation PASSED against profile 'My hexapod'
  scenes: seen rain 41 s, fire 12 s; heard rain 38 s, thunder 9 s (182 pictures looked at)
  classes: vehicle 61%, rapid 22%, handheld 12%, cut 5%; events: 214; impacts: 9
  effects 1.0.0: 182.40 s, 5 track(s)
    wind             Wind                  96 curve point(s), peak 100 %
    vibration        Seat vibration        140 curve point(s), peak 91 %
    air              Air blast             9 cue(s), 2.2 s, peak 100 %
    rain             Rain                  3 cue(s), 41.0 s, peak 95 %
    heat             Heat (fire)           2 cue(s), 12.0 s, peak 100 %
    bass track: 182.40 s mono @ 2000 Hz, low-pass 120 Hz, gain +0.0 dB, 713 KiB PCM
wrote storm-ride.motion (programme 3, 100 Hz, sha256 …) in 331.7 s
```

(The layout of the output; the numbers are an illustration. `fourdvr fx check storm-ride.motion`
then shows what the default limits make of 41 seconds of rain.)

Manual motion and hand-placed cues are made in the Editor; on the command line a hand-made
`.fx.json` can be embedded with `--fx` or afterwards with `fourdvr fx embed`.

### `validate`, `settings`, `profiles`

```bash
fourdvr extract validate programme.motion --profile my-hexapod.toml    # limit check of an existing file
fourdvr extract settings my-settings.json                              # write all settings with their defaults
fourdvr extract profiles                                               # list the bundled platform profiles
```

Batch example:

```bash
for v in videos/*.mp4; do
  fourdvr extract generate "$v" --projection 360 --stereo mono --profile hexapod.toml \
      --effects suggest --bass -o "out/$(basename "${v%.mp4}").motion" -q || echo "FAILED: $v"
done
```

---

## `fourdvr fx` — effects

### Files

```bash
fourdvr fx info FILE [--json]                    # tracks, cues, bass track of a .motion or .fx.json
fourdvr fx extract MOTION [-o OUT.fx.json]       # effects of a .motion file as a standalone file
fourdvr fx embed MOTION EFFECTS [-o OUT] [--force]   # put effects into a .motion file (in place without -o)
fourdvr fx strip MOTION [-o OUT]                 # remove the effects: a motion-only file again
```

`embed` and `strip` copy the motion samples bit-exactly; only the header changes. The file's
checksum changes with it, so upload the new file to the Control Server. `embed` refuses effects
whose duration differs from the motion by more than half a second (`--force` overrides).
`strip` of a file that was motion-only before `embed` gives back the original bytes.

### Venue

```bash
fourdvr fx example-patch [-o patch.toml] [--force] [--esp32]  # the commented example patch (--esp32: through ESP32 boards)
fourdvr fx check-patch patch.toml [--platforms P1,P2,...]     # validate; print the DMX address table
fourdvr fx check FILE [--patch patch.toml] [--platforms ...] [--strict]
fourdvr fx nodes [HOST ...] [--patch patch.toml] [--platforms ...] [--port N] [--seconds S]
```

`check` is the preflight: one complete show run of the programme through the venue's patch on a
simulated clock, with the engine of the show service. It lists per fixture the requested and the
delivered on-time and every place where a limit holds a cue off, effect types without a fixture,
a bass track without `[audio]`, flash rates above the venue's ceiling. `--strict` returns 1 if
anything is limited or unpatched. **Without `--patch`** the programme is checked against the
default limits of each effect type — what the Editor's **3 Check** does while no venue patch is
loaded.

`nodes` asks ESP32 boards on the effects link what they have and prints one line each: node id,
firmware, a platform, DMX lines, audio outputs, and — with `--patch` — which platform's or the
theatre's devices are wired to it. Give addresses, or a patch with `protocol = "esp32"` to ask the
boards it names. Returns 1 if none answers.

```
ESP32 effects nodes (UDP port 47002):
  192.168.10.101   node 1, firmware 1.0: platform, 1 DMX line(s), audio 2 channel(s) - PLATFORM-01 (line 1: theatre + PLATFORM-01)
  192.168.10.102   no answer - PLATFORM-02 (line 1: PLATFORM-02) - switched off, another address, or firmware without effects: its part is left out
```

`--platforms` (default `PLATFORM-01`) names the platforms whose per-platform fixtures exist; use
the ids and order of the controller configuration.

### Live DMX (no Control Server, no motion)

```bash
fourdvr fx play FILE --patch patch.toml [--platforms ...] [--start S] [--end S] [--loop]
                     [--every S] [-q] [--output sacn|artnet|enttec-pro|none]
fourdvr fx fire FIXTURE --patch patch.toml [--platforms ...] [--level 0..1] [--seconds S]
fourdvr fx blackout --patch patch.toml [--platforms ...]
fourdvr fx monitor [--protocol sacn|artnet] [-u UNIVERSE ...] [--bind IP] [--port N]
                   [--no-multicast] [--seconds S]
fourdvr fx devices
```

| Command | Does |
|---|---|
| `play` | plays the effects (and, with `[audio]`, the bass) of a programme to the venue's outputs in real time, all platforms together. One status line per `--every` seconds with the level of each effect type and the fixtures held by a limit. Ctrl+C stops; dark frames are sent on the way out |
| `fire` | switches one fixture on for a moment (at most 30 s); `id@PLATFORM` for a platform fixture. The fixture's limits apply — firing fog twice in a row shows its cool-down |
| `blackout` | sends zero to every universe of the patch |
| `monitor` | prints the DMX that arrives on this PC, on change: `U2 from '4D VR Theatre' seq 17:  1=153 2=255`. Returns 1 if nothing arrived |
| `devices` | lists sound outputs (for `[audio] device`) and serial ports (for `[output.enttec] port`) |

`--output` overrides the patch's protocol for this run (`none`: dry run). With an `esp32` patch,
`play`, `fire` and `blackout` go to the boards the patch names (list them in
`[output.esp32.platforms]`: these tools know no motion links); boards that do not answer are left
out.

`fourdvr fx test-vectors [-o FILE]` writes the test vectors of the ESP32 effects link (the file
`firmware/esp32-actuator-bridge/test/fx_vectors.txt`, which the firmware's host tests read).

---

## `fourdvr controller` — show service

```bash
fourdvr controller run -c controller.toml               # the service (INSTALL.md)
fourdvr controller check-config -c controller.toml      # platforms; with [effects]: the venue's address table
fourdvr controller check-platform platform.toml         # validate a platform profile, print its workspace
fourdvr controller motion-info FILE [--platform P.toml] # summary of a .motion file, including its effects
fourdvr controller play-local FILE --platform P.toml [--rate HZ] [--realtime]
fourdvr controller pose --platform P.toml --pose x,y,z,roll,pitch,yaw [--hold S]     # commissioning (moves hardware)
fourdvr controller geometry --base-radius R --platform-radius R --neutral-length L [...]
fourdvr controller ik --platform P.toml --pose x,y,z,roll,pitch,yaw
fourdvr controller esp32-sim [--port 47000] [--platform-id 1] [--stroke-mm 300]
                             [--dmx-ports 0|1|2] [--audio-channels 0|1|2] [--fx-port 47002]
fourdvr controller test-vectors [-o FILE]
```

`esp32-sim` is a simulated ESP32 board on this PC: the actuator bridge and, with `--dmx-ports` or
`--audio-channels`, an effects node that answers `fourdvr fx nodes` and receives DMX and sound
like the firmware does (ESP32_EFFECTS_LINK.md §4).

`check-config` returns 1 when the effects patch cannot be used and says so — the service itself
would still start and run motion only. With an `esp32` patch it prints which board carries which
devices, taking each platform's board from its motion link. Everything else is unchanged from
Motion Controller 1.0 (PLATFORM_CONFIGURATION.md, CALIBRATION.md, ESP32_PROTOCOL.md).

`play-local` runs a `.motion` file through the complete motion pipeline with a simulated bridge
and prints tracking error, stroke use and limiter activity. Effects are not involved: use
`fourdvr fx check` and `fourdvr fx play` for them.

---

## Environment

| Variable | Effect |
|---|---|
| `QT_QPA_PLATFORM=offscreen` | run the Editor without a display (tests) |
| `FOURDVR_MODELS=DIR` | use the scene models in this folder instead of the bundled ones (SCENE_DETECTION.md §5) |
| `APPIMAGE_EXTRACT_AND_RUN=1` | AppImage: run without FUSE (same as `--appimage-extract-and-run`) |
