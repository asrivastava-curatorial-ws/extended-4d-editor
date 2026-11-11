# ESP32 firmware 1.1.0: actuator bridge and effects node

One firmware source for the ESP32 boards of a theatre. A board can have any of three jobs,
chosen when the firmware is built:

| Job | Build setting | Link | What it does |
|---|---|---|---|
| **Actuator bridge** | `FDVR_HAS_ACTUATORS 1` (default) | motion link 1.0, UDP 47000 or USB serial | receives the six actuator positions of one platform and drives the servo outputs — unchanged since 1.0.0 |
| **DMX line** | `FDVR_HAS_DMX 1` | effects link "4DFX" 1.0, UDP 47002 | sends the DMX levels it receives on a DMX512 line (RS-485 driver on a UART pin) |
| **Audio output** | `FDVR_HAS_AUDIO 1` | effects link, UDP 47002 | plays the bass / seat-vibration sound it receives through an I2S audio converter into an amplifier |

With only the first job the firmware is the plain actuator bridge and does not open the effects
port. Without it (`FDVR_HAS_ACTUATORS 0`) the board is an effects-only node.

> **State.** The actuator bridge is the code of firmware 1.0.0. The effects part is new: its
> logic (`lib/fourdvr_fx`) is tested on a PC, and `src/main.cpp` passes compile checks against
> stand-in headers that mirror the declarations of ESP-IDF 4.4.7 (also compared with 5.1.4) —
> but **it has not been compiled with the ESP32 tool chain and has not run on a board.** Expect
> to prove pins, DMX timing and the audio output on your hardware:
> `../../docs/ESP32_EFFECTS_LINK.md`, "First power-on of a board".

## Actuator bridge

It implements the binary link protocol 1.0 (`../../docs/ESP32_PROTOCOL.md`): it receives COMMAND
packets (UDP over Ethernet, or COBS-framed USB serial), checks version, platform id, CRC, sequence
(loss/duplicates) and age (stale packets), slew-limits the six stroke targets, drives the outputs,
and returns a STATUS packet for every accepted command. If no valid command arrives for
`FDVR_TIMEOUT_MS` (200 ms) it latches a timeout fault and ramps every actuator to the park
position at `FDVR_PARK_SPEED_UM_S`. A latched fault is cleared only by a FAULT_RESET command.

## Effects node

It implements the effects link (`../../docs/ESP32_EFFECTS_LINK.md` §5): it tells the show service
what it has, keeps a DMX frame that the service updates 40 times a second and sends it on the line
continuously, and plays a numbered stream of 16-bit sound through a buffer that absorbs network
jitter and the difference between the PC's and the board's clocks. One second without DMX data
and the line is dark; an empty sound buffer is silence; while the board's emergency-stop input is
active the line is dark and the sound off, whatever arrives. All limits of the effects (how long,
how often) are applied by the show service on the PC, not here.

## Files

| Path | Content |
|---|---|
| `include/config.h` | per-board settings: what the board has, platform id, network, stroke, park position, speeds, time-outs, pins |
| `src/main.cpp` | Arduino glue: Ethernet / Wi-Fi / serial, PWM outputs, enable and emergency-stop pins, 1 kHz motion update; the effects task (UDP, DMX through UART1) and the audio task (I2S) on the other processor core |
| `lib/fourdvr_link/` | motion link: portable protocol and safety core (no Arduino dependency) |
| `lib/fourdvr_fx/` | effects node: packets, DMX frame, sound buffer, time-outs (no Arduino dependency) |
| `test/vectors.txt`, `test/fx_vectors.txt` | test vectors shared with the Python side (`fourdvr controller test-vectors`, `fourdvr fx test-vectors`) |
| `test/host/` | tests on the PC: `make` runs the unit tests of both cores (645 and 150 checks), compile checks of `main.cpp` in eleven build variants, two builds that must fail (a pin used twice), and builds `fxnode_sim`, the effects node behind a UDP socket on the PC |
| `platformio.ini` | build environments (table in `../../docs/ESP32_EFFECTS_LINK.md` §3) |

Build, flash and test: `../../INSTALL.md` §3, *ESP32 firmware*. Wiring of the actuator outputs and
the emergency-stop input: `../../docs/ESP32_PROTOCOL.md` §5. Wiring of the DMX line and the audio
converter, pins, settings: `../../docs/ESP32_EFFECTS_LINK.md` §2 and §3.

The emergency-stop input only *reports* the state of the hardware safety chain: it freezes the
motion setpoints and blanks the effects outputs. It is not part of the emergency-stop circuit,
which must cut actuator power and the power of effect devices in hardware.
