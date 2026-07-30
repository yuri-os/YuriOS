"""Backend registry — the swap table.

The source registry also carries `comfyui` / `replicate` (hosted-GPU paths).
This build vendors the GPU-free pair (`mock` needs nothing, `openrouter` needs
a key) plus two local-first cameras (→ ch. 11), each loading a single-file
checkpoint in-process on your own GPU: `diffusers` (an SDXL UNet) and `krea2`
(a Krea 2 diffusion transformer, kept in INT4). Which of the two a given
checkpoint needs is read off the file itself — see `sniff.py`, and
`world/selfies.py`'s `build_forge`. Add a provider by writing one
``ImageBackend`` and registering it here — nothing else changes.
"""

from __future__ import annotations

from typing import Any, Callable, Dict

from .base import ImageBackend


def _mock(**opts: Any) -> ImageBackend:
    from .mock import MockBackend
    return MockBackend(**opts)


def _openrouter(**opts: Any) -> ImageBackend:
    from .openrouter import OpenRouterBackend
    return OpenRouterBackend(**opts)


def _diffusers(**opts: Any) -> ImageBackend:
    from .diffusers import DiffusersBackend
    return DiffusersBackend(**opts)


def _krea2(**opts: Any) -> ImageBackend:
    from .krea2 import Krea2Backend
    return Krea2Backend(**opts)


REGISTRY: Dict[str, Callable[..., ImageBackend]] = {
    "mock": _mock,
    "openrouter": _openrouter,
    "diffusers": _diffusers,
    "krea2": _krea2,
}


def make_backend(name: str, **opts: Any) -> ImageBackend:
    if name not in REGISTRY:
        raise KeyError(f"unknown backend {name!r}; have: {', '.join(REGISTRY)}")
    return REGISTRY[name](**opts)


__all__ = ["ImageBackend", "make_backend", "REGISTRY"]
