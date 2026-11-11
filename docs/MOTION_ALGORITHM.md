# Motion Algorithm

> **Names.** Since version 2.0 the *Motion Controller* and the *Motion Extractor* are parts of one program,
> Extended 4D Editor: "Motion Controller" below is its show service (`fourdvr controller …`), "Motion
> Extractor" its motion extraction (the Editor and `fourdvr extract …`). The former commands
> `fourdvr-motion` and `fourdvr-extract` used in the examples still work unchanged.

How the Motion Extractor turns a video into platform motion. Everything is deterministic classical
signal processing and computer vision (OpenCV, NumPy, SciPy); random sampling (RANSAC) uses a fixed
seed, so identical input and settings give an identical file. No machine-learning model, language
model or network service is used.

```
1 decode ─► 2 features & optical flow ─► 3 rotation & translation ─► 4 signal cleaning
         ─► 5 event analysis & classification ─► 6 perceptual cueing ─► 7 filtering & scaling
         ─► 8 workspace / velocity / acceleration / jerk constraints ─► 9 validation ─► .motion
```

Module map: `video.py` (1), `analysis.py` (2), `egomotion.py` (3), `signals.py` (4, filters),
`events.py` (5), `cueing.py` (6–7), `constraints.py` (8), `validation.py` (9), `pipeline.py`.

## 1. Decoding

OpenCV/FFmpeg decodes the video once. For stereo layouts the **left eye** is used (SBS: left half,
OU: top half). Frames are converted to grey and scaled to `analysis.max_width` (640 px), and decimated
to at most `analysis.max_fps` (30) analysis frames per second. The SHA-256 of the complete file is
computed for the `.motion` header.

## 2. Features, optical flow, scene cuts

* **Features**: Shi–Tomasi corners detected on a `grid_cols × grid_rows` (12 × 8) grid,
  `features_per_cell` (6) per cell, so features cover the whole picture instead of clustering on one
  textured object. For 180°/360° video, areas beyond `pole_limit_deg` (72°) latitude are masked
  (equirectangular poles are strongly distorted).
* **Tracking**: pyramidal Lucas–Kanade optical flow (21 × 21 window, 3 pyramid levels) from frame
  *k* to *k+1*, then back again; a track is kept only if the forward–backward error is below 1 px.
  On 360° video, flow across the left/right seam is unwrapped.
* **Scene cuts**: Bhattacharyya distance between 64-bin grey histograms of consecutive frames above
  `cut_threshold` (0.45), or a *sudden* tracking collapse: fewer than 20 % of the features survive
  **and** fewer than 0.3 × the median survival of the preceding second. Fast motion through sky,
  fog or motion blur keeps the survival low for many frames; that is low confidence, not an edit.
  No motion is synthesised across a cut.

## 3. Camera rotation and translation (ego-motion)

Image points are converted to unit **bearing vectors** in the camera frame (x right, y down,
z forward):

* flat: pinhole model, focal length `f = (W/2) / tan(HFOV/2)` from the configured horizontal field of
  view; `b ∝ ((u − W/2)/f, (v − H/2)/f, 1)`;
* 180°/360°: equirectangular, `lon = (u/W − ½)·span`, `lat = (½ − v/H)·π`,
  `b = (cos lat · sin lon, −sin lat, cos lat · cos lon)` (span = π or 2π).

For static scene points, consecutive bearings satisfy `b₂ ∝ R·b₁ − R·T/depth`.

**Model selection.** A rotation-only model is fitted first: 2-point RANSAC with a weighted Kabsch
(SVD) solution. If it explains at least 50 % of the points (distant points dominate, so translation
parallax does not bias the rotation), it is used. Otherwise a rotation + translation model is fitted:
8-point essential-matrix RANSAC on bearing vectors (Sampson-type epipolar error), decomposition
into R and t with a cheirality check (points in front of the camera).

**Translation.** With R known, the translation direction t satisfies the epipolar constraint
`t · (R b₁ × b₂) = 0` for all inliers; t is the null vector (RANSAC + least squares), its sign
chosen from the expansion of the de-rotated flow (forward motion makes points move away from the
focus of expansion). Monocular video has no metric scale: the speed is a scale-free proxy |T|/depth
from the de-rotated flow magnitude. Each frame also gets a **confidence** (inlier fraction, number of
tracks).

