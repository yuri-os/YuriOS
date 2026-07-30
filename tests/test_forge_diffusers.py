"""The local camera (SELFIE_BACKEND=diffusers) — the in-process SDXL backend.

Entirely offline, no torch, no GPU: the backend's heavy seams (``_load``,
``_make_img2img``, ``_make_generator``) are swapped for fakes, the same
philosophy as the voice stack (SPEC §27) — what gets asserted is the contract:
prompt mapping, the Pie-author generation defaults, the hires-fix second pass,
provenance meta, and build_forge's loud degrade rule.
"""
from __future__ import annotations

import pytest
from PIL import Image

from yurios.forge.backends.diffusers import DiffusersBackend, _up
from yurios.forge.types import GenRequest
from yurios.world.selfies import build_forge


class _Out:
    def __init__(self, images):
        self.images = images


class FakePipe:
    """Stands in for StableDiffusionXLPipeline: records the call, returns a
    solid image at the requested size."""
    components = {}                            # what _make_img2img would spread

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, **kw):
        self.calls.append(kw)
        return _Out([Image.new("RGB", (kw["width"], kw["height"]), (10, 20, 30))])


class FakeI2I:
    """Stands in for the img2img pipeline: passes the (already resized) image
    through, recording the call."""
    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, **kw):
        self.calls.append(kw)
        return _Out([kw["image"]])


@pytest.fixture
def backend(monkeypatch, tmp_path):
    ckpt = tmp_path / "pieModels_lemonPie.safetensors"
    ckpt.write_bytes(b"not really a model")
    b = DiffusersBackend(model_path=str(ckpt))
    pipe, i2i = FakePipe(), FakeI2I()
    monkeypatch.setattr(b, "_load", lambda: pipe)
    monkeypatch.setattr(b, "_make_img2img", lambda p: i2i)
    monkeypatch.setattr(b, "_make_generator", lambda seed: None)
    enc_calls = []

    def fake_encode(p, prompt, negative):      # sentinels, no torch in tests
        enc_calls.append((prompt, negative))
        return "PE", "PP", "NE", "NP"

    monkeypatch.setattr(b, "_encode_prompts", fake_encode)
    b._fake_pipe, b._fake_i2i, b._enc_calls = pipe, i2i, enc_calls
    return b


def test_generate_maps_the_pie_author_defaults(backend):
    req = GenRequest(prompt="masterpiece, 1girl, cat ears", negative_prompt="lowres",
                     width=832, height=1216, seed=1234)
    result = backend.generate(req)

    assert result.data[:4] == b"\x89PNG"
    # the prompt rides as EMBEDDINGS, never raw strings — the 77-token CLIP cap
    # truncates strings silently (400-token identity + scene prompts)
    assert backend._enc_calls == [("masterpiece, 1girl, cat ears", "lowres")]
    (call,) = backend._fake_pipe.calls
    assert "prompt" not in call and "negative_prompt" not in call
    assert call["prompt_embeds"] == "PE"
    assert call["pooled_prompt_embeds"] == "PP"
    assert call["negative_prompt_embeds"] == "NE"
    assert call["negative_pooled_prompt_embeds"] == "NP"
    assert call["num_inference_steps"] == 30           # the Pie author's settings
    assert call["guidance_scale"] == 5.0
    assert call["width"] == 832 and call["height"] == 1216

    m = result.meta
    assert m["backend"] == "diffusers"
    assert m["model"] == "pieModels_lemonPie.safetensors"
    assert m["seed"] == 1234
    assert m["sampler"] == "DPM++ 2M Karras"
    assert m["prompt"] == req.prompt and m["negative"] == "lowres"


def test_the_hires_fix_is_a_second_low_denoise_pass(backend):
    result = backend.generate(GenRequest(prompt="p", width=832, height=1216, seed=1))

    (call,) = backend._fake_i2i.calls
    assert call["prompt_embeds"] == "PE"               # same encoders, same embeds
    assert call["negative_prompt_embeds"] == "NE"
    assert call["strength"] == 0.35                    # denoise: how far it may drift
    assert call["num_inference_steps"] == 30
    assert call["image"].size == (1248, 1824)          # 1.5x, snapped to multiples of 8
    assert result.meta["hires"] == "1.5x @ 0.35"
    assert (result.meta["width"], result.meta["height"]) == (1248, 1824)


def test_hires_can_be_turned_off(monkeypatch, tmp_path):
    ckpt = tmp_path / "m.safetensors"
    ckpt.write_bytes(b"x")
    b = DiffusersBackend(model_path=str(ckpt), hires=False)
    monkeypatch.setattr(b, "_load", lambda: FakePipe())
    monkeypatch.setattr(b, "_make_generator", lambda seed: None)
    monkeypatch.setattr(b, "_encode_prompts",
                        lambda p, prompt, negative: ("PE", "PP", "NE", "NP"))
    monkeypatch.setattr(b, "_make_img2img",
                        lambda p: pytest.fail("img2img must not be built"))

    result = b.generate(GenRequest(prompt="p", width=832, height=1216, seed=1))
    assert "hires" not in result.meta
    assert (result.meta["width"], result.meta["height"]) == (832, 1216)


def test_per_call_overrides_beat_the_defaults(backend):
    backend.generate(GenRequest(prompt="p", width=512, height=512,
                                steps=10, cfg=7.5, seed=42))
    (call,) = backend._fake_pipe.calls
    assert call["num_inference_steps"] == 10
    assert call["guidance_scale"] == 7.5


def test_a_seedless_request_still_records_the_seed_used(backend):
    result = backend.generate(GenRequest(prompt="p", width=64, height=64))
    assert isinstance(result.meta["seed"], int)        # provenance, not "random"


