# The single-file AppImage

An AppImage is one executable file that carries the application and everything it needs. The
script in `packaging/appimage/` turns this package into

```
dist/Extended-4D-Editor-3.0.0-x86_64.AppImage      (453 MiB / 475 MB)
```

which is the complete `fourdvr` command — Editor with scene detection, extraction, show service
and effects tools — on an x86_64 Linux computer **without Python, pip, Internet access or
administrator rights**. Everything it needs is inside, the two scene models included; it opens no
network connection of its own. It is the successor of the 2.0 AppImage (259 MiB): the difference
is the scene detection — two models (203 MB before compression) and the library that runs them.

| Inside the AppImage | Source |
|---|---|
| Python 3.12.7 | python-build-standalone release 20241016 (relocatable CPython) |
| NumPy, SciPy, OpenCV (headless, with FFmpeg), PySide6/Qt 6, pyqtgraph, Pydantic, websockets, PyAV, miniaudio, pyserial | PyPI wheels, versions in `packaging/appimage/requirements.lock` |
| ONNX Runtime 1.28.0 (with FlatBuffers, Protocol Buffers, packaging), kaldi-native-fbank | PyPI wheels, same file. ONNX Runtime is deliberately a version **below 1.29** (§4) |
| Scene models: `scene.onnx` (picture, 176 MB), `concepts.npz`, `sound.onnx` (27 MB), `sound-labels.csv`, with `LICENSES.txt` and a manifest | built during the AppImage build by `packaging/models/build_models.py` from pinned downloads (docs/SCENE_DETECTION.md §5); in `site-packages/fourdvr_extractor/models` |
| Qt X11 helper libraries (`libxcb-cursor`, `-icccm`, `-image`, `-keysyms`, `-render-util`, `-util`, `libxkbcommon`, `libxkbcommon-x11`) | Ubuntu 20.04 packages; often missing on minimal installations |
| Extended 4D Editor 3.0.0 with its fonts, example platform profile and example venue patches | this package |

Taken from the computer it runs on: glibc **2.34 or newer** (Ubuntu / Kubuntu 22.04+, Debian 12+,
Linux Mint 21+, Fedora 35+, RHEL/Rocky/AlmaLinux 9+; the Qt 6.12 wheels set this minimum). For the
Editor also OpenGL/EGL, the core X11/xcb libraries or Wayland, fontconfig/freetype and D-Bus —
present on every Linux desktop. The show service and the command-line tools need none of those.

## 1. Using it

```bash
sha256sum -c Extended-4D-Editor-3.0.0-x86_64.AppImage.sha256
chmod +x Extended-4D-Editor-3.0.0-x86_64.AppImage
./Extended-4D-Editor-3.0.0-x86_64.AppImage                          # the Editor
./Extended-4D-Editor-3.0.0-x86_64.AppImage ride360.mp4              # ... opening a video, project or .motion
./Extended-4D-Editor-3.0.0-x86_64.AppImage extract generate ride360.mp4 --projection 360 \
    --profile my-hexapod.toml --effects suggest --bass -o ride360.motion
./Extended-4D-Editor-3.0.0-x86_64.AppImage fx example-patch -o venue.toml
./Extended-4D-Editor-3.0.0-x86_64.AppImage fx check ride360.motion --patch venue.toml
./Extended-4D-Editor-3.0.0-x86_64.AppImage controller check-config -c controller.toml
./Extended-4D-Editor-3.0.0-x86_64.AppImage --version
```

Short command names (and the former ones) through symbolic links:

```bash
mkdir -p ~/Applications ~/.local/bin
mv Extended-4D-Editor-3.0.0-x86_64.AppImage ~/Applications/
for name in fourdvr fourdvr-extract fourdvr-motion; do
  ln -sf ~/Applications/Extended-4D-Editor-3.0.0-x86_64.AppImage ~/.local/bin/$name
done
fourdvr fx info ride360.motion
fourdvr-motion check-platform my-hexapod.toml        # behaves as the Motion Controller's command did
```

