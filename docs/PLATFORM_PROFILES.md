# Platform Profiles

> **Names.** Since version 2.0 the *Motion Controller* and the *Motion Extractor* are parts of one program,
> Extended 4D Editor: "Motion Controller" below is its show service (`fourdvr controller …`), "Motion
> Extractor" its motion extraction (the Editor and `fourdvr extract …`). The former commands
> `fourdvr-motion` and `fourdvr-extract` used in the examples still work unchanged.

A platform profile describes one type of motion platform: geometry, actuators, motion limits and
calibration. It is **the same TOML file** the Motion Controller uses to drive the platform (its full
reference is PLATFORM_CONFIGURATION.md in the Motion Controller package). The Motion Extractor uses
it to fit the generated motion to the platform and to validate it before export.

Rule: content for a theatre is generated and validated with the profile of **that theatre's
platforms**. Ask the motion technician for the file; do not edit limits to make content pass.

## What the extractor uses

| Profile section | Used for |
|---|---|
| `[platform]` `name` | shown in the GUI and stored in the validation record |
| `[geometry]` | inverse kinematics: actuator lengths for the workspace check and the 3D preview |
| `[actuators]` `min_length`, `max_length`, `soft_margin` | usable stroke (extension/compression limits) |
| `[actuators]` `max_velocity`, `max_acceleration` | actuator velocity/acceleration limits |
| `[limits.position/velocity/acceleration/jerk]` | per-axis limits for surge, sway, heave (m …) and roll, pitch, yaw (rad …) |
| `[calibration]` `neutral_pose`, `zero_offsets` | neutral reference for the workspace check |
| `[platform]` `driver`, `address`, `link_*`, `[motion]` | ignored by the extractor (runtime settings) |

The profile is checked when it is loaded (same rules as the Motion Controller: positive limits,
`min < 0 < max` position ranges, reachable single-axis limits, neutral and park poses inside the
stroke; `hydraulic`/`pneumatic` drivers are rejected because they are not implemented).

## Margins

The extractor deliberately stays below the profile's limits (settings `constraints`):

| Setting | Default | Meaning |
|---|---|---|
| `position_margin` | 0.92 | per-axis position compressed into 92 % of each limit |
| `derivative_margin` | 0.85 | velocity, acceleration and jerk kept below 85 % of each limit |
| `actuator_margin` | 0.85 | actuator velocity/acceleration below 85 % |
| `bandwidth_hz` | 6.0 | motion low-passed to the platform bandwidth |

Validation then checks against 100 % of the limits. The margins leave room for the Motion
Controller's blends and run-time limiter, so a validated file is normally played without any
run-time limiting.

## Traceability

The SHA-256 of the profile file is written into the `.motion` header (`validation.profile_sha256`)
together with its name. The Motion Controller reports the SHA-256 of the profile each platform runs
(`profile.profile_sha256` in its STATUS, visible in the dashboard device details). If the platform profile changes, re-validate all content:

```bash
for f in content/*.motion; do fourdvr-extract validate "$f" --profile my-hexapod.toml || echo "REVALIDATE: $f"; done
```

and regenerate the files that fail.

## Bundled example

`config/platforms/example-electric-6dof.toml` (also inside the installed package; list with
`fourdvr-extract profiles`): a 6-6 electric hexapod with 300 mm stroke, limits surge/sway ±120 mm,
heave ±100 mm, roll/pitch ±9.7°, yaw ±11.5°. It is an **example** for evaluation and testing, not
the data of a real platform.

## Making a profile for a new platform

The motion technician creates it with the Motion Controller tools:

```bash
fourdvr-motion geometry --base-radius 0.75 --platform-radius 0.55 --base-pair-angle 20 \
    --platform-pair-angle 20 --neutral-length 0.80 --rotation-centre 0,0,0.60   # ideal geometry
fourdvr-motion check-platform my-hexapod.toml                                   # validate, show reach
```

then corrects the geometry with measurements during commissioning (Motion Controller CALIBRATION.md).
