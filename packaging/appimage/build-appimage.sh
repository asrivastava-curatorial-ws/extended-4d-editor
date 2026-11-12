#!/usr/bin/env bash
# build-appimage.sh - build a fully offline, self-contained x86_64 AppImage of this product.
#
#   packaging/appimage/build-appimage.sh             first build: downloads the inputs once into
#                                                    packaging/appimage/cache (needs Internet)
#   OFFLINE=1 packaging/appimage/build-appimage.sh   rebuild without any network access, from the cache
#   PACKER=appimagetool packaging/appimage/...       pack with appimagetool instead of mksquashfs
#
#   WITHOUT_PYAV=1 packaging/appimage/...            leave out PyAV (FFmpeg libraries with GPL parts);
#                                                    the sound track is then read with the host's ffmpeg
#   WITHOUT_MODELS=1 packaging/appimage/...          leave out the two scene models (203 MB): no scene
#                                                    detection, everything else unchanged
#   MODELS_DIR=/path packaging/appimage/...          take the scene models from this folder (made with
#                                                    packaging/models/build_models.py) instead of building them
#
# The AppImage contains its own Python (python-build-standalone), every Python package the product
# needs (installed from wheels with the versions in requirements.lock), the two models of the scene
# detection and - for the desktop application - the X11 helper libraries Qt needs that minimal Linux
# installations lack. At run time it never uses the network (except what you configure: Control
# Server, ESP32 links, DMX) and does not depend on any Python installed on the target.
#
# Product-specific settings are in appimage.conf next to this script. See APPIMAGE.md.
set -euo pipefail
umask 022

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source-path=SCRIPTDIR source=appimage.conf
. "$HERE/appimage.conf"
: "${APP_NAME:?}" "${PACKAGE:?}" "${MODULE:?}" "${DESKTOP_FILE:?}" "${ICON_NAME:?}"
EXTRAS="${EXTRAS:-}"
BUNDLE_X11_HELPERS="${BUNDLE_X11_HELPERS:-0}"

VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION")"
ARCH=x86_64
CACHE="${CACHE:-$HERE/cache}"
WORK="${WORK:-$HERE/build}"
OUT_DIR="${OUT_DIR:-$ROOT/dist}"
OFFLINE="${OFFLINE:-0}"
SKIP_TEST="${SKIP_TEST:-0}"
WITHOUT_PYAV="${WITHOUT_PYAV:-0}"
WITHOUT_MODELS="${WITHOUT_MODELS:-0}"
MODELS_DIR="${MODELS_DIR:-}"                      # ready-made scene models; empty: build them here
MODELS_CACHE="${MODELS_CACHE:-$CACHE/models}"     # downloads of packaging/models/build_models.py (720 MB)
ONNX_TOOL=("onnx==1.23.2" "ml-dtypes==0.6.0")     # needed to write the picture model's file; not bundled
PACKER="${PACKER:-mksquashfs}"     # mksquashfs (squashfs-tools) or appimagetool

