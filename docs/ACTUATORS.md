# Electric, Hydraulic and Pneumatic Platforms

Since 3.4.0 the show service drives platforms on electric, hydraulic or pneumatic actuators — a
six-actuator hexapod or a seat on three or two — through an ESP32 board, a device with Modbus TCP or
a device with a UDP format of its own. This document explains what the software does and what the
device must do, how to describe a platform in its profile, how to wire the ESP32 board for analog
set-points, sensors and valves, and how to bring a fluid-power platform up safely.

> **State.** Every combination is tested against simulated devices with simulated actuators of each
> kind (`fourdvr controller modbus-sim`, `udp-sim`, `esp32-sim --platform`;
> `integration/run_actuator_integration.py`), not against real valves, drives or PLCs. The ESP32
> firmware has not been compiled with the ESP32 tool chain (firmware README). Prove every platform
> step by step as §7 describes before anybody sits on it.

## 1. Who does what

```
 show service (PC)              device                        actuator
 ─────────────────              ──────                        ────────
 programme → limits → legs  ──► ESP32 board / PLC / drive ──► valve amplifier or drive ──► cylinder / motor
 100 set-points a second        holds each actuator at its        (position loop)              │
 checks measured positions ◄──  set-point, reports positions ◄────────────── position sensor ◄─┘
 parks on a fault               and faults, parks on silence
```

* **The loop that holds an actuator at a position runs in the device**, not on the PC: in the servo
  drive, in the valve amplifier's position card, in the servo-pneumatic positioner or in the PLC.
  A PC on a network cannot close a hydraulic position loop safely (its timing is not guaranteed).
  The one exception is a cylinder on plain on/off valves: there is no position loop at all, the
  cylinder goes from end to end (§3).
* **The show service** turns the programme into one set-point per actuator, keeps every axis and
  actuator inside the profile's limits, sends the set-points 100 times a second, reads back the
  measured positions and the device's state, and stops and **parks** the platform when an actuator
  does not follow, the device reports a fault or the link goes quiet.
* **The device** must park by itself when the show service stops talking to it (an ESP32 board does
  this in its firmware; a PLC on the heartbeat register; a UDP device when the datagrams stop).
* **Safety is hardware** (§8): the emergency stop, load-holding valves and pressure relief do not
  depend on any of this software.

## 2. Which link for which device

| Actuators | Typical device | Profile | Device side |
|---|---|---|---|
| Electric, drives with a PWM or 0–10 V position input | ESP32 board | `driver = "esp32"` | `pio run -e wt32-eth01` (PWM) or `-e wt32-eth01-analog` (0–10 V, sensors, ready input) |
| Electric, drives or a motion controller with Modbus TCP | the drive / controller | `driver = "modbus_tcp"`, `[modbus]` | its register map (§6) |
| Electric, a motion controller with a UDP set-point format | the controller | `driver = "udp"`, `[udp]` | its datagram layout (§6) |
| Hydraulic, proportional / servo valves with amplifier and position card | ESP32 board → the amplifiers' 0–10 V command inputs; cylinder sensors → the board | `actuator = "hydraulic"`, `driver = "esp32"` | `-e wt32-eth01-analog` |
| Hydraulic, a motion PLC that closes the loops | the PLC | `actuator = "hydraulic"`, `driver = "modbus_tcp"` | §6 |
| Pneumatic, proportional valves with positioners (servo-pneumatics) | ESP32 board → the positioners' 0–10 V inputs | `actuator = "pneumatic"`, `driver = "esp32"` | `-e wt32-eth01-analog` |
| Pneumatic, plain 5/2 or 5/3 solenoid valves | ESP32 board with valve outputs, or a PLC's coils, or UDP | `actuator = "pneumatic"`, `control = "onoff"` | `-e wt32-eth01-valves`, `[modbus] valves`, `[udp] send = "valves"` |
| Any, for tests without hardware | — | `driver = "simulation"` | — |

