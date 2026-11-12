# End-to-end tests

Four scripts rehearse on one PC, without theatre hardware: `run_effects_integration.py` a show
with DMX on the network (sACN), `run_esp32_integration.py` one with DMX and bass through ESP32
boards, `run_live_integration.py` a live show from a (simulated) game, and
`run_stream_integration.py` streaming to simulated viewers and real browsers — all below.

## Network DMX: `run_effects_integration.py`

```
Editor export (motion + effects in one .motion)  ─►  Control Server  ─►  show service (2 simulated platforms,
a venue patch)  ─►  real sACN packets on the loopback interface  ─►  a receiver that compares every DMX frame
with what the programme prescribes at that venue
```

It then pauses, resumes and stops through the server, and plays a motion-only programme through
the same service to check that no effect is output.

### Run

Needs this package with the workstation extras (`pip install ".[studio]"`, or `".[dev]"`) and an
installed Control Server (its `fourdvr-server` command).

```bash
python integration/run_effects_integration.py                              # fourdvr-server on the PATH
python integration/run_effects_integration.py --server /opt/fourdvr-server/venv/bin/fourdvr-server
python integration/run_effects_integration.py --report result.json --work /tmp/fx-it   # keep logs and files
```

About 80 seconds. By default it generates its own test programme: a 36-second 360° pan with a
picture flash and bass bursts, extracted with `--effects suggest --bass`, plus hand-placed cues
that exercise every kind of limit (a water cue and a fog cue longer than the example venue
allows, a strobe cue asking for 12 flashes per second, two scent cartridges, air blasts, a wind
curve).

With your own programme (a Editor export that contains effects, and its video):

```bash
python integration/run_effects_integration.py --video ride.mp4 --motion ride.motion \
    --projection 360 --stereo mono --seconds 90
```

### What is checked

| Check | Meaning |
|---|---|
| the Control Server accepts both programmes | its validation (15 checks) passes for the file with effects and for the motion-only file |
| `check-config` accepts the configuration | controller configuration with `[effects]` and the venue patch |
| before the show: frames arrive and are dark | the service sends, and sends zeros |
| both platforms RUNNING; effects state reported | STATUS details on the server carry the `effects` block |
| dark until the timeline starts | nothing during PREPARE and the start lead |
| every fixture: output as prescribed, within 50 ms | each received DMX frame is compared with a simulated run of the same engine: its value must lie within what the programme prescribes during the ±50 ms around the frame's timeline position (±3 DMX steps); at most 1 % of frames may differ |
| refresh rate | as configured in the patch |
| PAUSE / RESUME / STOP | dark, continues at the programme position, dark |
| motion-only programme | platforms RUNNING and not a single non-zero DMX frame |
| log without errors | the show service's log |

The comparison uses the engine itself as the reference, so it tests transport, clock
synchronisation, gating and timing through the real server and network stack — not the engine's
rules, which the unit tests in `tests/fx` cover.

Result of the release runs (Control Server 1.0.0, Ubuntu 24.04 container; from the source
installation and from the unpacked AppImage): 25 of 25 checks passed, 0 frames off the mark on all
ten fixture instances.

## ESP32 boards: `run_esp32_integration.py`

```
Editor export (motion + effects + bass in one .motion)  ─►  Control Server  ─►  show service with three
platforms and a venue patch with protocol "esp32"  ─►  the effects link (UDP)  ─►  simulated boards
that record the DMX frames and the sound they are sent
```

The boards are **the firmware's own code**: the script compiles
`firmware/esp32-actuator-bridge/lib/fourdvr_fx` with the `fxnode_sim` program around it (needs
`g++`) and starts one process per board, each behind a real UDP socket on a loopback address. What
is tested is therefore the code that will run on the ESP32 — its packet handling, DMX frame, sound
buffer and time-outs — but not the board-specific part (network driver, UART, I2S), and no ESP32.
Without a compiler (`--no-compiler`) the Python simulation of the node is used instead.

| Board | Has | Role |
|---|---|---|
| node 1 (127.0.0.1) | a DMX line, two audio outputs | PLATFORM-01's devices, its bass shaker, and the theatre's devices |
| node 2 (127.0.0.2) | a DMX line, no audio output | PLATFORM-02's devices |
| (127.0.0.3) | — | PLATFORM-03 moves, but nothing answers on the effects port of its board |

