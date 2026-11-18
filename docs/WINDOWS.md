# Windows

Extended 4D Editor is developed and tested on Linux. Since 3.3 the source also holds everything
needed for a Windows build: code that no longer assumes Linux, a PyInstaller build of two programs,
an installer, and a GitHub Actions workflow that builds and tries them on a Windows machine.

**None of it has run on Windows yet.** The author's build machine is Linux and cannot build or start
a Windows program. What was done there: the code paths for Windows were written and type-checked,
the build description was run through PyInstaller's build on Linux (it found every module and data
file; the result was not started), and the workflow file was checked for syntax. The first run of
the workflow is the first test - read its log before you trust the result.

## Getting the `.exe`

1. Put the source into a GitHub repository (the whole folder, including `.github/`).
2. On GitHub: **Actions > Windows build > Run workflow**. Tick *Bundle the scene models* if you want
   scene detection without a download later (about 205 MB more). Or push a tag: `git tag v3.3.0 &&
   git push origin v3.3.0` builds with the models and attaches the files to a release of that name.
3. After 20-40 minutes the run offers two downloads under *Artifacts*:
   * `Extended-4D-Editor-3.3.0-windows-x64-setup` - the installer;
   * `Extended-4D-Editor-3.3.0-windows-x64-zip` - the same programs as a folder, and
     `windows-test-log.txt`.

What the workflow does, in this order: installs the program with all its options on Python 3.12;
runs the test suite (its result is reported in `windows-test-log.txt` and does not stop the build -
some tests assume Linux); builds; **tries the built programs** (version, help, the live sources, the
fixture library, an interactive session of a few seconds with its pages and simulated players, and
the Editor's window, which must come up and stay up); packs the ZIP; builds the installer.
If "Try the built programs" fails, there are no downloads: the log says which step failed.

If a run fails, the log of the failed step is what is needed to fix it.

## What is installed

| | |
|---|---|
| `Extended 4D Editor.exe` | the Editor's window |
| `fourdvr.exe` | every command of [CLI.md](CLI.md), in a command prompt |
| `_internal\` | Python, Qt, the libraries, the documents (`_internal\docs`), example configurations |

The installer installs for the current user by default (no administrator needed; it asks if you
want it for everybody). Options: a desktop icon, **add the folder to PATH** so that `fourdvr` works
in every command prompt, and opening `.4dvr` projects with a double-click. The Start menu gets the
Editor, "fourdvr command line" (a prompt in which the command works without PATH) and the
documents. Uninstalling removes all of it; your projects and settings stay.

The ZIP needs no installation: unpack it and start a program from the folder. The two programs
need the `_internal` folder next to them.

Windows may warn that the publisher is unknown: the files are not code-signed. Signing needs a
certificate in your organisation's name; the workflow has no step for it.

## What is different on Windows

* **Your own data** - downloaded scene models, your own fixture files - is under
  `%LOCALAPPDATA%\Extended 4D Editor` instead of `~/.local/share/extended-4d-editor`.
* **Serial ports** are named `COM3` and so on, where the documents say `/dev/ttyUSB0`.
* **The show service** (`fourdvr controller run`) runs as a program in a command prompt. The
  systemd unit and the installation script in `deploy/` are for Linux; running it as a Windows
  service (Task Scheduler, NSSM) is not set up.
* **The firewall** asks the first time a program listens: allow it on private networks for the
  stream server (TCP 8080), the interactive hub (TCP and UDP 47020) and the live sources' ports.
* **The live mode's screen source** was written for X11; whether it captures a Windows screen was
  not tried. Game telemetry over UDP, the open live format and sound input do not depend on it.
* **The AppImage** is Linux only.

## Building on your own Windows PC

```powershell
py -3.12 -m venv .venv
.venv\Scripts\activate
python -m pip install ".[all]" "pyinstaller>=6.6,<7"
pyinstaller --noconfirm --clean packaging\windows\extended-4d-editor.spec
dist\Extended-4D-Editor\fourdvr.exe version
```

For the installer, install [Inno Setup 6](https://jrsoftware.org/isinfo.php) and run
`ISCC.exe /DAppVersion=3.3.0 packaging\windows\installer.iss`. To bundle the scene models, build
them first (`python packaging\models\build_models.py --out build\models --cache
build\models-cache`, see [SCENE_DETECTION.md](SCENE_DETECTION.md)) and set
`FOURDVR_MODELS_DIR` to that folder before running PyInstaller.

Running from source needs no build at all: `pip install ".[studio]"` in a Python 3.12 (or newer)
environment, then `fourdvr gui`.
