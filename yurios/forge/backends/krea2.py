"""Local Krea 2 backend — her own camera, on your own GPU, INT4 (→ ch. 11, ch. 26).

The second local checkpoint family, next to `diffusers.py`'s SDXL. Krea 2 is a
diffusion *transformer* (28 blocks, ~12.5 B parameters), not a UNet, and it is
conditioned by a Qwen3-VL text encoder instead of SDXL's two CLIPs. `diffusers`
ships the architecture (`Krea2Pipeline`, `Krea2Transformer2DModel`); what it
does *not* ship is a way to read the community's quantized single-file
checkpoints, which is what this module adds.

**Why the weights stay quantized.** Dequantized to bf16 this transformer is
~23 GB — it does not fit on a 16 GB card, and dequantizing is therefore not an
option, it's a crash. The checkpoints are stored in "ConvRot W4A4" (INT4,
~6.1 GB), a layout implemented by Comfy Org's `comfy-kitchen`. So the weights
are never expanded: each quantized `nn.Linear` gets a `QuantizedTensor` in
place of its weight, and `comfy-kitchen`'s registered layout ops intercept
`F.linear` and run an INT4 tensor-core GEMM directly on the packed data.
That keeps the resident footprint at the file's own size and is *faster* than
bf16, not slower.

**What the checkpoint doesn't carry.** These single-file exports are the
transformer only — no text encoder, no VAE, no tokenizer. Those come once from
the `krea/Krea-2-Raw` repo on Hugging Face (everything except its own
multi-GB transformer weights, which this file replaces). That repo is *gated*:
accept its licence while signed in, then `huggingface-cli login`. Until then
this backend reports unhealthy and the SelfieLab degrades to placeholder cards,
the same graceful-fallback rule as a missing OpenRouter key.

Content posture: none, as with any local checkpoint — what renders is between
the user and the model's licence (`capabilities().uncensored` says so).

Heavy deps (torch, diffusers, transformers, comfy-kitchen) are lazy-imported on
first render — importing this module is free, per the repo's seam rule.
"""

from __future__ import annotations

import io
import json
import logging
import os
import secrets
from pathlib import Path
from typing import Any, Dict, Optional

from ..types import Capabilities, GenRequest, ImageResult
from .base import ImageBackend

log = logging.getLogger("forge.krea2")

# The companion repo the text encoder / VAE / tokenizer / scheduler come from.
# Everything EXCEPT its transformer weights — the local checkpoint is those.
COMPANION_REPO = "krea/Krea-2-Raw"
COMPANION_ALLOW = ["model_index.json", "scheduler/*", "tokenizer/*",
                   "text_encoder/*", "vae/*", "transformer/config.json"]

_GATED_HELP = (
    f"Krea 2 needs the text encoder and VAE from the gated Hugging Face repo "
    f"{COMPANION_REPO}. Accept its licence at https://huggingface.co/"
    f"{COMPANION_REPO} while signed in, then run `huggingface-cli login` "
    f"(or set HF_TOKEN). Only the small components are fetched — the "
    f"transformer weights are your local checkpoint.")


# ---- the key map: single-file checkpoint -> Krea2Transformer2DModel ----------
# Pure string rules, no torch: the whole mapping is unit-testable offline.

_TOP_LEVEL: Dict[str, str] = {
    "first.weight":              "img_in.weight",
    "first.bias":                "img_in.bias",
    "tproj.1.weight":            "time_mod_proj.weight",
    "tproj.1.bias":              "time_mod_proj.bias",
    "tmlp.0.weight":             "time_embed.linear_1.weight",
    "tmlp.0.bias":               "time_embed.linear_1.bias",
    "tmlp.2.weight":             "time_embed.linear_2.weight",
    "tmlp.2.bias":               "time_embed.linear_2.bias",
    "txtmlp.0.scale":            "txt_in.norm.weight",
    "txtmlp.1.weight":           "txt_in.linear_1.weight",
    "txtmlp.1.bias":             "txt_in.linear_1.bias",
    "txtmlp.3.weight":           "txt_in.linear_2.weight",
    "txtmlp.3.bias":             "txt_in.linear_2.bias",
    "last.norm.scale":           "final_layer.norm.weight",
    "last.modulation.lin":       "final_layer.scale_shift_table",
    "last.linear.weight":        "final_layer.linear.weight",
    "last.linear.bias":          "final_layer.linear.bias",
    "txtfusion.projector.weight": "text_fusion.projector.weight",
}

