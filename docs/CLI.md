# Command reference

```
fourdvr [FILE]                 the Editor (desktop application); FILE = project (.4dvr), video, .motion
                               or a recorded live session (.4dlive)
fourdvr extract COMMAND ...    video -> .motion
fourdvr controller COMMAND ... show service and platform tools
fourdvr fx COMMAND ...         effects files, venue patch, live DMX tools
fourdvr live COMMAND ...       live mode: motion, effects and bass from a game, a picture or sound
fourdvr stream COMMAND ...     streaming: videos to any device on the network, on one clock
fourdvr version | --version | --help
```

Every part has `--help` (`fourdvr fx --help`, `fourdvr fx play --help`, …). The AppImage is the
`fourdvr` command: `./Extended-4D-Editor-3.2.0-x86_64.AppImage fx info ride.motion`.

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
Extended 4D Editor 3.2.0: storm-ride.mp4 -> storm-ride.motion
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

### Devices: the fixture library, the boards, "is the venue ready?" (new in 3.2)

```bash
fourdvr fx fixtures search [WORD ...] [--category NAME]
fourdvr fx fixtures show FIXTURE [--mode MODE] [--effect TYPE] [--output N] [--max-rate HZ]
fourdvr fx fixtures entry FIXTURE --effect TYPE [--mode MODE] [--output N] [--max-rate HZ] [--id ID]
                          [--universe U] [--address A] [--scope theatre|platform]
                          [--patch patch.toml [--platforms ...]]
fourdvr fx fixtures library [--add FILE.json ...]
fourdvr fx nodes [HOST ...] [--patch patch.toml] [--find] [--port N] [--seconds S]
fourdvr fx ready --patch patch.toml [--platforms ...] [--server URL [--user NAME --password-file FILE] [--insecure]]
                 [--programme FILE] [--no-signal-loss] [--strict] [--json] [-q] [-o FILE]
```

| Command | Does |
|---|---|
| `fixtures search` | lists the devices of the fixture library whose maker and name contain every word, or a category; without anything: the categories. Returns 1 if nothing is found |
| `fixtures show` | the modes and channels of a device; with `--effect`, what each channel would get as a device for that effect |
| `fixtures entry` | the device as a `[[fixtures]]` entry: printed (notes go to the error output, so `>>` and `tee -a` take only the entry), or with `--patch` appended to that file - at the first free address of the universe, with a name of its own - if the patch still loads with it. `--output N`: one output of a dimmer or relay pack. `--max-rate`: the venue's assessed flash-rate limit, if it is above 3 |
| `fixtures library` | how many devices the library holds, from when, and where your own fixture files go; `--add` copies one there (Open Fixture Library JSON) |
| `nodes` | asks ESP32 boards what they have: the hosts given, or those of a patch, or with `--find` every board on this network segment (or at the broadcast addresses given). Returns 1 if none answered |
| `ready` | the venue check: patch, DMX output, ESP32 boards (answers, delay, lost answers, counters, signal loss), sound, the Control Server's headsets and platform controllers, a programme. Returns 0 if READY, 1 if not (`--strict`: also on warnings). The password comes from `--password-file` or `FOURDVR_SERVER_PASSWORD` |

[VENUE.md](VENUE.md) explains the library's rules and every line of the report.

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

---

## `fourdvr live` — motion, effects and bass from a running game, a live picture or sound

The guide is [LIVE.md](LIVE.md). Exit codes as everywhere: `0` success, `1` a check failed or the
configuration or a file cannot be used, `2` a package or device that the command needs is missing.

### `sources`

```bash
fourdvr live sources [--json]
```

Every source with the games it is for, its default UDP port, what to switch on in the game and
where its format is published.

### `example-config`, `check`

```bash
fourdvr live example-config [-o live.toml]
fourdvr live check (-c live.toml | --source TYPE) [options as for run]
```

