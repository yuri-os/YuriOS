"""Shared data types for the image service.

Everything that crosses the backend seam is one of these. A backend receives a
``GenRequest`` or ``EditRequest`` and returns an ``ImageResult`` — nothing else.
Keeping the seam this narrow is what makes backends interchangeable (→ ch. 26,
the consistency-machinery is provider-agnostic; → ch. 07, model-agnostic runtime).
"""

from __future__ import annotations

import io
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class Capabilities:
    """What a backend can actually do, so the service can route around gaps."""

    name: str
    supports_generate: bool = True
    supports_edit: bool = False            # reference-driven editing (→ ch. 26)
    supports_reference_images: bool = False
    max_reference_images: int = 0
    supports_lora: bool = False
    # Whether the backend will render the intimate register, or None if unknown.
    # Hosted frontier models refuse it; a local uncensored model won't (→ ch. 11,
    # the model requirement — this is just backend selection, not engine policy).
    uncensored: Optional[bool] = None
    notes: str = ""


@dataclass
class GenRequest:
    """A fully-assembled text-to-image request (register already folded in)."""

    prompt: str
    negative_prompt: str = ""
    width: int = 832
    height: int = 1216                     # 2:3, the portrait the canon art uses
    steps: Optional[int] = None
    cfg: Optional[float] = None
    seed: Optional[int] = None
    # Identity controls — used by backends that support them, ignored otherwise.
    reference_images: List[Path] = field(default_factory=list)
    lora: Optional[Tuple[str, float]] = None   # (path-or-name, weight)
    extra: Dict[str, Any] = field(default_factory=dict)  # backend-specific knobs


@dataclass
class EditRequest:
    """Reference-driven edit: hold *her* identity, change the scene (→ ch. 26)."""

    image: Path                            # the source image to edit / re-render
    instruction: str                       # "her, in the rain, looking back"
    negative_prompt: str = ""
    seed: Optional[int] = None
    strength: float = 0.7
    # Identity anchors for backends that hold a character across the edit
    # (e.g. IP-Adapter on the local diffusers backend, → ch. 26).
    reference_images: List[Path] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ImageResult:
    """One generated image plus the provenance of how it was made."""

    data: bytes
    mime: str = "image/png"
    meta: Dict[str, Any] = field(default_factory=dict)
    path: Optional[Path] = None

    @classmethod
    def new(cls, data: bytes, backend: str, **meta: Any) -> "ImageResult":
        m = {
            "request_id": uuid.uuid4().hex[:12],
            "backend": backend,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        m.update({k: v for k, v in meta.items() if v is not None})
        return cls(data=data, meta=m)

    def save(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.data)
        self.path = path
        return path

    def to_pil(self):
        from PIL import Image

        return Image.open(io.BytesIO(self.data))
