# End-to-end tests of the effects chain

Two scripts rehearse a show on one PC, without theatre hardware: `run_effects_integration.py`
with DMX on the network (sACN), and `run_esp32_integration.py` with DMX and bass through ESP32
boards (below).

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

Result of the release run (Control Server 1.0.0, Ubuntu 24.04 container, boards = `fxnode_sim`):
25 of 25 checks passed.

Physical devices are not part of these tests, and neither is an ESP32. Commission devices as
described in docs/EFFECTS_GUIDE.md §7 and boards as in docs/ESP32_EFFECTS_LINK.md §3.