# Inside any block, image or text-fusion. `mod.lin` only exists on image
# blocks (text-fusion blocks carry no time modulation).
_BLOCK_SUFFIX: Dict[str, str] = {
    "mod.lin":                   "scale_shift_table",
    "prenorm.scale":             "norm1.weight",
    "postnorm.scale":            "norm2.weight",
    "attn.wq.weight":            "attn.to_q.weight",
    "attn.wk.weight":            "attn.to_k.weight",
    "attn.wv.weight":            "attn.to_v.weight",
    "attn.wo.weight":            "attn.to_out.0.weight",
    "attn.gate.weight":          "attn.to_gate.weight",
    "attn.qknorm.qnorm.scale":   "attn.norm_q.weight",
    "attn.qknorm.knorm.scale":   "attn.norm_k.weight",
    "mlp.gate.weight":           "ff.gate.weight",
    "mlp.up.weight":             "ff.up.weight",
    "mlp.down.weight":           "ff.down.weight",
}

# Longest-first: "txtfusion.…_blocks." must be tried before a bare "blocks.".
_BLOCK_PREFIX = (
    ("txtfusion.layerwise_blocks.", "text_fusion.layerwise_blocks."),
    ("txtfusion.refiner_blocks.",   "text_fusion.refiner_blocks."),
    ("blocks.",                     "transformer_blocks."),
)


def rename_key(key: str) -> Optional[str]:
    """One checkpoint tensor name -> its diffusers name, or None if unknown."""
    if key in _TOP_LEVEL:
        return _TOP_LEVEL[key]
    for src, dst in _BLOCK_PREFIX:
        if key.startswith(src):
            idx, _, suffix = key[len(src):].partition(".")
            if not idx.isdigit():
                return None
            tail = _BLOCK_SUFFIX.get(suffix)
            return f"{dst}{idx}.{tail}" if tail else None
    return None


