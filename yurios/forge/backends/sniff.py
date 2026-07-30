"""Cheap checkpoint-architecture detection — no torch, no diffusers.

A `.safetensors` file's header (an 8-byte length prefix + a JSON tensor index)
is all it takes to tell an SDXL checkpoint from a Krea 2 one: read it, don't
load it. This is what lets `SELFIE_BACKEND=diffusers` pick the right backend
(→ ch. 26) for whatever checkpoint `SELFIE_LOCAL_MODEL` actually points at,
before anything heavy gets imported.
"""
from __future__ import annotations

import json
import struct
from pathlib import Path

# Krea 2 exports (this repo's DaSiWa-quantized ones, at least) carry these
# top-level key prefixes; no other family in this build's lineup does.
_KREA2_PREFIXES = ("blocks.", "txtfusion.", "first.", "tproj.", "tmlp.")

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
    """"sdxl" | "krea2" | "unknown" — "unknown" for anything that isn't
    recognizably either (caller decides how to fall back)."""
    header = read_safetensors_header(path)
    if not header:
        return "unknown"

    meta = header.get("__metadata__", {}) or {}
    arch = meta.get("modelspec.architecture", "")
    if arch.startswith("krea"):
        return "krea2"
    if arch:                                     # any other declared architecture
        return "sdxl" if "xl" in arch.lower() else "unknown"

    keys = [k for k in header if k != "__metadata__"]
    if any(k.startswith(_KREA2_PREFIXES) for k in keys):
        return "krea2"
    if any(k.startswith(("model.diffusion_model.", "conditioner.", "first_stage_model."))
           for k in keys):
        return "sdxl"
    return "unknown"
