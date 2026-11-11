#!/usr/bin/env python3
"""Build the scene-detection models of the Extended 4D Editor from their published originals.

    python packaging/models/build_models.py --out ~/.local/share/extended-4d-editor/models

Downloads (about 720 MB, once, into --cache; every file is checked against a pinned SHA-256):

* picture model - CLIP ViT-B/32 trained on LAION-400M by the OpenCLIP authors (MIT licence), from the
  OpenCLIP repository's release "v0.2-weights";
* sound model - Zipformer-S audio tagger trained on AudioSet with icefall (Apache-2.0), from the
  sherpa-onnx release "audio-tagging-models".

and writes into --out (about 205 MB):

    scene.onnx        the picture model's image tower as an ONNX graph (weights stored as 16-bit floats)
    concepts.npz      the text side: one embedding per prompt of concepts.json
    sound.onnx        the sound model (8-bit quantised, as published)
    sound-labels.csv  its 527 AudioSet class names
    LICENSES.txt      the licences of both models
    manifest.json     what was built from what

Needs Python 3.10+, NumPy and the `onnx` package; with `onnxruntime` installed the result is also
verified. PyTorch is not needed: the checkpoint is read by a small reader that accepts tensors and
nothing else (it cannot execute anything from the file), the ONNX graph is written node by node, and a
NumPy implementation of the same network serves as the reference the graph is checked against.
"""

from __future__ import annotations

import argparse
import collections
import gzip
import hashlib
import html
import io
import itertools
import json
import os
import pickle
import re
import struct
import sys
import tarfile
import urllib.request
import zipfile
from functools import lru_cache
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
FORMAT = 1
OPEN_CLIP = "4cc208d3a44a2fb6d6435006a46f520d2ccc29b1"       # revision the vocabulary and licence are taken from
ICEFALL = "9c966762e058bd7ba79d9da71e42fc06b64b703e"
SOUND_DIR = "sherpa-onnx-zipformer-small-audio-tagging-2024-04-15"

INPUTS: dict[str, tuple[str, str]] = {
    "clip-vit-b32-laion400m-e32.pt": (
        "https://github.com/mlfoundations/open_clip/releases/download/v0.2-weights/vit_b_32-quickgelu-laion400m_e32-46683a32.pt",
        "46683a32721d5c68911153698992361285d20ca690bb4f317c11e45c03d798fa",
    ),
    "bpe_simple_vocab_16e6.txt.gz": (
        f"https://raw.githubusercontent.com/mlfoundations/open_clip/{OPEN_CLIP}/src/open_clip/bpe_simple_vocab_16e6.txt.gz",
        "924691ac288e54409236115652ad4aa250f48203de50a9e4722a6ecd48d6804a",
    ),
    "open_clip-LICENSE.txt": (
        f"https://raw.githubusercontent.com/mlfoundations/open_clip/{OPEN_CLIP}/LICENSE",
        "4a5b4f13ea4792a8211a69f32d2b460b6d520e57cd23c6301e707c76a6e97a55",
    ),
    "zipformer-small-audio-tagging-2024-04-15.tar.bz2": (
        f"https://github.com/k2-fsa/sherpa-onnx/releases/download/audio-tagging-models/{SOUND_DIR}.tar.bz2",
        "07e2fafcdcbc461f2816188d9b0bbafced12584030cf67d5652e549ef256a2c6",
    ),
    "icefall-LICENSE.txt": (
        f"https://raw.githubusercontent.com/k2-fsa/icefall/{ICEFALL}/LICENSE",
        "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30",
    ),
}

MEAN = np.array([0.48145466, 0.4578275, 0.40821073], np.float32)      # CLIP picture normalisation
STD = np.array([0.26862954, 0.26130258, 0.27577711], np.float32)


