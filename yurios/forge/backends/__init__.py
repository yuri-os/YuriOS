"""Backend registry — the swap table, trimmed to this build's two entries.

The source registry also carries `comfyui` / `replicate` / `diffusers` (local
GPU and hosted-GPU paths). Build #4 vendors only the GPU-free pair on purpose
(SPEC §7.6): `mock` needs nothing, `openrouter` needs a key. Add a provider by
writing one ``ImageBackend`` and registering it here — nothing else changes.
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


REGISTRY: Dict[str, Callable[..., ImageBackend]] = {
    "mock": _mock,
    "openrouter": _openrouter,
}


def make_backend(name: str, **opts: Any) -> ImageBackend:
    if name not in REGISTRY:
        raise KeyError(f"unknown backend {name!r}; have: {', '.join(REGISTRY)}")
    return REGISTRY[name](**opts)


__all__ = ["ImageBackend", "make_backend", "REGISTRY"]
