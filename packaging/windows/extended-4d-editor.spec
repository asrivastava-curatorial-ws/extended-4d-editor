# -*- mode: python ; coding: utf-8 -*-
# PyInstaller build of Extended 4D Editor for Windows (one folder, two programs in it):
#
#     dist\Extended-4D-Editor\Extended 4D Editor.exe     the Editor's window
#     dist\Extended-4D-Editor\fourdvr.exe                the command line (fourdvr play run ..., fourdvr live ...)
#
#   python -m pip install ".[all]" "pyinstaller>=6.6,<7"
#   pyinstaller --noconfirm --clean packaging\windows\extended-4d-editor.spec
#
# Scene detection needs two model files (docs/APPIMAGE.md, "Scene models"). Build them with
# packaging\models\build_models.py and set FOURDVR_MODELS_DIR to their folder before this build;
# without it the programs work, but "Analyse video" drafts no effects from what is in the picture.
#
# docs/WINDOWS.md says what was and what was not tested. This file was written on Linux and has NOT
# been run by its author.
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

HERE = Path(SPECPATH).resolve()
ROOT = HERE.parents[1]
OURS = ["fourdvr_studio", "fourdvr_extractor", "fourdvr_fx", "fourdvr_motion", "fourdvr_live", "fourdvr_stream", "fourdvr_play"]

datas = []
hidden = []
for package in OURS:
    # fonts, profiles, the fixture library, the pages of the stream server and of the hub, the Godot add-on
    datas += collect_data_files(package, include_py_files=False)
    hidden += collect_submodules(package)       # every part is imported only when its command is used
hidden += collect_submodules("websockets")      # (it imports its own parts by name at run time)

models = os.environ.get("FOURDVR_MODELS_DIR", "")
if models and (Path(models) / "manifest.json").is_file():
    datas += [(str(p), "fourdvr_extractor/models") for p in Path(models).iterdir() if p.is_file()]
    print(f"scene models: bundled from {models}")
else:
    print("scene models: NOT bundled (FOURDVR_MODELS_DIR is not set or has no manifest.json)")

# what a user reads, next to the programs
for name in ("LICENSE", "README.md", "CHANGELOG.md", "THIRD_PARTY_LICENSES.md", "VERSION"):
    if (ROOT / name).is_file():
        datas.append((str(ROOT / name), "."))
for folder in ("docs", "config"):
    for path in (ROOT / folder).rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts:
            datas.append((str(path), str(Path(folder) / path.relative_to(ROOT / folder).parent)))

EXCLUDES = ["tkinter", "matplotlib", "IPython", "pytest", "onnx", "torch", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets"]
ICON = str(HERE / "extended-4d-editor.ico")


def analysis(script):
    return Analysis([str(HERE / script)], pathex=[str(ROOT / "src")], binaries=[], datas=datas, hiddenimports=hidden,
                    hookspath=[], runtime_hooks=[], excludes=EXCLUDES, noarchive=False)


console = analysis("fourdvr_console.py")
window = analysis("editor_window.py")
console_exe = EXE(PYZ(console.pure), console.scripts, [], exclude_binaries=True, name="fourdvr", console=True, icon=ICON,
                  upx=False, disable_windowed_traceback=False)
window_exe = EXE(PYZ(window.pure), window.scripts, [], exclude_binaries=True, name="Extended 4D Editor", console=False,
                 icon=ICON, upx=False)
COLLECT(console_exe, window_exe, console.binaries, console.datas, window.binaries, window.datas, strip=False, upx=False,
        name="Extended-4D-Editor")
