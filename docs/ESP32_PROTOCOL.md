# ESP32 Link Protocol 1.1 and Actuator Bridge

> **Names.** Since version 2.0 the *Motion Controller* and the *Motion Extractor* are parts of one program,
> Extended 4D Editor: "Motion Controller" below is its show service (`fourdvr controller …`), "Motion
> Extractor" its motion extraction (the Editor and `fourdvr extract …`). The former commands
> `fourdvr-motion` and `fourdvr-extract` used in the examples still work unchanged.

The binary protocol between the Motion Controller (`Esp32Driver`, `fourdvr_motion/esp32_protocol.py`)
and the ESP32 actuator bridge of one platform (`firmware/esp32-actuator-bridge`, core in
`lib/fourdvr_link`). Both implementations are tested against the same vectors
(`firmware/esp32-actuator-bridge/test/vectors.txt`, regenerate with `fourdvr-motion test-vectors`).

Electric, hydraulic and pneumatic actuators all use this link when an ESP32 board drives them; which
board settings and which profile keys go with which actuator: [ACTUATORS.md](ACTUATORS.md). Devices
with Modbus TCP or a UDP format of their own are driven without an ESP32 (same document).

This document is about the **motion link** (UDP port 47000): version 1.1 since 3.4.0 (§7). DMX
devices and bass shakers on the same or on another ESP32 use a second, separate link on UDP port
47002: [ESP32_EFFECTS_LINK.md](ESP32_EFFECTS_LINK.md). A board with the motion-only firmware never
sees that link's packets.

## 1. Transport

| Link | Framing | Default |
|---|---|---|
| Ethernet (recommended) | one packet per UDP datagram | bridge listens on UDP **47000**; replies to the sender's address/port |
| USB serial | COBS-encoded packet followed by a `0x00` delimiter | 921600 baud, 8N1 |
| Wi-Fi | as Ethernet | bench tests only (latency jitter unsuitable for passenger rides) |

Controller address syntax (platform profile `address`): `udp://192.168.10.101:47000`,
`serial:///dev/ttyUSB0?baud=921600`, `serial://COM3?baud=921600`.

## 2. Packets

All integers little-endian. Every packet ends with a **CRC-32** (IEEE 802.3 polynomial, as
`zlib.crc32`) over all preceding bytes.

### COMMAND (controller → bridge), 52 bytes

| Offset | Type | Field |
|---|---|---|
| 0 | 4 bytes | magic `"4DMC"` |
| 4 | u8 | version major = 1 |
| 5 | u8 | version minor = 1 (0 from controllers before 3.4.0) |
| 6 | u8 | type = 1 |
| 7 | u8 | platform id (1–250) — must equal the bridge's `FDVR_PLATFORM_ID` |
| 8 | u32 | sequence number (+1 per command) |
| 12 | u64 | timestamp, controller master clock in microseconds |
| 20 | u8 | command state: 0 IDLE, 1 RUN, 2 HOLD, 3 PARK, 4 FAULT_RESET |
| 21 | u8 | flags: bit 0 = targets valid; bits 4–6 = number of actuators the controller drives (2–6; 0 = six, as in 1.0) |
| 22 | u16 | reserved (0) |
| 24 | 6 × i32 | actuator stroke targets, micrometres from the fully retracted end; the first *n* are used, the rest 0 |
| 48 | u32 | CRC-32 |

### STATUS (bridge → controller), 72 bytes

| Offset | Type | Field |
|---|---|---|
| 0 | 4 bytes | magic `"4DMS"` |
| 4, 5 | u8, u8 | version major/minor |
| 6 | u8 | type = 2 |
| 7 | u8 | platform id |
| 8 | u32 | last accepted sequence number |
| 12 | u64 | timestamp of that command (echo → round-trip time) |
| 20 | u32 | uptime (ms) |
| 24 | u8 | device state: 0 BOOT, 1 IDLE, 2 RUNNING, 3 HOLD, 4 PARKING, 5 PARKED, 6 FAULT |
| 25 | u8 | fault flags (latched): 1 TIMEOUT, 2 INVALID_BURST, 4 STALE, 8 OUTPUT, 16 ESTOP, 32 CONFIG, 64 FEEDBACK (a position sensor out of range), 128 SUPPLY (the supply / drive-ready input dropped while running); FEEDBACK and SUPPLY also set OUTPUT, for controllers before 3.4.0 |
| 26 | u16 | capabilities (1.1; 0 from a 1.0 bridge): bits 0–3 number of actuators, bit 4 positions are measured, bits 5–6 output 0 PWM / 1 DAC 0–10 V / 2 valves, bit 7 a supply / drive-ready input is fitted |
| 28 | 6 × i32 | actuator positions (µm): measured by the sensors with bit 4 set, else the commanded output |
| 52 | u32 | packets received |
| 56 | u16 × 6 | lost, duplicate, invalid, stale, timeouts, reserved |
| 68 | u32 | CRC-32 |

