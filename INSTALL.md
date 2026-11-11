# Installing Extended 4D Editor

The same program is installed in two places, with different parts of it in use:

| Machine | Used for | Needs |
|---|---|---|
| **Workstation** (where programmes are made) | the Editor, analysis with scene detection, live testing | a desktop; any recent 4-core CPU, 8 GB RAM. No graphics card is needed: everything runs on the processor |
| **Show PC** (in the theatre) | `fourdvr controller run` as a service | Linux, wired network; no desktop needed |

Read the safety section of [README.md](README.md) before connecting actuators or effect devices.

## 1. The AppImage (Linux x86_64) — nothing to install

```bash
chmod +x Extended-4D-Editor-3.0.0-x86_64.AppImage
./Extended-4D-Editor-3.0.0-x86_64.AppImage                 # the Editor
./Extended-4D-Editor-3.0.0-x86_64.AppImage --help          # the command line
```

It contains its own Python, Qt, every library and the two scene-detection models, and never uses
the Internet: copy the one file to a computer without any network and everything works, scene
detection included. On a system
without FUSE (some servers, containers) add `--appimage-extract-and-run`, or extract it once with
`--appimage-extract` and run `squashfs-root/AppRun` — that is also the recommended way to run the
show service from it. Details, licences and how to build it: [docs/APPIMAGE.md](docs/APPIMAGE.md).

Optional conveniences:

```bash
mkdir -p ~/.local/bin && cp Extended-4D-Editor-3.0.0-x86_64.AppImage ~/.local/bin/
ln -sf Extended-4D-Editor-3.0.0-x86_64.AppImage ~/.local/bin/fourdvr           # `fourdvr ...` in any terminal
ln -sf Extended-4D-Editor-3.0.0-x86_64.AppImage ~/.local/bin/fourdvr-extract   # the former command names
ln -sf Extended-4D-Editor-3.0.0-x86_64.AppImage ~/.local/bin/fourdvr-motion
```

## 2. From this package (Linux, Windows; Python 3.12 or newer)

Install only what the machine needs:

| Command | Installs | For |
|---|---|---|
| `pip install ".[studio]"` | everything: desktop application, video analysis, scene detection (without its model files, see below), sound decoding, bass preview, USB DMX | workstation |
| `pip install ".[show]"` | show service + sound-card output + USB/serial | show PC with bass shakers or USB DMX / serial ESP32 links |
| `pip install .` | show service, network DMX (sACN / Art-Net), effects tools — NumPy, Pydantic and websockets only | show PC |

### Workstation, Linux

```bash
unzip Extended-4D-Editor-3.0.0.zip && cd Extended-4D-Editor
python3 -m venv ~/venvs/fourdvr && . ~/venvs/fourdvr/bin/activate
pip install ".[studio]"
fourdvr --version
fourdvr &                                  # the Editor
```

**Scene models.** The two model files for finding rain, fog, fire and the other scenes (203 MB)
are in the AppImage but not in this source package. Without them everything else works and the
Editor says that scenes cannot be looked for. To make them (once; downloads 720 MB from GitHub,
checked against checksums; no PyTorch needed):

```bash
pip install onnx
python packaging/models/build_models.py --out ~/.local/share/extended-4d-editor/models
```

Details and the models' origin: [docs/SCENE_DETECTION.md](docs/SCENE_DETECTION.md) §5.

If the window does not open and the message mentions the Qt "xcb" platform plugin, install the
X11 helper libraries (the AppImage brings its own):

```bash
sudo apt install -y libxcb-cursor0 libxkbcommon-x11-0 libxcb-icccm4 libxcb-keysyms1 libxcb-shape0 libegl1   # Debian/Ubuntu/Kubuntu
sudo dnf install -y xcb-util-cursor libxkbcommon-x11 xcb-util-wm xcb-util-keysyms                         # Fedora
```

### Workstation, Windows (PowerShell)

```powershell
Expand-Archive Extended-4D-Editor-3.0.0.zip -DestinationPath .
cd Extended-4D-Editor
py -3.12 -m venv $env:USERPROFILE\venvs\fourdvr
& $env:USERPROFILE\venvs\fourdvr\Scripts\Activate.ps1
pip install ".[studio]"
fourdvr
```

The Editor, extraction, the effects tools and network DMX are written to work on Windows as well,
but this release was built and tested on Linux only. The show service is developed and tested on
Linux; run the show PC on Linux.

### Offline installation

On a connected machine with the same operating system, CPU type and Python version:

