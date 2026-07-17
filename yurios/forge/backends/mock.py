"""A zero-dependency stub backend so the whole service runs anywhere.

It renders a deterministic placeholder card (gradient + the prompt text) instead
of calling a model. That's enough to exercise the entire pipeline — character
assembly, the selfie template library, provenance, saving — on a laptop with no
GPU, no API key, and no network. Swap in ``comfyui``/``openrouter``/``replicate``
to get real pixels; nothing else in the service changes.

Deterministic: the same (prompt, seed) always yields the same image.
"""

from __future__ import annotations

import hashlib
import io
import textwrap

from PIL import Image, ImageDraw

from ..types import Capabilities, EditRequest, GenRequest, ImageResult
from .base import ImageBackend


def _seed_of(req: GenRequest) -> int:
    if req.seed is not None:
        return req.seed
    return int(hashlib.sha256(req.prompt.encode()).hexdigest(), 16) % (2**31)


def _hue(seed: int, salt: int) -> tuple[int, int, int]:
    h = hashlib.sha256(f"{seed}:{salt}".encode()).digest()
    return (h[0], h[1], h[2])


def _render(width: int, height: int, seed: int, title: str, body: str) -> bytes:
    top, bottom = _hue(seed, 1), _hue(seed, 2)
    img = Image.new("RGB", (width, height))
    px = img.load()
    for y in range(height):
        t = y / max(1, height - 1)
        row = tuple(int(top[i] * (1 - t) + bottom[i] * t) for i in range(3))
        for x in range(width):
            px[x, y] = row
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, width - 1, height - 1], outline=(255, 255, 255), width=3)
    draw.text((24, 24), title, fill=(255, 255, 255))
    wrapped = "\n".join(textwrap.fill(line, width=max(20, width // 9))
                        for line in body.splitlines())
    draw.text((24, 64), wrapped, fill=(235, 235, 235))
    draw.text((24, height - 28), f"seed {seed}", fill=(255, 255, 255))
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


class MockBackend(ImageBackend):
    name = "mock"

    def generate(self, req: GenRequest) -> ImageResult:
        seed = _seed_of(req)
        body = req.prompt if len(req.prompt) < 600 else req.prompt[:600] + " …"
        data = _render(req.width, req.height, seed, "image-forge · mock", body)
        return ImageResult.new(data, self.name, model="mock", seed=seed,
                               prompt=req.prompt, negative=req.negative_prompt)

    def edit(self, req: EditRequest) -> ImageResult:
        base = Image.open(req.image).convert("RGB")
        draw = ImageDraw.Draw(base)
        draw.rectangle([0, base.height - 70, base.width, base.height], fill=(20, 20, 30))
        draw.text((16, base.height - 56), f"edit: {req.instruction[:80]}", fill=(255, 255, 255))
        out = io.BytesIO()
        base.save(out, "PNG")
        return ImageResult.new(out.getvalue(), self.name, model="mock-edit",
                               seed=req.seed, prompt=req.instruction)

    def capabilities(self) -> Capabilities:
        return Capabilities(
            name=self.name, supports_edit=True, supports_reference_images=True,
            max_reference_images=4, uncensored=True,
            notes="placeholder renderer; no model — for wiring up the pipeline",
        )
