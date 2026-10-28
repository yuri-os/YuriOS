"""Local Anima backend — her own camera, on your own GPU (→ ch. 11, ch. 26).

The third local checkpoint family, beside `diffusers.py`'s SDXL and `krea2.py`.
Anima is a ~2 B diffusion transformer — the Cosmos Predict2 DiT — conditioned
by a Qwen3-0.6B text encoder through a small "LLM adapter" that cross-attends
from T5 token embeddings to the Qwen3 states, and decoded by the Qwen-Image
VAE. `diffusers` ships all of it (`AnimaAutoBlocks`, a *modular* pipeline,
`CosmosTransformer3DModel`, `AnimaTextConditioner`); what it does not ship is
a way to read the community's single-file checkpoints, which is what this
module adds — the same job `krea2.py` does for its family, minus the INT4.

**What the checkpoint carries.** The diffusion transformer *and* the LLM
adapter, both under `model.diffusion_model.` — a fine-tune or merge may have
moved either, so both come from the file. It carries no text encoder, no VAE,
no tokenizers: those come once from the ungated `COMPANION_REPO` on Hugging
Face (everything except its own transformer and adapter weights, which this
file replaces), and are cached after that.

**Footprint.** bf16 throughout, nothing to dequantize: ~5.3 GB resident, ~9.3 GB
at peak for a 832×1216 render with guidance on (measured on a 5070 Ti) —
smaller than SDXL with its hires pass.

Content posture: none, as with any local checkpoint — what renders is between
the user and the model's licence (`capabilities().uncensored` says so).

Heavy deps (torch, diffusers, transformers) are lazy-imported on first render —
importing this module is free, per the repo's seam rule.
"""

from __future__ import annotations

import io
import json
import logging
import os
import secrets
from pathlib import Path
from typing import Any, Optional

from ..types import Capabilities, GenRequest, ImageResult
from .base import ImageBackend
from .sniff import ANIMA_PREFIXES

log = logging.getLogger("forge.anima")

# The companion repo the text encoder / VAE / tokenizers / scheduler come from.
# Everything EXCEPT its transformer and text-conditioner weights — the local
# checkpoint is those.
COMPANION_REPO = "circlestone-labs/Anima-Base-v1.0-Diffusers"
COMPANION_ALLOW = ["scheduler/*", "tokenizer/*", "t5_tokenizer/*", "text_encoder/*",
                   "vae/*", "transformer/config.json", "text_conditioner/config.json"]


# ---- the key map: single-file checkpoint -> the two diffusers models ---------
# Pure string rules, no torch: the whole mapping is unit-testable offline. The
# same table diffusers uses to convert an Anima LoRA
# (`_convert_non_diffusers_anima_lora_to_diffusers`), plus the q/k norms a LoRA
# never touches but a full checkpoint has.

# Exports differ in the wrapper around the model's own names; strip any of them.
# The list is sniff's, so the header check counts blocks under every prefix
# this loader reads.
_PREFIXES = ANIMA_PREFIXES
_ADAPTER = "llm_adapter."

# Applied in order, as substring replacements on a transformer key.
_TRANSFORMER_RULES: tuple[tuple[str, str], ...] = (
    ("adaln_modulation_self_attn.1",   "norm1.linear_1"),
    ("adaln_modulation_self_attn.2",   "norm1.linear_2"),
    ("adaln_modulation_cross_attn.1",  "norm2.linear_1"),
    ("adaln_modulation_cross_attn.2",  "norm2.linear_2"),
    ("adaln_modulation_mlp.1",         "norm3.linear_1"),
    ("adaln_modulation_mlp.2",         "norm3.linear_2"),
    ("self_attn.q_proj",               "attn1.to_q"),
    ("self_attn.k_proj",               "attn1.to_k"),
    ("self_attn.v_proj",               "attn1.to_v"),
    ("self_attn.output_proj",          "attn1.to_out.0"),
    ("self_attn.q_norm",               "attn1.norm_q"),
    ("self_attn.k_norm",               "attn1.norm_k"),
    ("cross_attn.q_proj",              "attn2.to_q"),
    ("cross_attn.k_proj",              "attn2.to_k"),
    ("cross_attn.v_proj",              "attn2.to_v"),
    ("cross_attn.output_proj",         "attn2.to_out.0"),
    ("cross_attn.q_norm",              "attn2.norm_q"),
    ("cross_attn.k_norm",              "attn2.norm_k"),
    ("mlp.layer1",                     "ff.net.0.proj"),
    ("mlp.layer2",                     "ff.net.2"),
    ("final_layer.adaln_modulation.1", "norm_out.linear_1"),
    ("final_layer.adaln_modulation.2", "norm_out.linear_2"),
    ("final_layer.linear",             "proj_out"),
    ("t_embedder.1",                   "time_embed.t_embedder"),
    ("t_embedding_norm",               "time_embed.norm"),
    ("x_embedder.proj.1",              "patch_embed.proj"),
)