# ====================================================================== downloads
def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def fetch(cache: Path, name: str, offline: bool) -> Path:
    url, digest = INPUTS[name]
    path = cache / name
    if path.exists() and sha256_of(path) == digest:
        return path
    if offline:
        raise SystemExit(f"error: {name} is not in {cache} (or has the wrong checksum) and --offline was given")
    print(f"  downloading {name}", flush=True)
    part = path.with_suffix(path.suffix + ".part")
    with urllib.request.urlopen(url, timeout=120) as r, open(part, "wb") as f:      # (fixed https addresses, above)
        for block in iter(lambda: r.read(1 << 20), b""):
            f.write(block)
    if sha256_of(part) != digest:
        part.rename(path.with_suffix(path.suffix + ".rejected"))
        raise SystemExit(f"error: checksum mismatch for {name}: kept as *.rejected, nothing was built")
    part.rename(path)
    return path


# ====================================================================== PyTorch checkpoint -> NumPy, without PyTorch
_DTYPES = {
    "FloatStorage": np.float32, "HalfStorage": np.float16, "DoubleStorage": np.float64, "LongStorage": np.int64,
    "IntStorage": np.int32, "ShortStorage": np.int16, "CharStorage": np.int8, "ByteStorage": np.uint8, "BoolStorage": np.bool_,
}


class _StorageType:
    def __init__(self, name: str) -> None:
        self.dtype = np.dtype(_DTYPES[name])


class _Storage:
    def __init__(self, key: str, dtype: np.dtype, numel: int) -> None:
        self.key, self.dtype, self.numel = key, dtype, numel
        self.data: np.ndarray | None = None


class _Tensor:
    def __init__(self, storage: _Storage, offset: int, size: tuple[int, ...], stride: tuple[int, ...]) -> None:
        self.storage, self.offset, self.size, self.stride = storage, offset, tuple(size), tuple(stride)

    def numpy(self) -> np.ndarray:
        flat = self.storage.data
        if flat is None:
            raise ValueError("tensor storage missing from the checkpoint")
        n = int(np.prod(self.size)) if self.size else 1
        contiguous, acc = [], 1
        for s in reversed(self.size):
            contiguous.insert(0, acc)
            acc *= s
        if tuple(contiguous) != self.stride and n > 1:
            raise ValueError("checkpoint holds a non-contiguous tensor")
        return flat[self.offset : self.offset + n].reshape(self.size)


def _rebuild_tensor(storage, offset, size, stride, requires_grad=False, backward_hooks=None, metadata=None):  # type: ignore[no-untyped-def]
    return _Tensor(storage, offset, size, stride)


def _rebuild_parameter(data, requires_grad=False, backward_hooks=None):  # type: ignore[no-untyped-def]
    return data


class _TensorsOnly(pickle.Unpickler):
    """Accepts ordered dictionaries, tensors and storages - the content of a plain state dict - and refuses
    every other name, so nothing in the file can be made to run."""

    def __init__(self, f, storages: dict[str, _Storage]) -> None:  # type: ignore[no-untyped-def]
        super().__init__(f)
        self.storages = storages

    def find_class(self, module: str, name: str):  # type: ignore[no-untyped-def]
        if module == "torch" and name in _DTYPES:
            return _StorageType(name)
        if (module, name) == ("collections", "OrderedDict"):
            return collections.OrderedDict
        if (module, name) == ("torch._utils", "_rebuild_tensor_v2"):
            return _rebuild_tensor
        if (module, name) == ("torch._utils", "_rebuild_parameter"):
            return _rebuild_parameter
        raise pickle.UnpicklingError(f"refused: {module}.{name} does not belong in a plain state dict")

    def persistent_load(self, pid):  # type: ignore[no-untyped-def]
        if not isinstance(pid, tuple) or len(pid) < 5 or pid[0] != "storage" or not isinstance(pid[1], _StorageType):
            raise pickle.UnpicklingError(f"refused persistent id {pid!r}")
        key = str(pid[2])
        if key not in self.storages:
            self.storages[key] = _Storage(key, pid[1].dtype, int(pid[4]))
        return self.storages[key]


def _to_numpy(obj):  # type: ignore[no-untyped-def]
    if isinstance(obj, _Tensor):
        return obj.numpy()
    if isinstance(obj, dict):
        return {k: _to_numpy(v) for k, v in obj.items()}
    return obj