```bash
pip download ".[studio]" -d vendor            # or ".[show]" / "."
```

Copy the folder with `vendor/` to the offline machine, then
`pip install --no-index --find-links vendor ".[studio]"`.

## 3. Show PC

### Requirements

| Item | Requirement |
|---|---|
| Computer | Linux PC or industrial PC (x86_64 or arm64). One process can run all 20 platforms; at 100 Hz each platform costs roughly 3–5 % of one CPU core. The effects add one thread (40 DMX frames per second, well under 1 % of a core) |
| Network | static IP; wired Ethernet to the Control Server (TCP 8080) and the ESP32 bridges (UDP 47000). DMX over the network (sACN, Art-Net) on the same or — better — a separate effects network |
| Per platform | an ESP32 actuator bridge and six electric servo actuators with drives (docs/ESP32_PROTOCOL.md) |
| Effects (optional) | **either** from the PC: a DMX node (sACN or Art-Net) or a USB DMX interface (Enttec DMX USB Pro compatible); for bass shakers a sound card or USB audio interface and an amplifier — **or** through the ESP32 boards: an RS-485 driver for a DMX line and an I2S audio converter with an amplifier on each board that has devices (UDP 47002; docs/ESP32_EFFECTS_LINK.md). Both can be mixed for sound |

### New installation

```bash
unzip Extended-4D-Editor-3.0.0.zip && cd Extended-4D-Editor
sudo useradd --system --home-dir /var/lib/fourdvr-motion --create-home --shell /usr/sbin/nologin fourdvr-motion
sudo python3 -m venv /opt/fourdvr-motion/venv
sudo /opt/fourdvr-motion/venv/bin/pip install ".[show]"
/opt/fourdvr-motion/venv/bin/fourdvr --version

sudo install -d -m 0750 -o fourdvr-motion -g fourdvr-motion /etc/fourdvr-motion /etc/fourdvr-motion/keys /etc/fourdvr-motion/effects
sudo install -d -m 0750 -o fourdvr-motion -g fourdvr-motion /var/lib/fourdvr-motion/cache
sudo cp -r config/platforms /etc/fourdvr-motion/
sudo cp config/controller.example.toml /etc/fourdvr-motion/controller.toml
sudo sed -i 's|^cache_dir = .*|cache_dir = "/var/lib/fourdvr-motion/cache"|' /etc/fourdvr-motion/controller.toml
```

(The user, the directories and the service keep the names of the former Motion Controller, so
that an existing installation is updated in place.)

1. **Platforms.** Make a platform profile for your platform type (copy
   `platforms/example-electric-6dof.toml`, enter the real geometry, actuator data and limits —
   docs/PLATFORM_CONFIGURATION.md). Edit `/etc/fourdvr-motion/controller.toml`: `server_url`,
   one `[[platforms]]` entry per platform.
2. **Device keys** (on the Control Server host), one file per platform:

   ```bash
   # on the server:
   fourdvr-server device-key PLATFORM-01 PLATFORM-02 > platform-keys.txt
   # on the show PC (copy platform-keys.txt over first):
   while IFS=$'\t' read -r id key; do
     printf '%s\n' "$key" | sudo tee "/etc/fourdvr-motion/keys/${id}.key" >/dev/null
   done < platform-keys.txt
   sudo chown fourdvr-motion: /etc/fourdvr-motion/keys/*.key && sudo chmod 600 /etc/fourdvr-motion/keys/*.key
   rm platform-keys.txt
   ```

3. **Effects (optional — skip this step for a motion-only theatre).** Write the venue patch and
   switch it on:

   ```bash
   /opt/fourdvr-motion/venv/bin/fourdvr fx example-patch | sudo tee /etc/fourdvr-motion/effects/patch.toml >/dev/null
   #   devices and shakers on the ESP32 boards instead:  fourdvr fx example-patch --esp32 | sudo tee …
   sudoedit /etc/fourdvr-motion/effects/patch.toml          # your devices: docs/EFFECTS_GUIDE.md
   sudo tee -a /etc/fourdvr-motion/controller.toml >/dev/null <<'CONF'

   [effects]
   patch = "effects/patch.toml"
   reference_platform = "PLATFORM-01"
   CONF
   ```

4. **Check everything.** `check-config` prints the platforms and, with effects, the DMX address
   table of the venue; it reports errors of the patch without preventing the motion-only start.

   ```bash
   sudo -u fourdvr-motion /opt/fourdvr-motion/venv/bin/fourdvr controller check-config -c /etc/fourdvr-motion/controller.toml
   sudo -u fourdvr-motion /opt/fourdvr-motion/venv/bin/fourdvr controller check-platform /etc/fourdvr-motion/platforms/my-platform.toml
   ```

