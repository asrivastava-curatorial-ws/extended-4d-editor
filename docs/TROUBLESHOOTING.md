# Troubleshooting

First steps:

```bash
fourdvr --version
fourdvr fx info programme.motion                                   # what is in the file
fourdvr fx check programme.motion --patch patch.toml               # what the venue does with it
fourdvr fx nodes --patch patch.toml --platforms PLATFORM-01,…      # effects through ESP32 boards: who answers
fourdvr controller check-config -c /etc/fourdvr-motion/controller.toml
sudo systemctl status fourdvr-motion
sudo journalctl -u fourdvr-motion -n 200 --no-pager
```

On the Control Server dashboard, click a platform tile: state, fault reason, errors, link
counters, limiter activity, clock offset — and, with effects configured, the `effects` block
(EFFECTS_GUIDE.md §9).

## Installation and start

| Problem | Fix |
|---|---|
| `error: the Editor window needs the workstation packages` / `motion extraction needs …` | this installation has only the show service. `pip install 'extended-4d-editor[studio]'` (or use the AppImage) |
| "Could not load the Qt platform plugin xcb" (Linux, installed with pip) | install the XCB libraries listed in INSTALL.md §2, or use the AppImage |
| Editor on a remote/headless machine | use the command line (`fourdvr extract generate …`, `fourdvr fx …`), or X11 forwarding |
| Windows: `fourdvr` not found | activate the virtual environment (`…\Scripts\Activate.ps1`); allow scripts with `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| AppImage: an error about FUSE or `fusermount` | install `fuse3` (or `fuse`), or run with `--appimage-extract-and-run`, or extract once with `--appimage-extract` (APPIMAGE.md) |
| AppImage: `GLIBC_2.34 not found` | the distribution is older than the minimum (Ubuntu 22.04, Debian 12, RHEL 9); install with pip instead |
| The text is not in JetBrains Mono | the font files are missing from the installation (`fourdvr_studio/gui/fonts`); reinstall. The Editor then uses the system's monospace font |

## Opening video, analysis, motion

| Message / symptom | Cause / fix |
|---|---|
| `cannot open … (unsupported container or codec)` | the decoder cannot read the file (e.g. ProRes RAW, some 10-bit HEVC). Make the delivery file first and **generate from the file you will upload**, e.g. `ffmpeg -i master.mov -c:v libx264 -crf 18 -pix_fmt yuv420p show.mp4` |
| `invalid frame rate` | variable/unknown frame rate; remux with ffmpeg (`-r 30`) |
| Wrong projection/stereo guessed | the guess uses only the aspect ratio; set it in the *Programme* tab or with `--projection/--stereo` |
| Almost no motion | static or handheld content is attenuated by design; dark or texture-less scenes give low confidence. Raise gains, or add motion by hand (USER_GUIDE.md, *Motion*) |
| Motion in the wrong direction | wrong projection/stereo/HFOV, or a mirrored video |
| Motion too strong / constantly limited | lower Global gain or Peak specific force; check that the platform profile is the right one |
| Status line: "the safety margins were not fully reached everywhere" | the constraint stage stopped after its last pass with some values still above the *margin* (85 % of a limit by default), not above the limit. **3 Check** shows the actual use of every limit (Validation tab); a PASSED validation is what counts. Lower the gain if you want more headroom |
| A manual gesture comes out smaller than drawn | the platform cannot follow it that fast: the constraint stage reduced it. Make it longer (drag its points apart) or smaller |
| Manual motion or a changed gain does not change the preview | the traces on the axis lanes are dashed until the motion is processed again — automatic after about half a second for programmes up to ten minutes, otherwise `Ctrl+R` (*Motion ▸ Update the motion now*) |
| The six axis lanes take all the room | click the heading **MOTION** (or press `M`) to fold them away; `E` folds the effect lanes |
| Motion across scene cuts | very soft transitions may not be detected as cuts; lower `cut_threshold` in a settings JSON, or set *Intensity* to 0 across the transition |
| Analysis is slow | reduce *Analysis width* (e.g. 480); long 8K files mainly cost decoding time |

## Sound track, bass track, suggestions

| Message / symptom | Cause / fix |
|---|---|
| "… has no sound track" | the video has no audio stream. Use *Bass from another file…* (click the lane **Sound · bass**) or `--fx` with a prepared effects file |
| "no audio decoder: install the 'av' Python package … or the ffmpeg program" | neither PyAV nor an `ffmpeg` program is available. `pip install av`, or `sudo apt install ffmpeg`, or use the AppImage (an AppImage built with `WITHOUT_PYAV=1` needs the `ffmpeg` program). With `--bass` this is an error before the analysis starts; with `--effects suggest` alone it is a note, and the suggestions are made from the picture |
| "the sound track has no low-frequency content worth a bass track" | below −50 dBFS after the low-pass: nothing to play on a subwoofer. That is a property of the sound mix |
| Bass of a 5.1 file is weak | with the `ffmpeg` program as decoder only the stereo mix-down is read; the LFE channel needs PyAV (in the AppImage and in `pip install ".[studio]"`) |
| *Suggest* finds no wind | no sustained forward motion of the camera was measured (pans, static shots). Draw the curve by hand |
| *Suggest* finds no strobe | no brief, strong brightening of the whole picture; a cut to a bright scene is deliberately not a flash |
| Exported file is large | the bass track: about 5 kB per second. Untick it for programmes that do not need it |
| "effects section too large for a .motion header" | more than 16 MiB, in practice a bass track of several hours: shorten or remove it |

## Scenes and drafted effects

| Message / symptom | Cause / fix |
|---|---|
| Effects tab: "Scenes cannot be looked for here: scene models not installed …" | an installation from source without the model files. Build them once (INSTALL.md §2, *Scene models*), or use the AppImage, which contains them |
| "… the onnxruntime package is not installed" / "kaldi_native_fbank" | `pip install "onnxruntime<1.29" kaldi-native-fbank` (both are in `".[studio]"`) |
| "the model files in … are of another version" | models built for another release: build them again with this release's `packaging/models/build_models.py` |
| "scene detection is not available: …" / "scene detection failed: …" | a model file is damaged or could not be loaded; the analysis itself is complete. Rebuild the models; with the AppImage, check its checksum |
| "the sound was not analysed: …" | the picture was scored, the sound model failed (the message says why); rain and fire then count only when clearly seen |
| Rain / fog / fire in the video, but no cue was drafted | *Effects* tab ▸ *Found in the video* shows what was seen, in seconds. Not seen: the scene is dark, short (under 2–3 s), behind the viewer in 360° video, or simply not recognised — add the cue by hand. Seen but no cue: the lane already had something on it (a second analysis fills only empty lanes; *Suggest effects again* replaces after asking) |
| A cue was drafted where nothing of the kind happens | a false alarm of the picture model (a fountain as rain, a sunset as fire …). Delete the cue. If it happens systematically with your material, switch *Look for effect scenes* off |
| Heat on every candle or fireplace | fire is fire to the model. Delete what is not wanted; the venue's limit keeps each activation short in any case |
| "Heard: sea, splash …" in a video without water | wind and ride noise sound like it to the sound model. Sound alone never drafts an effect |
| The same video gives slightly different cues on another computer | scores can differ in the last digits between processors; a passage right at the threshold may shift by a second |
| The analysis takes longer than in version 2 | scene detection adds about 0.1 s per second of video on a small machine, plus the sound. Switch it off for motion-only work (`--no-scenes`, or the switch in the Effects tab) |
| Does the analysis send my video anywhere? | no. Both models run on this computer's processor; the program opens no network connection for it (APPIMAGE.md §4) |

## Effects through ESP32 boards

| Message / symptom | Cause / fix |
|---|---|
| `fourdvr fx nodes`: "no answer" | the board is off or at another address; the firmware on it has no effects part (a platform-only build does not open port 47002); a firewall on the PC drops UDP 47002; `port` in `[output.esp32]` differs from `FDVR_FX_UDP_PORT` |
| A board answers, but "nothing" / no DMX line listed | the firmware was built without `FDVR_HAS_DMX` / `FDVR_HAS_AUDIO` (ESP32_EFFECTS_LINK.md §3) |
| Platform status shows a board with `present: false`, yet no error | intended: missing hardware is left out silently. The devices on that board do not play; everything else does |
| The Editor's LIVE mode or `fourdvr fx fire` reach no board, the show service does | they know no motion links: list every board in `[output.esp32.platforms]` |
| `theatre fixtures have no ESP32` (check-patch) | set `theatre = "<address>"` or `theatre_platform = "<platform id>"` in `[output.esp32]` |
| `with the esp32 output a theatre fixture is on universe 1 …` | theatre fixtures use `universe = 1` (the board's first DMX line) or `2` (its second) |
| Two fixtures "already used" on a board's line | the theatre's devices share a line with a platform's: their addresses overlap |
| Devices on a board flicker or go dark for a moment | DMX packets are lost (Wi-Fi, an overloaded switch): the board's `dmx_timeouts` counter rises when a whole second passes without data. Use a cable |
| Shaker sound crackles or drops out | `audio_underruns` / `audio_gaps` in `fourdvr fx nodes` or the platform status: network jitter larger than the board's buffer. Use a cable, or raise `FDVR_AUDIO_BUFFER_MS` (200 for Wi-Fi) |
| Shaker early or late against the motion | `latency_offset_s` in `[output.esp32.audio]` (positive: earlier) |
| DMX devices do not react although the board reports frames | A / B swapped at the RS-485 driver, driver not enabled (DE), no termination, wrong start address or DMX mode at the device. Check the line with a DMX tester |
| "EMERGENCY STOP ACTIVE: outputs dark" on a board without an emergency-stop wire | `FDVR_ESTOP_PIN` points to a pin that floats low. Effects-only builds default to −1 (no input); wire the input or set −1 |
| Build fails: "pin used twice" (static assertion) | two functions were given the same GPIO in `config.h` / `platformio.ini`. On a WT32-ETH01 that moves a platform only one output pin (GPIO17) is free: DMX fits, audio needs a second board |
| Build of `src/main.cpp` fails in the UART or I2S calls | the firmware was checked against the declarations of ESP-IDF 4.4 and 5.1 but never compiled for a board here: please report the compiler message |

## Effects at the venue

| Message / symptom | Cause / fix |
|---|---|
| Nothing happens, status `effects.error: disabled in the configuration` | `[effects] enabled = false` |
| `effects: ERROR in patch …` (check-config) / `effects.error` with a patch message | the patch has an error; the message names the fixture and key. The service runs motion only until it is fixed and restarted |
| `fixture X: universe 1 channel 10 already used by Y` | two fixtures overlap; with platform fixtures check `address_step` against the fixture's channel count |
| `fixture X: platform scope needs address_step, universe_step or addresses` | several platforms would share one address |
| `USB DMX output carries one universe; fixtures also use universes [...]` | a USB interface is one universe: move the fixtures or use a network node |
| `effects.programme_has_effects: false` | the programme on the server is a motion-only file. Export it from the Editor with its effects and upload it again |
| `effects.programme_error: …` | the effects section of the file is damaged; it is ignored and the motion plays. Export again; `fourdvr fx info file.motion` shows the same message |
| `effects.playing: false` while the platform runs | the platform is not in the playing state yet (engaging, paused), or the programme has no effects |
| A device does not react | `fourdvr fx fire <fixture> --patch …` and `fourdvr fx monitor` on a laptop in the same network: is the channel sent? Then universe/address at the node, the node's protocol (sACN / Art-Net), its universe numbering (Art-Net: `universe_base`), cabling, the device's DMX mode |
| Monitor shows nothing | wrong interface (`interface_ip` in the patch, `--bind` for the monitor), firewall (UDP 5568 sACN, 6454 Art-Net), multicast blocked by the switch (use `unicast = ["node address"]`) |
| A cue is cut short or missing | a limit of the venue: `effects.limited` in the status, `LIMITED` lines of `fourdvr fx check`, red marks in the Editor. Shorten the cue, or change the limit in the patch if the device allows it |
| Rain / heat / bubbles / snow stop after a few seconds | their default limits (EFFECTS_GUIDE.md §5): rain 5 s on and 5 s off, heat 6 s on and 10 s off, bubbles and snow 10 s on and 5 s off. Set the limits your devices and your room allow in `[fixtures.limits]` |
| A `bubbles` or `snow` fixture behaves differently after updating from 2.0 | these names have default limits now, for every limit value the patch does not set (MIGRATION.md) |
| Second water / fog cue never comes | it starts inside the pause (`min_off_s`) after the first one |
| Fog arrives late / fan is behind the picture | set the fixture's `lead_s` from a measurement |
| Strobe flashes slower than programmed | the venue's `max_rate_hz` (3 by default) — intended |
| The second of two lightning cues comes late or not at all | it starts sooner after the first flash than `max_rate_hz` allows (and, by default, inside the 1 s pause after an activation): move the cues apart, or make them one cue with a flash rate |
| `software flashes of up to … per second are uneven at … DMX frames per second` (check-patch) | a software flash is made of whole DMX frames. Use `flash = "fixture"` with the device's own rate channel, or a lower `max_rate_hz` |
| Effects of one platform missing | platform fixtures follow *their* platform: it is paused, faulted or not started. Theatre fixtures follow the reference platform |
| Flicker with two show-service processes | two processes send the same universe: give each universe to one process only (EFFECTS_GUIDE.md §4) |
| `output_errors` rising | sACN/Art-Net: no route to the node or the interface is down (it recovers by itself). USB: `USB DMX interface … is not open` — unplugged or no permission (group `dialout`); it is retried every 5 s |
| No bass | `effects.audio.error`: device name not found (`fourdvr fx devices`), no permission (group `audio`), `miniaudio` not installed (`pip install ".[show]"`); or the programme has no bass track (`fourdvr fx info`) |
| Bass late or early | `latency_offset_s` in `[audio]` (positive: earlier) |
| Bass distorted | lower `bass_gain_db` / `vibration_gain_db`; the output is soft-limited, so distortion usually comes after the PC (amplifier input, shaker excursion) |
| Devices stay on after the PC was switched off or unplugged | the device holds its last DMX value: set it to switch off on signal loss. This is a device setting, not something software can fix |

## Editor: live output

| Symptom | Cause / fix |
|---|---|
| ● LIVE asks for confirmation | it is about to send to real devices over the network, USB or ESP32 boards |
| LIVE but no output | effects are output only while **playing** (or with *Hold levels while paused*) |
| *▶ Fire* in the fixture table is grey and does nothing | not live |
| A strobe flashes only once while the playhead is held on its cue | intended for strobes flashed by this software (`flash = "software"`): one flash per place. Play the passage to see the rate |
| "sound: …" shows an error in the *Live* tab | no sound device, or the patch's `[audio] device` name does not match this computer |
| Timeline shows red marks | a limit would hold those cues off: the loaded venue patch's, or — without a patch — the default limit of that effect type (Effects tab ▸ Limits) |
| The ESP32 table says "no answer: left out" | that board did not reply to the Editor (see the section on ESP32 boards above); the rest plays |

## Show service: server connection, actuator link, motion quality

Unchanged from Motion Controller 1.0.

| Symptom | Cause / fix |
|---|---|
| Platform never appears on the dashboard | wrong `server_url`, firewall, server down; reconnection is automatic |
| `server rejected authentication` | wrong key for this platform id: `fourdvr-server device-key PLATFORM-07` |
| `server unreachable for 10s: returning to neutral` | by design after `server_loss_timeout_s`; effects stop with it |
| ERROR `no reply from actuator controller` | bridge not powered/flashed, wrong IP/port, wrong `link_platform_id`, firewall dropping UDP 47000 |
| ERROR `communication with actuator controller lost` | cable, switch, bridge reset. Fix the cause, then STOP (manual recovery) |
| ERROR `… ESTOP` | the safety chain is open. Reset the hardware E-stop, then STOP on the dashboard |
| `driver 'hydraulic' is not implemented` | only `servo_electric` and `simulation` exist |
| Platform moves in the wrong direction on one axis | geometry or drive direction wrong — stop and redo CALIBRATION.md §4 |
| Jerky motion, `loop_overruns` increasing | CPU overloaded: fewer platforms per process, faster computer. (The effects thread is separate and light; its own counter is `effects.overruns`) |
| Platform lags the picture | `latency_compensation_s` (CALIBRATION.md §5) |
| Server refuses the file: `motion_video_match` | the motion was made from a different video file than the one uploaded |
| Server refuses: `motion_programme_id` | wrong *Programme ID*; export again with the programme's number |

## Reporting a problem

Include `fourdvr --version`, `fourdvr fx info <file>`, the patch, the output of
`fourdvr controller check-config`, the relevant part of the service log, and for extraction
problems `fourdvr extract info <video>` and the settings JSON.