# ---- pinned external inputs: file name | URL | SHA-256 ------------------------------------------
PY_VERSION=3.12.7
PBS_RELEASE=20241016
PBS_FILE="cpython-${PY_VERSION}+${PBS_RELEASE}-${ARCH}-unknown-linux-gnu-install_only.tar.gz"
INPUTS=(
  "$PBS_FILE|https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}/cpython-${PY_VERSION}%2B${PBS_RELEASE}-${ARCH}-unknown-linux-gnu-install_only.tar.gz|43576f7db1033dd57b900307f09c2e86f371152ac8a2607133afa51cbfc36064"
  # The AppImage runtime (the small program at the start of every AppImage that mounts or extracts
  # the image) is only published as "continuous"; this pin is the file that was used for testing.
  # When upstream replaces it, the download is refused: check the new file, then set RUNTIME_SHA256.
  "runtime-${ARCH}|https://github.com/AppImage/type2-runtime/releases/download/continuous/runtime-${ARCH}|${RUNTIME_SHA256:-617aa77314d57c8b8e4fae9c3f0b35af7b30d5529cb69e98f64f92510ed98a46}"
)
APPIMAGETOOL="appimagetool-${ARCH}.AppImage|https://github.com/AppImage/appimagetool/releases/download/1.9.0/appimagetool-${ARCH}.AppImage|46fdd785094c7f6e545b61afcfb0f3d98d8eab243f644b4b17698c01d06083d1"
# X11 helper libraries for Qt's xcb platform plugin (Ubuntu 20.04 builds: glibc 2.31-compatible and
# older than the libxcb/libxkbcommon of any current desktop). Only for BUNDLE_X11_HELPERS=1.
UBUNTU=http://archive.ubuntu.com/ubuntu/pool
X11_DEBS=(
  "libxcb-cursor0_0.1.1-4ubuntu1_amd64.deb|$UBUNTU/universe/x/xcb-util-cursor/libxcb-cursor0_0.1.1-4ubuntu1_amd64.deb|c9b5d1ad4af57397b1bd77e0a92750e34419def134c0282a0836ae9efc07cf64"
  "libxcb-icccm4_0.4.1-1.1_amd64.deb|$UBUNTU/main/x/xcb-util-wm/libxcb-icccm4_0.4.1-1.1_amd64.deb|03cf5acb68f7d6bffa378f9d30e8d519ee3e5b10340df741af8c0d8bbaf5d3d4"
  "libxcb-image0_0.4.0-1build1_amd64.deb|$UBUNTU/main/x/xcb-util-image/libxcb-image0_0.4.0-1build1_amd64.deb|4002be5159fe91fcfbc6cdb68871163dbfd5c1092341bb51f26079e25b421d92"
  "libxcb-keysyms1_0.4.0-1build1_amd64.deb|$UBUNTU/main/x/xcb-util-keysyms/libxcb-keysyms1_0.4.0-1build1_amd64.deb|6cfbd86d2e1c66d2d8d0a6a17f27f0b825fc0d595647674714a2926a616bdae6"
  "libxcb-render-util0_0.3.9-1build1_amd64.deb|$UBUNTU/main/x/xcb-util-renderutil/libxcb-render-util0_0.3.9-1build1_amd64.deb|5486323b1a43b8d4d1f19986fac99a25c00676cd5596926d973e4966c8f05a84"
  "libxcb-util1_0.4.0-0ubuntu3_amd64.deb|$UBUNTU/main/x/xcb-util/libxcb-util1_0.4.0-0ubuntu3_amd64.deb|83f9a455c8c6e787fcad27a84108704c7aa7669405f2af097cd25cfeeb10d82a"
  "libxkbcommon0_0.10.0-1_amd64.deb|$UBUNTU/main/libx/libxkbcommon/libxkbcommon0_0.10.0-1_amd64.deb|b704ce1751dd938025b88a84c4054d4f36651661e473efeb3da8ed39aaf05868"
  "libxkbcommon-x11-0_0.10.0-1_amd64.deb|$UBUNTU/main/libx/libxkbcommon/libxkbcommon-x11-0_0.10.0-1_amd64.deb|929d5e4ee88d6f5f3b09e238ffae60d667e4c9c4731a0210227b092fc578c6ac"
)

die() { echo "build-appimage: error: $*" >&2; exit 1; }
step() { echo "==> $*"; }

[ "$(uname -s)" = Linux ] || die "build on Linux"
[ "$(uname -m)" = "$ARCH" ] || die "this script builds $ARCH AppImages; this machine is $(uname -m)"
for tool in tar sha256sum ar file ldd objdump unsquashfs; do
  command -v "$tool" >/dev/null || die "missing build tool: $tool (see APPIMAGE.md, prerequisites)"
done
case "$PACKER" in
  mksquashfs) command -v mksquashfs >/dev/null || die "missing build tool: mksquashfs (package squashfs-tools)" ;;
  appimagetool) INPUTS+=("$APPIMAGETOOL") ;;
  *) die "PACKER must be mksquashfs or appimagetool" ;;
esac
[ "$OFFLINE" = 1 ] || command -v curl >/dev/null || die "missing build tool: curl (or build with OFFLINE=1)"

