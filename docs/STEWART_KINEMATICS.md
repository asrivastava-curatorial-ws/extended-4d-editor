# Stewart Platform Kinematics, Limits and Driver Interface

> **Names.** Since version 2.0 the *Motion Controller* and the *Motion Extractor* are parts of one program,
> Extended 4D Editor: "Motion Controller" below is its show service (`fourdvr controller …`), "Motion
> Extractor" its motion extraction (the Editor and `fourdvr extract …`). The former commands
> `fourdvr-motion` and `fourdvr-extract` used in the examples still work unchanged.

Implementation: `fourdvr_motion/kinematics.py` (one implementation, used by the show service and the Editor),
`limits.py`, `sources.py`, `pipeline.py`, `drivers.py`.

## 1. Frames and pose

* **Base frame**: origin on the floor at the centre of the base, X forward, Y left, Z up (metres).
  Base joint coordinates `b_i` are given in this frame.
* **Platform frame**: moves with the seat; at the neutral pose it is the base frame translated to
  `neutral_position` (and rotated by `neutral_orientation`, normally zero). Platform joint
  coordinates `p_i` are given in this frame.
* **Rotation centre** `c` (platform frame): the point that stays fixed under a pure rotation,
  normally placed near the passenger's head so that roll/pitch/yaw feel like rotations of the head,
  not swings.
* **Pose** `[surge, sway, heave, roll, pitch, yaw]` (m, rad) = offset from neutral, the same
  convention as the `.motion` file (MOTION_FORMAT.md §3). `R(roll, pitch, yaw) = Rz(yaw) · Ry(pitch) · Rx(roll)`.

## 2. Inverse kinematics

For pose `(t, roll, pitch, yaw)` with `R0` = neutral orientation, `n` = neutral position:

```
R      = R0 · R(roll, pitch, yaw)
origin = n + R0·c + R0·t − R·c              (rotation about c, translation in the neutral frame)
leg_i  = origin + R·p_i − b_i
L_i    = |leg_i|                            (pin-to-pin length of actuator i)
```

Leg *i* connects base joint *i* to platform joint *i*; the order of the six joints in the profile
defines the actuator numbering (actuator 1 = first entry). The inverse kinematics is closed-form and
exact; it runs every control period for every platform.

**Jacobian** `J = ∂L/∂pose` (6 × 6, central differences) is used for diagnostics and forward
kinematics.

**Forward kinematics** (lengths → pose) uses Newton–Raphson with the Jacobian (converges in a few
iterations from a nearby guess). It is used at start-up and after a fault to compute the current pose
from the positions reported by the ESP32, so motion always starts from the real platform pose.

**Symmetric 6-6 geometry generator**: `fourdvr-motion geometry --base-radius R_b --platform-radius R_p
--base-pair-angle α_b --platform-pair-angle α_p --neutral-length L0 --rotation-centre x,y,z` prints a
`[geometry]` section (joint pairs 120° apart, platform pairs rotated by 60°, neutral height solved so
every leg has length L0).

## 3. Processing chain (every control period, per platform)

```
master time t ──► pose source (neutral | park | hold | programme trajectory) with C² blends
             ──► PoseLimiter: position, velocity, acceleration, jerk limits (6 axes)
             ──► + calibration zero offsets
             ──► ActuatorGuard: IK, actuator length (workspace), velocity, acceleration limits
             ──► strokes = L − min_length + actuator_offsets, clipped to [0, stroke]
             ──► driver (ESP32 link or simulation)
```

The **simulation driver** uses exactly this chain; only the ESP32 is replaced by an in-process port of
the firmware core.

### 3.1 Pose sources and blends

Programme trajectory: the `.motion` track (PCHIP interpolation) evaluated at the timeline position
(`t − epoch`, looping), optionally `latency_compensation_s` ahead. Every change of source (engage at
PLAY, pause, resume, stop, park, late join) is a **smootherstep blend**
`s(u) = 6u⁵ − 15u⁴ + 10u³` (zero velocity and acceleration at both ends) whose duration is computed
from the distance and the platform's velocity, acceleration and jerk limits
(`T = 1.1 · max(1.875·d/v, √(5.77·d/a), ∛(60·d/j))`, at least `motion.min_blend_s`, with
`park_speed_scale` for stop/park).

