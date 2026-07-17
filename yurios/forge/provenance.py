"""Provenance / metadata handling for outbound images.

Two postures, both supported, neither enforced (→ ch. 26, provenance + the
no-enforcement stance; → ch. 03, user-owned):

- ``strip``  — round-trip through Pillow to drop *all* upstream metadata. This is
  the opsec default the repo's batch pipeline uses (``artworks/generate.py``,
  → concepts/opsec.md): nothing about the generator travels with the file.
- ``embed``  — strip upstream metadata, then write a small ``content_credentials``
  record (the C2PA idea, minus cryptographic signing). A real duty-of-care build
  for a *hosted* operator would sign these; a user-owned build ships them as a
  sensible, removable default rather than a control it can enforce downstream.
- ``raw``    — pass bytes through untouched.
"""

from __future__ import annotations

import io
import json
from typing import Any, Dict

from PIL import Image, PngImagePlugin


def _reencode(data: bytes, pnginfo: PngImagePlugin.PngInfo | None = None) -> bytes:
    img = Image.open(io.BytesIO(data))
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if "A" in img.getbands() else "RGB")
    out = io.BytesIO()
    img.save(out, "PNG", pnginfo=pnginfo)
    return out.getvalue()


def apply(data: bytes, meta: Dict[str, Any], mode: str = "strip") -> bytes:
    if mode == "raw":
        return data
    if mode == "strip":
        return _reencode(data)
    if mode == "embed":
        info = PngImagePlugin.PngInfo()
        credentials = {
            "generator": "image-forge (YuriOS reference)",
            "backend": meta.get("backend"),
            "model": meta.get("model"),
            "created_at": meta.get("created_at"),
            "ai_generated": True,
        }
        info.add_text("content_credentials", json.dumps(credentials, separators=(",", ":")))
        return _reencode(data, info)
    raise ValueError(f"unknown provenance mode {mode!r} (strip|embed|raw)")
