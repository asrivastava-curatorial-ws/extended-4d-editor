# ESP32 firmware 1.3.0: actuator bridge and effects node

One firmware source for the ESP32 boards of a theatre. A board can have any of four jobs,
chosen when the firmware is built:

| Job | Build setting | Link | What it does |
|---|---|---|---|
| **Actuator bridge** | `FDVR_HAS_ACTUATORS 1` (default) | motion link 1.1, UDP 47000 or USB serial | receives the positions of one platform's 2 to 6 actuators and puts them out as PWM, as 0–10 V (servo drives, valve amplifiers, positioners) or as on/off valve outputs; reads position sensors and a supply / drive-ready input (new in 1.3.0) |
| **DMX line** | `FDVR_HAS_DMX 1` | effects link "4DFX" 1.1, UDP 47002 | sends the DMX levels it receives on a DMX512 line (RS-485 driver on a UART pin) |
| **Audio output** | `FDVR_HAS_AUDIO 1` | effects link, UDP 47002 | plays the bass / seat-vibration sound it receives through an I2S audio converter into an amplifier |
| **Seat buttons** (new in 1.2.0) | `FDVR_INPUT_COUNT 1…8` | effects link, UDP 47002 | reads up to eight push buttons, debounces them and reports every change: the buttons of this platform's seat in an interactive show (`fourdvr play`) |

With only the first job the firmware is the plain actuator bridge and does not open the effects
port. Without it (`FDVR_HAS_ACTUATORS 0`) the board is an effects-only node.

> **State.** The safety core of the actuator bridge (`lib/fourdvr_link`) and the new input /
> output logic (`lib/fourdvr_io`) are tested on a PC, like the effects logic (`lib/fourdvr_fx`), and
> `src/main.cpp` passes compile checks against stand-in headers that mirror the declarations of
> ESP-IDF 4.4.7 (also compared with 5.1.4) — but **the firmware has not been compiled with the
> ESP32 tool chain and has not run on a board.** Expect to prove pins, the DAC and sensor boards,
> DMX timing and the audio output on your hardware: `../../docs/ACTUATORS.md` §7 and
> `../../docs/ESP32_EFFECTS_LINK.md`, "First power-on of a board".

## Actuator bridge

It implements the binary link protocol 1.1 (`../../docs/ESP32_PROTOCOL.md`): it receives COMMAND
packets (UDP over Ethernet, or COBS-framed USB serial), checks version, platform id, CRC, sequence
(loss/duplicates), age (stale packets) and the number of actuators, slew-limits the stroke targets,
drives the outputs, and returns a STATUS packet for every accepted command. If no valid command
arrives for `FDVR_TIMEOUT_MS` (200 ms) it latches a timeout fault and ramps every actuator to the
park position at `FDVR_PARK_SPEED_UM_S`. A latched fault is cleared only by a FAULT_RESET command.

What it drives is chosen in `include/config.h` (`../../docs/ACTUATORS.md` §4):

| Setting | Output | For |
|---|---|---|
| `FDVR_AXES 2…6` | | the number of actuators (a hexapod 6; seats 3 or 2) |
| `FDVR_OUTPUT_PWM` (default) | one PWM pin per actuator | RC-style servos, drives with a PWM / pulse input |
| `FDVR_OUTPUT_DAC 1` | 0–10 V on GP8403 boards (I2C, two channels each) | servo drives, proportional / servo valve amplifiers and servo-pneumatic positioners with a 0–10 V position input (±10 V and 4–20 mA through the amplifier's own input card or a converter) |
| `FDVR_OUTPUT_VALVES 1` | one switched pin per cylinder (through a valve driver) | pneumatic cylinders on plain 5/2 or 5/3 valves: extend or retract; the PC switches them with hysteresis and dwell times |
| `FDVR_FEEDBACK 1` | ADS1115 boards (I2C, four inputs each) | position sensors (0–10 V through a divider, or 4–20 mA through 150 Ω): the board reports the measured positions, the PC compares them with the commands and parks on a following error; a sensor out of range is a fault |
| `FDVR_READY_PIN` | a digital input | a pressure switch, a hydraulic power unit's "ready" or a drive's "ready" contact: losing it while running parks the platform |

The board says in every STATUS packet how many actuators it drives, what kind of output it has
and whether it measures positions; the show service refuses to run a profile that does not fit.

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
| `src/main.cpp` | Arduino glue: Ethernet / Wi-Fi / serial, PWM, DAC or valve outputs, sensors, ready, enable and emergency-stop pins, 1 kHz motion update; the effects task (UDP, DMX through UART1) and the audio task (I2S) on the other processor core |
| `lib/fourdvr_link/` | motion link: portable protocol and safety core (no Arduino dependency) |
| `lib/fourdvr_io/` | output and sensor arithmetic: PWM duty, DAC codes and registers, valve states, ADS1115 set-up, sensor scaling and range watch, ready-input debounce (no Arduino dependency) |
| `lib/fourdvr_fx/` | effects node: packets, DMX frame, sound buffer, time-outs (no Arduino dependency) |
| `test/vectors.txt`, `test/fx_vectors.txt` | test vectors shared with the Python side (`fourdvr controller test-vectors`, `fourdvr fx test-vectors`) |
| `test/host/` | tests on the PC: `make` runs the unit tests of the three cores (663, 182 and 48 checks), compile checks of `main.cpp` in 22 build variants, 14 builds that must fail (a pin used twice, two output kinds, too many buttons or actuators), and builds `fxnode_sim`, the effects node behind a UDP socket on the PC |
| `platformio.ini` | build environments (table in `../../docs/ESP32_EFFECTS_LINK.md` §3) |

Build, flash and test: `../../INSTALL.md` §3, *ESP32 firmware*. Wiring of the actuator outputs and
the emergency-stop input: `../../docs/ESP32_PROTOCOL.md` §5; of the DAC, sensor and valve boards and
the ready input: `../../docs/ACTUATORS.md` §4. Wiring of the DMX line and the audio
converter, pins, settings: `../../docs/ESP32_EFFECTS_LINK.md` §2 and §3.

The emergency-stop input only *reports* the state of the hardware safety chain: it freezes the
motion setpoints and blanks the effects outputs. It is not part of the emergency-stop circuit,
which must cut actuator power and the power of effect devices in hardware.