Start-menu entry (FOSS tools such as AppImageLauncher or Gear Lever do this for you):

```bash
cd /tmp && ~/Applications/Extended-4D-Editor-3.0.0-x86_64.AppImage --appimage-extract 'extended-4d-editor.*'
install -D -m 644 squashfs-root/extended-4d-editor.png ~/.local/share/icons/hicolor/256x256/apps/extended-4d-editor.png
sed "s|^Exec=.*|Exec=$HOME/Applications/Extended-4D-Editor-3.0.0-x86_64.AppImage %f|" \
    squashfs-root/extended-4d-editor.desktop | tee ~/.local/share/applications/extended-4d-editor.desktop
rm -rf squashfs-root
```

**FUSE.** An AppImage mounts itself with FUSE. The runtime used here is statically linked, so it
does not need `libfuse2`; it needs `/dev/fuse` and the `fusermount3` (or `fusermount`) program,
package `fuse3` (or `fuse`). Without FUSE (some servers, containers, locked-down systems):

```bash
./Extended-4D-Editor-3.0.0-x86_64.AppImage --appimage-extract-and-run --version   # unpacks to /tmp each start
./Extended-4D-Editor-3.0.0-x86_64.AppImage --appimage-extract                     # once, then:
./squashfs-root/AppRun --version
```

## 2. The show service from the AppImage

For a service, extract the image once — no FUSE mount to keep alive, faster start:

```bash
sudo install -d /opt/extended-4d-editor && cd /opt/extended-4d-editor
sudo /path/to/Extended-4D-Editor-3.0.0-x86_64.AppImage --appimage-extract >/dev/null     # -> squashfs-root/
sudo /opt/extended-4d-editor/squashfs-root/AppRun --version
```

Then follow INSTALL.md §3 for the user, directories, configuration and keys (use
`/opt/extended-4d-editor/squashfs-root/AppRun` wherever it says `/opt/fourdvr-motion/venv/bin/fourdvr`),
and in `/etc/systemd/system/fourdvr-motion.service` use the `ExecStart` line that is prepared as a
comment in `deploy/systemd/fourdvr-motion.service`:

```ini
ExecStart=/opt/extended-4d-editor/squashfs-root/AppRun controller run -c /etc/fourdvr-motion/controller.toml
```

Updating: extract the new AppImage next to the old directory, switch the directory name (or a
symbolic link) and restart the service.

## 3. Building it

Any x86_64 Linux; the build does not use the system Python.

```bash
sudo apt install curl binutils file squashfs-tools        # Debian / Ubuntu / Mint
sudo dnf install curl binutils file squashfs-tools        # Fedora / RHEL / Rocky

cd Extended-4D-Editor
packaging/appimage/build-appimage.sh
ls -lh dist/
```

Internet is needed once, for about 1.2 GB of downloads (cached in `packaging/appimage/cache/`:
450 MB of Python, wheels and helper packages, 720 MB of model files). The script:

1. downloads (or takes from the cache) python-build-standalone, the AppImage runtime and the Qt
   helper packages, and **checks each against a pinned SHA-256**;
2. unpacks Python into `AppDir/usr/python`, builds this package's wheel, downloads the dependency
   wheels (binary wheels only, versions from `requirements.lock`) and installs everything **with
   `--no-index`** into that Python, then runs `pip check`;
3. refuses to go on if the installed ONNX Runtime contains a telemetry client (§4);
4. builds the two scene models with the bundled Python: downloads their published files (or
   takes them from the cache), checks each against a pinned SHA-256, converts the picture model,
   verifies it against a reference implementation, and puts the result into
   `site-packages/fourdvr_extractor/models`. The `onnx` package this needs is installed into the
   build directory only, not into the image;
5. removes what is not needed at run time (test suite, Tk, IDLE, pip, headers) and byte-compiles
   everything (the image is read-only);
6. adds the Qt helper libraries, `AppRun`, the desktop entry, the icon and the licence notes;
7. checks with `ldd` that every bundled library outside Qt resolves inside the AppDir and reports
   the newest glibc symbol any bundled binary needs;