def rename_key(key: str) -> Optional[tuple[str, str]]:
    """One checkpoint tensor name -> (component, its diffusers name), where
    component is "transformer" or "text_conditioner". None for a tensor that
    belongs to neither (a stray text encoder or VAE baked into the file)."""
    for prefix in _PREFIXES:
        if key.startswith(prefix):
            key = key[len(prefix):]
            break
    if key.startswith(_ADAPTER):
        # The adapter's own names are diffusers' already.
        return "text_conditioner", key[len(_ADAPTER):]
    if key.startswith("blocks."):
        key = "transformer_blocks." + key[len("blocks."):]
    elif not key.startswith(("final_layer.", "t_embedder.", "t_embedding_norm.",
                             "x_embedder.")):
        return None
    for src, dst in _TRANSFORMER_RULES:
        key = key.replace(src, dst)
    return "transformer", key


def _ensure_cosmos_resize() -> None:
    """Give diffusers' Cosmos transformer the one torchvision call it makes.

    `transformer_cosmos.py` resizes its padding mask with
    `torchvision.transforms.functional.resize`, and imports torchvision only if
    it is installed — so without it every render dies on a NameError. Adding
    torchvision would mean a third wheel the installer must keep matched to
    torch's build (see install.sh's `install_torch`), for one nearest-neighbour
    resize of a mask that the Anima blocks always build as zeros. This supplies
    exactly that call, with `F.interpolate`, and only when the name is missing:
    with torchvision installed it never runs.
    """
    import types

    import torch.nn.functional as F
    from diffusers.models.transformers import transformer_cosmos

    if hasattr(transformer_cosmos, "transforms"):
        return

    def resize(t, size, interpolation=None):
        return F.interpolate(t, size=list(size), mode="nearest")

    transformer_cosmos.transforms = types.SimpleNamespace(
        functional=types.SimpleNamespace(resize=resize),
        InterpolationMode=types.SimpleNamespace(NEAREST="nearest"))


