"""Local diffusers backend — her own camera, on your own GPU (→ ch. 11, ch. 26).

The third registry entry the seam always had a slot for: an SDXL checkpoint
(`.safetensors`) loaded *in-process* with Hugging Face ``diffusers`` — no A1111,
no ComfyUI, no API key, no third party in the room. The model file is
user-supplied (never shipped): any Illustrious-lineage SDXL base works — the
`Pie Models <https://civitai.com/models/1593793/pie-models>`_ family is the
reference pick — and swapping checkpoints is swapping one file path.

The shipped generation defaults are the Pie author's own: DPM++ 2M on Karras
sigmas, 30 steps, CFG 5, and a hires-fix second pass (latent-free LANCZOS
upscale → img2img at low denoise, the same trick A1111's "Hires fix" is). The
prompt side needs nothing from here: ``characters/yuri.yaml`` already carries
the booru-style quality preamble these bases respond to.

Content posture: none. A local checkpoint has no refusal layer and this backend
adds none — what renders is between the user and the model's license
(``capabilities().uncensored`` says exactly that).

VRAM: fp16 SDXL is ~7 GB resident; next to a ~6.7 GB LLM in LM Studio that fits
a 16 GB card. ``cpu_offload=True`` trades ~2× slower renders for a much smaller
resident footprint. Heavy deps (torch, diffusers) are lazy-imported on first
render — importing this module is free, per the repo's seam rule.
"""

from __future__ import annotations

import io
import logging
import os
import secrets
from pathlib import Path

from PIL import Image                       # Pillow is a core dep (see mock.py)

from ..types import Capabilities, GenRequest, ImageResult
from .base import ImageBackend

log = logging.getLogger("forge.diffusers")