8. packs the AppDir into a SquashFS image (zstd) behind the AppImage runtime, writes the
   `.sha256` file, then unpacks the result and tests it **in a network-less namespace**
   (`unshare -rn`): version, the controller and extraction commands, both example patches through
   `fx check-patch`, a picture through the picture model and the sound model loaded, and the
   Editor window built off-screen with the bundled font.

Options (environment variables): `OFFLINE=1` (no network, cache only), `CACHE=…`, `WORK=…`,
`OUT_DIR=…` (default `dist/`), `SKIP_TEST=1`, `WITHOUT_PYAV=1` and `WITHOUT_MODELS=1` (§5),
`MODELS_DIR=…` (take ready-made model files from this folder instead of building them),
`MODELS_CACHE=…` (where the model downloads are kept; default `cache/models`),
`PACKER=appimagetool` (pack with appimagetool 1.9.0 instead of `mksquashfs`).

Rebuild without Internet: `OFFLINE=1 packaging/appimage/build-appimage.sh`. For an air-gapped
build machine copy `packaging/appimage/cache/` along with the package.

Downloading the inputs by hand (the script does this itself; names must match):

```bash
mkdir -p packaging/appimage/cache && cd packaging/appimage/cache
curl -fL -o 'cpython-3.12.7+20241016-x86_64-unknown-linux-gnu-install_only.tar.gz' \
  'https://github.com/astral-sh/python-build-standalone/releases/download/20241016/cpython-3.12.7%2B20241016-x86_64-unknown-linux-gnu-install_only.tar.gz'
curl -fL -o runtime-x86_64 https://github.com/AppImage/type2-runtime/releases/download/continuous/runtime-x86_64
U=http://archive.ubuntu.com/ubuntu/pool
for p in universe/x/xcb-util-cursor/libxcb-cursor0_0.1.1-4ubuntu1_amd64.deb \
         main/x/xcb-util-wm/libxcb-icccm4_0.4.1-1.1_amd64.deb \
         main/x/xcb-util-image/libxcb-image0_0.4.0-1build1_amd64.deb \
         main/x/xcb-util-keysyms/libxcb-keysyms1_0.4.0-1build1_amd64.deb \
         main/x/xcb-util-renderutil/libxcb-render-util0_0.3.9-1build1_amd64.deb \
         main/x/xcb-util/libxcb-util1_0.4.0-0ubuntu3_amd64.deb \
         main/libx/libxkbcommon/libxkbcommon0_0.10.0-1_amd64.deb \
         main/libx/libxkbcommon/libxkbcommon-x11-0_0.10.0-1_amd64.deb; do
  wget -nc "$U/$p"
done
cd -
```

The expected SHA-256 values are in `build-appimage.sh` (`INPUTS`, `X11_DEBS`); a file that does
not match is renamed to `*.rejected` and the build stops. The AppImage runtime is published only
as "continuous": if upstream has replaced it, check the new file and build with
`RUNTIME_SHA256=<its sha256>`.

The model downloads for an air-gapped build machine: on a connected computer run

```bash
python packaging/models/build_models.py --cache packaging/appimage/cache/models --out /tmp/scene-models
```

(needs Python with NumPy and `pip install onnx`). It fills the cache with the five input files —
their URLs and SHA-256 values are at the top of the script — and `/tmp/scene-models` then holds
ready-made models that `MODELS_DIR=/tmp/scene-models` can use as well.

## 4. Proving that it works offline

```bash
unshare -rn ./Extended-4D-Editor-3.0.0-x86_64.AppImage --appimage-extract-and-run fx example-patch | head -3
```

runs it in a network namespace without any interface. If unprivileged user namespaces are
disabled (Ubuntu 24.04 restricts them through AppArmor), use `firejail --net=none …`, or pull the
network cable. The same works for a whole analysis with scene detection:

```bash
unshare -rn ./Extended-4D-Editor-3.0.0-x86_64.AppImage --appimage-extract-and-run \
    extract generate ride.mp4 --effects suggest --bass -o ride.motion
```

