# Third-party components

Extended 4D Editor itself is MIT-licensed (LICENSE). It uses the following components. When it
is installed with pip, they come from PyPI under their own licences; the AppImage redistributes
them, each with its licence text in its `*.dist-info` directory or package folder
(`usr/python/lib/python3.12/site-packages` inside the image), and the two scene models described
below.

| Component | Used for | Licence | Needed by |
|---|---|---|---|
| Python | runtime | PSF License | all |
| NumPy | numerics | BSD-3-Clause | all |
| Pydantic (pydantic-core, annotated-types, typing-extensions, typing-inspection) | server protocol messages | MIT (typing-extensions: PSF) | show service |
| websockets | connection to the Control Server | BSD-3-Clause | show service |
| SciPy | filters, resampling | BSD-3-Clause | Editor / extraction |
| OpenCV (`opencv-python-headless`), with its bundled FFmpeg (LGPL build) | video decoding, feature tracking | Apache-2.0 (FFmpeg: LGPL-2.1-or-later) | Editor / extraction |
| Qt for Python (`PySide6-Essentials`, `shiboken6`) and Qt 6 | desktop application | LGPL-3.0 (also GPL / commercial) | Editor |
| pyqtgraph | motion plots | MIT | Editor |
| PyAV (`av`) | decoding the sound track | BSD-3-Clause; **its binary wheels bundle FFmpeg libraries that include GPL-licensed parts (x264, x265)** | Editor (optional: an `ffmpeg` program is used instead when PyAV is absent) |
| miniaudio (`miniaudio` Python package, with the miniaudio C library) | bass / seat-vibration output | MIT (C library: MIT-0 or public domain) | optional (`[show]`, `[studio]`) |
| cffi, pycparser | binding of miniaudio | MIT, BSD-3-Clause | with miniaudio |
| pyserial | USB DMX interfaces, serial ESP32 links | BSD-3-Clause | optional (`[show]`, `[studio]`) |
| ONNX Runtime (`onnxruntime`, version below 1.29 — see the note) | running the two scene models on the processor | MIT; third-party notices in `onnxruntime/ThirdPartyNotices.txt` | Editor / extraction (scene detection) |
| FlatBuffers, Protocol Buffers (`flatbuffers`, `protobuf`), `packaging` | required by ONNX Runtime | Apache-2.0, BSD-3-Clause, Apache-2.0 or BSD-2-Clause | with ONNX Runtime |
| kaldi-native-fbank | sound features (mel filter bank) for the sound model | Apache-2.0 | Editor / extraction (scene detection) |
| ONNX (`onnx`) | writing the picture model's file when the models are built | Apache-2.0 | building the models only; not in the AppImage |
| JetBrains Mono (Regular, Medium, Bold; v2.304) | user-interface font, in `src/fourdvr_studio/gui/fonts/` | SIL Open Font License 1.1 (`OFL.txt` next to the fonts) | Editor |
| AppImage type-2 runtime | start-up code of the AppImage | MIT | AppImage only |
| python-build-standalone | the Python inside the AppImage | Python: PSF; build scripts: MPL-2.0; bundled libraries under their own licences (`usr/python/lib/python3.12/LICENSE.txt` and the distribution's licence files) | AppImage only |
| libxcb utility libraries, libxkbcommon (Ubuntu 20.04 builds) | Qt's X11 platform plugin on minimal systems | MIT / X11 (`usr/share/doc/` inside the image) | AppImage only |

## The scene models

Two neural-network models are inside the AppImage (`site-packages/fourdvr_extractor/models`, with
`LICENSES.txt` and a manifest of their origin). They are not in the source package;
`packaging/models/build_models.py` downloads them from their publishers and converts them
(docs/SCENE_DETECTION.md §5).

| File | What | From | Licence |
|---|---|---|---|
| `scene.onnx`, `concepts.npz` | image part of CLIP ViT-B/32, trained on LAION-400M by the OpenCLIP authors; weights unchanged, stored as 16-bit numbers. `concepts.npz` holds this project's own descriptions, turned into numbers by the model's text part | <https://github.com/mlfoundations/open_clip>, release `v0.2-weights`, file `vit_b_32-quickgelu-laion400m_e32-46683a32.pt` | MIT — the licence of the repository that publishes the weights; no separate licence accompanies them |
| `sound.onnx`, `sound-labels.csv` | Zipformer audio tagger (small), trained on AudioSet with icefall; unchanged | <https://github.com/k2-fsa/sherpa-onnx>, release `audio-tagging-models`, `sherpa-onnx-zipformer-small-audio-tagging-2024-04-15` | Apache-2.0, as stated in the model's README (icefall: Apache-2.0). The 527 sound names are those of Google's AudioSet, which its publisher offers under CC BY 4.0 (the ontology under CC BY-SA 4.0) |

Both models were trained by their authors on material collected from the Internet (image–text
pairs; YouTube sound clips). This project did not train or change them. If the provenance of
training data matters for your use, read the publishers' documentation; the models can be left
out (delete the folder, or build the AppImage with `WITHOUT_MODELS=1`): the program then works
as before, without scene detection.

## Notes for those who pass the software on

* **Qt** is used under the LGPLv3: the Qt libraries are separate shared objects that can be
  replaced (in the AppImage: extract with `--appimage-extract`, replace the files under
  `site-packages/PySide6/Qt/lib`, repack). Source code of Qt and PySide6: <https://download.qt.io/>.
* **PyAV / FFmpeg**: because the FFmpeg build inside the `av` wheel contains GPL components, the
  GNU General Public License applies to that part of an AppImage (or any other bundle) that
  includes it. If you give such a bundle to others, pass on the licence texts (they are in
  `site-packages/av-*.dist-info/licenses` and `sboms`) and make the corresponding source available
  (PyAV: <https://github.com/PyAV-Org/PyAV>; its FFmpeg build scripts: `pyav-ffmpeg` in the same
  organisation). To avoid this, build the AppImage with `WITHOUT_PYAV=1` (docs/APPIMAGE.md) — the
  Editor then reads sound tracks with an `ffmpeg` program installed on the computer, and everything
  else is unaffected. Using the software inside your own organisation is not distribution.
* **ONNX Runtime and telemetry.** From version 1.29 the Linux builds of ONNX Runtime contain a
  client that sends usage events to Microsoft and is switched on by default (the package's own
  `Privacy.md`). This program is meant to send nothing anywhere. It therefore requires a version
  below 1.29 — the AppImage contains 1.28.0, whose library has no such client — and additionally
  sets `ORT_DISABLE_TELEMETRY=1` before loading ONNX Runtime, the documented switch that keeps the
  client from being created. If you raise the version limit, keep that switch and check the
  package's privacy notes.
* This table reflects the versions pinned in `packaging/appimage/requirements.lock` at release
  time. Check the licence files of the versions you actually ship.

This is a summary for orientation, not legal advice.