# fetch NAME URL SHA256 - take NAME from the cache or download it; always verify the checksum
fetch() {
  local name="$1" url="$2" sum="$3" target="$CACHE/$1"
  if [ ! -f "$target" ]; then
    [ "$OFFLINE" != 1 ] || die "$name is not in $CACHE (OFFLINE=1); run once with network access"
    step "download $name"
    curl -fL --retry 3 --connect-timeout 30 -o "$target.part" "$url"
    mv "$target.part" "$target"
  fi
  echo "$sum  $target" | sha256sum -c --quiet - || { mv "$target" "$target.rejected"; die "checksum mismatch for $name (kept as $target.rejected)"; }
}

mkdir -p "$CACHE/wheels" "$OUT_DIR"
for entry in "${INPUTS[@]}"; do IFS='|' read -r n u s <<<"$entry"; fetch "$n" "$u" "$s"; done
if [ "$BUNDLE_X11_HELPERS" = 1 ]; then
  for entry in "${X11_DEBS[@]}"; do IFS='|' read -r n u s <<<"$entry"; fetch "$n" "$u" "$s"; done
fi

step "prepare AppDir"
rm -rf "$WORK"
mkdir -p "$WORK/AppDir/usr" "$WORK/dist"
APPDIR="$WORK/AppDir"
tar -xzf "$CACHE/$PBS_FILE" -C "$APPDIR/usr"          # -> AppDir/usr/python
PY="$APPDIR/usr/python/bin/python3"
"$PY" -c 'import sys; assert sys.version_info[:2] == (3, 12)'
export PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1 PYTHONNOUSERSITE=1 PIP_CONFIG_FILE=/dev/null \
  PIP_ROOT_USER_ACTION=ignore
unset PIP_INDEX_URL PIP_EXTRA_INDEX_URL PIP_FIND_LINKS PYTHONPATH PYTHONHOME

step "wheels (versions from requirements.lock)"
if [ "$OFFLINE" != 1 ]; then
  "$PY" -m pip download --quiet --only-binary=:all: --dest "$CACHE/wheels" "setuptools>=69" wheel
fi
# build from a clean copy: the source tree stays untouched and no stale build/ output can leak in
mkdir -p "$WORK/src"
tar -C "$ROOT" --exclude=./packaging/appimage/cache --exclude=./packaging/appimage/build --exclude=./build \
  --exclude=./packaging/models/cache --exclude=./dist --exclude='*.egg-info' --exclude=__pycache__ --exclude=.venv \
  -cf - . | tar -C "$WORK/src" -xf -
"$PY" -m pip wheel --quiet --no-deps --no-index --find-links "$CACHE/wheels" --wheel-dir "$WORK/dist" "$WORK/src"
if [ "$OFFLINE" != 1 ]; then
  "$PY" -m pip download --quiet --only-binary=:all: --dest "$CACHE/wheels" \
    --find-links "$WORK/dist" -c "$HERE/requirements.lock" "${PACKAGE}${EXTRAS}==${VERSION}"
fi
rm -f "$CACHE/wheels/${PACKAGE//-/_}-"*.whl   # the product itself always comes from this source tree

step "install $PACKAGE $VERSION into the bundled Python (no network)"
"$PY" -m pip install --quiet --no-index --find-links "$WORK/dist" --find-links "$CACHE/wheels" \
  -c "$HERE/requirements.lock" --no-warn-script-location "${PACKAGE}${EXTRAS}==${VERSION}"
if [ "$WITHOUT_PYAV" = 1 ]; then
  "$PY" -m pip uninstall --quiet -y av
  echo "    PyAV left out: the AppImage contains no GPL-licensed FFmpeg build"
fi
"$PY" -m pip check
"$PY" -m pip freeze > "$WORK/installed-packages.txt"
SITE="$APPDIR/usr/python/lib/python3.12/site-packages"

