"""The third local camera (Anima) — the in-process Cosmos transformer backend.

Entirely offline, no torch, no GPU, no Hugging Face: the backend's heavy seams
(``_load``, ``_set_guidance``, ``_make_generator``) are swapped for fakes, the
same philosophy as the SDXL and Krea 2 backends' tests.

The key map — a checkpoint's tensor names against the two diffusers models' own
— is tested as pure string rules, which is exactly what it is. The real thing
(every tensor of a real checkpoint landing, strict) was checked against a
Civitai Anima merge when this backend was written; that needs the file.
"""
from __future__ import annotations

import pytest
from PIL import Image

from tests.support.checkpoints import write_safetensors_header
from yurios.forge.backends.anima import COMPANION_REPO, AnimaBackend, rename_key
from yurios.forge.backends.sniff import sniff_local_checkpoint_architecture
from yurios.forge.types import GenRequest
from yurios.world.selfies import build_forge

_ANIMA_KEYS = ["model.diffusion_model.blocks.0.self_attn.q_proj.weight",
               "model.diffusion_model.llm_adapter.embed.weight"]


class FakePipe:
    """Stands in for the Anima modular pipeline: records the call, returns a
    solid image the way `output="images"` does — a bare list."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, **kw):
        self.calls.append(kw)
        return [Image.new("RGB", (kw["width"], kw["height"]), (10, 20, 30))]


@pytest.fixture
def backend(monkeypatch, tmp_path):
    ckpt = write_safetensors_header(tmp_path / "someAnima_v10.safetensors", _ANIMA_KEYS,
                                    {"modelspec.architecture": "anima"})
    b = AnimaBackend(model_path=str(ckpt))
    pipe = FakePipe()
    guidance: list[float] = []

    def fake_load(force_offload=False):
        b._offloading = bool(b.cpu_offload or force_offload)
        return pipe

    monkeypatch.setattr(b, "_load", fake_load)
    monkeypatch.setattr(b, "_set_guidance", lambda p, cfg: guidance.append(cfg))
    monkeypatch.setattr(b, "_make_generator", lambda seed: None)
    b._fake_pipe = pipe
    b._fake_guidance = guidance
    return b


# ---- the key map (pure string rules; no torch, no checkpoint) ---------------

@pytest.mark.parametrize("src,want", [
    ("model.diffusion_model.blocks.3.self_attn.q_proj.weight",
     "transformer_blocks.3.attn1.to_q.weight"),
    ("model.diffusion_model.blocks.3.self_attn.output_proj.weight",
     "transformer_blocks.3.attn1.to_out.0.weight"),
    ("model.diffusion_model.blocks.3.self_attn.k_norm.weight",
     "transformer_blocks.3.attn1.norm_k.weight"),
    ("model.diffusion_model.blocks.27.cross_attn.v_proj.weight",
     "transformer_blocks.27.attn2.to_v.weight"),
    ("model.diffusion_model.blocks.27.cross_attn.q_norm.weight",
     "transformer_blocks.27.attn2.norm_q.weight"),
    ("model.diffusion_model.blocks.0.adaln_modulation_self_attn.1.weight",
     "transformer_blocks.0.norm1.linear_1.weight"),
    ("model.diffusion_model.blocks.0.adaln_modulation_cross_attn.2.weight",
     "transformer_blocks.0.norm2.linear_2.weight"),
    ("model.diffusion_model.blocks.0.adaln_modulation_mlp.1.weight",
     "transformer_blocks.0.norm3.linear_1.weight"),
    ("model.diffusion_model.blocks.0.mlp.layer1.weight",
     "transformer_blocks.0.ff.net.0.proj.weight"),
    ("model.diffusion_model.blocks.0.mlp.layer2.weight",
     "transformer_blocks.0.ff.net.2.weight"),
    ("model.diffusion_model.final_layer.adaln_modulation.1.weight", "norm_out.linear_1.weight"),
    ("model.diffusion_model.final_layer.linear.weight", "proj_out.weight"),
    ("model.diffusion_model.t_embedder.1.linear_2.weight",
     "time_embed.t_embedder.linear_2.weight"),
    ("model.diffusion_model.t_embedding_norm.weight", "time_embed.norm.weight"),
    ("model.diffusion_model.x_embedder.proj.1.weight", "patch_embed.proj.weight"),
])
def test_rename_key_maps_the_transformer(src, want):
    assert rename_key(src) == ("transformer", want)


def test_the_text_adapter_goes_to_the_conditioner_under_its_own_names():
    # A fine-tune or merge may have moved the adapter too, so it comes from the
    # file — and diffusers' AnimaTextConditioner already uses the file's names.
    assert rename_key("model.diffusion_model.llm_adapter.blocks.2.cross_attn.q_proj.weight") \
        == ("text_conditioner", "blocks.2.cross_attn.q_proj.weight")
    assert rename_key("model.diffusion_model.llm_adapter.embed.weight") \
        == ("text_conditioner", "embed.weight")


def test_the_adapters_attention_is_not_renamed_like_the_transformers():
    # "self_attn.q_proj" is a transformer rule; applied inside the adapter it
    # would load nothing and fail strict — the adapter branch must win first.
    part, name = rename_key("model.diffusion_model.llm_adapter.blocks.0.self_attn.q_proj.weight")
    assert (part, name) == ("text_conditioner", "blocks.0.self_attn.q_proj.weight")


@pytest.mark.parametrize("prefix", ["model.diffusion_model.", "diffusion_model.", "net.", ""])
def test_every_export_wrapper_is_stripped(prefix):
    assert rename_key(f"{prefix}blocks.1.mlp.layer2.weight") == \
        ("transformer", "transformer_blocks.1.ff.net.2.weight")


@pytest.mark.parametrize("key", ["text_encoders.qwen3.model.embed_tokens.weight",
                                 "vae.decoder.conv_in.weight", "totally.unknown"])
def test_tensors_that_belong_to_neither_model_are_skipped(key):
    assert rename_key(key) is None


# ---- architecture sniffing --------------------------------------------------

def test_sniff_reads_the_declared_architecture(tmp_path):
    p = write_safetensors_header(tmp_path / "a.safetensors", _ANIMA_KEYS,
                                 {"modelspec.architecture": "anima"})
    assert sniff_local_checkpoint_architecture(p) == "anima"


def test_sniff_tells_anima_from_sdxl_by_key_shape_alone(tmp_path):
    # Both live under model.diffusion_model.; without metadata the adapter is
    # what tells them apart, and it must be looked for before the SDXL rule —
    # this was the bug: an Anima file handed to the SDXL loader.
    p = write_safetensors_header(tmp_path / "a.safetensors", _ANIMA_KEYS)
    assert sniff_local_checkpoint_architecture(p) == "anima"


def test_sniff_tells_a_bare_anima_export_from_krea2(tmp_path):
    # Unwrapped, Anima's blocks start "blocks." just like Krea 2's.
    p = write_safetensors_header(
        tmp_path / "a.safetensors",
        ["blocks.0.self_attn.q_proj.weight", "llm_adapter.embed.weight"])
    assert sniff_local_checkpoint_architecture(p) == "anima"


# ---- the render contract ----------------------------------------------------

def test_generate_passes_plain_strings_at_full_length(backend):
    req = GenRequest(prompt="masterpiece, 1girl, cat ears", negative_prompt="lowres",
                     width=832, height=1216, seed=1234)
    result = backend.generate(req)

    assert result.data[:4] == b"\x89PNG"
    call = backend._fake_pipe.calls[0]
    assert call["prompt"] == "masterpiece, 1girl, cat ears"
    assert call["negative_prompt"] == "lowres"
    assert call["width"] == 832 and call["height"] == 1216
    assert call["max_sequence_length"] == 512
    assert call["output"] == "images"


def test_generate_uses_the_configured_defaults(backend):
    backend.generate(GenRequest(prompt="x", width=64, height=64))
    assert backend._fake_pipe.calls[0]["num_inference_steps"] == 30
    assert backend._fake_guidance == [5.0]


def test_per_call_overrides_beat_the_instance_defaults(backend):
    backend.generate(GenRequest(prompt="x", width=64, height=64, steps=12, cfg=1.0))
    assert backend._fake_pipe.calls[0]["num_inference_steps"] == 12
    assert backend._fake_guidance == [1.0]


def test_an_empty_negative_becomes_none_not_an_empty_string(backend):
    backend.generate(GenRequest(prompt="x", negative_prompt="", width=64, height=64))
    assert backend._fake_pipe.calls[0]["negative_prompt"] is None


def test_a_seedless_request_still_records_one(backend):
    result = backend.generate(GenRequest(prompt="x", width=64, height=64))
    assert isinstance(result.meta["seed"], int)


def test_provenance_names_the_backend_and_the_checkpoint(backend):
    meta = backend.generate(GenRequest(prompt="x", width=64, height=64)).meta
    assert meta["backend"] == "anima"
    assert meta["model"] == "someAnima_v10.safetensors"
    assert meta["sampler"] == "FlowMatchEulerDiscrete"


def test_the_guider_is_rebuilt_only_when_guidance_changes():
    b = AnimaBackend(model_path="x.safetensors")
    swaps: list[object] = []

    class Pipe:
        def update_components(self, **kw):
            swaps.append(kw["guider"])

    pytest.importorskip("diffusers")
    b._set_guidance(Pipe(), 5.0)
    b._set_guidance(Pipe(), 5.0)
    b._set_guidance(Pipe(), 4.0)
    assert len(swaps) == 2
    assert swaps[-1].guidance_scale == 4.0


# ---- OOM: shrink and retry once (the SDXL backend's rule) ------------------

def _modes(backend, monkeypatch, outcomes, crowded=False):
    """Stub `_render` to record the mode each attempt ran in, setting
    `_offloading` the way the real `_load` would. See test_forge_diffusers.py."""
    seen: list[bool] = []

    def attempt(req, force_offload=False):
        seen.append(force_offload)
        backend._offloading = bool(backend.cpu_offload or force_offload or crowded)
        out = outcomes[len(seen) - 1]
        if isinstance(out, Exception):
            raise out
        return out

    monkeypatch.setattr(backend, "_render", attempt)
    return seen


def test_out_of_memory_retries_once_with_offload(monkeypatch, backend):
    modes = _modes(backend, monkeypatch, [
        RuntimeError("CUDA out of memory. Tried to allocate 2 GiB"), "second-result"])
    monkeypatch.setattr(backend, "_teardown", lambda: None)

    assert backend.generate(GenRequest(prompt="x")) == "second-result"
    assert modes == [False, True]
    assert backend.cpu_offload is False         # the user's setting is untouched


def test_out_of_memory_while_already_offloading_propagates(monkeypatch, backend):
    backend.cpu_offload = True
    modes = _modes(backend, monkeypatch, [RuntimeError("CUDA out of memory")])
    with pytest.raises(RuntimeError):
        backend.generate(GenRequest(prompt="x"))
    assert modes == [False]


def test_a_non_oom_runtime_error_is_not_retried(monkeypatch, backend):
    modes = _modes(backend, monkeypatch,
                   [RuntimeError("doesn't match the Anima transformer")])
    with pytest.raises(RuntimeError, match="doesn't match"):
        backend.generate(GenRequest(prompt="x"))
    assert modes == [False]


# ---- health / capabilities --------------------------------------------------

def test_health_needs_deps_and_a_real_checkpoint(monkeypatch, tmp_path):
    monkeypatch.setattr(AnimaBackend, "deps_available", staticmethod(lambda: True))
    ckpt = write_safetensors_header(tmp_path / "a.safetensors", _ANIMA_KEYS)
    assert AnimaBackend(model_path=str(ckpt)).health() is True
    assert AnimaBackend(model_path=str(tmp_path / "gone.safetensors")).health() is False
    assert AnimaBackend(model_path="").health() is False

    monkeypatch.setattr(AnimaBackend, "deps_available", staticmethod(lambda: False))
    assert AnimaBackend(model_path=str(ckpt)).health() is False


def test_capabilities_are_honest_about_the_local_checkpoint():
    caps = AnimaBackend(model_path="a.safetensors").capabilities()
    assert caps.name == "anima"
    assert caps.uncensored is True
    assert COMPANION_REPO in caps.notes


def test_a_failed_companion_download_says_what_it_was_for(monkeypatch):
    def offline(*a, **kw):
        raise OSError("Network is unreachable")

    monkeypatch.setattr("huggingface_hub.snapshot_download", offline)
    with pytest.raises(RuntimeError, match="text encoder and VAE") as e:
        AnimaBackend(model_path="a.safetensors")._snapshot()
    assert "Network is unreachable" in str(e.value)


def test_the_companion_fetch_skips_the_weights_the_checkpoint_carries():
    from yurios.forge.backends.anima import COMPANION_ALLOW
    assert "transformer/config.json" in COMPANION_ALLOW
    assert not any(p.startswith(("transformer/*", "text_conditioner/*"))
                   or p.endswith(".safetensors") for p in COMPANION_ALLOW)


def test_a_short_card_chooses_offload_before_trying_resident():
    assert AnimaBackend._needs_offload(AnimaBackend.RESIDENT_FREE_GIB - 1) is True
    assert AnimaBackend._needs_offload(AnimaBackend.RESIDENT_FREE_GIB + 1) is False


def test_a_resident_floor_is_declared_for_the_vram_parker():
    assert isinstance(AnimaBackend.RESIDENT_FREE_GIB, float)


# ---- build_forge: one knob, the file picks the loader -----------------------

def test_build_forge_routes_an_anima_checkpoint_to_the_anima_backend(
        monkeypatch, cfg, tmp_path):
    monkeypatch.setattr(AnimaBackend, "deps_available", staticmethod(lambda: True))
    ckpt = write_safetensors_header(tmp_path / "a.safetensors", _ANIMA_KEYS,
                                    {"modelspec.architecture": "anima"})
    cfg.selfie_backend = "diffusers"             # the knob still says diffusers
    cfg.selfie_local_model = str(ckpt)
    cfg.selfie_local_steps, cfg.selfie_local_cfg = 40, 4.5

    forge, status = build_forge(cfg)
    assert status == "anima"
    assert forge.backend.name == "anima"
    assert (forge.backend.steps, forge.backend.cfg) == (40, 4.5)   # SDXL's knobs


def test_build_forge_says_so_loudly_without_the_anima_deps(
        monkeypatch, cfg, tmp_path, caplog):
    monkeypatch.setattr(AnimaBackend, "deps_available", staticmethod(lambda: False))
    ckpt = write_safetensors_header(tmp_path / "a.safetensors", _ANIMA_KEYS)
    cfg.selfie_backend = "anima"
    cfg.selfie_local_model = str(ckpt)

    with caplog.at_level("WARNING"):
        forge, status = build_forge(cfg)
    assert forge.backend.name == "anima"         # never swapped for mock (§7.6)
    assert status.startswith("anima (unavailable") and "dependencies" in status
    assert "forge-local" in caplog.text