### 3.2 PoseLimiter — hard per-axis limits

A third-order (jerk-limited) tracker. When the reference is feasible its jerk is fed forward and the
output equals the reference exactly. Otherwise a cascaded **square-root controller** (velocity demand
`√(2·a_max·|e|)` far from the target, linear close to it) removes the error with time-optimal braking,
so steps and infeasible inputs never cause overshoot or oscillation. Every period it clamps jerk,
acceleration and velocity to `limits.jerk/acceleration/velocity`, guards position with a
braking-distance check, and finally clamps position to `limits.position` (counted as
`position_clamps` etc. in the platform STATUS `limit_events`).

### 3.3 ActuatorGuard — actuator limits

1. **Workspace**: if any leg length leaves the usable stroke
   `[min_length + soft_margin, max_length − soft_margin]`, the pose is scaled toward neutral by
   bisection until all six lengths fit (`workspace_scalings`).
2. **Velocity and acceleration**: the pose step requested by the limiter is scaled by one factor α
   (pose = previous + α·step), which keeps the six lengths consistent with a rigid-body pose; α < 1
   slows actuators that would exceed `max_velocity` or `max_acceleration`; α up to 1.5 (never beyond
   pose or stroke limits) lets a leg brake gently instead of stopping abruptly (`actuator_rate_scalings`,
   `actuator_accel_excess`).

These limits are enforced **before** any command leaves the controller. The ESP32 adds an independent
slew limit (`FDVR_MAX_SPEED_UM_S`). Neither replaces limits in the servo drives nor the mechanical and
electrical safety measures listed in README.md.

`fourdvr-motion check-platform <profile>` prints the single-axis reach of the geometry against the
configured limits and the worst-case combined actuator demand.

## 4. Driver interface (`ActuatorDriver`)

Every actuator technology implements this interface (`fourdvr_motion/drivers.py`):

```python
class ActuatorDriver(ABC):
    kind: str                                    # "servo_electric", "simulation", ...
    def open(self) -> None: ...                  # acquire link/hardware; may raise on configuration errors
    def close(self) -> None: ...                 # release; safe to call twice
    def send(self, command: ActuatorCommand) -> None: ...
        # once per control period: command.state (IDLE | RUN | HOLD | PARK | FAULT_RESET),
        # command.strokes_m (6 stroke targets, metres from the retracted end), command.master_time
    def poll(self, now: float) -> LinkStatus: ...
        # connected, last_status_age_ms, device_state, faults[], positions_m (6) or None,
        # rtt_ms, counters{}
```

The runtime treats `connected == False` or a non-empty `faults` list as a fault: it stops the
programme, commands PARK, reports the fault to the server, and needs `positions_m` to restart from
the real pose.

### Implemented drivers

| `driver` | Status |
|---|---|
| `servo_electric` | **implemented**: electric servo actuators behind the ESP32 bridge (ESP32_PROTOCOL.md) over UDP or serial |
| `simulation` | **implemented**: same driver and protocol, in-process simulated ESP32 (testing only) |
| `hydraulic` | **not implemented** — configuration is rejected |
| `pneumatic` | **not implemented** — configuration is rejected |

### Requirements for a future hydraulic or pneumatic driver (documented interface only)

A hydraulic or pneumatic implementation is **not part of release 1.0.1** and must not be assumed to
exist. A future driver would subclass `ActuatorDriver` and keep the pose pipeline unchanged; in
addition it would need, at minimum: a closed-loop position controller per actuator running in the
valve controller (servo/proportional valves, position and pressure sensing), pressure and
temperature supervision, load-holding (pilot-operated check valves) on loss of power or signal, a
controlled descent/park path that works without electrical power, accumulator and supply
monitoring, and its own fault reporting through `LinkStatus.faults`. These are fluid-power design
tasks for qualified engineers and are outside the scope of this software.