# ONNX Runtime 1.29 and later contain a telemetry client on Linux. This product sends nothing anywhere:
# refuse to bundle such a build (requirements.lock pins a version without it).
if [ -d "$SITE/onnxruntime" ] && grep -qa "telemetry_1ds" "$SITE"/onnxruntime/capi/*.so*; then
  die "the bundled ONNX Runtime contains a telemetry client: pin a version below 1.29 in requirements.lock"
fi

if [ "$WITHOUT_MODELS" = 1 ]; then
  echo "    scene models left out: the AppImage has no scene detection"
else
  step "scene models (picture and sound) for the offline scene detection"
  MODELS_OUT="$SITE/fourdvr_extractor/models"
  mkdir -p "$MODELS_OUT"
  if [ -n "$MODELS_DIR" ]; then
    for f in manifest.json scene.onnx concepts.npz sound.onnx sound-labels.csv LICENSES.txt; do
      [ -f "$MODELS_DIR/$f" ] || die "MODELS_DIR=$MODELS_DIR has no $f"
      install -m 0644 "$MODELS_DIR/$f" "$MODELS_OUT/$f"
    done
  else
    if [ "$OFFLINE" != 1 ]; then
      "$PY" -m pip download --quiet --only-binary=:all: --no-deps --dest "$CACHE/wheels" "${ONNX_TOOL[@]}"
    fi
    "$PY" -m pip install --quiet --no-index --no-deps --find-links "$CACHE/wheels" --target "$WORK/tools" "${ONNX_TOOL[@]}"
    offline=()
    [ "$OFFLINE" != 1 ] || offline=(--offline)
    "$PY" "$ROOT/packaging/models/build_models.py" --out "$MODELS_OUT" --cache "$MODELS_CACHE" --tools "$WORK/tools" "${offline[@]}"
  fi
  "$PY" -I -c 'from fourdvr_extractor import scene
ok, why = scene.availability()
assert ok, why
print("    scene models:", ", ".join(sorted(p.name for p in scene.find_models()[0].iterdir())))'
fi

step "trim the interpreter (tests, Tk, IDLE, pip, headers, scripts with absolute paths)"
PYLIB="$APPDIR/usr/python/lib/python3.12"
"$PY" -m pip uninstall --quiet -y pip
rm -rf "$PYLIB/test" "$PYLIB/idlelib" "$PYLIB/tkinter" "$PYLIB/turtledemo" "$PYLIB/ensurepip" \
  "$PYLIB"/lib-dynload/_tkinter* "$APPDIR"/usr/python/lib/{tcl,tk,itcl,thread}* \
  "$APPDIR"/usr/python/lib/lib{tcl,tk}* "$APPDIR/usr/python/include" "$APPDIR/usr/python/share"
find "$APPDIR/usr/python/bin" -mindepth 1 ! -name python3 ! -name python3.12 -delete
find "$APPDIR/usr/python" -name '__pycache__' -type d -prune -exec rm -rf {} +
# byte-compile once; the AppImage is read-only, so Python cannot write .pyc files at run time
# (a few test/template files in third-party packages are not valid Python 3.12 and are skipped)
"$PY" -m compileall -q -j 0 --invalidation-mode unchecked-hash "$PYLIB" > "$WORK/compileall.log" ||
  echo "    $(grep -c '^\*\*\*' "$WORK/compileall.log" || true) file(s) not byte-compiled, see $WORK/compileall.log"

if [ "$BUNDLE_X11_HELPERS" = 1 ]; then
  step "X11 helper libraries for Qt"
  mkdir -p "$APPDIR/usr/lib" "$APPDIR/usr/share/doc" "$WORK/debs"
  for entry in "${X11_DEBS[@]}"; do
    IFS='|' read -r n _u _s <<<"$entry"
    d="$WORK/debs/${n%%_*}"; mkdir -p "$d"
    member="$(ar t "$CACHE/$n" | grep '^data\.tar')"
    (cd "$d" && ar x "$CACHE/$n" "$member" && tar -xf "$member")
    cp -a "$d"/usr/lib/x86_64-linux-gnu/*.so.* "$APPDIR/usr/lib/"
    cp -a "$d"/usr/share/doc/* "$APPDIR/usr/share/doc/" 2>/dev/null || true
  done
fi

step "AppRun, desktop entry, icon, licences"
install -m 0755 "$HERE/AppRun" "$APPDIR/AppRun"
install -m 0644 "$HERE/$DESKTOP_FILE" "$APPDIR/$DESKTOP_FILE"
install -m 0644 "$HERE/$ICON_NAME.png" "$APPDIR/$ICON_NAME.png"
ln -sf "$ICON_NAME.png" "$APPDIR/.DirIcon"
install -D -m 0644 "$HERE/$DESKTOP_FILE" "$APPDIR/usr/share/applications/$DESKTOP_FILE"
install -D -m 0644 "$HERE/$ICON_NAME.png" "$APPDIR/usr/share/icons/hicolor/256x256/apps/$ICON_NAME.png"
install -D -m 0644 "$ROOT/LICENSE" "$APPDIR/usr/share/doc/$APP_NAME/LICENSE"
install -m 0644 "$WORK/installed-packages.txt" "$APPDIR/usr/share/doc/$APP_NAME/installed-packages.txt"
cat > "$APPDIR/usr/share/doc/$APP_NAME/README-APPIMAGE.txt" <<EOF
$APP_NAME $VERSION AppImage ($ARCH)
Python $PY_VERSION from python-build-standalone $PBS_RELEASE (licence: usr/python/lib/python3.12/LICENSE.txt).
Python packages: installed-packages.txt; each package's licence is in its *.dist-info directory under
usr/python/lib/python3.12/site-packages. Qt for Python (PySide6) is LGPLv3: its libraries are
separate shared objects in site-packages/PySide6/Qt/lib and can be replaced (extract the AppImage
with --appimage-extract, replace the files, repackage with appimagetool).
PyAV ("av", BSD) brings FFmpeg libraries in site-packages/av.libs; that FFmpeg build includes
GPL-licensed parts (x264, x265), so the GNU General Public License applies to it when this file
is passed on to others: licence texts and the list of bundled libraries are in
site-packages/av-*.dist-info (licenses/, sboms/); the sources are those of the PyAV project
(https://github.com/PyAV-Org/PyAV and pyav-ffmpeg). An AppImage without it: build with WITHOUT_PYAV=1.
User interface font: JetBrains Mono, SIL Open Font License 1.1 (site-packages/fourdvr_studio/gui/fonts/OFL.txt).
Scene detection: two neural-network models in site-packages/fourdvr_extractor/models (their origin and
licences: LICENSES.txt and manifest.json there; MIT and Apache-2.0), run by ONNX Runtime (MIT; its
third-party notices: site-packages/onnxruntime/ThirdPartyNotices.txt). The bundled ONNX Runtime is a
version without a telemetry client, and AppRun sets ORT_DISABLE_TELEMETRY=1 as well.
EOF

step "check that the bundled native code resolves"
# Everything outside Qt must be self-contained.
missing=0
while IFS= read -r -d '' so; do
  file -b "$so" | grep -q '^ELF' || continue
  case "$so" in */site-packages/PySide6/*|*/site-packages/shiboken6/*) continue ;; esac
  nf="$(LD_LIBRARY_PATH="$APPDIR/usr/lib" ldd "$so" 2>/dev/null | awk '/not found/ {print $1}' || true)"
  if [ -n "$nf" ]; then echo "unresolved in $so: $nf"; missing=1; fi
done < <(find "$APPDIR/usr" -type f \( -name '*.so' -o -name '*.so.*' -o -path '*/bin/python3*' \) -print0)
[ "$missing" = 0 ] || die "unresolved shared libraries outside Qt (see above)"
# Qt (PySide6) uses the desktop's OpenGL, font, D-Bus and core X11 libraries. The parts this
# application loads must resolve with the bundled helpers plus those host libraries.
qt="$APPDIR/usr/python/lib/python3.12/site-packages/PySide6"
if [ -d "$qt" ]; then
  host_libs=""
  for so in "$qt"/Qt/lib/libQt6{Core,Gui,Widgets,DBus,OpenGL,XcbQpa}.so.6 "$qt"/Qt{Core,Gui,Widgets}.abi3.so \
            "$qt"/Qt/plugins/platforms/libq{xcb,offscreen}.so; do
    [ -e "$so" ] || continue
    nf="$(LD_LIBRARY_PATH="$APPDIR/usr/lib" ldd "$so" 2>/dev/null | awk '/not found/ {print $1}' || true)"
    host_libs="$host_libs $nf"
  done
  host_libs="$(tr ' ' '\n' <<<"$host_libs" | sed '/^$/d' | sort -u | tr '\n' ' ')"
  for lib in $host_libs; do
    [ ! -e "$APPDIR/usr/lib/$lib" ] || die "bundled library $lib was not picked up"
  done
  echo "    Qt needs from the host and this build machine lacks: ${host_libs:-nothing}"
