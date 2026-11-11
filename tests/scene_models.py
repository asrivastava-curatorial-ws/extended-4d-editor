"""Tiny stand-in models with the file layout of the real scene models (packaging/models/build_models.py):
the same graph builder with a small random network, random concept embeddings, and a three-node sound
model. They exercise all of the plumbing; what they "detect" is arbitrary."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packaging" / "models"))

SOUND_NAMES = ["Rain", "Raindrop", "Rain on surface", "Thunderstorm", "Thunder", "Fire", "Crackle", "Explosion", "Boom",
               "Splash, splatter", "Spray", "Waterfall", "Waves, surf", "Ocean", "Gurgling", "Boiling", "Wind", "Wind noise (microphone)"]


def tiny_state_dict(width: int = 64, layers: int = 2, patch: int = 32, size: int = 64, embed: int = 32, seed: int = 3) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(seed)

    def r(*shape: int, scale: float = 0.2) -> np.ndarray:
        return (scale * rng.standard_normal(shape)).astype(np.float32)

    sd = {
        "visual.conv1.weight": r(width, 3, patch, patch, scale=0.02),
        "visual.class_embedding": r(width),
        "visual.positional_embedding": r((size // patch) ** 2 + 1, width),
        "visual.ln_pre.weight": 1 + r(width), "visual.ln_pre.bias": r(width),
        "visual.ln_post.weight": 1 + r(width), "visual.ln_post.bias": r(width),
        "visual.proj": r(width, embed),
    }
    for i in range(layers):
        p = f"visual.transformer.resblocks.{i}."
        sd |= {
            p + "ln_1.weight": 1 + r(width), p + "ln_1.bias": r(width), p + "ln_2.weight": 1 + r(width), p + "ln_2.bias": r(width),
            p + "attn.in_proj_weight": r(3 * width, width), p + "attn.in_proj_bias": r(3 * width),
            p + "attn.out_proj.weight": r(width, width), p + "attn.out_proj.bias": r(width),
            p + "mlp.c_fc.weight": r(4 * width, width), p + "mlp.c_fc.bias": r(4 * width),
            p + "mlp.c_proj.weight": r(width, 4 * width), p + "mlp.c_proj.bias": r(width),
        }
    return sd


def make_models(directory: Path, size: int = 64, embed: int = 32) -> Path:
    import onnx
    from build_models import MEAN, STD, build_image_onnx
    from onnx import TensorProto, helper, numpy_helper

    directory.mkdir(parents=True, exist_ok=True)
    onnx.save(build_image_onnx(tiny_state_dict(size=size, embed=embed), half=True), str(directory / "scene.onnx"))
    spec = json.loads((ROOT / "packaging" / "models" / "concepts.json").read_text())
    rng = np.random.default_rng(5)

    def unit(n: int) -> np.ndarray:
        v = rng.standard_normal((n, embed)).astype(np.float32)
        return v / np.linalg.norm(v, axis=1, keepdims=True)

    arrays = {"common": unit(len(spec["common"]))}
    for name, c in spec["concepts"].items():
        arrays[f"pos:{name}"], arrays[f"contrast:{name}"] = unit(len(c["pos"])), unit(len(c["contrast"]))
    np.savez(directory / "concepts.npz", spec=np.array(json.dumps({"common": spec["common"], "concepts": spec["concepts"]})),
             picture_size=np.array(size), mean=MEAN, std=STD, **arrays)
    labels = [f"class {i}" for i in range(527)]
    for i, name in enumerate(SOUND_NAMES):
        labels[20 + 7 * i] = name
    (directory / "sound-labels.csv").write_text(
        "index,mid,display_name\n" + "".join(f'{i},/m/{i},"{n}"\n' for i, n in enumerate(labels)), encoding="utf-8")
    w = (0.02 * rng.standard_normal((80, 527))).astype(np.float32)
    nodes = [
        helper.make_node("ReduceMean", ["x"], ["pooled"], axes=[1], keepdims=0),
        helper.make_node("MatMul", ["pooled", "w"], ["scores"]),
        helper.make_node("Sigmoid", ["scores"], ["logits"]),
    ]
    graph = helper.make_graph(
        nodes, "tiny_tagger",
        [helper.make_tensor_value_info("x", TensorProto.FLOAT, ["N", "T", 80]), helper.make_tensor_value_info("x_lens", TensorProto.INT64, ["N"])],
        [helper.make_tensor_value_info("logits", TensorProto.FLOAT, ["N", 527])], [numpy_helper.from_array(w, "w")])
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 9
    onnx.save(model, str(directory / "sound.onnx"))
    (directory / "manifest.json").write_text(json.dumps({"format": 1, "concepts": list(spec["concepts"])}))
    return directory