class Krea2Backend(ImageBackend):
    name = "krea2"

    # What a fully resident pipeline wants: ~6.4 GB of INT4 transformer plus the
    # ~8.5 GB Qwen3-VL text encoder, and both are on the card at once unless
    # offloading cycles them. Measured on a 16 GB 5070 Ti, where even with the
    # LLM parked there isn't room — so on that card this reads "offload", which
    # is the honest answer rather than an OOM and a reload. A 24 GB card clears
    # it and runs resident.
    RESIDENT_FREE_GIB = 15.5

    # Base (midtrain) Krea 2 sampling defaults, from the diffusers docstring.
    # Distilled "turbo"/TDM checkpoints want few steps and NO guidance —
    # driving one at 28/4.5 burns the image, so the two are picked apart below.
    BASE_STEPS, BASE_CFG = 28, 4.5
    TURBO_STEPS, TURBO_CFG = 8, 0.0

    def __init__(
        self,
        model_path: str = "",
        *,
        device: str = "cuda",
        steps: int = 0,                        # 0 = pick from the checkpoint
        cfg: float = -1.0,                     # <0 = pick from the checkpoint
        cpu_offload: bool = False,
        max_sequence_length: int = 512,
        attention_backend: str = "_native_cudnn",
    ) -> None:
        self.model_path = model_path
        self.device = device
        self.steps = steps
        self.cfg = cfg
        self.cpu_offload = cpu_offload
        self.max_sequence_length = max_sequence_length
        self.attention_backend = attention_backend
        self._pipe = None                      # loaded on first render

    # ---- availability (cheap; imports nothing heavy) ----

    @staticmethod
    def _needs_offload(free_gib: float) -> bool:
        return free_gib < Krea2Backend.RESIDENT_FREE_GIB

    @staticmethod
    def deps_available() -> bool:
        import importlib.util
        return all(importlib.util.find_spec(m) is not None
                   for m in ("torch", "diffusers", "transformers", "comfy_kitchen"))

    def health(self) -> bool:
        return (self.deps_available()
                and bool(self.model_path)
                and Path(self.model_path).is_file())

    # ---- the checkpoint's own metadata ----

    def _metadata(self) -> Dict[str, Any]:
        """The safetensors header's `__metadata__`, or {} if unreadable."""
        from .sniff import read_safetensors_header
        return read_safetensors_header(self.model_path).get("__metadata__", {}) or {}

    def _is_turbo(self) -> bool:
        """Distilled checkpoints say so in their title (or their filename)."""
        title = str(self._metadata().get("modelspec.title", ""))
        hay = f"{title} {Path(self.model_path).name}".lower()
        return any(t in hay for t in ("turbo", "tdm", "lightning", "distill"))

    def _defaults(self) -> tuple[int, float]:
        """(steps, cfg) — explicit config wins; otherwise the checkpoint picks."""
        turbo = self._is_turbo()
        steps = self.steps or (self.TURBO_STEPS if turbo else self.BASE_STEPS)
        cfg = self.cfg if self.cfg >= 0 else (self.TURBO_CFG if turbo else self.BASE_CFG)
        return steps, cfg

    # ---- the companion components (one gated fetch, cached forever after) ----

    @staticmethod
    def _prepare_env() -> None:
        # Fragmentation, not footprint, kills most low-VRAM renders. Costs
        # nothing; setdefault means the user's own setting always wins.
        os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

    def _snapshot(self) -> str:
        """Text encoder + VAE + tokenizer + scheduler, never the transformer."""
        from huggingface_hub import snapshot_download
        try:
            return snapshot_download(COMPANION_REPO, allow_patterns=COMPANION_ALLOW)
        except Exception as e:
            # Gated / not-logged-in is the overwhelmingly likely cause, and the
            # stock message doesn't say what to do about it. Anything else
            # (offline, disk) still reaches the user with its own text.
            raise RuntimeError(f"{_GATED_HELP} (underlying error: {e})") from e

    # ---- loading: the weights never leave INT4 ----

    def _quantized_tensor(self, qdata, scale, spec: Dict[str, Any], dtype):
        """Wrap packed INT4 data so `F.linear` runs the tensor-core GEMM."""
        from comfy_kitchen.tensor.base import QuantizedTensor
        from comfy_kitchen.tensor.convrot_w4a4 import TensorCoreConvRotW4A4Layout
        rows, packed = qdata.shape
        params = TensorCoreConvRotW4A4Layout.Params(
            scale=scale, orig_dtype=dtype,
            orig_shape=(rows, packed * 2),     # 2 nibbles per int8 byte
            convrot_groupsize=int(spec.get("convrot_groupsize", 256)),
            quant_group_size=int(spec.get("quant_group_size", 64)),
            linear_dtype="int4")
        return QuantizedTensor(qdata, "TensorCoreConvRotW4A4Layout", params)

    def _build_transformer(self, snapshot: str, dtype, device: str):
        from accelerate import init_empty_weights
        from diffusers import Krea2Transformer2DModel
        from safetensors.torch import safe_open

        cfg_path = Path(snapshot) / "transformer" / "config.json"
        with init_empty_weights():             # meta device: allocates nothing
            model = (Krea2Transformer2DModel.from_config(
                        json.loads(cfg_path.read_text()))
                     if cfg_path.is_file() else Krea2Transformer2DModel())

        want = set(model.state_dict())
        seen: set[str] = set()
        with safe_open(self.model_path, framework="pt", device="cpu") as f:
            meta = f.metadata() or {}
            qlayers = json.loads(meta.get("_quantization_metadata", "{}")).get("layers", {})
            for key in f.keys():
                if key.endswith(".weight_scale"):
                    continue                   # consumed with its weight
                new = rename_key(key)
                if new is None:
                    log.debug("krea2: ignoring unmapped tensor %s", key)
                    continue
                base = key[: -len(".weight")] if key.endswith(".weight") else key
                if base in qlayers:
                    t = self._quantized_tensor(
                        f.get_tensor(key).to(device),
                        f.get_tensor(base + ".weight_scale").to(device),
                        qlayers[base], dtype)
                else:
                    t = f.get_tensor(key).to(device=device, dtype=dtype)
                    if new.endswith("scale_shift_table") and t.ndim == 1:
                        t = t.reshape(6, -1)   # blocks store the 6 rows flat
                _assign_param(model, new, t)
                seen.add(new)

        missing = want - seen
        if missing:
            raise RuntimeError(
                f"{Path(self.model_path).name} is missing {len(missing)} tensor(s) "
                f"this Krea 2 architecture needs, e.g. {sorted(missing)[:4]}. "
                f"It may be a different Krea 2 variant than this build maps.")
        model.eval()
        return model

    def _load(self):
        if self._pipe is not None:
            return self._pipe
        self._prepare_env()
        import torch
        from diffusers import Krea2Pipeline

        cuda = self.device == "cuda" and torch.cuda.is_available()
        dtype = torch.bfloat16 if cuda else torch.float32
        if cuda and not self.cpu_offload \
                and self._needs_offload(torch.cuda.mem_get_info()[0] / 1024**3):
            # Cheaper than the crash: loading resident here would OOM partway,
            # and the retry below would pay for the whole load twice (~a minute
            # on this model). Decide up front instead. (Same rule as the SDXL
            # backend, with a floor this model's two big components set.)
            log.warning(
                "krea2: %.1f GiB VRAM free, a resident pipeline wants ~%.0f — "
                "using model CPU offload for this session "
                "(SELFIE_LOCAL_CPU_OFFLOAD=true makes it permanent).",
                torch.cuda.mem_get_info()[0] / 1024**3, self.RESIDENT_FREE_GIB)
            self.cpu_offload = True
        offload = self.cpu_offload and cuda
        snapshot = self._snapshot()
        # When offloading, the transformer must start on the CPU like every
        # other component: accelerate's hooks move each one onto the card only
        # for its own turn, and a transformer pinned to cuda up front would sit
        # there through the text encoder's pass — which is exactly the 8.5 GB
        # that then has nowhere to land. Building it on the device directly is
        # the resident path only.
        transformer = self._build_transformer(
            snapshot, dtype, "cpu" if (offload or not cuda) else "cuda")
        pipe = Krea2Pipeline.from_pretrained(
            snapshot, transformer=transformer, torch_dtype=dtype)
        if offload:
            pipe.enable_model_cpu_offload()
        else:
            pipe = pipe.to("cuda" if cuda else "cpu")
        if cuda:
            self._pick_attention(pipe)
        self._pipe = pipe
        return pipe

    def _pick_attention(self, pipe) -> None:
        """Krea 2 attends over ~4.5 k tokens (512 text + a 1 MP latent grid)
        behind a padding mask, and the mask is the problem: PyTorch's flash
        kernel refuses a non-null `attn_mask` and the memory-efficient one is
        commonly unavailable, so SDPA silently falls back to the *math* kernel
        — which materialises the whole 48-head attention matrix. Measured on a
        5070 Ti at this build's default size: 8.59 GB for math versus 0.16 GB
        for cuDNN, i.e. the difference between OOM and comfortable.

        Not fatal if it doesn't take (older torch, no cuDNN attention): the
        render still runs on whatever SDPA picks, it just needs far more room.
        """
        try:
            pipe.transformer.set_attention_backend(self.attention_backend)
            log.debug("krea2: attention backend %s", self.attention_backend)
        except Exception as e:
            log.warning(
                "krea2: could not select the %s attention backend (%s) — "
                "falling back to PyTorch's default, which for a masked "
                "sequence this long can need several GB more VRAM.",
                self.attention_backend, e)

    def _teardown(self) -> None:
        """Drop the pipeline and hand the VRAM back (the parker's reset)."""
        self._pipe = None
        import gc
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass

    def _make_generator(self, seed: int):
        import torch
        device = self.device if (self.device == "cuda"
                                 and torch.cuda.is_available()) else "cpu"
        return torch.Generator(device=device).manual_seed(seed)

    # ---- the seam ----

    def generate(self, req: GenRequest) -> ImageResult:
        oom = False
        try:
            return self._render(req)
        except RuntimeError as e:
            # NOTE: the retry must happen OUTSIDE this except block — while the
            # exception is alive its traceback pins the dead pipeline's frame,
            # and with it the gigabytes the retry needs back. (Same rule as the
            # SDXL backend; see diffusers.py.)
            if self.cpu_offload or "out of memory" not in str(e).lower():
                raise
            oom = True
        if oom:
            log.warning(
                "krea2: CUDA out of memory — retrying this render with model "
                "CPU offload (slower, much smaller VRAM footprint). Make it "
                "permanent: SELFIE_LOCAL_CPU_OFFLOAD=true in .env")
            self.cpu_offload = True
            self._teardown()
            return self._render(req)

    def _render(self, req: GenRequest) -> ImageResult:
        pipe = self._load()
        seed = req.seed if req.seed is not None else secrets.randbelow(2**31)
        d_steps, d_cfg = self._defaults()
        steps = req.steps or d_steps
        cfg = req.cfg if req.cfg is not None else d_cfg

        # Unlike SDXL's two 77-token CLIPs, Qwen3-VL takes the whole assembled
        # prompt (512 tokens here), so plain strings go straight in — no
        # chunking, no embedding surgery.
        image = pipe(
            prompt=req.prompt,
            negative_prompt=req.negative_prompt or None,
            width=req.width, height=req.height,
            num_inference_steps=steps,
            guidance_scale=cfg,
            max_sequence_length=self.max_sequence_length,
            generator=self._make_generator(seed),
        ).images[0]

        out = io.BytesIO()
        image.save(out, "PNG")
        return ImageResult.new(
            out.getvalue(), self.name,
            model=Path(self.model_path).name, seed=seed,
            prompt=req.prompt, negative=req.negative_prompt,
            steps=steps, cfg=cfg, sampler="FlowMatchEulerDiscrete",
            width=image.width, height=image.height)

    def capabilities(self) -> Capabilities:
        return Capabilities(
            name=self.name,
            uncensored=True,                  # a local checkpoint refuses nothing
            notes=f"local Krea 2 INT4 checkpoint {self.model_path or '(unset)'} "
                  f"via diffusers + comfy-kitchen; text encoder/VAE from "
                  f"{COMPANION_REPO}; no LoRA in this build",
        )


def _assign_param(model, dotted: str, tensor) -> None:
    """Put one tensor on the meta-initialised model, parameter or buffer.

    `load_state_dict` is deliberately not used: these are tensor *subclasses*
    (QuantizedTensor), and assigning them directly is what keeps the packed
    INT4 data — and its layout dispatch — intact.
    """
    import torch.nn as nn
    mod = model
    *path, leaf = dotted.split(".")
    for part in path:
        mod = getattr(mod, part)
    if leaf in getattr(mod, "_buffers", {}):
        mod._buffers[leaf] = tensor
    else:
        mod._parameters[leaf] = nn.Parameter(tensor, requires_grad=False)