fi
glibc="$(find "$APPDIR/usr" -type f \( -name '*.so*' -o -path '*/bin/python3*' \) -exec objdump -T {} + 2>/dev/null \
  | grep -o 'GLIBC_[0-9.]*' | sort -uV | tail -1)"
echo "    newest glibc symbol version required: ${glibc#GLIBC_}"

step "build the AppImage ($PACKER)"
OUT="$OUT_DIR/${APP_NAME}-${VERSION}-${ARCH}.AppImage"
rm -f "$OUT" "$WORK/image.squashfs"
if [ "$PACKER" = mksquashfs ]; then
  # An AppImage (type 2) is the runtime followed directly by a SquashFS image of the AppDir.
  mksquashfs "$APPDIR" "$WORK/image.squashfs" -root-owned -noappend -no-xattrs -quiet -no-progress \
    -comp zstd -Xcompression-level 15 -b 256K -mkfs-time 0 -all-time 0
  cat "$CACHE/runtime-${ARCH}" "$WORK/image.squashfs" > "$OUT"
else
  # appimagetool is itself an AppImage: extract-and-run works without FUSE (containers, CI)
  export ARCH
  APPIMAGE_EXTRACT_AND_RUN=1 "$CACHE/appimagetool-${ARCH}.AppImage" --no-appstream \
    --runtime-file "$CACHE/runtime-${ARCH}" "$APPDIR" "$OUT"
