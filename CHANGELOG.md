# Changelog

All notable changes are documented here. The project uses semantic versioning. Versions before
2.0.0 are those of the two products this one replaces.

## 3.2.0 - 2026-11-12 — the venue: patch window, fixture library, devices and DMX with the film, "is it ready?"

What a venue's technician did in a text editor and with a device's manual now has a window and a
library, and one report says whether the venue is ready. Programmes, the timeline, the show
service, the firmware and every file format are those of 3.1; a venue patch gained three optional
keys (docs/VENUE.md, docs/MIGRATION.md).

### Fixture library (new)

* **653 real DMX devices** of 138 makers with their modes and channels, from the Open Fixture
  Library (MIT) as of 11 November 2026, inside the package (230 KB) - no network needed. Fixture
  files of your own, or a newer copy of the whole library, are read from a folder.
* **`fourdvr fx fixtures search | show | entry | library`**: find a device, see what each of its
  channels would get for an effect, and get the `[[fixtures]]` entry - printed, or appended to a
  patch file at the first free address, only if the patch still loads with it.
* What is decided for you: the channel the effect drives and its value range (fog output for fog,
  speed for wind, the dimmer otherwise; the colours of a light without a dimmer), colours and
  shutter of a light, resting values, one output of a dimmer or relay pack, 16-bit channels.
* **A strobe flashes itself only if it can be as slow as the venue's limit** (3 flashes a second
  unless assessed otherwise); else its dimmer is switched by us, and a strobe with unknown rates
  and nothing to switch is refused. `check-patch` now warns about a hand-written strobe whose
  slowest rate is above its limit.
* Not read: pixel matrices and modes of more than 32 channels (765 of the 2 904 modes).

### The venue window in the Editor (new: *Live ▸ Venue…*, Ctrl+Shift+D)

* **The patch, made and changed in a window:** the output (sACN, Art-Net, USB DMX, ESP32 boards),
  the boards - **found on the network** and given to their platforms with a double-click - and
  the devices, generic ones or from the fixture library, with every address checked as it is
  typed. Save writes the file as a person would and makes it the Editor's venue patch.
* **Devices with the film, without hardware:** one tile per device as a generic stand-in for what
  it is used for (a fan turns, fog thickens, a strobe flashes at its rate, drops fall, a heater
  glows), with its level, the values sent and what holds it back.
* **Live DMX monitor:** the 512 channels of a universe, each value as a bar and a number, the
  channels of a device in its effect's colour.
* Both follow the playhead. Without LIVE this is a **simulation**: the same effects service as in
  the theatre, with the same limits, sending to nobody. With LIVE it is what goes to the devices.

### "Is the venue ready?" (new: `fourdvr fx ready`, and a button in the venue window)

* One report: the patch; the DMX output (Art-Net nodes that answer a poll); every ESP32 board -
  does it answer, **how long an answer takes, how many were lost**, has it what the patch expects,
  emergency stop, its error counters - **and that its DMX line goes dark by itself when the
  signal stops**; the sound output; the Control Server's **headsets and platform controllers**;
  what the venue does with a programme. READY or NOT READY, exit code 0 or 1, text or JSON.
* Nothing is switched on. The signal-loss test sends the frame of a venue at rest and is left out
  while a show service or the Editor is sending.
* `fourdvr fx nodes --find` asks every board on the network segment instead of named ones.

### Changed

* The limits check no longer says "no fixture for effect type 'vibration'" and "[audio] is not
  enabled" for a venue that plays bass and seat vibration through its ESP32 boards.
* Venue patch: optional keys `model` and `mode` on a fixture and `name` on a channel. **A show
  service older than 3.2 refuses a patch that has them** - update the show PC with the workstation.

### Fixed

* The program could end with a crash message ("Segmentation fault") after its window had been
  closed and everything was saved. Widgets that were put into a row before the row was put into its
  window stayed with Python as well as with the window, and were destroyed twice at the very end.
  Found with the new windows, whose test runs ended that way two times in six; the Editor's own
  window has had thirteen such widgets since 3.0, though it was never seen to crash. Widgets are
  now handed to their windows, and the windows are destroyed in order when the program ends.

### Tests