Rotation rates (rad/s) and the translation proxy are transformed to the platform frame
(X forward = camera z, Y left = −camera x, Z up = −camera y).

## 4. Signal cleaning

Per-frame estimates are resampled to the motion sample rate, outliers are removed with robust
statistics (median/percentile scaling), low-confidence frames are attenuated, and zero-phase
Butterworth filters (forward–backward, no delay relative to the picture) separate low- and
high-frequency content.

## 5. Event analysis and classification

From rotation rates ω and the velocity proxy v the analysis derives: forward speed (forward and
backward motion), longitudinal acceleration/deceleration, **lateral and vertical acceleration**
including the centripetal terms of the rotating vehicle frame (a = dv/dt + ω × v with v along X):

* lateral `a_y = v_x · ω_yaw` — towards the inside of a turn;
* vertical `a_z = −v_x · ω_pitch` (+pitch = nose down) — dips, valleys, loops and pull-ups push the
  rider into the seat (upward), crests and drops lift the rider out of it (downward). On roller
  coasters this vertical load is the dominant sensation; it becomes heave onset cues;

banking (roll with turning), vibration level (band-passed high-frequency rotation/translation
energy) and impact-like events: isolated spikes in the change of motion, with a robust z-score
above 6 against the whole programme **and** above 5 against the preceding, the surrounding and the
following 10 s — a continuously rough or fast section, or its onset, is not a series of impacts.

Every instant gets one class (1 s analysis windows, then a median filter so classes do not flicker):

| Class | Rule (simplified) | Typical content |
|---|---|---|
| `cut` | within ±0.5 s of a scene cut | edits |
| `static` | rotation RMS < 0.02 rad/s and not moving, or low confidence | tripod shots, titles |
| `rapid` | rotation RMS > 0.8 rad/s, or acceleration > 3 × the programme's median while moving | rides, roller coasters, action |
| `flight` | sustained forward motion, roll/pitch-dominant rotation, low vibration | flights, drones |
| `vehicle` | sustained forward motion, yaw-dominant rotation | cars, trains, boats |
| `handheld` | everything else | walking, handheld wobble |

Context rule: a `handheld` stretch of at most 10 s between two ride-class stretches (`vehicle`,
`flight`, `rapid`) becomes `vehicle`. The camera mounting does not change within seconds without an
edit; on ride videos, calmer stretches with distant scenery otherwise look like handheld footage
(the monocular speed proxy is small there) and would be attenuated. Static stretches and cuts are
kept (real stops and edits).

Detected events (turn, banking, acceleration, deceleration, dip, crest, impact, cut) with time and
magnitude are stored in the `.motion` header (`generation.events`) together with the class segments.
`dip` marks the onset of an upward vertical load (valley, loop), `crest` of a downward one (hill
top, drop).

## 6. Perceptual cueing (classical washout with tilt coordination)