Command signals other than 0–10 V (±10 V, 4–20 mA) are set on the amplifier's input card or made
with an isolated signal converter after the board. Ready-made example profiles:
`config/platforms/example-electric-6dof.toml`, `example-hydraulic-6dof.toml` (Modbus TCP),
`example-pneumatic-3dof.toml` (ESP32, 0–10 V), `example-pneumatic-onoff-3dof.toml` (ESP32, valves),
`example-electric-2dof.toml` (UDP). Check any profile with `fourdvr controller check-platform`.

## 3. The profile

```toml
[platform]
actuator = "hydraulic"    # electric (default) | hydraulic | pneumatic
control = "position"      # position (default): a set-point per actuator | onoff: plain valves
driver = "modbus_tcp"     # esp32 | modbus_tcp | udp | simulation   (servo_electric: the name before 3.4)
address = "tcp://192.168.10.60:502"   # esp32: udp://… or serial://…; modbus_tcp: tcp://host:port; udp: udp://host:port
```

**Geometry with fewer actuators.** `[geometry]` takes 2 to 6 joints. `dof` names the axes the
platform moves; the others are held at zero, and a programme's motion on them is left out (counted
as `axes_left_out` in the status, shown by the Editor's validation as "this platform does not move
on this axis"). A seat on three actuators moves heave, roll and pitch around a guided central
pivot (`rotation_centre`); on two, roll and pitch around a fixed pivot.

```
fourdvr controller geometry --legs 3 --base-radius 0.45 --neutral-length 0.50   # front + two rear corners
fourdvr controller geometry --legs 2 --base-radius 0.40 --neutral-length 0.55   # two rear corners
```

print a `[geometry]` to start from; measure the real joints afterwards (CALIBRATION.md).

**Plain on/off valves** (`control = "onoff"`). A cylinder on a 5/2 valve is either retracted or
extended. The show service still computes where each cylinder should be and switches its valve:

```toml
[onoff]
rest = "retracted"   # where a cylinder sits when the programme does not ask for it (retracted | extended)
on_level = 0.25      # switch over when the demand passes 25 % of the half stroke from the neutral length …
off_level = 0.10     # … and back when it falls below 10 % (hysteresis: no chattering)
min_dwell_s = 0.25   # at least this long between two switchings of one valve
travel_s = 0.5       # how long a stroke takes (sets the following-error check)
```

Expect a seat that moves in steps: set the speed with the cylinders' flow-control valves and end
cushioning, not in software. Content made for proportional platforms plays, but only its larger
movements come through.

**Supervision** (`[supervision]`, §5) is optional; the defaults suit the actuator kind.

## 4. The ESP32 board: 0–10 V outputs, sensors, valves, ready input

The firmware (1.3.0) is set up in `include/config.h` or with build flags. The board tells the show
service how many actuators it drives and what kind of output it has; a profile that does not fit is
refused ("CONFIG: the board drives 6 actuators, the profile has 3").