Example: a RUN command for a seat on three actuators (flags = 0x31: targets valid, three
actuators) for platform 1, sequence 7, timestamp 1 700 000 000 123 456 µs, three targets of
150 000 µm (150 mm) encodes to these 52 bytes:

```
34 44 4d 43 01 01 01 01 07 00 00 00 40 22 20 18 24 0a 06 00 01 31 00 00
f0 49 02 00 f0 49 02 00 f0 49 02 00 00 00 00 00 00 00 00 00 00 00 00 00 2a 2b 27 f0
```

## 3. Behaviour of the bridge

**Validation of every packet** — a packet is *invalid* (counted, ignored) if its length, magic,
version major, type or CRC is wrong, or the platform id is not this bridge's. 20 invalid packets
within one second latch `INVALID_BURST`.

**Duplicates and loss** — sequence numbers must increase; a repeated or older number is a
*duplicate* (ignored); a gap increments *lost*.

**Stale packets** — the bridge learns the controller's time base from the packets with the
shortest transit (largest timestamp − local time). A packet older than `FDVR_STALE_MS` (100 ms)
than that estimate is *stale* and ignored (five stale packets in a row re-learn the time base, e.g.
after a controller restart).

**Watchdog** — while IDLE/RUNNING/HOLD, if no valid command arrives for `FDVR_TIMEOUT_MS` (200 ms),
the bridge latches `TIMEOUT` and **parks**: every output ramps to its park position at
`FDVR_PARK_SPEED_UM_S` (default 60 mm/s). Drives stay enabled while parking (controlled return).

**Command states**