```bash
python integration/run_esp32_integration.py --server /opt/fourdvr-server/venv/bin/fourdvr-server
python integration/run_esp32_integration.py --report result.json --work /tmp/esp32-it     # keep logs and recordings
```

About 95 seconds. It needs the loopback addresses 127.0.0.2 and 127.0.0.3 (present on Linux).

| Check | Meaning |
|---|---|
| the firmware's effects core builds for this PC and passes its own tests | `test_fx`, 150 checks, with the controller's packets as test vectors |
| each board says what it has | answers to HELLO |
| the Control Server accepts the programme; `check-config` accepts the configuration | a programme with 11 effect tracks and a bass track; each platform's board is taken from its motion link |
| all three platforms RUNNING | also the one whose board has no effects at all |
| the show service reports the boards it found | the missing one is listed as not present, and is no error |
| node 1: the 7 theatre fixtures and the 5 fixtures of PLATFORM-01 as prescribed; node 2: the 5 of PLATFORM-02 | every recorded DMX value is compared with a simulated run of the engine (within 75 ms of the programme): 0 values off the mark |
| no other slot of a line is ever lit; before the show both lines are dark | |
| node 1: the bass shaker plays the programme's bass track, in step with the show | the recorded sound against the programme's bass at the show position: correlation 1.000, 25 ms early — the allowance for the board's output stage |
| node 1: the other channel carries the seat-vibration tone at the track's level | 45 Hz |
| no sound before the show and on the board without audio output | nothing is sent to it, nothing is counted as an error |
| no invalid packet, no gap in the sound, no line time-out | the boards' own counters |
| emergency stop at node 1 | its line dark and its shaker silent within 0.1 s for as long as it lasts; the other board is not touched; afterwards effects and sound return |
| node 2 unplugged | the show goes on, its platform runs, nothing is reported as an error |
| node 2 plugged in again | found and driven again within three seconds |
| PAUSE / RESUME / STOP | dark and silent, continues with the programme, dark and silent |
| show service log without errors | |
| `fourdvr fx ready` while the show service runs (new in 3.2) | both boards answer; the signal-loss test is left out and the lines are not touched (the time-out counters above stay at zero); the three platform controllers are connected at the Control Server; the board that is not there makes the venue NOT READY |
| `fourdvr fx ready` with nobody sending (two boards started again) | each board's DMX line is seen to go dark by itself after the test frames stop: 1.1 s |

Result of the release run (Control Server 1.0.0, Ubuntu 24.04 container, boards = `fxnode_sim`):
27 of 27 checks passed.

Physical devices are not part of these tests, and neither is an ESP32. Commission devices as
described in docs/EFFECTS_GUIDE.md §7 and boards as in docs/ESP32_EFFECTS_LINK.md §3.

## A live show: `run_live_integration.py`

```
"the game": fourdvr live sim --format f1 (F1 25 telemetry, real UDP)  ─►  fourdvr live run (two platforms, a venue
patch, recording)  ─►  two motion boards behind real UDP sockets (the simulated ESP32 with the firmware's link
guard), and real sACN packets to a receiver  ─►  the recorded session  ─►  a programme  ─►  Control Server
```

The game runs for half a minute, is closed, and comes back; the live show then ends by itself.
No Control Server is needed for the show (a live show runs without one); with `--server` a last
step checks that a programme made of the recorded session is accepted.

```bash
python integration/run_live_integration.py                                        # the show and the conversion
python integration/run_live_integration.py --server /opt/fourdvr-server/venv/bin/fourdvr-server
python integration/run_live_integration.py --keep                                 # keep logs, session and programmes
```

About 75 seconds, 95 with the server step. Needs the workstation extras for the conversion.

| Check | Meaning |
|---|---|
| `fourdvr live check` accepts the configuration | source, both platforms, the venue patch |
| before the game runs | both boards are commanded, stand at neutral, no fault |
| each board: a command every control period for the whole show | no gap near the board's time-out; always RUN, never a fault, every target inside the stroke |
| both platforms get the same motion; it stays inside the position limits | the boards' targets, turned back into a pose with the platform's own kinematics |
| the right way round | nose up while the car speeds up, right side down in the left bend, left side down in the right bend, in that order — through the real decoder, cueing, limiter, kinematics and link |
| the game is closed | within 3.5 s the platforms stand at neutral and stay there; wind and every other effect off within 2.5 s |
| the game comes back | motion and wind return |
| wind follows the car's speed; rain from the game's weather, no longer than the fixture's limit; the collision fires the air blast once | DMX as received |
| nothing the source did not ask for is ever lit | fog, strobe, scent, water |
| the live show ends by itself, exit code 0, log without errors; the boards park by themselves | |
| the session was recorded; `fourdvr live convert` makes a programme that passes validation, with effects and a bass track; `fourdvr fx check` accepts it | |
| with `--server`: `convert --video` finds where a video of the session begins | the test makes a 360° video that turns as the recording says the car turned, beginning 7 s into it: found within 0.2 s |
| … the programme has the video's length and reference, the rain where the video shows it | |
| … the Control Server refuses the session programme made without the video, and accepts and publishes the one made for it | its own validation (15 checks) |