def read_checkpoint(path: str | Path) -> dict[str, np.ndarray]:
    """The tensors of a PyTorch checkpoint (zip container or the older stream format) as NumPy arrays."""
    path = Path(path)
    storages: dict[str, _Storage] = {}
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            pkl = next(n for n in z.namelist() if n.endswith("data.pkl"))
            obj = _TensorsOnly(io.BytesIO(z.read(pkl)), storages).load()
            for st in storages.values():
                st.data = np.frombuffer(z.read(f"{pkl[: -len('data.pkl')]}data/{st.key}"), dtype=st.dtype, count=st.numel)
    else:
        with open(path, "rb") as f:
            if pickle.Unpickler(f).load() != 0x1950A86A20F9469CFC6C:
                raise ValueError(f"{path.name} is not a PyTorch checkpoint")
            pickle.Unpickler(f).load()                # protocol version (an integer)
            _TensorsOnly(f, {}).load()                # system information (a plain dictionary)
            obj = _TensorsOnly(f, storages).load()
            for key in _TensorsOnly(f, {}).load():    # storage keys, then the raw data in that order
                st = storages[str(key)]
                (numel,) = struct.unpack("<q", f.read(8))
                if numel != st.numel:
                    raise ValueError("checkpoint storage sizes do not match")
                st.data = np.frombuffer(f.read(numel * st.dtype.itemsize), dtype=st.dtype)
    sd = _to_numpy(obj)
    while isinstance(sd, dict) and "state_dict" in sd:
        sd = sd["state_dict"]
    return sd


# ====================================================================== the network in NumPy (reference + text tower)
def _layer_norm(x: np.ndarray, w: np.ndarray, b: np.ndarray, eps: float = 1e-5) -> np.ndarray:
    m = x.mean(-1, keepdims=True)
    v = ((x - m) ** 2).mean(-1, keepdims=True)
    return (x - m) / np.sqrt(v + eps) * w + b


def _softmax(x: np.ndarray) -> np.ndarray:
    e = np.exp(x - x.max(axis=-1, keepdims=True))
    return e / e.sum(axis=-1, keepdims=True)


def _layers(sd: dict[str, np.ndarray], prefix: str) -> int:
    return 1 + max(int(k[len(prefix) :].split(".")[1]) for k in sd if k.startswith(prefix + "resblocks."))