| State | Bridge action |
|---|---|
| IDLE | hold the current outputs, state IDLE |
| RUN (+ targets valid) | track the targets, clamped to `[0, stroke]` and slew-limited to `FDVR_MAX_SPEED_UM_S` (a second, independent speed limit behind the controller's) |
| HOLD | freeze at the current outputs |
| PARK | ramp to the park position (PARKING → PARKED) |
| FAULT_RESET | clear latched faults (not while the E-stop input is active), state IDLE, outputs held |

While a fault is latched only FAULT_RESET is accepted.

**E-stop input** (`FDVR_ESTOP_PIN`, active LOW with pull-up): when active the bridge enters FAULT
with `ESTOP`, freezes its setpoints and drops the drive-enable output. This input *monitors* the
hardware safety chain; it is not the emergency stop. The E-stop circuit must remove actuator power
in hardware, independently of the ESP32 (README.md, safety section). With the pull-up a broken
monitoring wire reads "not active" — another reason why this input is informational only.

**Number of actuators** — a command for another number of actuators than the bridge's
`FDVR_AXES` (count 0 counts as six) latches `CONFIG`: a profile and a board that do not fit never
move anything.

**Inputs** (firmware 1.3.0, `FDVR_FEEDBACK`, `FDVR_READY_PIN`) — a position sensor that reads
outside its valid range latches `FEEDBACK`; the supply / drive-ready input dropping while RUNNING
or in HOLD latches `SUPPLY` (a hydraulic power unit or compressor that stops is not a reason to
fault a parked platform). Both park the platform.

**Valve outputs** (`FDVR_OUTPUT_VALVES`) — a target of at least half the stroke switches the
cylinder's valve to extend, below it to retract, at once: the controller keeps the switching
hysteresis and dwell times (ACTUATORS.md §3). Parking switches every valve to its park side.

**Replies** — a STATUS for every accepted command, plus a keep-alive STATUS every 100 ms.

## 4. Behaviour of the controller (Esp32Driver)

* Sends one COMMAND per control period (100 Hz default): RUN with the six stroke targets in
  normal operation, PARK while the platform is in FAULT, FAULT_RESET for a few periods when an
  operator STOP/SYNC ALL (or `fault_recovery = "auto"`) clears a fault, IDLE before the link is up.
* At start it waits for the first STATUS and computes the platform pose from the reported
  positions (forward kinematics), so motion always starts from where the platform really is.
* The link is *lost* when no valid STATUS arrives for the profile's `link_timeout_ms` (250 ms).
  Link loss or any fault flag reported by the bridge puts the platform into FAULT: the programme
  stops, the controller commands PARK, the fault is reported to the Control Server (state ERROR
  with the reason). Recovery is manual by default (operator STOP or SYNC ALL), or automatic after
  2 s of healthy link with `fault_recovery = "auto"`.
* Round-trip time from the echoed timestamp, and the bridge's counters, are reported in the
  platform's STATUS (`link`) and on the dashboard.
* It sends the profile's number of actuators in the flags and compares the bridge's capabilities
  with the profile: another number of actuators, or valves where the profile asks for positions (or
  the other way round), is a fault ("CONFIG: the board drives 6 actuators, the profile has 3").
  A bridge before firmware 1.3.0 reports no capabilities; the check is skipped.
* With measured positions (capabilities bit 4) it supervises the following error: an actuator
  that stays further from its recent commands than the profile allows is a fault, and the platform
  parks (ACTUATORS.md §5).

## 5. Bridge hardware and wiring (reference design)

| Signal | Default pin (config.h) | Notes |
|---|---|---|
| 6 × position setpoint | GPIO 2, 4, 12, 14, 15, 32 (`FDVR_OUTPUT_PINS`) | PWM 1 kHz, 14 bit; 10 %…90 % duty = 0…full stroke (`FDVR_PWM_DUTY_MIN/MAX`). Feed each servo drive's position input through an RC filter / isolated PWM-to-analog converter as the drive requires |
| 0–10 V set-points, sensors | I2C on GPIO 14 (SDA), 15 (SCL) | with `FDVR_OUTPUT_DAC` / `FDVR_FEEDBACK`: GP8403 and ADS1115 boards (ACTUATORS.md §4) |
| Valve outputs | the first `FDVR_AXES` pins of `FDVR_OUTPUT_PINS` | with `FDVR_OUTPUT_VALVES`, through a valve driver (ACTUATORS.md §4) |
| Supply / drive ready | `FDVR_READY_PIN` (e.g. GPIO 39) | pressure switch, power unit "ready", drive "ready"; input-only pins need an external pull-up |
| Drive enable | GPIO 33 (`FDVR_ENABLE_PIN`) | HIGH = enabled; LOW at boot, on E-stop and on output faults |
| E-stop monitor | GPIO 35 (`FDVR_ESTOP_PIN`) | input-only pin; LOW = safety chain open. Use a voltage-free auxiliary contact of the safety relay |
| Ethernet | LAN8720 PHY (WT32-ETH01 / Olimex ESP32-POE) | static IP `FDVR_IP`, port 47000 |
| USB serial | on-board USB-UART | 921600 baud |

Devices with Modbus TCP are driven by the show service directly (`driver = "modbus_tcp"`,
ACTUATORS.md); drives that take step/direction or CAN set-points need a different output stage in
`src/main.cpp` (`writeOutputs()`); the link protocol does not change. The drives must have their
own position, velocity and current limits and safe-torque-off wired to the E-stop chain.

## 6. Network setup

* One static IP per bridge (e.g. `192.168.10.101` … `.120`), configured in `include/config.h`
  (`FDVR_IP`, `FDVR_GATEWAY`, `FDVR_SUBNET`), the same address in the platform profile.
* Wired Ethernet only, through a managed switch; keep the motion segment free of bulk traffic
  (VLAN or separate switch). Expected RTT on a quiet LAN: < 2 ms.
* The bridge accepts commands from any source address that sends valid packets for its platform
  id; isolate the motion network from guest/visitor networks.

## 7. Versioning

Packets carry major/minor version bytes. A bridge rejects (counts as invalid) packets whose major
version differs; minor versions may only add meaning to reserved fields.

**1.1** (Extended 4D Editor 3.4.0, firmware 1.3.0) added the number of actuators (COMMAND flags
bits 4–6), the capabilities (STATUS offset 26, reserved before) and the faults FEEDBACK and SUPPLY.
A 1.1 bridge with six actuators works with a 1.0 controller (count 0 = six; FEEDBACK and SUPPLY
also show as OUTPUT); a 1.0 bridge ignores the new bits and reports no capabilities, and a 1.1
controller then skips the checks of §4.