def test_up_snaps_to_multiples_of_eight():
    assert _up(832, 1.5) == 1248
    assert _up(1216, 1.5) == 1824
    assert _up(831, 1.5) % 8 == 0
    assert _up(10, 1.5) == 64                          # the floor


def test_health_needs_deps_and_a_real_checkpoint(tmp_path, monkeypatch):
    ckpt = tmp_path / "m.safetensors"
    ckpt.write_bytes(b"x")
    ok = DiffusersBackend(model_path=str(ckpt))
    monkeypatch.setattr(DiffusersBackend, "deps_available", staticmethod(lambda: True))
    assert ok.health() is True
    assert DiffusersBackend(model_path=str(tmp_path / "nope.safetensors")).health() is False
    assert DiffusersBackend(model_path="").health() is False
    monkeypatch.setattr(DiffusersBackend, "deps_available", staticmethod(lambda: False))
    assert ok.health() is False


def test_capabilities_say_what_a_local_checkpoint_is(backend):
    caps = backend.capabilities()
    assert caps.name == "diffusers"
    assert caps.uncensored is True                     # no refusal layer, by design
    assert "lemonPie" in caps.notes


# ---- build_forge wiring (the loud degrade rule, B2 §3) ----

def test_a_working_local_camera_is_wired_from_config(cfg, tmp_path, monkeypatch):
    ckpt = tmp_path / "pie.safetensors"
    ckpt.write_bytes(b"x")
    monkeypatch.setattr(DiffusersBackend, "deps_available", staticmethod(lambda: True))
    cfg = cfg.model_copy(update={"selfie_backend": "diffusers",
                                 "selfie_local_model": str(ckpt),
                                 "selfie_local_steps": 20,
                                 "selfie_local_hires": False})
    forge, status = build_forge(cfg)
    assert status == "diffusers"
    assert isinstance(forge.backend, DiffusersBackend)
    assert forge.backend.steps == 20 and forge.backend.hires is False


def test_a_missing_checkpoint_degrades_to_mock_loudly(cfg, tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(DiffusersBackend, "deps_available", staticmethod(lambda: True))
    cfg = cfg.model_copy(update={"selfie_backend": "diffusers",
                                 "selfie_local_model": str(tmp_path / "gone.safetensors")})
    with caplog.at_level("WARNING"):
        forge, status = build_forge(cfg)
    assert status.startswith("mock") and "diffusers unavailable" in status
    assert forge.backend.name == "mock"
    assert any("SELFIE_LOCAL_MODEL" in r.message for r in caplog.records)


def test_missing_deps_degrade_to_mock_loudly(cfg, tmp_path, monkeypatch, caplog):
    ckpt = tmp_path / "pie.safetensors"
    ckpt.write_bytes(b"x")
    monkeypatch.setattr(DiffusersBackend, "deps_available", staticmethod(lambda: False))
    cfg = cfg.model_copy(update={"selfie_backend": "diffusers",
                                 "selfie_local_model": str(ckpt)})
    with caplog.at_level("WARNING"):
        forge, status = build_forge(cfg)
    assert forge.backend.name == "mock"
    assert any("forge-local" in r.message for r in caplog.records)


# ---- the VRAM wall: OOM degrades, it never crashes the turn ----

def test_an_oom_retries_once_with_cpu_offload(backend, monkeypatch):
    calls = []

    def flaky(req):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("CUDA out of memory. Tried to allocate 14.00 MiB")
        return "RESULT"

    torn = []
    monkeypatch.setattr(backend, "_render", flaky)
    monkeypatch.setattr(backend, "_teardown", lambda: torn.append(1))
    assert backend.cpu_offload is False

    assert backend.generate(GenRequest(prompt="p")) == "RESULT"
    assert calls == [1, 1]                             # exactly one retry
    assert torn == [1]                                 # VRAM handed back first
    assert backend.cpu_offload is True                 # …and the fix sticks


def test_a_non_oom_error_propagates(backend, monkeypatch):
    def boom(req):
        raise RuntimeError("NaN in the unet")

    monkeypatch.setattr(backend, "_render", boom)
    with pytest.raises(RuntimeError, match="NaN"):
        backend.generate(GenRequest(prompt="p"))
    assert backend.cpu_offload is False                # no false-positive flip


def test_an_oom_with_offload_already_on_propagates(backend, monkeypatch):
    def boom(req):
        raise RuntimeError("CUDA out of memory")

    backend.cpu_offload = True
    monkeypatch.setattr(backend, "_render", boom)
    with pytest.raises(RuntimeError, match="out of memory"):
        backend.generate(GenRequest(prompt="p"))       # the lab's quiet-message rule


def test_prepare_env_sets_expandable_segments_but_respects_the_user(monkeypatch):
    import os
    monkeypatch.delenv("PYTORCH_CUDA_ALLOC_CONF", raising=False)
    DiffusersBackend._prepare_env()
    assert os.environ["PYTORCH_CUDA_ALLOC_CONF"] == "expandable_segments:True"
    monkeypatch.setenv("PYTORCH_CUDA_ALLOC_CONF", "max_split_size_mb:128")
    DiffusersBackend._prepare_env()
    assert os.environ["PYTORCH_CUDA_ALLOC_CONF"] == "max_split_size_mb:128"


def test_the_resident_fit_threshold():
    # ~9.9 GiB free OOM'd a real load next to an LLM co-tenant; 11 is the floor
    assert DiffusersBackend._needs_offload(9.9) is True
    assert DiffusersBackend._needs_offload(10.9) is True
    assert DiffusersBackend._needs_offload(11.0) is False
    assert DiffusersBackend._needs_offload(24.0) is False