**Nothing reports home.** The program itself opens network connections only where you configure
them (Control Server, ESP32 boards, DMX). One bundled library needs a remark: **ONNX Runtime**,
which runs the scene models. From version 1.29 its Linux builds contain a client that sends usage
events to Microsoft and is on by default (the package's own `Privacy.md`). The image therefore
contains version 1.28.0, whose library has no such client — the build stops if it finds one —
and `AppRun` sets `ORT_DISABLE_TELEMETRY=1` in addition, the documented off-switch of the newer
versions. To see for yourself that an analysis makes no network system call at all:

```bash
./Extended-4D-Editor-3.0.0-x86_64.AppImage --appimage-extract >/dev/null
strace -f -e trace=network -o net.txt squashfs-root/AppRun extract generate ride.mp4 --effects suggest -o ride.motion
grep -c 'AF_INET' net.txt        # prints 0
```

## 5. Licences

The AppImage redistributes the components listed in [THIRD_PARTY_LICENSES.md](../THIRD_PARTY_LICENSES.md);
each package's licence is in its `*.dist-info` folder inside
`usr/python/lib/python3.12/site-packages`, Python's in `usr/python/lib/python3.12/LICENSE.txt`,
the Debian copyright files in `usr/share/doc/`, and `usr/share/doc/Extended-4D-Editor/` lists all
installed packages.

Three points matter if you pass the file on to others:

* **Qt** (LGPLv3) is linked dynamically; its libraries are separate files a user can replace.
* **PyAV** bundles FFmpeg libraries that include GPL-licensed encoders (x264, x265). The GNU GPL
  therefore applies to that part of the image when it is distributed. `WITHOUT_PYAV=1
  packaging/appimage/build-appimage.sh` builds an image without it (about 30 MiB smaller): the
  Editor then reads sound tracks through an `ffmpeg` program installed on the computer
  (`sudo apt install ffmpeg`; stereo mix-down only, no separate LFE channel). Without that program
  there is no bass track and no sound-based suggestion — `--bass` says so and stops — and
  everything else is unchanged. The show service never uses PyAV.
* **The scene models** are redistributed under the licences their publishers state (MIT for the
  picture model's repository, Apache-2.0 for the sound model); `LICENSES.txt` next to them holds
  the texts and where each file comes from (THIRD_PARTY_LICENSES.md, *The scene models*).
  `WITHOUT_MODELS=1` builds an image without them: no scene detection, everything else unchanged.
  With both options the image is 247 MiB.

## 6. What was tested

Built on Ubuntu 24.04 (container, x86_64) with the default `mksquashfs` packer and **without
network access** (`OFFLINE=1` inside `unshare -rn`, every input from the cache): 453 MiB, newest
glibc symbol 2.34. The image's **contents** were tested after unpacking with `unsquashfs`:

* the build's own smoke test without network (above), including a picture through the picture
  model;
* the models are inside with their licence file and manifest; the bundled ONNX Runtime is 1.28.0
  and its library contains no telemetry client; `onnx`, the build tool, is not inside;
* an analysis with scene detection under `strace`: not one network system call;
* the 85-second test film of docs/SCENE_DETECTION.md §3 in a network-less namespace: rain, heat,
  fog, snow and bubbles cues where they belong, and the same cues as from a source installation;
* both end-to-end tests with the image's Python and `AppRun` as the `fourdvr` command —
  `integration/run_effects_integration.py` (extraction with suggestions and bass track, Control
  Server 1.0.0, the show service with two simulated platforms and a venue patch, real sACN on the
  loopback interface, every DMX frame compared) and `integration/run_esp32_integration.py` (three
  platforms, effects and bass through boards that are the firmware's own code) — all checks
  passed;
* `fourdvr-motion` / `fourdvr-extract` symbolic-link dispatch; the show service loads neither Qt,
  OpenCV, SciPy, PyAV nor ONNX Runtime;
* the Editor on X11 (Xvfb, Qt `xcb` platform) with the bundled helper libraries: window, fonts,
  a video opened and analysed in one step with the bundled models, effects drafted, the check,
  a cue played to a (captured) live output, export.

The small variant (`WITHOUT_PYAV=1 WITHOUT_MODELS=1`, 247 MiB) was built the same way and
checked for: its smoke test, no PyAV and no models inside, a bass track and the motion-based
suggestions through the computer's `ffmpeg` program together with a note that the scene models
are missing, and the error message of `--bass` when there is no `ffmpeg` program either. Each of
the two options alone, `MODELS_DIR`, and the show-PC-only variant of §7 were not built.

**Not executed in the test environment:** the AppImage runtime itself (the FUSE mount on
double-click, `--appimage-extract-and-run`) and the `PACKER=appimagetool` variant — the
environment's policy did not allow running those downloaded binaries. The runtime is the standard
upstream type-2 runtime and the packing is the documented AppImage layout (runtime followed by the
SquashFS image), the same as for the earlier AppImages. Check once on your machine:

```bash
./Extended-4D-Editor-3.0.0-x86_64.AppImage --version                            # FUSE mount
./Extended-4D-Editor-3.0.0-x86_64.AppImage --appimage-extract-and-run --version # without FUSE
```

Both must print `3.0.0`.

## 7. Changing what goes into the AppImage

| Change | How |
|---|---|
| Python package versions | edit `packaging/appimage/requirements.lock`, build (online), re-test |
| Python version | `PY_VERSION`, `PBS_RELEASE` and the SHA-256 in `build-appimage.sh` |
| Older distributions (glibc < 2.34) | Qt is the reason for 2.34: lock `pyside6-essentials`/`shiboken6` to an older release whose Linux wheel is tagged `manylinux_2_28`, build, check the glibc line in the output |
| No scene detection (203 MB less before compression) | `WITHOUT_MODELS=1` |
| Another ONNX Runtime | edit `requirements.lock`, but stay below 1.29 unless you have checked the newer version's telemetry (§4): the build refuses a library that contains the client |
| Other scene descriptions | edit `packaging/models/concepts.json` and build (docs/SCENE_DETECTION.md §5) |
| A small image for show PCs only | set `EXTRAS="[show]"` and `BUNDLE_X11_HELPERS=0` in `appimage.conf` and build with `WITHOUT_MODELS=1`: no Qt, OpenCV, SciPy, PyAV, ONNX Runtime. The Editor and extraction then answer with a clear message; skip the build's smoke test, which starts the Editor (`SKIP_TEST=1`). This variant was not built for the release; the Motion Controller 1.0 image of that kind was 68 MiB |
| ARM64 (aarch64) | not covered: needs the aarch64 Python build, `runtime-aarch64` and arm64 helper packages; install with pip on ARM show PCs |

## 8. Troubleshooting

| Problem | Fix |
|---|---|
| `missing build tool: mksquashfs` | install `squashfs-tools` |
| `checksum mismatch for runtime-x86_64` | upstream replaced the continuous runtime: inspect the new file, then `RUNTIME_SHA256=<sha256> packaging/appimage/build-appimage.sh` |
| `… is not in … (OFFLINE=1)` | the cache is incomplete; build once with network access, or copy the cache |
| `No matching distribution` during the build | a version in `requirements.lock` has no wheel for Python 3.12/x86_64; choose one that has |
| an error about FUSE or `fusermount` when starting | install `fuse3` (or `fuse`), or use `--appimage-extract-and-run` / `--appimage-extract` |
| `GLIBC_2.34 not found` | the distribution is older than the minimum; see §7 or install with pip |
| Editor: `could not load the Qt platform plugin "xcb"` | start with `QT_DEBUG_PLUGINS=1` to see the missing library; on Wayland try `QT_QPA_PLATFORM=wayland` |
| no sound device found by the show service | the AppImage uses ALSA / PulseAudio / PipeWire of the host through miniaudio; the service user needs group `audio` and no other program may hold the device exclusively |
