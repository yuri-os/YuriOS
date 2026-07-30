"""The other local camera (Krea 2) — the in-process INT4 transformer backend.

Entirely offline, no torch, no GPU, no Hugging Face: the backend's heavy seams
(``_load``, ``_make_generator``) are swapped for fakes, the same philosophy as
the SDXL backend's tests and the voice stack (SPEC §27).

The one thing that genuinely cannot be faked into correctness is the key map —
a checkpoint's tensor names against ``Krea2Transformer2DModel``'s own — so that
is tested as pure string rules, which is exactly what it is.
"""
from __future__ import annotations

import json
import struct

import pytest
from PIL import Image

from yurios.forge.backends.krea2 import Krea2Backend, rename_key
from yurios.forge.backends.sniff import sniff_local_checkpoint_architecture
from yurios.forge.types import GenRequest
from yurios.world.selfies import build_forge


def write_safetensors_header(path, keys, metadata=None):
    """A real safetensors header (8-byte length + JSON) with no tensor data —
    which is all the sniffing and the metadata reads ever look at."""
    header = {k: {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]} for k in keys}
    if metadata is not None:
        header["__metadata__"] = metadata
    blob = json.dumps(header).encode()
    path.write_bytes(struct.pack("<Q", len(blob)) + blob + b"\x00" * 4)
    return path


class _Out:
    def __init__(self, images):
        self.images = images


