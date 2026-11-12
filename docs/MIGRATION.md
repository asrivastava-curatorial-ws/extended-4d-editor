# Coming from an earlier version

Nothing you have stops working, and nothing has to be converted: `.motion` files, projects
(`.4dvr`), effects files (`.fx.json`), venue patches, controller configurations, platform
profiles, device keys, the commands and the service all stay as they are. The Control Server and
the VR Player need no update.

* [From Extended 4D Editor 3.1](#from-extended-4d-editor-31)
* [From Extended 4D Editor 3.0](#from-extended-4d-editor-30)
* [From 4D VR Theatre Studio 2.0](#from-4d-vr-theatre-studio-20)
* [From Motion Extractor 1.1 and Motion Controller 1.0](#from-motion-extractor-11-and-motion-controller-10)

## From Extended 4D Editor 3.1

Replace the AppImage (`Extended-4D-Editor-3.2.0-x86_64.AppImage`; a desktop entry or links that
name the old file need the new name) or update the installation (`pip install --upgrade
".[studio]"` / `".[show]"`). That is all.

3.2 adds the venue window, the fixture library and the venue check ([VENUE.md](VENUE.md)). No
file format changes and no new package is needed.

**One thing to know:** a patch written by the venue window, or with `fourdvr fx fixtures entry`,
holds keys an older show service does not know (`model`, `mode`, a channel's `name`) and refuses
("unknown key"). Update the show PC together with the workstation - or delete those three keys:
they are for people, the show plays the `channels`. A patch that was never touched by 3.2 is as
it was.

## From Extended 4D Editor 3.0

Replace the AppImage (`Extended-4D-Editor-3.2.0-x86_64.AppImage`) or update the installation
(`pip install --upgrade ".[studio]"` / `".[show]"`). That is all.

3.1 adds the live mode (`fourdvr live …`, [LIVE.md](LIVE.md)) with one file type, the recorded live
session (`.4dlive`), and streaming (`fourdvr stream …`, [STREAMING.md](STREAMING.md)). Nothing else
changes: the `.motion` and effects formats, projects, venue patches, controller configurations,
the show service, the ESP32 firmware (1.1.0) and the link protocols are those of 3.0, and no new
package is needed — both additions use what a 3.0 installation already has.

The show service and the live show drive the same boards. Run one of them at a time.

## From 4D VR Theatre Studio 2.0

Version 3.0 is the same program under a new name — **Extended 4D Editor** — with three additions:
the video's scenes are looked at and effects are drafted from them, the window is organised around
four buttons and one grouped timeline, and effects can go out through the ESP32 boards.

### What is renamed

| Before (2.0) | Now (3.0) |
|---|---|
| 4D VR Theatre Studio | Extended 4D Editor |
| `4D-VR-Theatre-Studio-2.0.0-x86_64.AppImage` | `Extended-4D-Editor-3.2.0-x86_64.AppImage` (454 MiB instead of 259 MiB: it contains the scene models) |
| Python distribution `fourdvr-studio` | `extended-4d-editor` — **uninstall the old one first:** `pip uninstall fourdvr-studio`, then `pip install ".[studio]"` (or `".[show]"`) |
| desktop launcher `fourdvr-studio` | `extended-4d-editor` (the old name still starts it) |
| `generation.generator` / `meta.generator` in files | `Extended-4D-Editor` (was `4D-VR-Theatre-Studio`) |
| ESP32 firmware 1.0.0 | 1.1.0: the actuator bridge as before, plus the optional effects node |

Not renamed: the commands (`fourdvr`, `fourdvr extract`, `fourdvr controller`, `fourdvr fx`,
`fourdvr-extract`, `fourdvr-motion`), the Python modules (`fourdvr_studio`, `fourdvr_extractor`,
`fourdvr_fx`, `fourdvr_motion`), the project format, the service and its directories, the sACN
source name.

### The window

| Before | Now |
|---|---|
| *Open video…*, *File ▸ Open project…*, *Open .motion file* | **1 Open…** takes any of the three |
| **1 Analyse, 2 Generate, 3 Process, 4 Validate**, *Run all*, then *Suggest effects* | **2 Analyse video** does all of it and drafts the effect lanes that are still empty |
| pressing *2 Generate* / *3 Process* after changing a gain | not needed: the motion follows half a second after a setting changes (`Ctrl+R`, *Motion ▸ Update the motion now*, for programmes over ten minutes) |
| *4 Validate*, and *Check against the venue patch* | **3 Check** does both |
| **5 Export .motion…** | **4 Export .motion…** |
| reference lanes *Translation* and *Rotation*; manual-motion lanes appear when used (`M` shows all) | group **Motion**: six axis lanes, always there, each showing its axis' motion with what you draw on top. `M` folds the group away |
| *+ Track* in the Effects tab | **+ Add effect** above the timeline |
| bass track settings in the Effects tab | options of the lane **Sound · bass** (click the lane) |
| the venue check needs a patch | without a patch the check uses the default limits of each effect type |
| inspector shows the selection | it also shows, for every lane, what the lane is and which limit applies to it; effect lanes have *Scale…* |

### What behaves differently

* **Effects are drafted by the analysis.** In 2.0 a freshly analysed video had no effects until
  you asked for suggestions. Now **2 Analyse video** fills the empty effect lanes. One `Ctrl+Z`
  removes the draft; *Motion ▸ Analyse the video again without drafting effects* analyses only.
  On the command line nothing changed: `extract generate` drafts effects only with
  `--effects suggest`.
* **Water, fog and scent are suggested now**, together with the new rain, heat, bubbles and snow —
  from scenes found in the picture and the sound ([SCENE_DETECTION.md](SCENE_DETECTION.md)). 2.0
  never suggested them. They are a draft to review; `--no-scenes` or *Look for effect scenes*
  switched off gives the suggestions of 2.0.
* **Four more effect names are built in:** `rain`, `heat`, `bubbles`, `snow`, each with default
  limits (EFFECTS_GUIDE.md §5).
  * If a venue patch already had fixtures with `effect = "bubbles"` or `"snow"` (venue-defined
    types in 2.0), those fixtures now receive the default limits for every limit value the patch
    does not set itself — for example a total of 60 s per show. Set the values you want in
    `[fixtures.limits]`; `fourdvr fx check-patch` prints the limits in force.
  * A show PC still running 2.0 treats the four names as venue-defined types: it plays them if its
    patch has such fixtures, but **without default limits**. Give those fixtures limits in the
    patch, or update the show PC.
* **`fourdvr fx check`** no longer requires `--patch` (without one: the default limits).
* A project saved by 3.0 also holds what was found in the video. 2.0 opens such a project and
  ignores that part; 3.0 opens 2.0 projects (analyse again to have scenes looked for).

### What is new and optional

* **Effects through ESP32 boards**: `protocol = "esp32"` in the venue patch
  (EFFECTS_GUIDE.md §3.1, [ESP32_EFFECTS_LINK.md](ESP32_EFFECTS_LINK.md)). Venues that send DMX
  from the PC change nothing. Boards that only move a platform keep their firmware.
* `fourdvr fx nodes`, `fourdvr fx example-patch --esp32`, `fourdvr fx test-vectors`,
  `fourdvr controller esp32-sim --dmx-ports … --audio-channels …`, `fourdvr extract generate --no-scenes`.

### Python API

| Before | Now |
|---|---|
| `fourdvr_studio.PRODUCT_NAME == "4D-VR-Theatre-Studio"` | `"Extended-4D-Editor"`; `PRODUCT_TITLE == "Extended 4D Editor"` |
| `StudioWindow.run_all()` | still there; it is `analyse_all()` |
| `StudioWindow.run_preflight()` returned nothing | returns `(summary, number of devices limited)` |
| `StudioWindow.open_video_dialog()`, `open_motion_dialog()`, `open_project_dialog()` | `open_dialog()`; `open_path(path)` opens any of the three kinds |
| — | `fourdvr_extractor.scene`, `fourdvr_fx.bridge`, `fourdvr_fx.preflight.default_patch`, `fourdvr_fx.patch.default_limits`, `StudioProject.scene` |

## From Motion Extractor 1.1 and Motion Controller 1.0

Version 2.0 put both products into one program and added 4D effects; version 3.0 is described
above. Coming straight from 1.x, both apply.

### What stays exactly as it is

| Item | Status |
|---|---|
| Control Server 1.0.0, VR Player | **no update needed.** The server validates, stores and distributes files with effects like any other `.motion` file (tested end to end, `integration/`) |
| existing `.motion` files | play unchanged. They are motion-only programmes |
| ESP32 actuator-bridge firmware 1.0.0, link protocol 1.0 | unchanged — no reflashing |
| server protocol `4D-VR-THEATRE/1` | unchanged (the show service adds an `effects` block to its STATUS details when effects are configured; the server passes it through) |
| platform profiles, controller configuration, device keys | unchanged. A configuration without `[effects]` behaves as before |
| motion | the extraction pipeline produces bit-identical motion from the same analysis; the show service's motion pipeline (limits, kinematics, link) is the 1.0.1 code. Checked against the 1.x releases with the roller-coaster test programme |
| commands | `fourdvr-extract …`, `fourdvr-motion …`, `fourdvr-extractor-gui` still exist |
| service | unit name `fourdvr-motion`, user `fourdvr-motion`, `/etc/fourdvr-motion`, `/var/lib/fourdvr-motion` keep their names |

### What 2.0 brought

| | |
|---|---|
| One program | `fourdvr` (the desktop application), `fourdvr extract`, `fourdvr controller`, `fourdvr fx`; one source package; **one AppImage** instead of two |
| The desktop application | the Motion Extractor window, restyled, with a timeline: motion, manual motion, effect tracks, the options of the selection, undo/redo, projects (`.4dvr`), live DMX output |
| Manual motion | intensity curve and per-axis curves with ready-made gestures, applied before the constraint stage |
| Effects | wind, water, air, fog, strobe, scent, seat vibration, raw DMX channels, any venue-defined type; bass track from the sound; suggestions from the analysis |
| Venue patch | DMX addresses, lead times and safety limits of a theatre's devices (TOML) |
| Outputs | sACN (E1.31), Art-Net, USB DMX (Enttec DMX USB Pro compatible), sound card for bass and seat vibration |
| Tools | `fourdvr fx info / embed / extract / strip / check-patch / check / play / fire / blackout / monitor / devices` |

### Workstation

Replace the old application with the Editor. As AppImages, or in separate virtual environments,
old and new can exist side by side; inside one Python environment uninstall the old package first
(both contain the `fourdvr_extractor` module):

```bash
# AppImage: delete 4D-VR-Theatre-Motion-Extractor-1.1.0-x86_64.AppImage, use Extended-4D-Editor-3.2.0-x86_64.AppImage
# pip:
pip uninstall fourdvr-motion-extractor
pip install ".[studio]"            # in the unzipped Extended-4D-Editor folder
```

| Before | Now |
|---|---|
| `fourdvr-extractor-gui` | `fourdvr` (the old name still starts it) |
| `fourdvr-extract generate …` | the same, or `fourdvr extract generate …` |
| `<Extractor AppImage> cli generate …` | `<Editor AppImage> extract generate …` |
| settings JSON (`--settings`) | unchanged |
| workflow buttons 1–7 | **1 Open… · 2 Analyse video · 3 Check · 4 Export**; Play is in the transport bar (Space) |
| an exported `.motion` file | the same file when the programme has no effects (`generation.generator` in the header now says `Extended-4D-Editor`) |

Existing `.motion` files can be opened in the Editor to add effects without the video
(**1 Open…**); the motion is copied bit-exactly.

### Show PC

See INSTALL.md §3 "Updating an installation …": install the new package into
the same environment, replace the unit file, restart. No configuration change. Effects stay off
until an `[effects]` section names a venue patch.

| Before | Now |
|---|---|
| `fourdvr-motion run -c controller.toml` | the same, or `fourdvr controller run -c controller.toml` |
| `pip install ".[serial]"` | `pip install ".[show]"` (serial + sound-card output); `[serial]` still exists |
| Motion Controller AppImage | the Editor AppImage: `…AppImage controller run -c …`; a symbolic link named `fourdvr-motion` to it behaves like the old AppImage |
| STATUS details | plus `effects` when configured |

The default installation (`pip install .`) of the show PC pulls in only NumPy, Pydantic and
websockets — no desktop toolkit, no video libraries.

### Python API

| Before | Now |
|---|---|
| `fourdvr_extractor.gui.app.MainWindow` | still importable; it is `fourdvr_studio.gui.main_window.StudioWindow` |
| `fourdvr_extractor.motionfile`, `.kinematics`, `.platform_config` | still importable; the single implementation is `fourdvr_motion.*` |
| `ExtractionProject.export(path)` | plus an optional `effects=` argument |
| `Analysis` | plus `luma` (picture brightness per analysed frame) |
| — | `fourdvr_fx` (effects), `fourdvr_studio.project.StudioProject`, `fourdvr_extractor.manual`, `.soundtrack`, `.suggest` |

## Versions that work together

| This program | Control Server | VR Player | ESP32 firmware | `.motion` |
|---|---|---|---|---|
| Extended 4D Editor 3.2.0 | 1.0.0 (not used by the live mode) | unchanged | motion: 1.0.0 or 1.1.0; effects through a board: 1.1.0 | 1.0 (effects optional) |
| Extended 4D Editor 3.1.0 | 1.0.0 (not used by the live mode) | unchanged | motion: 1.0.0 or 1.1.0; effects through a board: 1.1.0 | 1.0 (effects optional) |
| Extended 4D Editor 3.0.0 | 1.0.0 | unchanged | motion: 1.0.0 or 1.1.0; effects through a board: 1.1.0 | 1.0 (effects optional) |
| 4D VR Theatre Studio 2.0.0 | 1.0.0 | unchanged | 1.0.0 | 1.0 (effects optional) |

A show PC still running Motion Controller 1.0 plays files that contain effects as motion only.
A show PC still running Studio 2.0 plays files made with 3.0, with the note on the four new effect
names above.