`check` says what a configuration means — source, platforms, effects output, directions that are
turned, strength — and whether it can run (exit code 1 with `PROBLEM …` if, for example, a source
needs the game's address and none is given).

### `run`

```bash
fourdvr live run (-c live.toml | --source TYPE) [--port N] [--host ADDRESS] [--bind ADDRESS] [--file FILE]
                 [--gain G] [--axis NAME=FACTOR]... [--platform PROFILE.toml]... [--simulate] [--patch PATCH.toml]
                 [--hold] [--no-sound] [--record [FILE.4dlive]] [--window | --no-window] [--seconds S]
                 [--status-interval S]
```

| Option | Meaning |
|---|---|
| `-c, --config` | the configuration (LIVE.md §3); every other option adds to it or overrides it |
| `--source`, `--port`, `--host`, `--bind`, `--file` | the source instead of the configuration's `[source]` |
| `--gain` | strength of the motion, 0–2 |
| `--axis NAME=FACTOR` | direction factor (`surge`, `sway`, `heave`, `roll`, `pitch`, `yaw`); `-1` turns it round |
| `--platform` | a platform profile in addition to the configuration's (may be repeated) |
| `--simulate` | simulation driver for every platform: no hardware is addressed |
| `--patch` | the venue's effects patch |
| `--hold` | start held: nothing moves until released (Space) |
| `--no-sound` | without a patch: no shaker sound on this computer |
| `--record [FILE]` | record the session — to FILE, or with a dated name into the configured (`[live] record`) or current folder |
| `--window` / `--no-window` | the live window (default: on for a picture source in delay mode, otherwise the terminal). With the window the show runs in a second process that the window starts (LIVE.md §8); grabbing the screen (`capture = "screen"`) needs the window |
| `--seconds` | end by itself after this long |

Keys in the terminal: Space hold / release, `+` `-` strength, `e` engage after a fault, `r` record
on / off, `q` end. Ending (also with Ctrl+C) fades to rest and brings the platforms to neutral
before the links are closed. The end report says how many samples the source delivered, how many
packets or values were refused, each platform's last phase and how often its limiter had to work.

### `monitor`

```bash
fourdvr live monitor (-c live.toml | --source TYPE) [--port N] [--host ADDRESS] [--axis NAME=FACTOR]... [--seconds S]
                     [--record [FILE.4dlive]]
```

No platform, no effects: prints what the source delivers in plain words whenever that changes
(`speeding up`, `braking`, `pushed left (left turn)`, `nose turning left`, `lifted` …) with the
numbers behind them. This is the tool for checking a game's directions (LIVE.md §4).

### `sim`

```bash
fourdvr live sim --format FORMAT [--to HOST[:PORT]] [--rate HZ] [--seconds S]
```

Plays the game's part: sends a made-up 40-second drive (pull away, a left bend, a rough stretch, a
right bend, a crest, rain, a collision, braking), again and again, as packets of `open`, `forza`,
`f1`, `dirt`, `pcars2`, `outsim`, `beamng`, `xplane`, `flightgear` — or waits to be asked like the
real thing for `assetto` (handshake on UDP 9996) and `gt7` (heartbeat on UDP 33739).

### `info`, `convert`, `align`

```bash
fourdvr live info SESSION.4dlive [--json]
fourdvr live convert SESSION.4dlive [-o OUT.motion] [--platform PROFILE.toml] [--programme-id N] [--gain G]
                     [--video VIDEO [--offset S] [--projection flat|180|360] [--stereo mono|sbs|ou]]
                     [--kind auto|car|aircraft|ride] [--start S] [--end S] [--no-effects] [--no-bass]
                     [--allow-violations]
fourdvr live align SESSION.4dlive VIDEO [--seconds S] [--projection …] [--stereo …]
```

`convert` makes a programme of a recorded session (LIVE.md §11): motion with the offline cueing,
fitted to and validated for the platform profile (default: the bundled example), the session's
effects as tracks, the shakers' sound as bass track. Exit code 1 if the validation fails (nothing
is written unless `--allow-violations` — testing only). Needs the workstation packages.

`--video` names the video the programme is for — a recording of the same session. The programme
then has the video's length and reference (the Control Server accepts a motion file only with the
video it names), and `--offset` is the second of the session at which the video begins; without
it that second is found by comparing the picture with the session, and the command stops with an
error rather than guess if the match is poor. `--start` / `--end` cannot be combined with
`--video`. `align` only reports that second (exit code 1: not found).

### `sender`

```bash
fourdvr live sender godot|unity|python|flightgear [-o FILE_OR_FOLDER]
```

Prints (or writes) a ready sender of the open live format ([LIVE_FORMAT.md](LIVE_FORMAT.md)) for
Godot 4 (`FourDLiveSender.gd`), Unity (`FourDLiveSender.cs`) or plain Python, or the protocol file
FlightGear needs (`fourdvr.xml`).