5. **Service.**

   ```bash
   sudo install -m 0644 deploy/systemd/fourdvr-motion.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now fourdvr-motion
   sudo journalctl -u fourdvr-motion -f
   ```

   The platforms appear on the Control Server dashboard as PLATFORM-01 …, state READY once their
   ESP32 bridge answers. With effects configured, each platform's details on the server show an
   `effects` block (running, playing, output, errors, fixtures held by a limit).

### Updating an installation of 4D VR Theatre Studio 2.0 or Motion Controller 1.0

Nothing in the configuration has to change; effects stay off until you add `[effects]`, and an
existing venue patch works as it is.

```bash
sudo systemctl stop fourdvr-motion
sudo /opt/fourdvr-motion/venv/bin/pip uninstall -y fourdvr-studio fourdvr-motion-controller    # whichever is installed
sudo /opt/fourdvr-motion/venv/bin/pip install ".[show]"            # in the unzipped Extended-4D-Editor folder
sudo install -m 0644 deploy/systemd/fourdvr-motion.service /etc/systemd/system/    # adds the audio group, new command name
sudo systemctl daemon-reload && sudo systemctl start fourdvr-motion
```

`fourdvr-motion run -c …` is still a valid command, so an unchanged old unit file works too.
More: [docs/MIGRATION.md](docs/MIGRATION.md).

### ESP32 firmware

Boards that only move a platform need nothing: the motion link is unchanged since Motion
Controller 1.0, and a board with the old firmware keeps working. A board that is to drive DMX
devices or a bass shaker needs one of the effects variants of the firmware:

```bash
pip install platformio
cd firmware/esp32-actuator-bridge
# per board: set the platform id, IP address, stroke, park position and pins in include/config.h
pio run -e wt32-eth01                      # platform only (Ethernet, WT32-ETH01)
pio run -e wt32-eth01-dmx                  # platform + a DMX line
pio run -e wt32-eth01-fx                   # no platform: a DMX line + an audio output
pio run -e wt32-eth01 -t upload            # flash over USB
pio device monitor -b 921600
```

Other environments: `olimex-esp32-poe`, `esp32dev-serial`, `esp32dev-wifi`, `esp32dev-wifi-fx`,
`esp32dev-wifi-all` (Wi-Fi: bench tests and effects-only boards). Wiring, pins, settings and the
first power-on of a board: [docs/ESP32_EFFECTS_LINK.md](docs/ESP32_EFFECTS_LINK.md). **The effects
part of the firmware has been tested on a PC only — it was not compiled for or run on an ESP32
for this release.** Tests of both protocol cores on the PC:
`make -C firmware/esp32-actuator-bridge/test/host`.

### First runs without hardware

1. **Simulated platforms**: `driver = "simulation"` (or `config/platforms/simulation-6dof.toml`).
   Connect to the server and play a programme; the dashboard shows RUNNING, positions and drift.
2. **Effects without devices**: on any PC in the effects network run
   `fourdvr fx monitor -u 1 -u 2` — it prints the DMX that arrives (sACN; `--protocol artnet` for
   Art-Net) while the service, `fourdvr fx play` or the Editor's LIVE mode sends it.
3. **Dry run of a programme**: `fourdvr controller play-local programme.motion --platform my-platform.toml`
   (limiter activity and actuator demand) and
   `fourdvr fx check programme.motion --patch effects/patch.toml` (what the venue's limits do to
   the effects).
4. **A simulated ESP32 board**: `fourdvr controller esp32-sim --dmx-ports 1 --audio-channels 2`
   answers on this PC like a board with a DMX line and two audio outputs; `fourdvr fx nodes 127.0.0.1`
   lists it.
5. **The whole chain on one PC** with the Control Server: `integration/run_effects_integration.py`
   (network DMX) and `integration/run_esp32_integration.py` (ESP32 boards).

Then follow docs/CALIBRATION.md for the platforms and docs/EFFECTS_GUIDE.md §7 for the effect devices.

## 4. Tests

```bash
pip install -e ".[dev]"
pytest                                      # includes the desktop application off-screen
make -C firmware/esp32-actuator-bridge/test/host                                   # needs g++
python integration/run_effects_integration.py --server /path/to/fourdvr-server     # needs the Control Server
python integration/run_esp32_integration.py --server /path/to/fourdvr-server       # ... and g++ for the firmware's own code
```