Result of the release run (Control Server 1.0.0, Ubuntu 24.04 container; from the source
installation and from the unpacked AppImage): 27 of 27 checks passed.

What a real game sends — above all, which way round its values are — and what real boards do is
not part of this test: docs/LIVE.md §4 and §13.

## Streaming: `run_stream_integration.py`

```
fourdvr stream serve FOLDER  ─►  fifty simulated viewers (fourdvr stream probe)
                             ─►  four browsers (headless Chromium) on the viewer's page, a fifth on the operator's
Control Server  ─►  show service (a simulated platform)          with --server
                └►  fourdvr stream serve FOLDER --follow …  ─►  two more browsers
```

The script uses nothing of the product but its command, so it runs with any Python 3.10 or newer
(`python3`). Its browser part needs Playwright for Python with its Chromium:

```bash
pip install playwright && playwright install chromium
python3 integration/run_stream_integration.py --fourdvr /opt/fourdvr-motion/venv/bin/fourdvr
python3 integration/run_stream_integration.py --fourdvr … --server /opt/fourdvr-server/venv/bin/fourdvr-server
python3 integration/run_stream_integration.py --fourdvr … --no-browser          # server and simulated viewers only
python3 integration/run_stream_integration.py --fourdvr … --keep                # keep videos, logs and screenshots
```

About two minutes, three and a half with the Control Server part. The test videos are made with
`fourdvr stream test-video` (which needs the workstation packages) in the open codecs VP8 and VP9,
because the test browser has no H.264.

| Check | Meaning |
|---|---|
| `test-video` and `check` | three test videos are made; codecs, length, kind and "plays in browsers" are read without decoding |
| pages, list, byte ranges, the operator's key | the server as a browser and a player use it |
| fifty viewers as fast as it comes | every one is served; meanwhile the server answers other requests at once (release runs: 12 to 15 Gbit/s together on the loopback of a two-core machine, other requests within 10 ms) |
| fifty viewers at the video's own pace | none would have stalled |
| play is announced three seconds ahead | every page has the video loaded and stands on its first frame |
| five seconds into the show | all four pages play, each within 80 ms of the server's timeline (what each page shows is compared with the timeline by the page itself, frame by frame) |
| sound | the page that a person joined plays with sound, those that started by themselves without |
| the server knows its viewers | 4 watching, 4 in step, nobody loading |
| pause; a jump while paused | all four on the same frame (within 6 ms) |
| resume; a late viewer; a jump while playing | in step again within five seconds |
| a 360° video, again and again | drawn on its canvas; in step across the turn at its end; dragging looks around |
| orientation and eyes | a phone held upright looks ahead, turned left it looks left, lying flat it looks down; an eye's view is taken from its projection matrix; each eye gets its half of a stereo frame |
| the operator's page | shows the viewers; its Play button starts a video for all |
| watching alone | a viewer picks another video while the show runs; the show's viewers are not disturbed |
| stop; no errors, no stalls | |
| with `--server`: a programme is published | the Control Server accepts the test video with a motion file made for it |
| … a stream server follows the show | it signs in with a viewer's account |
| … PLAY | the stream plays the programme's video at the theatre's position (within 60 ms); browsers on it show that position |
| … the stream's own operator | cannot interfere |
| … PAUSE, RESUME, STOP | followed; after PAUSE the browsers stand on the theatre's frame |

Result of the release runs (Control Server 1.0.0, Ubuntu 24.04 container, Chromium headless; from
the source installation and from the unpacked AppImage): 33 of 33 checks passed.

No phone, tablet, television or headset is part of this test, no browser but Chromium and no real
network: docs/STREAMING.md §8.