class AnimaBackend(ImageBackend):
    name = "anima"

    # Peak allocation at the shipped selfie size with guidance on is ~9.3 GiB
    # (two transformer passes a step); this is that plus the allocator's slack.
    RESIDENT_FREE_GIB = 10.5

    def __init__(
        self,
        model_path: str = "",
        *,
        device: str = "cuda",
        steps: int = 30,
        cfg: float = 5.0,
        cpu_offload: bool = False,
        max_sequence_length: int = 512,
    ) -> None:
        self.model_path = model_path
        self.device = device
        self.steps = steps
        self.cfg = cfg
        # The user's setting, never rewritten from in here; `_offloading` is
        # how the loaded pipeline was actually built. (Same split, and the same
        # reason, as the SDXL backend — see diffusers.py.)
        self.cpu_offload = cpu_offload
        self.max_sequence_length = max_sequence_length
        self._pipe: Any = None                 # loaded on first render
        self._offloading: bool | None = None   # mode of the loaded pipe
        self._cuda = False                     # where the loaded pipe runs
        self._guidance: float | None = None    # the guider the pipe holds now

    # ---- availability (cheap; imports nothing heavy) ----

    @staticmethod
    def _needs_offload(free_gib: float) -> bool:
        return free_gib < AnimaBackend.RESIDENT_FREE_GIB

    @staticmethod
    def deps_available() -> bool:
        import importlib.util
        return all(importlib.util.find_spec(m) is not None
                   for m in ("torch", "diffusers", "transformers", "accelerate"))

    def health(self) -> bool:
        return (self.deps_available()
                and bool(self.model_path)
                and Path(self.model_path).is_file())

    # ---- the companion components (one fetch, cached forever after) ----

    @staticmethod
    def _prepare_env() -> None:
        # Fragmentation, not footprint, kills most low-VRAM renders. Costs
        # nothing; setdefault means the user's own setting always wins.
        os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

    def _snapshot(self) -> Path:
        """Text encoder + VAE + tokenizers + scheduler, never the weights the
        checkpoint already holds."""
        from huggingface_hub import snapshot_download
        try:
            return Path(snapshot_download(COMPANION_REPO, allow_patterns=COMPANION_ALLOW))
        except Exception as e:
            # The repo is public, so this is the network or the disk; the
            # stock message doesn't say what the download was for.
            raise RuntimeError(
                f"Anima needs its text encoder and VAE from {COMPANION_REPO} on "
                f"Hugging Face (~1.5 GB, fetched once). The download failed: {e}"
            ) from e

    # ---- loading ----

    def _build_models(self, snapshot: Path, dtype) -> tuple[Any, Any]:
        """The transformer and the text conditioner, from the checkpoint."""
        from accelerate import init_empty_weights
        from diffusers import AnimaTextConditioner, CosmosTransformer3DModel
        from safetensors.torch import safe_open

        def config(sub: str) -> dict:
            return json.loads((snapshot / sub / "config.json").read_text())

        with init_empty_weights():             # meta device: allocates nothing
            transformer = CosmosTransformer3DModel.from_config(config("transformer"))
            conditioner = AnimaTextConditioner.from_config(config("text_conditioner"))

        weights: dict[str, dict[str, Any]] = {"transformer": {}, "text_conditioner": {}}
        with safe_open(self.model_path, framework="pt", device="cpu") as f:
            for key in f.keys():
                mapped = rename_key(key)
                if mapped is None:
                    log.debug("anima: ignoring unmapped tensor %s", key)
                    continue
                part, new = mapped
                weights[part][new] = f.get_tensor(key).to(dtype)

        for part, model in (("transformer", transformer),
                            ("text_conditioner", conditioner)):
            try:
                model.load_state_dict(weights[part], strict=True, assign=True)
            except RuntimeError as e:
                raise RuntimeError(
                    f"{Path(self.model_path).name} doesn't match the Anima {part} "
                    f"this build loads — it may be a different Anima variant. "
                    f"({str(e)[:400]})") from e
            model.eval()
        return transformer, conditioner

    def _load(self, force_offload: bool = False):
        if self._pipe is not None:
            return self._pipe
        self._prepare_env()
        import torch
        from diffusers import (AnimaAutoBlocks, AutoencoderKLQwenImage,
                               FlowMatchEulerDiscreteScheduler)
        from transformers import AutoTokenizer, Qwen3Model

        _ensure_cosmos_resize()
        cuda = self.device == "cuda" and torch.cuda.is_available()
        dtype = torch.bfloat16 if cuda else torch.float32
        offload = self.cpu_offload or force_offload
        if cuda and not offload:
            free = torch.cuda.mem_get_info()[0] / 1024**3
            if self._needs_offload(free):
                # Decide up front rather than OOM partway through a resident
                # load and pay for the load twice (the SDXL backend's rule).
                log.warning(
                    "anima: %.1f GiB VRAM free, a resident pipeline wants "
                    "~%.1f — using model CPU offload for this render "
                    "(SELFIE_LOCAL_CPU_OFFLOAD=true makes it permanent).",
                    free, self.RESIDENT_FREE_GIB)
                offload = True
        offload = offload and cuda
        self._offloading = offload             # before the weights move

        snapshot = self._snapshot()
        transformer, conditioner = self._build_models(snapshot, dtype)
        modules = {
            "transformer": transformer,
            "text_conditioner": conditioner,
            "text_encoder": Qwen3Model.from_pretrained(
                snapshot / "text_encoder", torch_dtype=dtype),
            "vae": AutoencoderKLQwenImage.from_pretrained(
                snapshot / "vae", torch_dtype=dtype),
        }
        # Any: diffusers' lazy-import stand-ins hide these classes' methods from mypy.
        manager: Any = None
        if offload:
            # The modular pipeline's own offloader: every model waits on the
            # CPU and is moved onto the card for its forward pass, evicting
            # others only when the next one doesn't fit — so on a roomy card it
            # ends up resident anyway, and on a crowded one it shrinks to fit.
            # `update_components` registers each model with it.
            from diffusers import ComponentsManager
            manager = ComponentsManager()
        blocks: Any = AnimaAutoBlocks()
        pipe = blocks.init_pipeline(components_manager=manager)
        pipe.update_components(
            tokenizer=AutoTokenizer.from_pretrained(snapshot / "tokenizer"),
            t5_tokenizer=AutoTokenizer.from_pretrained(snapshot / "t5_tokenizer"),
            scheduler=FlowMatchEulerDiscreteScheduler.from_pretrained(snapshot / "scheduler"),
            **modules)
        if manager is not None:
            manager.enable_auto_cpu_offload(device="cuda")
        else:
            pipe.to("cuda" if cuda else "cpu")
        self._cuda = cuda
        self._guidance = None
        self._pipe = pipe
        return pipe

    def _set_guidance(self, pipe, cfg: float) -> None:
        """Guidance lives on the modular pipeline's guider component, not in
        the call — swap it only when the number changes."""
        if self._guidance == cfg:
            return
        from diffusers import ClassifierFreeGuidance
        pipe.update_components(guider=ClassifierFreeGuidance(guidance_scale=cfg))
        self._guidance = cfg

    def _teardown(self) -> None:
        """Drop the pipeline and hand the VRAM back (the parker's reset)."""
        self._pipe = None
        self._offloading = None                # the next load decides afresh
        self._guidance = None
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
        return torch.Generator(device="cuda" if self._cuda else "cpu").manual_seed(seed)

    # ---- the seam ----

    def generate(self, req: GenRequest) -> ImageResult:
        try:
            return self._render(req)
        except RuntimeError as e:
            # The retry must happen OUTSIDE this except block, and the check is
            # on the pipeline's mode, not the user's setting — the SDXL
            # backend's rules, for the same reasons (see diffusers.py).
            if self._offloading is not False or "out of memory" not in str(e).lower():
                raise
        # Only a resident render that ran out of memory gets here.
        log.warning(
            "anima: CUDA out of memory — retrying this render with model "
            "CPU offload (slower, much smaller VRAM footprint). Make it "
            "permanent: SELFIE_LOCAL_CPU_OFFLOAD=true in .env")
        self._teardown()
        return self._render(req, force_offload=True)

    def _render(self, req: GenRequest, force_offload: bool = False) -> ImageResult:
        pipe = self._load(force_offload)
        seed = req.seed if req.seed is not None else secrets.randbelow(2**31)
        steps = req.steps or self.steps
        cfg = req.cfg if req.cfg is not None else self.cfg
        self._set_guidance(pipe, cfg)

        # Qwen3 reads the whole assembled prompt (up to 512 tokens), so plain
        # strings go straight in — no CLIP chunking, no embedding surgery.
        image = pipe(
            prompt=req.prompt,
            negative_prompt=req.negative_prompt or None,
            width=req.width, height=req.height,
            num_inference_steps=steps,
            max_sequence_length=self.max_sequence_length,
            generator=self._make_generator(seed),
            output="images",
        )[0]

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
            notes=f"local Anima checkpoint {self.model_path or '(unset)'} via "
                  f"diffusers; text encoder/VAE from {COMPANION_REPO}; no LoRA "
                  f"in this build",
        )