* 20 new automated tests (350 in all). The ESP32 end-to-end test (27 checks) now also runs the
  venue check against the firmware's own code: with the show service running it leaves the lines
  alone; with nobody sending, each line is seen to go dark 1.1 s after the frames stop.
* Not tested: any real device with a library entry, any board, any Art-Net node, any theatre's
  network (docs/VENUE.md §5).

## 3.1.0 - 2026-11-12 — live mode and streaming

Motion, effects and bass can now come from something that is happening, not only from a file: the
telemetry of a running game or flight simulator, a captured screen or stream, or sound. And the
same program can offer videos to any device on the network, on one clock. Nothing that existed
changes: programmes, the Editor, the show service, files and configurations are as in 3.0
(docs/MIGRATION.md).

### `fourdvr live` (new)

* **`fourdvr live run`** drives the platforms, the DMX effects and the shakers from a live source,
  on the show PC, without the Control Server. Each platform is the show service's own runtime
  (link supervision, fault handling, limiter, kinematics, actuator guard) following a live pose;
  effects go through the unchanged effects engine with the venue patch and its limits.
* **Sources:** game telemetry in the formats their makers publish — Forza Data Out (Motorsport and
  Horizon), EA SPORTS F1 23 / 24 / 25, Codemasters "extradata 3" (DiRT Rally and others), Project
  CARS 2 / Automobilista 2, Assetto Corsa, Live for Speed OutSim / OutGauge, BeamNG MotionSim,
  X-Plane data output, FlightGear (with a protocol file this program writes) — Gran Turismo 7 as
  an experimental source (its format is community knowledge, not published by its maker); an
  **open format** of our own for any experience, with ready senders for Godot, Unity and Python;
  a **captured picture** (screen, capture card, stream, video file); **sound**; a recorded
  session; a built-in demonstration drive. docs/TELEMETRY_FORMATS.md says for every value where
  it comes from and whether its direction is stated, conventional or assumed.
* **Real-time cueing** with the transfer functions of the offline cueing (tested to give the same
  output), soft saturation, and a per-platform soft range before the limiter.
* **Safety net for an unchecked source:** values that are not numbers are refused, extreme ones
  cut; when the source stalls, pauses or is held, motion and effects fade to rest and come back
  only after the source has been steady again; a platform fault waits for the operator; a source
  can never command a pose or a DMX value directly; a stated strobe is ignored unless the venue
  allows it.
* **Effects from telemetry:** wind from speed, seat vibration from engine and road, an air blast
  at a hit, water spray from puddles, rain and snow from the game's weather, and whatever an
  open-format sender states. **Shaker sound** is made as the show runs (engine tone following the
  revs, road rumble, thumps) and goes to the sound card or the ESP32 audio nodes.
* **A picture as source** uses the extractor's tracking frame by frame. Its delay is handled in
  one of two ways: *delay* — the live window shows the picture a set time late, exactly when the
  motion that belongs to it arrives — or *direct* — the picture is not ours, the fast cues are
  turned down. Masks for HUD and cockpit, rest on menus and black screens, optional live scene
  detection, a test pattern to measure the capture delay (`fourdvr live calibrate`).
* **The live window** (`--window`; Editor: *Live ▸ Live show from a game or a picture…*): the
  delayed picture or, without a picture, traces of what the platform is asked to do; state, axes,
  effect levels, platforms; the operator's keys (hold, strength, delay, engage, record). The show
  itself runs in a second process that the window starts: grabbing and painting cannot delay the
  platforms' commands, and a window that is closed or dies ends the show at rest.
* **`fourdvr live monitor`** prints what a source says in plain words ("braking", "pushed left",
  "nose turning left") for checking a game's directions; `[axes]` turns what is the other way
  round.
* **`fourdvr live sim`** plays a game's part in any telemetry format, to test a venue's network
  and configuration without the game.
* **Recording and programmes:** a session is recorded as a `.4dlive` file; `fourdvr live convert`
  and *File ▸ Open* in the Editor make a `.motion` programme of it with the offline cueing, the
  fit to the platform and the validation, the session's effects as tracks and the shakers' sound
  as bass track. Given a video of the session (`--video`; the Editor asks), the programme is made
  for that video — its length and reference, as the Control Server requires — and where the video
  begins in the session is found from the picture (`fourdvr live align`, or `--offset` by hand).