fi
chmod 0755 "$OUT"
(cd "$OUT_DIR" && sha256sum "$(basename "$OUT")" > "$(basename "$OUT").sha256")

if [ "$SKIP_TEST" != 1 ]; then
  step "smoke test: unpack the image and start it without network access"
  offset="$(stat -c %s "$CACHE/runtime-${ARCH}")"
  rm -rf "$WORK/smoke"
  unsquashfs -q -n -o "$offset" -d "$WORK/smoke" "$OUT" >/dev/null
  if command -v unshare >/dev/null && unshare -rn true 2>/dev/null; then
    jail=(unshare -rn)
  else
    echo "    (unshare is not permitted here: tested with the network available)"
    jail=()
  fi
  app="$WORK/smoke/AppRun"
  [ "$("${jail[@]}" "$app" --version)" = "$VERSION" ] || die "smoke test: wrong version"
  "${jail[@]}" "$app" controller --version >/dev/null
  "${jail[@]}" "$app" extract profiles >/dev/null
  "${jail[@]}" "$app" fx example-patch -o "$WORK/smoke-patch.toml" --force >/dev/null
  "${jail[@]}" "$app" fx check-patch "$WORK/smoke-patch.toml" --platforms PLATFORM-01,PLATFORM-02 | tail -1
  "${jail[@]}" "$app" fx example-patch --esp32 -o "$WORK/smoke-patch.toml" --force >/dev/null
  "${jail[@]}" "$app" fx check-patch "$WORK/smoke-patch.toml" --platforms PLATFORM-01,PLATFORM-02 | tail -1
  # live mode: the sources are listed, and the demonstration drive moves a simulated platform for three seconds
  [ "$("${jail[@]}" "$app" live sources | grep -c 'UDP port')" -ge 11 ] || die "smoke test: live sources missing"
  "${jail[@]}" "$app" live run --source demo --platform "$ROOT/config/platforms/simulation-6dof.toml" \
      --seconds 3 --no-window --null-audio > "$WORK/smoke-live.txt" 2>&1 || { cat "$WORK/smoke-live.txt"; die "smoke test: live run failed"; }
  if ! grep -q 'STOPPED' "$WORK/smoke-live.txt" || grep -q -i 'fault\|error' "$WORK/smoke-live.txt"; then
    cat "$WORK/smoke-live.txt"; die "smoke test: live run did not end clean"
  fi
  echo "    live mode: OK ($(tail -1 "$WORK/smoke-live.txt"))"
  # streaming: a video is described without decoding it, and the server starts and ends
  if [ "$WITHOUT_PYAV" != 1 ]; then
    "${jail[@]}" "$app" stream test-video -o "$WORK/smoke-stream.mp4" --seconds 2 --size 320x180 --force --quiet >/dev/null
    "${jail[@]}" "$app" stream check "$WORK/smoke-stream.mp4" | grep -q 'browsers    yes' || die "smoke test: stream check"
  else
    printf 'not a video, only something to serve' > "$WORK/smoke-stream.mp4"
  fi
  "${jail[@]}" "$app" stream serve "$WORK/smoke-stream.mp4" --port 0 --seconds 1 --quiet > "$WORK/smoke-stream.txt" 2>&1 \
    || { cat "$WORK/smoke-stream.txt"; die "smoke test: stream serve failed"; }
  grep -q '^operator      http' "$WORK/smoke-stream.txt" || { cat "$WORK/smoke-stream.txt"; die "smoke test: stream serve said nothing"; }
  echo "    streaming: OK ($(grep -c '' "$WORK/smoke-stream.txt") lines; $(tail -1 "$WORK/smoke-stream.txt"))"
  # the fixture library and the venue check: a device of the library becomes a patch, and that patch is found ready
  [ "$("${jail[@]}" "$app" fx fixtures search --category Hazer | grep -c -i 'hazer')" -ge 5 ] || die "smoke test: the fixture library is missing"
  rm -f "$WORK/smoke-venue.toml"
  "${jail[@]}" "$app" fx fixtures entry cameo/instant-hazer-1500-t-pro --effect fog --patch "$WORK/smoke-venue.toml" \
      > "$WORK/smoke-venue.txt" 2>&1 || { cat "$WORK/smoke-venue.txt"; die "smoke test: fx fixtures entry failed"; }
  "${jail[@]}" "$app" fx ready --patch "$WORK/smoke-venue.toml" -q > "$WORK/smoke-ready.txt" 2>&1 \
    || { cat "$WORK/smoke-ready.txt"; die "smoke test: fx ready failed"; }
  grep -q '^READY' "$WORK/smoke-ready.txt" || { cat "$WORK/smoke-ready.txt"; die "smoke test: fx ready gave no verdict"; }
  echo "    fixture library and venue check: OK ($(grep -m1 -o 'added [^:]*' "$WORK/smoke-venue.txt"); $(tail -1 "$WORK/smoke-ready.txt"))"
  if [ "$WITHOUT_MODELS" != 1 ]; then
    # the scene models, without any network: one picture through the picture model, and the sound model loaded
    "${jail[@]}" "$WORK/smoke/usr/python/bin/python3" -I -c '