def _transformer(x: np.ndarray, sd: dict[str, np.ndarray], prefix: str, mask: np.ndarray | None = None) -> np.ndarray:
    b, t, d = x.shape
    heads = max(1, d // 64)
    hd = d // heads
    for i in range(_layers(sd, prefix)):
        p = f"{prefix}resblocks.{i}."
        y = _layer_norm(x, sd[p + "ln_1.weight"], sd[p + "ln_1.bias"])
        qkv = y @ sd[p + "attn.in_proj_weight"].T + sd[p + "attn.in_proj_bias"]
        q, k, v = (a.reshape(b, t, heads, hd).transpose(0, 2, 1, 3) for a in np.split(qkv, 3, axis=-1))
        att = (q @ k.transpose(0, 1, 3, 2)) / np.sqrt(hd)
        if mask is not None:
            att = att + mask
        y = (_softmax(att) @ v).transpose(0, 2, 1, 3).reshape(b, t, d)
        x = x + y @ sd[p + "attn.out_proj.weight"].T + sd[p + "attn.out_proj.bias"]
        y = _layer_norm(x, sd[p + "ln_2.weight"], sd[p + "ln_2.bias"])
        y = y @ sd[p + "mlp.c_fc.weight"].T + sd[p + "mlp.c_fc.bias"]
        y = y / (1.0 + np.exp(-1.702 * y))                       # QuickGELU
        x = x + y @ sd[p + "mlp.c_proj.weight"].T + sd[p + "mlp.c_proj.bias"]
    return x


def encode_image(sd: dict[str, np.ndarray], pixels: np.ndarray) -> np.ndarray:
    """pixels: (N, 3, S, S) float32, normalised. Returns unit vectors (N, D)."""
    w = sd["visual.conv1.weight"]
    d, c, ps, _ = w.shape
    n, g = pixels.shape[0], pixels.shape[2] // ps
    patches = pixels.reshape(n, c, g, ps, g, ps).transpose(0, 2, 4, 1, 3, 5).reshape(n, g * g, c * ps * ps)
    x = patches @ w.reshape(d, -1).T
    x = np.concatenate([np.broadcast_to(sd["visual.class_embedding"], (n, 1, d)), x], axis=1) + sd["visual.positional_embedding"]
    x = _layer_norm(x, sd["visual.ln_pre.weight"], sd["visual.ln_pre.bias"])
    x = _transformer(x, sd, "visual.transformer.")
    x = _layer_norm(x[:, 0], sd["visual.ln_post.weight"], sd["visual.ln_post.bias"]) @ sd["visual.proj"]
    return x / np.linalg.norm(x, axis=-1, keepdims=True)


def encode_text(sd: dict[str, np.ndarray], tokens: np.ndarray) -> np.ndarray:
    """tokens: (N, 77). Returns unit vectors (N, D)."""
    x = sd["token_embedding.weight"][tokens] + sd["positional_embedding"]
    t = x.shape[1]
    x = _transformer(x, sd, "transformer.", np.triu(np.full((t, t), -np.inf, np.float32), 1))
    x = _layer_norm(x, sd["ln_final.weight"], sd["ln_final.bias"])
    x = x[np.arange(x.shape[0]), tokens.argmax(-1)] @ sd["text_projection"]
    return x / np.linalg.norm(x, axis=-1, keepdims=True)


class Tokenizer:
    """The byte-pair encoder of CLIP, for plain lower-case ASCII prompts."""

    def __init__(self, vocab_gz: str | Path) -> None:
        merges = [tuple(m.split()) for m in gzip.open(vocab_gz).read().decode("utf-8").split("\n")[1 : 49152 - 256 - 2 + 1]]
        first = list(range(ord("!"), ord("~") + 1)) + list(range(ord("¡"), ord("¬") + 1)) + list(range(ord("®"), ord("ÿ") + 1))
        codes = first + [256 + i for i in range(256 - len(first))]
        vocab = [chr(c) for c in codes]
        vocab = vocab + [v + "</w>" for v in vocab] + ["".join(m) for m in merges] + ["<|startoftext|>", "<|endoftext|>"]
        self.encoder = {v: i for i, v in enumerate(vocab)}
        self.ranks = {m: i for i, m in enumerate(merges)}
        self.words = re.compile(r"'s|'t|'re|'ve|'m|'ll|'d|[a-z]+|[0-9]|[^\sa-z0-9]+")

    @lru_cache(maxsize=4096)  # noqa: B019 - one tokenizer per build
    def _bpe(self, token: str) -> tuple[str, ...]:
        word: tuple[str, ...] = (*token[:-1], token[-1] + "</w>")
        while len(word) > 1:
            best = min(itertools.pairwise(word), key=lambda p: self.ranks.get(p, float("inf")))
            if best not in self.ranks:
                break
            merged, i = [], 0
            while i < len(word):
                if i < len(word) - 1 and (word[i], word[i + 1]) == best:
                    merged.append(word[i] + word[i + 1])
                    i += 2
                else:
                    merged.append(word[i])
                    i += 1
            word = tuple(merged)
        return word

    def __call__(self, texts: list[str], context: int = 77) -> np.ndarray:
        out = np.zeros((len(texts), context), np.int64)
        for row, text in enumerate(texts):
            text = re.sub(r"\s+", " ", html.unescape(text)).strip().lower()
            if not text.isascii():
                raise SystemExit(f"error: prompt {text!r}: only plain ASCII prompts are supported")
            ids = [self.encoder["<|startoftext|>"]]
            for word in self.words.findall(text):
                ids += [self.encoder[p] for p in self._bpe(word)]
            ids.append(self.encoder["<|endoftext|>"])
            if len(ids) > context:
                raise SystemExit(f"error: prompt too long: {text!r}")
            out[row, : len(ids)] = ids
        return out


# ====================================================================== the image tower as an ONNX graph
def build_image_onnx(sd: dict[str, np.ndarray], half: bool = True):  # type: ignore[no-untyped-def]
    """ONNX model: pixels (N, 3, S, S) -> embedding (N, D), unit length. `half` stores the large weight
    tensors as 16-bit floats and converts them to 32-bit inside the graph (ONNX Runtime folds that
    when it loads the model): half the file, the same results."""
    import onnx
    from onnx import TensorProto, helper, numpy_helper

    nodes: list = []
    inits: list = []

    def const(name: str, arr: np.ndarray) -> str:
        arr = np.ascontiguousarray(arr) if arr.ndim else arr      # (ascontiguousarray would turn a scalar into a vector)
        if half and arr.dtype == np.float32 and arr.size > 4096:
            inits.append(numpy_helper.from_array(arr.astype(np.float16), name + "_h"))
            nodes.append(helper.make_node("Cast", [name + "_h"], [name], to=TensorProto.FLOAT))
        else:
            inits.append(numpy_helper.from_array(arr, name))
        return name

    def node(op: str, ins: list[str], out: str, **attrs) -> str:  # type: ignore[no-untyped-def]
        nodes.append(helper.make_node(op, ins, [out], name=out, **attrs))
        return out

    def ints(name: str, values: list[int]) -> str:
        return const(name, np.array(values, np.int64))

    def linear(x: str, w: np.ndarray, b: np.ndarray, name: str) -> str:
        y = node("MatMul", [x, const(name + "_w", w.T.astype(np.float32))], name + "_mm")
        return node("Add", [y, const(name + "_b", b.astype(np.float32))], name + "_out")

    def lnorm(x: str, prefix: str, name: str) -> str:
        return node("LayerNormalization", [x, const(name + "_g", sd[prefix + ".weight"]), const(name + "_o", sd[prefix + ".bias"])],
                    name, axis=-1, epsilon=1e-5)

    w = sd["visual.conv1.weight"]
    d, ps = int(w.shape[0]), int(w.shape[2])
    heads = max(1, d // 64)
    hd = d // heads
    size = ps * round((sd["visual.positional_embedding"].shape[0] - 1) ** 0.5)
    x = node("Conv", ["pixels", const("patch_w", w)], "patches", kernel_shape=[ps, ps], strides=[ps, ps])
    x = node("Transpose", [node("Reshape", [x, ints("shape_tokens", [0, d, -1])], "patches_flat")], "tokens", perm=[0, 2, 1])
    first = node("Slice", [x, ints("i0", [0]), ints("i1", [1]), ints("ax1", [1])], "first_token")
    cls = node("Add", [node("Mul", [first, const("zero", np.zeros((), np.float32))], "first_zero"),
                       const("class_embedding", sd["visual.class_embedding"])], "class_token")
    x = node("Add", [node("Concat", [cls, x], "with_class", axis=1), const("positional_embedding", sd["visual.positional_embedding"])], "embedded")
    x = lnorm(x, "visual.ln_pre", "ln_pre")
    scale = const("att_scale", np.array(1.0 / np.sqrt(hd), np.float32))
    gelu_k = const("gelu_k", np.array(1.702, np.float32))
    split_shape, merge_shape = ints("split_heads", [0, 0, heads, hd]), ints("merge_heads", [0, 0, d])
    for i in range(_layers(sd, "visual.transformer.")):
        p, n = f"visual.transformer.resblocks.{i}.", f"b{i}"
        qkv = linear(lnorm(x, p + "ln_1", n + "_ln1"), sd[p + "attn.in_proj_weight"], sd[p + "attn.in_proj_bias"], n + "_qkv")
        nodes.append(helper.make_node("Split", [qkv], [n + "_q", n + "_k", n + "_v"], name=n + "_split", axis=-1, num_outputs=3))
        q = node("Transpose", [node("Reshape", [n + "_q", split_shape], n + "_q4")], n + "_qt", perm=[0, 2, 1, 3])
        k = node("Transpose", [node("Reshape", [n + "_k", split_shape], n + "_k4")], n + "_kt", perm=[0, 2, 3, 1])
        v = node("Transpose", [node("Reshape", [n + "_v", split_shape], n + "_v4")], n + "_vt", perm=[0, 2, 1, 3])
        att = node("Softmax", [node("Mul", [node("MatMul", [q, k], n + "_qk"), scale], n + "_qks")], n + "_att", axis=-1)
        y = node("Reshape", [node("Transpose", [node("MatMul", [att, v], n + "_av")], n + "_avt", perm=[0, 2, 1, 3]), merge_shape], n + "_merged")
        x = node("Add", [x, linear(y, sd[p + "attn.out_proj.weight"], sd[p + "attn.out_proj.bias"], n + "_proj")], n + "_res1")
        y = linear(lnorm(x, p + "ln_2", n + "_ln2"), sd[p + "mlp.c_fc.weight"], sd[p + "mlp.c_fc.bias"], n + "_fc")
        y = node("Mul", [y, node("Sigmoid", [node("Mul", [y, gelu_k], n + "_gk")], n + "_gs")], n + "_gelu")
        x = node("Add", [x, linear(y, sd[p + "mlp.c_proj.weight"], sd[p + "mlp.c_proj.bias"], n + "_cproj")], n + "_res2")
    x = lnorm(node("Gather", [x, const("idx0", np.array(0, np.int64))], "class_out", axis=1), "visual.ln_post", "ln_post")
    node("LpNormalization", [node("MatMul", [x, const("proj", sd["visual.proj"])], "projected")], "embedding", axis=-1, p=2)
    graph = helper.make_graph(
        nodes, "clip_image_tower",
        [helper.make_tensor_value_info("pixels", TensorProto.FLOAT, ["n", 3, size, size])],
        [helper.make_tensor_value_info("embedding", TensorProto.FLOAT, ["n", int(sd["visual.proj"].shape[1])])],
        inits,
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 18)], producer_name="extended-4d-editor")
    model.ir_version = 9
    onnx.checker.check_model(model)
    return model


