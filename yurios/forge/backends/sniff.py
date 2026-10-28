"""Cheap checkpoint-architecture detection — no torch, no diffusers.

A `.safetensors` file's header (an 8-byte length prefix + a JSON tensor index)
is all it takes to tell an SDXL checkpoint from a Krea 2 or an Anima one: read
it, don't load it. This is what lets `SELFIE_BACKEND=diffusers` pick the right backend
(→ ch. 26) for whatever checkpoint `SELFIE_LOCAL_MODEL` actually points at,
before anything heavy gets imported.
"""
from __future__ import annotations

import json
import struct
import re
from dataclasses import dataclass
from pathlib import Path

# Krea 2 exports (this repo's DaSiWa-quantized ones, at least) carry these
# top-level key prefixes; no other family in this build's lineup does.
_KREA2_PREFIXES = ("blocks.", "txtfusion.", "first.", "tproj.", "tmlp.")

# Anima's text adapter, under whatever wrapper the export put round it. Nothing
# else carries one — and it must be looked for before the SDXL rule below,
# because an Anima file shares SDXL's `model.diffusion_model.` prefix.
_ANIMA_MARKER = "llm_adapter."

# A real header is JSON in the low megabytes. Anything claiming more is a file
# that isn't safetensors at all — and since the length is the first 8 bytes of
# whatever we were handed, believing it means trying to allocate a random
# 64-bit number of bytes. Bound it before reading, not after.
_MAX_HEADER_BYTES = 64 * 1024 * 1024


def read_safetensors_header(path: str | Path) -> dict:
    """The tensor index + `__metadata__`, or {} for anything unreadable. Never
    raises and never loads a byte of tensor data."""
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        with p.open("rb") as f:
            n = struct.unpack("<Q", f.read(8))[0]
            if not 0 < n <= min(_MAX_HEADER_BYTES, p.stat().st_size):
                return {}
            header = json.loads(f.read(n))
        return header if isinstance(header, dict) else {}
    except (OSError, struct.error, json.JSONDecodeError, ValueError, MemoryError):
        return {}


def sniff_local_checkpoint_architecture(path: str | Path) -> str:
    """"sdxl" | "krea2" | "anima" | "unknown" — "unknown" for anything that
    isn't recognizably one of them (caller decides how to fall back)."""
    return _architecture(read_safetensors_header(path))


def _architecture(header: dict) -> str:
    if not header:
        return "unknown"

    meta = header.get("__metadata__", {}) or {}
    arch = meta.get("modelspec.architecture", "")
    if arch.startswith("krea"):
        return "krea2"
    if arch.startswith("anima"):
        return "anima"
    if arch:                                     # any other declared architecture
        return "sdxl" if "xl" in arch.lower() else "unknown"

    keys = [k for k in header if k != "__metadata__"]
    if any(_ANIMA_MARKER in k for k in keys):
        return "anima"
    if any(k.startswith(_KREA2_PREFIXES) for k in keys):
        return "krea2"
    if any(k.startswith(("model.diffusion_model.", "conditioner.", "first_stage_model."))
           for k in keys):
        return "sdxl"
    return "unknown"


# --- will this build actually load it? -------------------------------------------
#
# The architecture says which backend gets the file; it does not say the backend
# can read it. Each local backend reads one shape of its family and refuses the
# rest at load time — after a minute of moving weights, in a log line. These are
# the same refusals, read off the header before a byte of tensor data moves.

#: The one Krea 2 weight layout krea2.py runs (comfy-kitchen's INT4 ConvRot).
KREA2_LAYOUT = "convrot_w4a4"
#: The Anima-Base 2B transformer anima.py builds and loads with strict=True:
#: 28 blocks, 2048 wide. The 2.9B / 3.8B variants are other shapes.
ANIMA_BLOCKS = 28
ANIMA_WIDTH = 2048
#: The wrappers Anima exports put round the model's own names — ComfyUI's
#: `model.diffusion_model.`, the bare `diffusion_model.`, and the original
#: release's `net.`. anima.py strips the same ones when it loads, so a prefix
#: this build reads is one it counts.
ANIMA_PREFIXES = ("model.diffusion_model.", "diffusion_model.", "net.")
_ANIMA_BLOCK = re.compile(r"^blocks\.(\d+)\.")
_ANIMA_PROBE = "blocks.0.adaln_modulation_mlp.1.weight"


def _unwrap_anima(key: str) -> str:
    for prefix in ANIMA_PREFIXES:
        if key.startswith(prefix):
            return key[len(prefix):]
    return key


@dataclass(frozen=True)
class Verdict:
    """What a checkpoint is and whether this build can render with it.

    `status` is "ok" (loads as-is), "warn" (loads, but expect a problem the
    note names) or "unsupported" (this build would refuse it)."""
    architecture: str
    status: str
    note: str

    @property
    def usable(self) -> bool:
        return self.status != "unsupported"


def verify_local_checkpoint(path: str | Path) -> Verdict:
    """Read one header and say whether the backend its architecture routes to
    would load the file. Never raises; never loads tensor data."""
    header = read_safetensors_header(path)
    arch = _architecture(header)
    keys = [k for k in header if k != "__metadata__"]
    if arch == "unknown":
        return Verdict(arch, "unsupported",
                       "not a checkpoint this build recognizes (SDXL, Krea 2 or Anima)"
                       if header else "unreadable: not a .safetensors file, or incomplete")
    if arch == "krea2":
        meta = header.get("__metadata__", {}) or {}
        try:
            layers = json.loads(meta.get("_quantization_metadata", "{}")).get("layers", {})
        except (TypeError, ValueError):
            layers = {}
        layouts = {str((spec or {}).get("format", "")) for spec in layers.values()}
        if layouts == {KREA2_LAYOUT}:
            return Verdict(arch, "ok", "Krea 2, INT4 ConvRot W4A4")
        found = ", ".join(sorted(layouts - {""})) or "not quantized"
        return Verdict(arch, "unsupported",
                       f"Krea 2 stored as {found}; the krea2 backend runs only "
                       f"INT4 ConvRot W4A4 files")
    if arch == "anima":
        names = {_unwrap_anima(k): k for k in keys}
        blocks = {int(m.group(1)) for k in names for m in [_ANIMA_BLOCK.match(k)] if m}
        probe = header.get(names.get(_ANIMA_PROBE, "")) or {}
        shape = probe.get("shape") if isinstance(probe, dict) else None
        width = shape[1] if isinstance(shape, list) and len(shape) == 2 else 0
        if len(blocks) == ANIMA_BLOCKS and width == ANIMA_WIDTH:
            return Verdict(arch, "ok", "Anima 2B")
        return Verdict(arch, "unsupported",
                       f"an Anima variant with {len(blocks)} blocks at width {width or '?'}; "
                       f"the anima backend builds the 2B model "
                       f"({ANIMA_BLOCKS} blocks at {ANIMA_WIDTH})")
    if "v_pred" in keys:
        return Verdict(arch, "warn",
                       "SDXL with v-prediction; the SDXL backend samples it as "
                       "epsilon, so pictures may come out grey or washed out")
    return Verdict(arch, "ok", "SDXL")