class FakePipe:
    """Stands in for Krea2Pipeline: records the call, returns a solid image."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, **kw):
        self.calls.append(kw)
        return _Out([Image.new("RGB", (kw["width"], kw["height"]), (10, 20, 30))])


@pytest.fixture
def backend(monkeypatch, tmp_path):
    ckpt = write_safetensors_header(
        tmp_path / "someKrea2_base.safetensors", ["blocks.0.attn.wq.weight"],
        {"modelspec.architecture": "krea-2", "modelspec.title": "Krea 2 Base"})
    b = Krea2Backend(model_path=str(ckpt))
    pipe = FakePipe()
    monkeypatch.setattr(b, "_load", lambda: pipe)
    monkeypatch.setattr(b, "_make_generator", lambda seed: None)
    b._fake_pipe = pipe
    return b


# ---- the key map (pure string rules; no torch, no checkpoint) ---------------

@pytest.mark.parametrize("src,want", [
    ("first.weight",                       "img_in.weight"),
    ("tproj.1.bias",                       "time_mod_proj.bias"),
    ("tmlp.0.weight",                      "time_embed.linear_1.weight"),
    ("txtmlp.0.scale",                     "txt_in.norm.weight"),
    ("last.modulation.lin",                "final_layer.scale_shift_table"),
    ("last.norm.scale",                    "final_layer.norm.weight"),
    ("txtfusion.projector.weight",         "text_fusion.projector.weight"),
    ("blocks.7.attn.wq.weight",            "transformer_blocks.7.attn.to_q.weight"),
    ("blocks.7.attn.wo.weight",            "transformer_blocks.7.attn.to_out.0.weight"),
    ("blocks.7.attn.qknorm.qnorm.scale",   "transformer_blocks.7.attn.norm_q.weight"),
    ("blocks.7.mlp.down.weight",           "transformer_blocks.7.ff.down.weight"),
    ("blocks.7.mod.lin",                   "transformer_blocks.7.scale_shift_table"),
    ("blocks.27.prenorm.scale",            "transformer_blocks.27.norm1.weight"),
    ("txtfusion.layerwise_blocks.1.mlp.up.weight",
     "text_fusion.layerwise_blocks.1.ff.up.weight"),
    ("txtfusion.refiner_blocks.0.attn.wv.weight",
     "text_fusion.refiner_blocks.0.attn.to_v.weight"),
])
def test_rename_key_maps_the_architecture(src, want):
    assert rename_key(src) == want


def test_txtfusion_blocks_are_not_swallowed_by_the_bare_blocks_prefix():
    # "blocks." is a substring of "…_blocks."; the prefix table is ordered so
    # the specific ones win. A regression here silently loads text-fusion
    # weights into the image tower.
    assert rename_key("txtfusion.refiner_blocks.0.prenorm.scale") == \
        "text_fusion.refiner_blocks.0.norm1.weight"


@pytest.mark.parametrize("key", ["blocks.0.unknown.thing", "totally.unknown",
                                 "blocks.notanumber.mod.lin"])
def test_rename_key_returns_none_for_the_unknown(key):
    assert rename_key(key) is None


# ---- architecture sniffing --------------------------------------------------

def test_sniff_reads_the_declared_architecture(tmp_path):
    p = write_safetensors_header(tmp_path / "k.safetensors", ["blocks.0.mod.lin"],
                                 {"modelspec.architecture": "krea-2"})
    assert sniff_local_checkpoint_architecture(p) == "krea2"


def test_sniff_falls_back_to_key_shape_with_no_metadata(tmp_path):
    p = write_safetensors_header(
        tmp_path / "k.safetensors",
        ["blocks.0.mod.lin", "txtfusion.projector.weight", "first.weight"])
    assert sniff_local_checkpoint_architecture(p) == "krea2"


def test_sniff_recognises_an_sdxl_checkpoint(tmp_path):
    p = write_safetensors_header(
        tmp_path / "s.safetensors",
        ["model.diffusion_model.input_blocks.0.0.weight", "conditioner.embedders.0.x"])
    assert sniff_local_checkpoint_architecture(p) == "sdxl"


def test_sniff_is_unknown_for_junk_and_missing_files(tmp_path):
    junk = tmp_path / "junk.safetensors"
    junk.write_bytes(b"definitely not a safetensors file")
    assert sniff_local_checkpoint_architecture(junk) == "unknown"
    assert sniff_local_checkpoint_architecture(tmp_path / "nope.safetensors") == "unknown"


# ---- sampling defaults: a distilled checkpoint must not be driven like a base

def test_turbo_checkpoints_get_few_steps_and_no_guidance(tmp_path):
    ckpt = write_safetensors_header(
        tmp_path / "x.safetensors", ["blocks.0.mod.lin"],
        {"modelspec.title": "DaSiWa-Krea2-Turbo-UC-CuteDisaster-v2"})
    b = Krea2Backend(model_path=str(ckpt))
    assert b._is_turbo() is True
    assert b._defaults() == (Krea2Backend.TURBO_STEPS, Krea2Backend.TURBO_CFG)


def test_base_checkpoints_get_the_base_defaults(tmp_path):
    ckpt = write_safetensors_header(
        tmp_path / "x.safetensors", ["blocks.0.mod.lin"],
        {"modelspec.title": "Krea 2 Raw"})
    b = Krea2Backend(model_path=str(ckpt))
    assert b._is_turbo() is False
    assert b._defaults() == (Krea2Backend.BASE_STEPS, Krea2Backend.BASE_CFG)


def test_turbo_is_detected_from_the_filename_when_metadata_is_silent(tmp_path):
    ckpt = write_safetensors_header(tmp_path / "someModel_turbo.safetensors",
                                    ["blocks.0.mod.lin"], {})
    assert Krea2Backend(model_path=str(ckpt))._is_turbo() is True


def test_explicit_config_beats_the_checkpoint(tmp_path):
    ckpt = write_safetensors_header(
        tmp_path / "x.safetensors", ["blocks.0.mod.lin"],
        {"modelspec.title": "Krea 2 Turbo"})
    b = Krea2Backend(model_path=str(ckpt), steps=20, cfg=3.5)
    assert b._defaults() == (20, 3.5)


def test_guidance_zero_is_a_real_value_not_an_unset_one(tmp_path):
    # cfg=0.0 disables guidance and must survive; only cfg<0 means "auto".
    ckpt = write_safetensors_header(tmp_path / "x.safetensors", ["blocks.0.mod.lin"],
                                    {"modelspec.title": "Krea 2 Raw"})
    assert Krea2Backend(model_path=str(ckpt), cfg=0.0)._defaults()[1] == 0.0


# ---- the render contract ----------------------------------------------------

def test_generate_passes_plain_strings_at_full_length(backend):
    # Unlike SDXL's 77-token CLIPs, Qwen3-VL takes the whole prompt — so the
    # backend must NOT do embedding surgery, it must hand the string over.
    req = GenRequest(prompt="masterpiece, 1girl, cat ears", negative_prompt="lowres",
                     width=1024, height=1024, seed=1234)
    result = backend.generate(req)

    assert result.data[:4] == b"\x89PNG"
    call = backend._fake_pipe.calls[0]
    assert call["prompt"] == "masterpiece, 1girl, cat ears"
    assert call["negative_prompt"] == "lowres"
    assert call["width"] == 1024 and call["height"] == 1024
    assert call["max_sequence_length"] == 512


def test_generate_uses_the_checkpoints_own_defaults(backend):
    backend.generate(GenRequest(prompt="x", width=1024, height=1024))
    call = backend._fake_pipe.calls[0]
    assert call["num_inference_steps"] == Krea2Backend.BASE_STEPS
    assert call["guidance_scale"] == Krea2Backend.BASE_CFG


def test_per_call_overrides_beat_the_instance_defaults(backend):
    backend.generate(GenRequest(prompt="x", width=64, height=64, steps=12, cfg=2.0))
    call = backend._fake_pipe.calls[0]
    assert call["num_inference_steps"] == 12 and call["guidance_scale"] == 2.0


def test_an_empty_negative_becomes_none_not_an_empty_string(backend):
    backend.generate(GenRequest(prompt="x", negative_prompt="", width=64, height=64))
    assert backend._fake_pipe.calls[0]["negative_prompt"] is None


def test_a_seedless_request_still_records_one(backend):
    result = backend.generate(GenRequest(prompt="x", width=64, height=64))
    assert isinstance(result.meta["seed"], int)


def test_provenance_names_the_backend_and_the_checkpoint(backend):
    meta = backend.generate(GenRequest(prompt="x", width=64, height=64)).meta
    assert meta["backend"] == "krea2"
    assert meta["model"].endswith(".safetensors")
    assert meta["sampler"] == "FlowMatchEulerDiscrete"


# ---- OOM: flip to offload and retry once (the SDXL backend's rule) ----------

def test_out_of_memory_retries_once_with_offload(monkeypatch, backend):
    calls = []

    def boom(req):
        calls.append(backend.cpu_offload)
        if len(calls) == 1:
            raise RuntimeError("CUDA out of memory. Tried to allocate 2 GiB")
        return "second-result"

    monkeypatch.setattr(backend, "_render", boom)
    monkeypatch.setattr(backend, "_teardown", lambda: None)
    assert backend.generate(GenRequest(prompt="x")) == "second-result"
    assert calls == [False, True]               # retried, with offload flipped on


def test_out_of_memory_while_already_offloading_propagates(monkeypatch, backend):
    backend.cpu_offload = True

    def boom(req):
        raise RuntimeError("CUDA out of memory")

    monkeypatch.setattr(backend, "_render", boom)
    with pytest.raises(RuntimeError):
        backend.generate(GenRequest(prompt="x"))


def test_a_non_oom_runtime_error_is_not_retried(monkeypatch, backend):
    def boom(req):
        raise RuntimeError("the checkpoint is missing a tensor")

    monkeypatch.setattr(backend, "_render", boom)
    with pytest.raises(RuntimeError, match="missing a tensor"):
        backend.generate(GenRequest(prompt="x"))
    assert backend.cpu_offload is False


# ---- health / capabilities --------------------------------------------------

def test_health_needs_deps_and_a_real_checkpoint(monkeypatch, tmp_path):
    monkeypatch.setattr(Krea2Backend, "deps_available", staticmethod(lambda: True))
    ckpt = write_safetensors_header(tmp_path / "k.safetensors", ["blocks.0.mod.lin"])
    assert Krea2Backend(model_path=str(ckpt)).health() is True
    assert Krea2Backend(model_path=str(tmp_path / "gone.safetensors")).health() is False
    assert Krea2Backend(model_path="").health() is False

    monkeypatch.setattr(Krea2Backend, "deps_available", staticmethod(lambda: False))
    assert Krea2Backend(model_path=str(ckpt)).health() is False


def test_capabilities_are_honest_about_the_local_checkpoint(tmp_path):
    ckpt = write_safetensors_header(tmp_path / "k.safetensors", ["blocks.0.mod.lin"])
    caps = Krea2Backend(model_path=str(ckpt)).capabilities()
    assert caps.name == "krea2"
    assert caps.uncensored is True
    assert "krea/Krea-2-Raw" in caps.notes


def test_the_gated_repo_error_says_what_to_do(monkeypatch, tmp_path):
    ckpt = write_safetensors_header(tmp_path / "k.safetensors", ["blocks.0.mod.lin"])
    b = Krea2Backend(model_path=str(ckpt))

    def denied(*a, **kw):
        raise OSError("401 Client Error: gated repo")

    monkeypatch.setattr("huggingface_hub.snapshot_download", denied)
    with pytest.raises(RuntimeError, match="huggingface-cli login"):
        b._snapshot()


def test_a_short_card_chooses_offload_before_trying_resident():
    # The OOM retry works, but it pays for the whole load twice — so the
    # decision is made up front from free VRAM.
    assert Krea2Backend._needs_offload(Krea2Backend.RESIDENT_FREE_GIB - 1) is True
    assert Krea2Backend._needs_offload(Krea2Backend.RESIDENT_FREE_GIB + 1) is False


def test_a_resident_floor_is_declared_for_the_vram_parker():
    # world/vram.py reads this off the backend to decide whether to park her
    # brain; None here would silently disable parking for a 6 GB render.
    assert isinstance(Krea2Backend.RESIDENT_FREE_GIB, float)


# ---- build_forge: one knob, the file picks the loader -----------------------

def test_build_forge_routes_a_krea2_checkpoint_to_the_krea2_backend(
        monkeypatch, cfg, tmp_path):
    monkeypatch.setattr(Krea2Backend, "deps_available", staticmethod(lambda: True))
    ckpt = write_safetensors_header(
        tmp_path / "k.safetensors", ["blocks.0.mod.lin"],
        {"modelspec.architecture": "krea-2"})
    cfg.selfie_backend = "diffusers"             # the knob still says diffusers
    cfg.selfie_local_model = str(ckpt)

    forge, status = build_forge(cfg)
    assert status == "krea2"
    assert forge.backend.name == "krea2"


def test_build_forge_still_routes_sdxl_to_the_diffusers_backend(
        monkeypatch, cfg, tmp_path):
    from yurios.forge.backends.diffusers import DiffusersBackend
    monkeypatch.setattr(DiffusersBackend, "deps_available", staticmethod(lambda: True))
    ckpt = write_safetensors_header(
        tmp_path / "s.safetensors",
        ["model.diffusion_model.input_blocks.0.0.weight"])
    cfg.selfie_backend = "diffusers"
    cfg.selfie_local_model = str(ckpt)

    forge, status = build_forge(cfg)
    assert status == "diffusers"
    assert forge.backend.name == "diffusers"


def test_build_forge_degrades_loudly_without_the_krea2_deps(
        monkeypatch, cfg, tmp_path, caplog):
    monkeypatch.setattr(Krea2Backend, "deps_available", staticmethod(lambda: False))
    ckpt = write_safetensors_header(
        tmp_path / "k.safetensors", ["blocks.0.mod.lin"],
        {"modelspec.architecture": "krea-2"})
    cfg.selfie_backend = "krea2"
    cfg.selfie_local_model = str(ckpt)

    with caplog.at_level("WARNING"):
        forge, status = build_forge(cfg)
    assert forge.backend.name == "mock"
    assert "krea2" in status
    assert "forge-krea2" in caplog.text          # names the extra to install