### `fourdvr stream` (new)

* **`fourdvr stream serve`** offers videos over HTTP to any device on the network (docs/STREAMING.md):
  a page for browsers — flat, 180° and 360° video, mono or stereo, with looking around by
  dragging, by a phone's sensors and, in a headset's browser, in VR (both over https) — and each
  file's address for any player. Files are sent as they are (byte ranges, the operating system's
  sendfile): the computer hardly matters, the network decides how many viewers there can be.
* **One clock:** everybody who has the page open sees the same moment. Play is announced a few
  seconds ahead; each device compares its clock with the server's and steers its player. An
  operator's page (play, cue, pause, jump, loop; who is watching and how far off each device is),
  the same as commands (`fourdvr stream control`), and `--play --loop` for a screen without an
  operator.
* **In step with the theatre:** `--follow` takes the timeline from the Control Server — unchanged,
  with a viewer's account — so a phone or a wall screen shows the programme at the position the
  headsets and platforms are at.
* **Tools:** `check` (what a video is, whether browsers play it, what it asks of a network),
  `prepare` (index to the front without re-encoding, or H.264 for every browser), `test-video`
  (a clock, a sweeping bar and a beep: are two screens in step?), `probe` (simulated viewers: does
  the network carry them?), `cert` (a certificate of one's own for https).
* Needs nothing beyond the smallest installation; a systemd unit is in `deploy/systemd/`. In the
  Editor: *Live ▸ Stream the video to the devices on the network…*.

### Other changes

* The bass outputs (sound card and ESP32 audio nodes) can play a live feed beside or instead of a
  programme's bass track.
* The Editor opens `.4dlive` files and has three new entries in the *Live* menu.
* 133 new automated tests (330 in all) and two more end-to-end tests:
  `integration/run_live_integration.py` (a simulated game over real UDP, two simulated boards,
  real sACN, and the Control Server accepting the programme made of the recorded session) and
  `integration/run_stream_integration.py` (a stream server, fifty simulated viewers, four real
  browsers, and a second stream server following the Control Server's show).

### Not tested

No real game, console, simulator, capture card, camera, stream or sound input was available: the
telemetry decoders are tested against packets built from the published layouts, the picture path
against a rendered camera rotation and a ride video, everything else against simulations. The
directions of each game's values must be checked once with `fourdvr live monitor` (docs/LIVE.md §4).
The Godot and Unity senders were not run inside those engines.

Streaming was tried with simulated viewers and with headless Chromium only: no real phone, tablet,
television or headset, no other browser, no real network. VR mode in a headset's browser and
looking around by a phone's sensors are untested on devices (docs/STREAMING.md §8).

## 3.0.0 - 2026-11-11 — Extended 4D Editor

The program has a new name, looks at what a video shows to draft its effects, is organised around
one timeline for motion, vibration and effects, and can send the effects through the ESP32 boards.
Files, projects, patches, configurations and commands of 2.0 keep working (docs/MIGRATION.md).

### Name

- **Extended 4D Editor** instead of 4D VR Theatre Studio: window, AppImage
  (`Extended-4D-Editor-3.0.0-x86_64.AppImage`), source package, Python distribution
  (`extended-4d-editor`; uninstall `fourdvr-studio` first), desktop launcher (`extended-4d-editor`).
  The commands (`fourdvr …`) and the Python modules keep their names.

### Scenes in the video → effects (new)

- While a video is analysed, one picture a second is scored for rain, fog or smoke, fire, bubbles /
  underwater, snow, splashing water, forest, sea and flowers, and the sound track for rain,
  thunder, fire, explosions, splashing, sea, bubbling and wind — by two neural-network models that
  are inside the AppImage and run on the processor. No network, no service; nothing leaves the
  computer. 180° and 360° video: the forward view is looked at.
- From the scenes, effects are drafted: rain, fog bursts, heat for fire, bubbles, snow, water
  spray, scent (cartridge 1 forest, 2 sea, 3 flowers). Sound alone drafts nothing. Together with
  the existing suggestions (wind, seat vibration, air blasts, strobe) every effect lane that is
  still empty gets a draft.
- Four more built-in effect types with default limits: `rain`, `heat` (warm air or a heat lamp for
  fire scenes), `bubbles`, `snow`. "Fog" is labelled "Fog / smoke". The effects document format is
  unchanged (the type of a track has always been a name).
- What was seen and heard is shown in the Effects tab, printed by `extract generate --effects
  suggest`, and saved with the project. `--no-scenes` / *Look for effect scenes* switches the search
  off; the motion analysis is bit for bit the same with and without it.
- Picture model: CLIP ViT-B/32 (OpenCLIP, LAION-400M), image part, 176 MB; sound model: Zipformer
  audio tagger (icefall / sherpa-onnx, AudioSet), 27 MB. `packaging/models/build_models.py` builds
  both from pinned downloads with checksums, without PyTorch, reading the published weights with a
  reader that accepts numbers only, and checks the result against its own reference
  implementation. docs/SCENE_DETECTION.md says what was measured and what was not.

### The window

- Four numbered buttons: **1 Open…** (video, project or `.motion` file), **2 Analyse video**
  (analysis, motion inside the platform's limits, validation, and the draft of the effects, in one
  go), **3 Check** (motion against the platform's limits, effects against the venue's or the
  default limits), **4 Export .motion…**. A line next to them says what comes next.
- One timeline in three groups — **Motion**, **Vibration and sound**, **Effects** — that fold away
  with a click on their heading (`M`, `E`).
- Motion: an intensity lane and a lane for each of the six axes, always visible. Each axis lane
  shows what the platform does on that axis (to the scale of the axis' travel) and carries what is
  drawn by hand on top.
- Clicking a lane shows its options next to the timeline: what the lane is, the limit that applies
  to it (the venue's, or the default), and what can be set — the amount of automatic motion per
  axis, gestures, the bass track's settings on the sound lane, the parameter of new cues (flash
  rate, scent cartridge), DMX address of a raw channel, *Scale…* for a whole effect lane.
- The motion follows the settings by itself: a changed gain or axis amount is applied half a
  second later (`Ctrl+R` for long programmes). The separate Generate / Process / Validate buttons
  are gone.
- A second analysis keeps every lane that has something on it and drafts only the empty ones.
- The limit check also works without a venue patch (default limits) and runs after every edit;
  red marks under a cue show what a limit will cut.
- Live tab: with an ESP32 patch, a table of the boards — which platform or the theatre each
  serves, its address, what it has, whether it answers.
- *Help ▸ How to make a programme*.

### Effects through ESP32 boards (new, optional)

- Venue patch `protocol = "esp32"`: the show service sends DMX levels and the bass / seat-vibration
  sound to ESP32 boards, which put them on a DMX512 line and an I2S audio output. Every platform's
  board has a DMX line of its own; the theatre's devices are on a named board. The show service
  takes each platform's board from its motion link.
- A link of its own, "4DFX" 1.0 on UDP port 47002, beside the unchanged motion link. Boards say
  what they have; the service sends just that. A board that does not answer, has no DMX line or no
  audio output, or a platform without a board is left out silently and looked for again every
  second — motion and all other boards are not affected. Platform status lists the boards.
- Sound goes out as a numbered 16-bit stream at the board's rate (4 kHz), ahead of time by the
  board's buffer; a platform's board plays that platform's show.
- Firmware 1.1.0 (`firmware/esp32-actuator-bridge`): build variants with actuators, a DMX line
  and an audio output in any combination; an effects-only board needs no platform. The effects
  node is a portable core (`lib/fourdvr_fx`) with host tests, shared test vectors and a PC
  simulator; the line goes dark after a second without data, and dark and silent while the
  board's emergency-stop input is active. **Tested on a PC only: not compiled for or run on an
  ESP32** (docs/ESP32_EFFECTS_LINK.md).
- Tools: `fourdvr fx nodes`, `fourdvr fx example-patch --esp32`, `fourdvr fx test-vectors`,
  `fourdvr controller esp32-sim --dmx-ports N --audio-channels N`; `check-config` and
  `check-patch` print which board carries which devices.
- `integration/run_esp32_integration.py`: Control Server, three platforms, the firmware's own
  code as the boards behind real UDP — DMX equal to the programme, the bass in step, emergency
  stop at a board, a board unplugged and plugged in again, a platform without a board.

### Other changes

- `fourdvr fx check` works without `--patch` (default limits).
- Every command ends quietly when the reader of its output stops early (`… | head -3`,
  `… | grep -q`); before, a "Broken pipe" error line could appear.
- `extract generate`: scenes are looked for only with `--effects suggest`; new `--no-scenes`.
- Suggestions never replace a hand-made second lane of a type, as before; the automatic draft
  after an analysis additionally never touches a lane that has anything on it.
- AppImage: the scene models and ONNX Runtime are inside (453 MiB instead of 259 MiB); it still
  needs no network at any time. ONNX Runtime is held below 1.29, because later Linux builds contain
  a telemetry client; the build refuses such a library and `ORT_DISABLE_TELEMETRY=1` is set as well
  (THIRD_PARTY_LICENSES.md). New build options `WITHOUT_MODELS=1`, `MODELS_DIR`, `MODELS_CACHE`.

### Compatibility

- `.motion` 1.0, effects document 1.0, server protocol, motion link, platform profiles, controller
  configuration and venue patches are unchanged. Projects of 2.0 open in 3.0 and the other way
  round.
- Venue patches that used `bubbles` or `snow` as venue-defined types: these names now have default
  limits for values the patch does not set (docs/MIGRATION.md).
- The motion pipeline is the code of 2.0: the same analysis and settings give the same motion.

## 2.0.0 - 2026-11-11 — 4D VR Theatre Studio

One program and one AppImage instead of Motion Extractor and Motion Controller, and 4D effects.

### One program

- `fourdvr`: the Studio (desktop application) without arguments, `fourdvr extract …`,
  `fourdvr controller …` (the show service), `fourdvr fx …`. `fourdvr-extract`, `fourdvr-motion`
  and `fourdvr-extractor-gui` remain as names for the same things.
- One source package with installation profiles: `pip install .` (show service: NumPy, Pydantic,
  websockets only), `".[show]"` (plus sound-card and USB/serial output), `".[studio]"` (everything).
  The show service does not load the desktop toolkit, video analysis or SciPy (tested).
- One AppImage, `4D-VR-Theatre-Studio-2.0.0-x86_64.AppImage`; symbolic links named
  `fourdvr-motion` / `fourdvr-extract` to it behave as the former commands.
- The duplicated modules of the two products (`motionfile`, `kinematics`, `platform_config`) are
  one implementation now.

### Effects (optional everywhere)

- Effects document `4D-VR-THEATRE-EFFECTS` 1.0 (docs/EFFECTS_FORMAT.md): tracks of cues (with
  level and fades) and level curves for wind, water, air, fog, strobe, scent, seat vibration, raw
  DMX channels and venue-defined types, plus a bass track (low-passed programme sound, mono,
  2 kHz). It is embedded in the `.motion` header (key `effects`), so a programme stays one file
  that the unchanged Control Server 1.0.0 validates and distributes; standalone as `.fx.json`.
- Venue patch (TOML, docs/EFFECTS_GUIDE.md): fixtures per effect type, theatre or per-platform
  scope, DMX addressing, channel roles (level, switch, rate, static), lead times, cartridge
  matching for scent, operator switches and masters per type.
- Safety limits per fixture, enforced after the programme is evaluated: maximum level, longest
  activation, pause after an activation, duty cycle, total per show run, soft start/stop, strobe
  flash-rate ceiling (default 3 per second). Conservative defaults per effect type. Strobes that
  the software flashes keep their ceiling in real time — also across cues, seeks, loops and a
  dragged playhead in the Studio — and lose no flash when a DMX frame is late.
- Outputs: sACN / E1.31 (multicast and unicast, universe discovery, stream termination), Art-Net,
  USB DMX (Enttec DMX USB Pro compatible; re-opened automatically after unplugging), and a
  sound-card output for the bass track and a synthesised seat-vibration tone, locked to the show
  timeline (re-synchronised with a cross-fade beyond 30 ms drift, soft-limited).
- Show service: optional `[effects]` section. Platform fixtures follow their own platform and are
  off unless it is playing; theatre fixtures and the bass follow a reference platform. Everything
  goes dark on pause, stop, fault and shutdown. The effects run in their own thread; a wrong patch,
  an unreachable node, a missing sound card or a damaged effects section is reported in the
  platform status and never affects motion. `check-config` prints the venue's address table;
  `motion-info` lists a file's effects.
- Tools: `fourdvr fx info / extract / embed / strip`, `example-patch`, `check-patch`,
  `check` (preflight: a simulated show run of a programme through a venue's patch — what is
  limited, what has no device), `play`, `fire`, `blackout`, `monitor` (shows arriving sACN /
  Art-Net), `devices`.

### Studio

- The Motion Extractor window became the Studio: GitHub-dark look, JetBrains Mono (bundled, OFL),
  menus, a transport bar and a timeline with reference lanes (motion, sound and bass envelope,
  scene cuts, impacts), manual-motion lanes and effect tracks; an inspector for the selection;
  undo/redo; copy/paste; snapping; projects (`.4dvr`, including the analysis, so a project reopens
  without analysing again).
- Manual motion: an intensity curve (×0 … ×2 of the automatic motion) and curves for the six axes,
  with fourteen ready-made gestures, eased between points and applied before the constraint stage —
  hand-made motion passes the same limits and validation. Optional programmes with hand-made
  motion only.
- Sound track: decoded with PyAV (LFE channel of surround files included) or an `ffmpeg` program;
  waveform and bass envelope on the timeline; bass track built automatically, or from another file.
- Suggested effects from the analysis: wind from forward speed, seat vibration from camera shake
  and bass, air blasts at impacts, strobe where the picture flashes. Water, fog and scent are never
  suggested.
- Live output: the Studio drives the venue's devices through the same engine as the show service
  while playing; hold-at-playhead, per-fixture *Fire*, monitor table, blackout; sound-only preview
  without a patch. Venue check with the limited passages marked on the timeline.
- Existing `.motion` files can be opened to add or edit effects; the motion is copied bit-exactly.
- `fourdvr extract generate` gained `--effects suggest`, `--bass`, `--fx`.

### Unchanged

- Motion: extraction produces bit-identical samples from the same analysis as Motion Extractor
  1.1.0; the show service's motion pipeline is the code of Motion Controller 1.0.1 (identical
  `play-local` result on the test programme; platform poses bit-identical with and without
  effects).
- `.motion` format 1.0, server protocol, ESP32 link protocol and firmware.

### Tested / not tested

- 171 automated tests; end-to-end run through the Control Server 1.0.0 to real sACN packets on a
  network interface with frame-by-frame comparison (`integration/run_effects_integration.py`).
- Not tested: physical DMX nodes and effect devices, a physical USB DMX interface, a physical
  sound card with amplifier and shakers. See README.md.

---

## Motion Controller 1.0.1 - 2026-11-03

- Control loop: every tick samples the motion track at its *scheduled* instant instead of the
  wake-up time (wake-up jitter appeared as velocity/jerk noise and triggered the run-time limiter
  on validated files).
- `fourdvr-motion esp32-sim` ends without a traceback on Ctrl+C / SIGINT.
- Packaging: single-file offline AppImage.

## Motion Controller 1.0.0 - 2026-11-02

- First release: motion controller runtime for up to 20 six-DOF Stewart platforms; shared
  timeline playback of `.motion` files with PCHIP interpolation, smooth blends, hard limits on
  pose and actuators; Stewart inverse kinematics; servo/electric driver over UDP or serial to the
  ESP32 actuator bridge (binary protocol 1.0); simulation driver; ESP32 firmware with host-side
  unit tests. Hydraulic and pneumatic drivers are not implemented.

## Motion Extractor 1.1.0 - 2026-11-03

- Vertical load from the vertical centripetal acceleration (dips, valleys, loops, crests);
  scene cuts only on a severe *and* sudden tracking collapse; NaN-safe classification; short
  handheld stretches inside a ride are treated as ride; impacts must stand out locally;
  `generate -o DIR/FILE` creates and checks the directory before the analysis; offline AppImage.

## Motion Extractor 1.0.0 - 2026-11-02

- First release: offline desktop application and CLI that derive perceptual motion cues from a
  programme video and export `.motion` files; deterministic ego-motion estimation for flat, 180
  and 360 video; classical washout and tilt coordination; platform-profile constraints and
  validation; 3D Stewart platform preview.