A platform with ±10 cm and ±10° cannot reproduce real motion, so the extractor synthesises the
**vestibular cues** a passenger would feel (Reid & Nahon's classical washout structure):

1. **Specific force** — the acceleration proxy is normalised so its 95th percentile corresponds to
   `peak_specific_force` (1.5 m/s²), then multiplied by the class gain schedule.
2. **Translational onset cues** (surge, sway, heave) — high-pass filtered specific force, double
   integrated, with a further washout so the platform always drifts back to neutral:
   `x(s)/a(s) = s / ((s² + 2ζω s + ω²)(s + ω_b))`, ω from `translation_hp_hz` (0.5 Hz; ×1.5 for heave),
   ω_b from `translation_washout_hz` (0.12 Hz), ζ = 1.
3. **Tilt coordination** — the low-frequency part (`tilt_lp_hz` 0.35 Hz) of the longitudinal and
   lateral specific force is rendered by tilting the seat so gravity supplies it:
   `pitch = −asin(f_x / g)`, `roll = asin(f_y / g)`, limited to `max_tilt_deg` (7°), rate-limited to
   `tilt_rate_deg_s` (3°/s, below the rotation-perception threshold) and Gaussian-smoothed (no
   overshoot). Sustained acceleration therefore feels like acceleration, not like tilting.
4. **Rotational cues** — camera rotation rates × `rotation_gain` (0.6), high-pass "washed out" and
   integrated: `angle(s)/rate(s) = s / (s² + 2ζω s + ω²)`, ω from `rotation_hp_hz` (0.25 Hz) for
   roll/pitch and `yaw_hp_hz` (0.35 Hz) for yaw. Turns and banks are felt at their onset and the
   seat returns to neutral.
5. **Texture** — vibration (`vibration_heave_m` 2 mm, `vibration_rotation_rad` 0.004 rad at full
   level) and impacts (`impact_heave_m` 12 mm, `impact_duration_s` 0.15 s), scaled by
   `vibration_gain`/`impact_gain`.
6. **Class gain schedule** — per class (translation/tilt, rotation, texture) multipliers, smoothed
   over about 1 s so gains never step:

   | Class | translation/tilt | rotation | texture |
   |---|---|---|---|
   | static, cut | 0 | 0 | 0 |
   | handheld | 0.15 | 0.25 | 0.2 |
   | vehicle | 1.0 | 0.8 | 1.0 |
   | flight | 0.8 | 1.0 | 0.3 |
   | rapid | 1.0 | 1.0 | 1.0 |

7. Sum of the channels × `global_gain` × per-axis gains, and a fade-in/out envelope of `fade_s`
   (1.5 s) at the programme start and end (the programme loops seamlessly from neutral to neutral).

## 7–8. Filtering, scaling and platform constraints

With the platform profile (`constraints.py`):

1. zero-phase low-pass to the platform bandwidth (`bandwidth_hz`, 6 Hz);
2. per-axis **soft workspace compression** to `position_margin` (92 %) of `limits.position` — a smooth
   saturation (tanh-shaped), not clipping, so peaks are rounded rather than flattened;
3. iterative **local gain reduction**: wherever velocity, acceleration or jerk of an axis exceed
   `derivative_margin` (85 %) of the limit, a smooth gain envelope (held minimum + Gaussian
   smoothing) reduces only that stretch of motion; the same for the **combined workspace** (inverse
   kinematics: every actuator inside its usable stroke) and for actuator velocity/acceleration
   (`actuator_margin` 85 %);
4. repeat until everything passes (at most `max_iterations`, 25). Timing is never changed — only
   intensity, locally and smoothly.

## 9. Validation

The final trajectory is differentiated (velocity, acceleration, jerk) and checked against every
limit of the profile and against the actuator stroke, velocity and acceleration through inverse
kinematics (validation.py). The report (also stored in the `.motion` header) lists maxima, limits
and violations; export is refused when it fails. This is a numerical check of software limits, not
a safety assessment (README.md).

## Limitations

* Monocular video has no absolute scale: speeds and accelerations are relative; the content team
  sets the intensity with the gains and the preview.
* Scenes dominated by independently moving objects (crowds, water, close traffic) can disturb the
  ego-motion estimate; low-texture scenes (sky, fog, darkness) give low confidence and little motion.
* Rolling-shutter wobble and image stabilisation in the source video change the apparent motion.
* Very short jolts (shorter than about a quarter of a second) are removed as estimation outliers
  together with tracking glitches, so they do not produce impact cues.
* Sustained attitude is not cued: the video gives rotation *rates*, and integrating them to an
  absolute attitude drifts (on the roller-coaster test video the integrated pitch was off by tens of
  degrees after the lift hill). A long climb (lift hill) is therefore cued at its onset and then
  washed out, not held as a sustained back-tilt. Lower `rotation_hp_hz` to hold rotational cues
  longer.
* Parts of the vehicle in the picture (car front, dashboard, cockpit frame) move with the camera.
  The robust estimation treats them as outliers while most tracked features are on the scene
  (on the test ride, masking the visible car front changed the integrated rotation by about 1 %).
* Fast spins in 360° video near the poles are partly masked (pole limit).
* Automatic classification is rule-based and can be wrong for unusual content; check segments in
  the preview and adjust gains or class gains (settings JSON) if needed.
* Review every programme on the real platform with test riders before public use.