| What | Parts | Wiring (WT32-ETH01) |
|---|---|---|
| 0–10 V set-points (`FDVR_OUTPUT_DAC 1`) | GP8403 boards, two channels each, I2C addresses 0x58 (actuators 1–2), 0x59 (3–4), 0x5A (5–6) | SDA IO14, SCL IO15, 3V3/GND; channel 0 → the odd actuator's command input, channel 1 → the even one's. Supply the boards as their data sheet says. `FDVR_DAC_CODE_RETRACTED` / `_EXTENDED` swap the direction |
| Position sensors (`FDVR_FEEDBACK 1`) | ADS1115 boards, four inputs each, 0x48 (actuators 1–4), 0x49 (5–6) | same I2C bus; a 4–20 mA sensor through a 150 Ω precision resistor to GND (0.6–3.0 V); a 0–10 V sensor through a divider to at most 3.3 V. Measure the raw values at both ends at commissioning (`FDVR_SENSOR_RAW_RETRACTED` / `_EXTENDED`); outside `FDVR_SENSOR_RAW_MIN_VALID … MAX_VALID` is a FEEDBACK fault (4–20 mA: a broken wire reads below 3.6 mA) |
| Valve outputs (`FDVR_OUTPUT_VALVES 1`) | a low-side driver per valve (MOSFET module or a ULN2803-type array with free-wheeling diodes), solenoids on their own 24 V supply | IO2, IO4, IO12 for three cylinders (`FDVR_OUTPUT_PINS`); high = extend (`FDVR_VALVE_ACTIVE_HIGH`). IO2 and IO12 are boot pins: the driver inputs must not pull them high at power-on |
| Supply / drive ready (`FDVR_READY_PIN 39`) | a voltage-free contact: pressure switch, power unit "ready", drive "ready" | contact from 3V3 to IO39, 10 kΩ from IO39 to GND: a broken wire reads "not ready". Lost for 50 ms while running or holding: a SUPPLY fault, the platform parks |
| Drive enable, e-stop monitor | as before | ESP32_PROTOCOL.md §5 |

Build environments: `wt32-eth01-analog` (six 0–10 V outputs, sensors, ready input; add
`-D FDVR_AXES=3` for a seat), `wt32-eth01-valves` (three valves, ready input; add
`-D FDVR_FEEDBACK=1` with position sensors). The firmware refuses to build when a pin is used twice
or is one of the Ethernet chip's.

## 5. Supervision: following error and faults

When the positions are measured (an ESP32 board with sensors, a Modbus device with `positions`, a
UDP device with `feedback = true`), the show service compares each actuator with what it was told:

* A measured position may lie anywhere between the smallest and the largest command of the last
  `lag_s` seconds (the actuator's normal delay), plus `following_error` metres.
* Beyond that for longer than `following_error_s`: the platform faults ("actuator 2 does not follow
  its command: 31 mm off for 0.3 s") and parks.

| Kind | `following_error` | `following_error_s` | `lag_s` |
|---|---|---|---|
| electric | 0.020 m | 0.3 s | 0.08 s |
| hydraulic | 0.015 m | 0.3 s | 0.10 s |
| pneumatic | 0.030 m | 0.5 s | 0.20 s |
| on/off valves | a quarter of the stroke | the stroke time (at least 0.5 s) | 1.5 × the stroke time |

Other faults that stop and park a platform: the link goes quiet (`link_timeout_ms`); the device
reports a fault (ESP32: any fault flag; Modbus: `fault_bit`); the supply drops (ESP32: the ready
input; Modbus: `ready_bit` cleared while running); a sensor out of range. Recovery is as for any
fault: operator STOP or SYNC ALL, or automatic with `fault_recovery = "auto"`.

Without measured positions the show service cannot see a stuck actuator; a UDP device without
`feedback` cannot even be seen to be there (its status reads "UNSUPERVISED"). Use those only where
the device supervises itself.

## 6. Devices with Modbus TCP or UDP

**Modbus TCP** (`driver = "modbus_tcp"`): every `period_ms` (10 ms) the show service writes the
set-points (or the valve coils), the command state and the heartbeat, and reads the measured
positions and the status. Addresses are protocol addresses from 0 (a PLC's "40001" is holding
register 0).

| `[modbus]` key | Meaning (default) |
|---|---|
| `unit` | unit id (1) |
| `setpoints` | holding register of actuator 1's set-point; the others follow (0) |
| `type`, `word_order` | `u16` `i16` `u32` `i32` `f32`; 32-bit values take two registers, `big` = high word first |
| `range` | the values for fully retracted and fully extended ([0, 10000]) |
| `positions`, `positions_table`, `positions_range` | measured positions: first register (−1: none), `input` or `holding`, their range (as `range`) |
| `command` | holding register for the state: 0 idle, 1 run, 2 hold, 3 park, 4 reset (−1: none) |
| `status`, `status_table`, `ready_bit`, `fault_bit` | the device's status register and its bits (−1: none) |
| `heartbeat` | holding register counted up every cycle: **the device must park when it stops changing** |
| `enable_coil` | coil set while the platform may move (−1: none) |
| `valves` | `control = "onoff"`: coil of actuator 1's valve, the others follow |
| `period_ms`, `timeout_ms` | cycle (2–100 ms) and the answer time-out (5–1000 ms) |

When the show service parks a platform it ramps the set-points to the park position itself, at the
profile's park speed, and sends state 3 (park).

**UDP** (`driver = "udp"`): one datagram per control period: `header` bytes, an optional `counter`,
an optional `state` byte (as above), then the values.

| `[udp]` key | Meaning (default) |
|---|---|
| `send` | `positions` (one value per actuator), `pose` (the axes of `pose_axes`, scaled by `pose_scale`) or `valves` (one bit per actuator) |
| `type`, `byte_order` | `f32` `f64` `i16` `u16` `i32` `u32`; `little` or `big` |
| `range` | the values for fully retracted and fully extended (positions; default: metres of stroke) |
| `header`, `counter`, `state` | hexadecimal bytes ("4D 53"), `u8` / `u16` / `u32`, `true` / `false` |
| `feedback` | the device answers each datagram with its measured positions in the same layout (not with `send = "pose"`) |

The device must park by itself when the datagrams stop.

## 7. Bench tests and the first power-on

**Without hardware**, each kind of device can be simulated on the bench PC with the actuators of
the profile (an electric drive, a hydraulic or pneumatic cylinder with its delay, valves with their
stroke time):

```
fourdvr controller modbus-sim --platform hydraulic.toml --port 1502     # profile address: tcp://127.0.0.1:1502
fourdvr controller udp-sim    --platform electric-2dof.toml --port 7408 # udp://127.0.0.1:7408
fourdvr controller esp32-sim  --platform pneumatic-3dof.toml --port 47000
```

Then run the show service against them (`fourdvr controller run`, or `fourdvr live run` with
`--platform`) exactly as against the real device.

**First power-on of a fluid-power platform** — with a second person at the emergency stop, the
platform empty and the area guarded:

1. Profile: `fourdvr controller check-platform` must say "configuration OK"; check the reach
   table against the real cylinders.
2. Device alone, without the show service: at reduced pressure, move each cylinder by hand from
   the amplifier's or PLC's own commissioning tool. Check the direction, the end positions and the
   sensor readings (ESP32: `FDVR_SENSOR_RAW_*`; Modbus: `positions_range`).
3. Pull the link (unplug the network cable): the device must park by itself.
4. Show service with `pause_behaviour = "hold"`, reduced `limits` and the platform's own speed
   limits low: SYNC, then a gentle programme. Watch `following_error_mm` in the status: it should
   stay well below the limit. Then raise pressure, limits and speeds step by step.
5. Provoke each fault once: emergency stop, supply off (pump / compressor), a sensor unplugged, the
   link pulled during motion. Each must end with the platform parked.

## 8. Fluid-power safety is hardware

This software stops and parks a platform when something is wrong; it cannot make a hydraulic or
pneumatic platform safe. The machine builder must provide, among others:

* an emergency stop that removes the power to the pump and to the valves in hardware, independent
  of any PC, PLC or ESP32 (ISO 13849-1 for the safety functions);
* load-holding valves (pilot-operated check or counterbalance valves) at each cylinder, so that a
  burst hose or a power loss does not drop the platform, and a controlled descent for evacuation;
* pressure relief, accumulator discharge on stop, filtration, hose restraints (ISO 4413);
* pneumatics: a soft-start and dump valve, exhaust that does not let the seat fall, lock-out of
  the air supply for maintenance (ISO 4414);
* guarding of pinch points, and the rules for amusement rides where they apply (EN 13814,
  ASTM F2291).