class DiffusersBackend(ImageBackend):
    name = "diffusers"

    # Free VRAM (GiB) below which the resident fp16 pipeline won't fit next to
    # everything else on the card — measured against an LLM co-tenant (~5 GiB)
    # on a 16 GiB card, where ~9.9 GiB free still OOM'd during load.
    RESIDENT_FREE_GIB = 11.0

    @staticmethod
    def _needs_offload(free_gib: float) -> bool:
        return free_gib < DiffusersBackend.RESIDENT_FREE_GIB

    def __init__(
        self,
        model_path: str = "",
        *,
        device: str = "cuda",
        steps: int = 30,
        cfg: float = 5.0,
        hires: bool = True,
        hires_scale: float = 1.5,
        hires_denoise: float = 0.35,
        cpu_offload: bool = False,
    ) -> None:
        self.model_path = model_path
        self.device = device
        self.steps = steps
        self.cfg = cfg
        self.hires = hires
        self.hires_scale = hires_scale
        self.hires_denoise = hires_denoise
        # The user's setting, and it stays theirs: a crowded card can force
        # *this* load to offload, but nothing here may rewrite what they asked
        # for. That distinction is `_offloading` below — how the pipeline
        # currently on the card was actually built, re-decided on every load.
        # (They used to be one flag, which meant one crowded moment condemned
        # every later render in the process to the slow path, long after the
        # card had emptied — and silently disabled the OOM retry, because the
        # retry's "am I already as small as I get?" check read the same flag.)
        self.cpu_offload = cpu_offload
        self._pipe = None                       # loaded on first render
        self._offloading: bool | None = None    # mode of the loaded pipe

    # ---- availability (cheap; imports nothing heavy) ----

    @staticmethod
    def deps_available() -> bool:
        import importlib.util
        return all(importlib.util.find_spec(m) is not None
                   for m in ("torch", "diffusers"))

    def health(self) -> bool:
        return (self.deps_available()
                and bool(self.model_path)
                and Path(self.model_path).is_file())

    # ---- the pipeline (lazy; the seams stay importable with no GPU) ----

    @staticmethod
    def _prepare_env() -> None:
        # Fragmentation, not footprint, is what kills most low-VRAM renders
        # ("reserved but unallocated" in the OOM report). Expandable segments
        # cost nothing; setdefault means the user's own setting always wins.
        os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

    def _load(self, force_offload: bool = False):
        if self._pipe is not None:
            if self._offloading is False and self._resident_reuse_would_oom():
                # The pipeline was loaded resident when the card had room, and
                # the card no longer does — her brain came back beside it. A
                # resident pipeline needs headroom for activations, not just for
                # its own weights, so reusing this one now is the OOM. Trading
                # ~20 s of reload for a photo that actually arrives.
                log.warning("diffusers: the card filled up since this pipeline "
                            "was loaded — reloading it with CPU offload rather "
                            "than rendering into what's left.")
                self._teardown()
            else:
                return self._pipe
            force_offload = True
        self._prepare_env()
        import torch
        from diffusers import (DPMSolverMultistepScheduler,
                               StableDiffusionXLPipeline)

        cuda = self.device == "cuda" and torch.cuda.is_available()
        dtype = torch.float16 if cuda else torch.float32
        offload = self.cpu_offload or force_offload
        if cuda and not offload:
            free = torch.cuda.mem_get_info()[0] / 1024**3
            if self._needs_offload(free):
                # Cheaper than the crash: the resident fp16 pipeline wants ~11
                # GiB (weights + context + generation peaks). Sharing the card
                # with an LLM usually means that room isn't there — offload
                # from the start instead of OOM-ing mid-load and retrying (the
                # backstop below). This load only: the next one measures again,
                # and after a park there is usually room to be resident.
                log.warning(
                    "diffusers: %.1f GiB VRAM free, the resident pipeline "
                    "wants ~%.0f — using model CPU offload for this render "
                    "(SELFIE_LOCAL_CPU_OFFLOAD=true makes it permanent).",
                    free, self.RESIDENT_FREE_GIB)
                offload = True
        offload = offload and cuda
        # Recorded before the weights move, so a load that OOMs partway still
        # tells `generate` which mode died — that is what decides whether
        # there is a smaller way to try again.
        self._offloading = offload
        pipe = StableDiffusionXLPipeline.from_single_file(
            self.model_path, torch_dtype=dtype)
        # DPM++ 2M is dpmsolver++'s default algorithm; Karras is the sigmas.
        pipe.scheduler = DPMSolverMultistepScheduler.from_config(
            pipe.scheduler.config, use_karras_sigmas=True)
        if offload:
            pipe.enable_model_cpu_offload()
        else:
            pipe = pipe.to(self.device if cuda else "cpu")
        try:
            pipe.vae.enable_slicing()           # cheaper decode, free quality
        except Exception:
            try:
                pipe.enable_vae_slicing()       # diffusers < 0.32 API
            except Exception:
                pass
        try:
            pipe.vae.enable_tiling()            # the low-VRAM decode: hires-fix
        except Exception:                       # resolutions OOM without it
            pass
        self._pipe = pipe
        return pipe

    #: Headroom a *resident* pipeline needs on top of its own weights, for
    #: activations and the VAE decode. Measured against free VRAM while the
    #: pipeline is already loaded, so it is not the same number as
    #: RESIDENT_FREE_GIB (which sizes an empty card for a fresh load).
    REUSE_FREE_GIB = 2.5

    def _resident_reuse_would_oom(self) -> bool:
        """Is there still room to run the pipeline we're already holding?"""
        try:
            import torch
            if self.device != "cuda" or not torch.cuda.is_available():
                return False
            return (torch.cuda.mem_get_info()[0] / 1024**3) < self.REUSE_FREE_GIB
        except Exception:
            return False        # can't measure → behave exactly as before

    def _teardown(self) -> None:
        """Drop the pipeline and hand the VRAM back (the OOM retry's reset)."""
        self._pipe = None
        self._offloading = None                 # the next load decides afresh
        import gc
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass

    def _make_img2img(self, pipe):
        """The hires-fix second pass rides the same weights, no double load."""
        from diffusers import StableDiffusionXLImg2ImgPipeline
        return StableDiffusionXLImg2ImgPipeline(**pipe.components)

    def _make_generator(self, seed: int):
        import torch
        device = self.device if (self.device == "cuda"
                                 and torch.cuda.is_available()) else "cpu"
        return torch.Generator(device=device).manual_seed(seed)

    # ---- long prompts: the 77-token CLIP cap --------------------------------
    # SDXL's two text encoders each truncate at 77 tokens, silently. Her
    # assembled prompt (quality preamble + identity + scene) runs ~400, so a
    # naive call renders a generic girl: her defining features and the whole
    # scene sit past token 77. The fix is the standard long-prompt technique —
    # chunk into ≤75-token pieces, encode each, concatenate the embeddings —
    # which is also why generate() below passes *_embeds, never raw strings.

    def _n_chunks(self, pipe, text: str) -> int:
        longest = 0
        for tok in (pipe.tokenizer, pipe.tokenizer_2):
            longest = max(longest, len(_tokens(tok, text)))
        room = pipe.tokenizer.model_max_length - 2   # BOS + EOS
        return max(1, -(-longest // room))

    def _encode_text(self, pipe, text: str, n_chunks: int):
        """One text → (embeds, pooled) for BOTH SDXL encoders, chunked."""
        import torch
        exec_device = getattr(pipe, "_execution_device", None) or pipe.device
        per_encoder, pooled = [], None
        for idx, (tok, enc) in enumerate(((pipe.tokenizer, pipe.text_encoder),
                                          (pipe.tokenizer_2, pipe.text_encoder_2))):
            ids = _tokens(tok, text)
            room = tok.model_max_length - 2
            chunks = [ids[i:i + room] for i in range(0, len(ids), room)] or [[]]
            chunks += [[]] * (n_chunks - len(chunks))    # pad to the pair's length
            embeds = []
            for ci, chunk in enumerate(chunks):
                row = [tok.bos_token_id, *chunk, tok.eos_token_id]
                row += [tok.eos_token_id] * (tok.model_max_length - len(row))
                out = enc(torch.tensor([row], device=enc.device),
                          output_hidden_states=True)
                embeds.append(out.hidden_states[-2])     # SDXL: penultimate layer
                if idx == 1 and ci == 0:
                    pooled = out.text_embeds             # pooled comes from enc #2
            per_encoder.append(torch.cat(embeds, dim=1))
        embeds = torch.cat(per_encoder, dim=-1).to(exec_device, dtype=pipe.dtype)
        return embeds, pooled.to(exec_device, dtype=pipe.dtype)

    def _encode_prompts(self, pipe, prompt: str, negative: str):
        """The (positive, negative) embed pair at a shared chunk count — CFG
        concatenates them inside the UNet, so the lengths must match."""
        n = max(self._n_chunks(pipe, prompt), self._n_chunks(pipe, negative))
        pe, pp = self._encode_text(pipe, prompt, n)
        ne, np_ = self._encode_text(pipe, negative, n)
        return pe, pp, ne, np_

    # ---- the seam ----

    def generate(self, req: GenRequest) -> ImageResult:
        oom = False
        try:
            return self._render(req)
        except RuntimeError as e:
            # VRAM wall (LLM + SDXL sharing one card). NOTE: the retry must
            # happen OUTSIDE this except block — while the exception is alive,
            # its traceback pins the dead pipeline's frame, and with it the
            # gigabytes of VRAM the retry needs back.
            #
            # `_offloading is False` — the render that just died was resident,
            # so there is a smaller way to run it. Anything else (already
            # offloading, or no pipeline to speak of) has nothing left to give.
            # Read the *pipeline's* mode, never the user's setting: they were
            # one flag once, and a session that had been forced to offload
            # therefore looked like a session with nothing left to try.
            if self._offloading is not False or "out of memory" not in str(e).lower():
                raise
            oom = True
        if oom:
            # Retry ONCE on model CPU offload — slower, but a fraction of the
            # resident footprint. For this render: the pipeline is dropped
            # after it either way, and the next one measures the card again.
            log.warning(
                "diffusers: CUDA out of memory — retrying this render with "
                "model CPU offload (slower, much smaller VRAM footprint). "
                "Make it permanent: SELFIE_LOCAL_CPU_OFFLOAD=true in .env")
            self._teardown()
            return self._render(req, force_offload=True)

    def _render(self, req: GenRequest, force_offload: bool = False) -> ImageResult:
        pipe = self._load(force_offload)
        seed = req.seed if req.seed is not None else secrets.randbelow(2**31)
        gen = self._make_generator(seed)
        steps, cfg = req.steps or self.steps, req.cfg or self.cfg
        embeds = self._encode_prompts(pipe, req.prompt, req.negative_prompt or "")

        image = pipe(
            prompt_embeds=embeds[0],
            pooled_prompt_embeds=embeds[1],
            negative_prompt_embeds=embeds[2],
            negative_pooled_prompt_embeds=embeds[3],
            width=req.width, height=req.height,
            num_inference_steps=steps,
            guidance_scale=cfg,
            generator=gen,
        ).images[0]

        did_hires = False
        if self.hires:
            i2i = self._make_img2img(pipe)
            w = _up(req.width, self.hires_scale)
            h = _up(req.height, self.hires_scale)
            image = i2i(
                prompt_embeds=embeds[0],
                pooled_prompt_embeds=embeds[1],
                negative_prompt_embeds=embeds[2],
                negative_pooled_prompt_embeds=embeds[3],
                image=image.resize((w, h), Image.Resampling.LANCZOS),
                strength=self.hires_denoise,      # denoise = how far it may drift
                num_inference_steps=steps,
                guidance_scale=cfg,
                generator=self._make_generator(seed),
            ).images[0]
            did_hires = True

        out = io.BytesIO()
        image.save(out, "PNG")
        return ImageResult.new(
            out.getvalue(), self.name,
            model=Path(self.model_path).name, seed=seed,
            prompt=req.prompt, negative=req.negative_prompt,
            steps=steps, cfg=cfg, sampler="DPM++ 2M Karras",
            width=image.width, height=image.height,
            hires=f"{self.hires_scale}x @ {self.hires_denoise}" if did_hires else None)

    def capabilities(self) -> Capabilities:
        return Capabilities(
            name=self.name,
            uncensored=True,                  # a local checkpoint refuses nothing
            notes=f"local SDXL checkpoint {self.model_path or '(unset)'} "
                  f"via diffusers; no LoRA in this build",
        )


def _tokens(tok, text: str) -> list[int]:
    """Raw ids for the whole text. ``verbose=False`` silences the stock
    "sequence is longer than the model maximum" warning — the chunking above
    is exactly what handles long prompts, so the warning is a false alarm here."""
    ids = tok(text, add_special_tokens=False, verbose=False).input_ids
    if ids and isinstance(ids[0], list):             # defensive: nested batch
        ids = ids[0]
    return ids


def _up(px: int, scale: float) -> int:
    """Hires-fix target size, snapped to a multiple of 8 (VAE-friendly)."""
    return max(64, round(px * scale / 8) * 8)
