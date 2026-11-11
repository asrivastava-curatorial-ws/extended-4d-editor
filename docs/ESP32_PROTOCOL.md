# ESP32 Link Protocol 1.0 and Actuator Bridge

> **Names.** Since version 2.0 the *Motion Controller* and the *Motion Extractor* are parts of one program,
> Extended 4D Editor: "Motion Controller" below is its show service (`fourdvr controller …`), "Motion
> Extractor" its motion extraction (the Editor and `fourdvr extract …`). The former commands
> `fourdvr-motion` and `fourdvr-extract` used in the examples still work unchanged.

The binary protocol between the Motion Controller (`ServoElectricDriver`, `fourdvr_motion/esp32_protocol.py`)
and the ESP32 actuator bridge of one platform (`firmware/esp32-actuator-bridge`, core in
`lib/fourdvr_link`). Both implementations are tested against the same vectors
(`firmware/esp32-actuator-bridge/test/vectors.txt`, regenerate with `fourdvr-motion test-vectors`).

This document is about the **motion link** (UDP port 47000), which is unchanged since 1.0. DMX
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
| 5 | u8 | version minor = 0 |
| 6 | u8 | type = 1 |
| 7 | u8 | platform id (1–250) — must equal the bridge's `FDVR_PLATFORM_ID` |
| 8 | u32 | sequence number (+1 per command) |
| 12 | u64 | timestamp, controller master clock in microseconds |
| 20 | u8 | command state: 0 IDLE, 1 RUN, 2 HOLD, 3 PARK, 4 FAULT_RESET |
| 21 | u8 | flags: bit 0 = targets valid |
| 22 | u16 | reserved (0) |
| 24 | 6 × i32 | actuator stroke targets, micrometres from the fully retracted end |
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
| 25 | u8 | fault flags (latched): 1 TIMEOUT, 2 INVALID_BURST, 4 STALE, 8 OUTPUT, 16 ESTOP, 32 CONFIG |
| 26 | u16 | reserved |
| 28 | 6 × i32 | actuator positions (µm); the commanded output when the drives give no feedback |
| 52 | u32 | packets received |
| 56 | u16 × 6 | lost, duplicate, invalid, stale, timeouts, reserved |
| 68 | u32 | CRC-32 |

Example: a RUN command (flags = 1) for platform 1, sequence 7, timestamp 1 700 000 000 123 456 µs,
all six targets 150 000 µm (150 mm) encodes to these 52 bytes:

```
34 44 4d 43 01 00 01 01 07 00 00 00 40 22 20 18 24 0a 06 00 01 01 00 00
f0 49 02 00 f0 49 02 00 f0 49 02 00 f0 49 02 00 f0 49 02 00 f0 49 02 00 ec 7e ff 10
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

**Replies** — a STATUS for every accepted command, plus a keep-alive STATUS every 100 ms.

## 4. Behaviour of the controller (ServoElectricDriver)

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

## 5. Bridge hardware and wiring (reference design)

| Signal | Default pin (config.h) | Notes |
|---|---|---|
| 6 × position setpoint | GPIO 2, 4, 12, 14, 15, 32 (`FDVR_PWM_PINS`) | PWM 1 kHz, 14 bit; 10 %…90 % duty = 0…full stroke (`FDVR_PWM_DUTY_MIN/MAX`). Feed each servo drive's position input through an RC filter / isolated PWM-to-analog converter as the drive requires |
| Drive enable | GPIO 33 (`FDVR_ENABLE_PIN`) | HIGH = enabled; LOW at boot, on E-stop and on output faults |
| E-stop monitor | GPIO 35 (`FDVR_ESTOP_PIN`) | input-only pin; LOW = safety chain open. Use a voltage-free auxiliary contact of the safety relay |
| Ethernet | LAN8720 PHY (WT32-ETH01 / Olimex ESP32-POE) | static IP `FDVR_IP`, port 47000 |
| USB serial | on-board USB-UART | 921600 baud |

Drives that take step/direction, CAN or Modbus setpoints need a different output stage in
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
