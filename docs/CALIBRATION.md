# Commissioning and Calibration

> **Names.** Since version 2.0 the *Motion Controller* and the *Motion Extractor* are parts of one program,
> Extended 4D Editor: "Motion Controller" below is its show service (`fourdvr controller …`), "Motion
> Extractor" its motion extraction (the Editor and `fourdvr extract …`). The former commands
> `fourdvr-motion` and `fourdvr-extract` used in the examples still work unchanged.

This is the **software part** of commissioning a platform. It assumes the mechanical, electrical
and safety design has been done and verified by qualified engineers (README.md, safety section).
Nothing here certifies a platform for passengers.

> Work with at least two people. Keep the hardware emergency stop within reach at all times.
> Nobody sits on or stands next to the platform during commissioning; use ballast for load tests.

Tools: the platform profile (PLATFORM_CONFIGURATION.md), `fourdvr-motion`, a tape measure or
laser distance meter, a digital inclinometer, the drive manufacturer's commissioning software.

## 1. Before software controls anything

| Check | Pass criterion |
|---|---|
| Emergency stop | pressing any E-stop removes actuator power **in hardware** (safe torque off / contactor), with the controller and ESP32 running; reset requires a deliberate action |
| Mechanical end stops | present on every actuator, both ends |
| Drive limits | each servo drive has its own position, velocity and current limits configured (independent of this software) |
| Drive setpoint scaling | 10 % PWM duty ⇒ fully retracted, 90 % ⇒ fully extended (or the scaling you set in `config.h`), verified with the drive's own tools |
| E-stop monitor input | the bridge reports `ESTOP` (fault 16, state FAULT) when the chain is open |
| Network | bridge pingable at its static IP; cable, not Wi-Fi |

## 2. Geometry and profile

1. Measure the six lower (base) and upper (platform) joint centres at neutral, in the frames of
   PLATFORM_CONFIGURATION.md §2 (base origin on the floor at the centre, X forward, Y left, Z up).
   Enter them, the neutral position and the rotation centre (near the seated passengers' heads) in
   the profile.
2. Enter `min_length`/`max_length` (pin-to-pin, from the actuator data sheet and measured), the
   rated `max_velocity`/`max_acceleration` at the payload (with margin), conservative limits.
3. `fourdvr-motion check-platform my-hexapod.toml` — must say `configuration OK`; read the reach table:
   every configured limit must lie inside the reach.
4. `fourdvr-motion ik --platform my-hexapod.toml --pose 0,0,0,0,0,0` — the neutral leg lengths must
   match the measured lengths within a few millimetres; otherwise the geometry is wrong.

## 3. Link test without motion

Drives **disabled** (or actuators mechanically decoupled):

1. Flash the bridge (INSTALL.md §5) with the right `FDVR_PLATFORM_ID`, IP, stroke and park values.
2. Run the controller with this platform; the dashboard shows the platform READY.
3. Check the platform details: `link.connected = true`, RTT a few ms, `lost/duplicate/invalid/stale`
   stay 0 (a few `lost` after start-up are normal).
4. Pull the Ethernet cable: within `link_timeout_ms` the platform shows ERROR "communication with
   actuator controller lost"; the bridge reports state PARKING → PARKED. Re-plug, press STOP on the
   dashboard (manual fault recovery): the platform returns to READY.

## 4. First motion (drives enabled, no load, no people)

1. Start with the platform at park. Small pose steps with `fourdvr-motion pose` (stop the
   controller service for this platform first, the tool talks to the bridge directly):

   ```bash
   fourdvr-motion pose --platform my-hexapod.toml --pose 0,0,0,0,0,0 --hold 5       # neutral
   fourdvr-motion pose --platform my-hexapod.toml --pose 0,0,0.02,0,0,0 --hold 5    # heave +20 mm
   fourdvr-motion pose --platform my-hexapod.toml --pose 0.02,0,0,0,0,0 --hold 5    # surge forward
   fourdvr-motion pose --platform my-hexapod.toml --pose 0,0,0,0.035,0,0 --hold 5   # roll +2°: right side down
   fourdvr-motion pose --platform my-hexapod.toml --pose 0,0,0,0,0.035,0 --hold 5   # pitch +2°: nose down
   fourdvr-motion pose --platform my-hexapod.toml --pose 0,0,0,0,0,0.035 --hold 5   # yaw +2°: nose left
   ```

   Values are metres and radians. Each command blends to the pose, holds it, and returns to
   neutral. **Verify every direction** against MOTION_FORMAT.md §3. A wrong direction means a wrong
   joint order/coordinates or a reversed drive — fix it before anything else.
2. **Actuator offsets**: at neutral, measure each actuator's pin-to-pin length and compare it with
   the IK length (`fourdvr-motion ik --pose 0,0,0,0,0,0`). Add `L_ik − L_measured` (metres; positive
   when the actuator is too short) to that actuator's entry in `calibration.actuator_offsets`; repeat
   until all six match within 1 mm.
3. **Levelling**: at neutral, measure seat roll and pitch with the inclinometer (and height). Enter the
   negated error in `calibration.zero_offsets` (`[0, 0, dz, -roll_error, -pitch_error, 0]`) and repeat
   until the seat is level within your tolerance.
4. **Park pose**: check boarding height and that the platform rests safely at `park_pose`.
5. Increase single-axis amplitudes step by step to the configured limits; watch for noise,
   vibration, drive warnings and thermal behaviour.

## 5. Latency and timing

Play a programme with a sharp motion cue next to a headset or screen showing the same programme.
If the platform consistently lags the picture, set `latency_compensation_s` (0–0.2 s) to the measured
actuator/drive latency. Check `loop_overruns` in the platform STATUS stays at 0 (the control loop
keeps its rate) and `max |drift|` on the dashboard stays small.

## 6. Content and load tests (ballast)

1. Validate each programme against this profile: `fourdvr-extract validate programme.motion --profile my-hexapod.toml`
   and dry-run it: `fourdvr-motion play-local programme.motion --platform my-hexapod.toml`
   (limiter activity and actuator demand for the whole file).
2. Run every programme with **maximum ballast** (worst-case payload and centre of gravity) through the
   Control Server, including PAUSE/RESUME, STOP mid-programme, a programme switch and loop points.
3. Fault tests under ballast: pull the bridge's Ethernet cable during motion (platform must park
   with a controlled ramp), stop the controller process (`systemctl stop`; the bridge must park after
   200 ms), stop the Control Server (platform continues `server_loss_timeout_s`, then neutral),
   press the E-stop (power cut in hardware).
4. Record results, profile SHA-256 and firmware version in the commissioning log.

## 7. After commissioning

* Keep the profile file under change control; every change ⇒ repeat the relevant tests and re-validate
  content (its `profile_sha256` changes).
* Daily: E-stop test, visual inspection, one run without passengers.
* Periodically (per the risk assessment): repeat sections 3–6.

## Physical safety responsibilities (outside this software)

Structural design · actuator ratings · mechanical stops · electrical protection · emergency stop ·
actuator failure behaviour · runaway prevention · passenger restraint · load testing ·
commissioning · professional engineering validation. See README.md for details. This software is not
certified for human safety and cannot replace an emergency stop.