import numpy as np
from fourdvr_extractor import scene
models, why = scene.find_models()
assert models is not None, why
tagger = scene.PictureTagger(models)
tagger.offer(0.0, np.full((360, 640, 3), 90, np.uint8), "flat")
times, picture = tagger.finish()
assert len(times) == 1 and set(picture) >= {"rain", "fog", "fire", "bubbles", "snow"}
scene.SoundTagger(models)
print("    scene models: OK (" + ", ".join(sorted(picture)) + "; sound model loaded)")
'
  fi
  # the desktop application: build the window off-screen and draw it once
  QT_QPA_PLATFORM=offscreen "${jail[@]}" "$WORK/smoke/usr/python/bin/python3" -I -c '
import sys
from PySide6.QtWidgets import QApplication
from fourdvr_studio.gui import theme
from fourdvr_studio.gui.main_window import StudioWindow
app = QApplication([])
theme.apply(app)
win = StudioWindow()
win.show(); app.processEvents()
assert theme.load_fonts() == "JetBrains Mono", "bundled font missing"
assert not win.grab().isNull()
win.close()
print("    Editor window: OK (bundled font " + theme.load_fonts() + ")")
' 2>&1 | grep -v propagateSizeHints
  rm -rf "$WORK/smoke" "$WORK/smoke-patch.toml" "$WORK/smoke-live.txt" "$WORK/smoke-stream.mp4" "$WORK/smoke-stream.txt" \
         "$WORK/smoke-venue.toml" "$WORK/smoke-venue.txt" "$WORK/smoke-ready.txt"
fi
echo "built $OUT ($(du -h "$OUT" | cut -f1)); newest glibc symbol needed: ${glibc#GLIBC_}"