# ====================================================================== build
def test_picture(size: int = 224) -> np.ndarray:
    """A deterministic, structured test picture (normalised pixels) for comparing implementations."""
    y, x = np.mgrid[0:size, 0:size].astype(np.float32) / size
    rgb = np.stack([0.5 + 0.5 * np.sin(9 * x + 2 * y), y, 0.5 + 0.5 * np.cos(14 * x * y)], axis=-1)
    rgb[size // 4 : size // 2, size // 3 : 2 * size // 3] = (0.9, 0.2, 0.1)
    return ((rgb - MEAN) / STD).transpose(2, 0, 1)[None].astype(np.float32)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--out", required=True, help="directory to write the model files to")
    ap.add_argument("--cache", default=str(HERE / "cache"), help="where the downloads are kept (default: packaging/models/cache)")
    ap.add_argument("--concepts", default=str(HERE / "concepts.json"), help="the prompts (default: concepts.json next to this script)")
    ap.add_argument("--offline", action="store_true", help="do not download: every input must be in the cache")
    ap.add_argument("--tools", action="append", default=[], help="extra directory to import the onnx package from")
    args = ap.parse_args(argv)
    sys.path[:0] = args.tools
    out, cache = Path(args.out), Path(args.cache)
    out.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    spec = json.loads(Path(args.concepts).read_text())
    files = {name: fetch(cache, name, args.offline) for name in INPUTS}

    print("picture model: reading the checkpoint", flush=True)
    sd = read_checkpoint(files["clip-vit-b32-laion400m-e32.pt"])
    visual = {k: v for k, v in sd.items() if k.startswith("visual.")}
    import onnx

    model = build_image_onnx(visual, half=True)
    onnx.save(model, str(out / "scene.onnx"))
    reference = encode_image(visual, test_picture())
    try:
        os.environ["ORT_DISABLE_TELEMETRY"] = "1"      # newer ONNX Runtime builds report usage otherwise
        import onnxruntime as ort

        got = ort.InferenceSession(str(out / "scene.onnx"), providers=["CPUExecutionProvider"]).run(None, {"pixels": test_picture()})[0]
        cosine = float((got * reference).sum())
        if cosine < 0.9995:
            raise SystemExit(f"error: the ONNX graph does not reproduce the reference network (cosine {cosine:.5f})")
        print(f"  ONNX graph agrees with the NumPy reference (cosine {cosine:.6f})")
    except ImportError:
        print("  note: onnxruntime is not installed - the ONNX graph was not verified")

    print("picture model: embedding the prompts (a few minutes)", flush=True)
    tok = Tokenizer(files["bpe_simple_vocab_16e6.txt.gz"])
    text = {k: v for k, v in sd.items() if not k.startswith("visual.")}
    prompts = list(dict.fromkeys([*spec["common"], *(p for c in spec["concepts"].values() for p in (*c["pos"], *c["contrast"]))]))
    emb = np.concatenate([encode_text(text, tok(prompts[i : i + 24])) for i in range(0, len(prompts), 24)]).astype(np.float32)
    index = {p: i for i, p in enumerate(prompts)}
    arrays: dict[str, np.ndarray] = {"common": emb[[index[p] for p in spec["common"]]]}
    for name, c in spec["concepts"].items():
        arrays[f"pos:{name}"] = emb[[index[p] for p in c["pos"]]]
        arrays[f"contrast:{name}"] = emb[[index[p] for p in c["contrast"]]]
    np.savez(out / "concepts.npz", spec=np.array(json.dumps({"common": spec["common"], "concepts": spec["concepts"]})),
             picture_size=np.array(int(model.graph.input[0].type.tensor_type.shape.dim[2].dim_value)), mean=MEAN, std=STD, **arrays)

    print("sound model: unpacking", flush=True)
    with tarfile.open(files["zipformer-small-audio-tagging-2024-04-15.tar.bz2"]) as tar:
        for member, target in (("model.int8.onnx", "sound.onnx"), ("class_labels_indices.csv", "sound-labels.csv")):
            src = tar.extractfile(f"{SOUND_DIR}/{member}")
            if src is None:
                raise SystemExit(f"error: {member} is missing from the sound model archive")
            (out / target).write_bytes(src.read())
        readme = tar.extractfile(f"{SOUND_DIR}/README.md")
        sound_readme = readme.read().decode("utf-8", "replace") if readme is not None else ""

    (out / "LICENSES.txt").write_text(
        "Models of the Extended 4D Editor's scene detection\n"
        "==================================================\n\n"
        "scene.onnx, concepts.npz - CLIP ViT-B/32 trained on LAION-400M by the OpenCLIP authors, published in the\n"
        "OpenCLIP repository (https://github.com/mlfoundations/open_clip, release v0.2-weights,\n"
        "vit_b_32-quickgelu-laion400m_e32-46683a32.pt). Converted without changes to the weights (stored as 16-bit\n"
        "floats). The repository's licence (MIT):\n\n"
        + files["open_clip-LICENSE.txt"].read_text()
        + "\n\nsound.onnx, sound-labels.csv - Zipformer-S audio tagger, trained on AudioSet with icefall\n"
        "(https://github.com/k2-fsa/icefall, egs/audioset/AT, Apache License 2.0), as published in the sherpa-onnx\n"
        f"release audio-tagging-models ({SOUND_DIR}), unchanged. Its README states:\n\n"
        + sound_readme.strip()
        + "\n\nThe class names are those of the AudioSet ontology (Google, CC BY-SA 4.0).\n"
        "The Apache License 2.0 as distributed with icefall:\n\n"
        + files["icefall-LICENSE.txt"].read_text()
    )
    manifest = {
        "format": FORMAT,
        "picture": {"file": "scene.onnx", "concepts": "concepts.npz", "model": "CLIP ViT-B/32, LAION-400M (OpenCLIP), image tower",
                    "licence": "MIT", "source": INPUTS["clip-vit-b32-laion400m-e32.pt"][0]},
        "sound": {"file": "sound.onnx", "labels": "sound-labels.csv", "model": "Zipformer-S audio tagger, AudioSet (icefall / sherpa-onnx)",
                  "licence": "Apache-2.0", "source": INPUTS["zipformer-small-audio-tagging-2024-04-15.tar.bz2"][0],
                  "sample_rate_hz": 16000, "mel_bins": 80},
        "concepts": list(spec["concepts"]),
        "inputs": {name: digest for name, (_url, digest) in INPUTS.items()},
        "files": {p.name: {"bytes": p.stat().st_size, "sha256": sha256_of(p)} for p in sorted(out.iterdir()) if p.name != "manifest.json"},
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    total = sum(p.stat().st_size for p in out.iterdir())
    print(f"wrote {out} ({total / 1e6:.0f} MB): " + ", ".join(sorted(p.name for p in out.iterdir())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
