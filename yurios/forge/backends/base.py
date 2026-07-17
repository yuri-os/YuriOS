"""The backend seam — the one interface every image generator is reduced to.

A backend is *any* way of turning a request into pixels: a local ComfyUI graph,
a hosted API, a stub. The service knows nothing about which one it holds. Swapping
the generator is swapping the object behind this interface — the image-side of the
model-agnostic runtime (→ ch. 07; → ch. 26, the consistency machinery is generic).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..types import Capabilities, EditRequest, GenRequest, ImageResult


class ImageBackend(ABC):
    name: str = "base"

    @abstractmethod
    def generate(self, req: GenRequest) -> ImageResult:
        """Text-to-image. Must return PNG (or other) bytes in an ImageResult."""

    def edit(self, req: EditRequest) -> ImageResult:
        """Reference-driven edit. Override in backends that support it."""
        raise NotImplementedError(f"{self.name} backend does not support editing")

    @abstractmethod
    def capabilities(self) -> Capabilities:
        ...

    def health(self) -> bool:
        """Cheap reachability check. Override for networked backends."""
        return True