### `devices`, `calibrate`

```bash
fourdvr live devices
fourdvr live calibrate (-c live.toml | --source screen ...) [--seconds S]
```

`devices` lists the sound inputs (for `type = "sound"` and `sound_device`; on PulseAudio / PipeWire
the "Monitor of …" entries carry what the computer plays) and the screens (for `capture = "screen"`).

For a picture source: shows a black / white test pattern in a window and measures how long after
each change the captured picture shows it (LIVE.md §9). The pattern must be inside what the source
captures.

## `fourdvr stream` — videos to any device on the network, on one clock

[STREAMING.md](STREAMING.md) is the guide. `serve`, `check`, `probe`, `control` and `cert` need
nothing beyond the smallest installation; `prepare` and `test-video` need the workstation packages.

### `serve`

```bash
fourdvr stream serve VIDEO_OR_FOLDER... [--port 8080] [--bind ADDRESS] [--title TEXT]
                     [--projection flat|180|360] [--stereo mono|sbs|ou]
                     [--play [VIDEO]] [--loop] [--start-lead S] [--key KEY] [--max-viewers N] [--no-library]
                     [--cert FILE --cert-key FILE]
                     [--follow URL --user NAME --password-file FILE] [--quiet]
```

| Option | Meaning |
|---|---|
| `VIDEO_OR_FOLDER` | files, or folders whose videos are offered (a folder's `stream.toml` gives titles, projection, stereo and programme numbers) |
| `--projection`, `--stereo` | what a single file is, if the guess from its name and shape is wrong |
| `--play [VIDEO]`, `--loop` | start the show at once — the first video, or the named one — and play it again and again |
| `--start-lead` | play starts this many seconds in the future, so that every device is ready (default 3) |
| `--key` | the key of the operator's page (or the environment variable `FOURDVR_STREAM_KEY`); without it a new random one at every start |
| `--max-viewers` | more are refused (default 100) |
| `--no-library` | only the show: viewers cannot pick a video for themselves |
| `--cert`, `--cert-key` | https (STREAMING.md §6) |
| `--follow URL`, `--user`, `--password-file` | follow the show of the Control Server at URL with that account (role VIEWER); the password also from `FOURDVR_STREAM_PASSWORD` |

It prints the addresses — for viewers (`/`), for watching alone (`/library`), for the operator
(`/control?key=…`), for any player (`/media/NAME`) — and then a line whenever something happens.
Ctrl+C ends it.

### `check`, `prepare`, `test-video`

```bash
fourdvr stream check VIDEO_OR_FOLDER... [--viewers N] [--network MBIT] [--projection …] [--stereo …] [--json]
fourdvr stream prepare SOURCE [-o OUT.mp4] [--transcode [--height H] [--bitrate MBIT]] [--print-ffmpeg] [--force]
fourdvr stream test-video -o FILE.mp4|FILE.webm [--seconds S] [--size WxH] [--fps N] [--projection flat|360]
                          [--codec h264|vp8|vp9] [--force]
```

`check` reads a video without decoding it: picture size, codecs, length, bitrate, what it is shown
as, whether browsers play it — and what `--viewers` of them ask of a network; with `--network` the
exit code is 1 if that is more than four fifths of it. `prepare` writes an MP4 with its index at
the front, copying picture and sound as they are — or, with `--transcode`, H.264 with AAC sound;
`--print-ffmpeg` prints the `ffmpeg` command that does the same. `test-video` makes a clock with
a bar that crosses the picture once a second and a beep on every second.

### `probe`, `control`, `cert`

```bash
fourdvr stream probe URL [--viewers 50] [--seconds 20] [--bitrate MBIT] [--flat-out] [--insecure] [--json]
fourdvr stream control play|load [VIDEO] | pause | resume | seek | stop | loop on|off | status
                       [--position S] [--loop] [--url http://SERVER:8080] [--key KEY] [--insecure] [--json]
fourdvr stream cert [-o FOLDER] [NAME_OR_ADDRESS...] [--print] [--force]
```

`probe` is that many simulated viewers of one video's address (`…/media/NAME`), each taking it at
the video's pace; exit code 1 if a real player would have stalled (STREAMING.md §2). `control` is
the operator's page as commands (the key also from `FOURDVR_STREAM_KEY`). `cert` makes
`stream.crt` and `stream.key` for this computer's addresses with the `openssl` program (`--print`:
only the command).
