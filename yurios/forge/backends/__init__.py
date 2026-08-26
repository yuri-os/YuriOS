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

import threading
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


#: Backends built once for the whole process, keyed by what they were asked
#: for (SPEC §7.6a). A host runs every character in one process against one
#: GPU, and each of them builds a camera of its own — so two characters pointed at the same
#: checkpoint used to load that checkpoint TWICE, one resident pipeline each,
#: on a card with room for one. There is nothing per-character about a pile of
#: weights: same file, same device, same settings is the same pipeline, and
#: what differs between two characters (her template book, her name on the
#: provenance) lives in `ImageForge` above the backend, not in it.
#:
#: Opt-in, and `build_forge` opts in only for the local cameras. A hosted
#: backend holds no card and shares nothing worth sharing, and one object per
#: character keeps their API renders from queueing behind each other.
_SHARED: Dict[tuple, ImageBackend] = {}
_SHARED_LOCK = threading.Lock()


def _share_key(name: str, opts: dict) -> tuple:
    """Everything that decides what the weights are and how they render.

    `repr` rather than the values themselves so the key is hashable whatever a
    backend takes, and every option is in it rather than a chosen few: two
    cameras may share a pipeline only if they would have built the same one.
    Differ by a single step count and you get your own, which is the honest
    answer — and the card-claim in `world/vram.py` is what keeps those two
    from being resident at the same moment.
    """
    return (name,) + tuple(sorted((k, repr(v)) for k, v in opts.items()))


def make_backend(name: str, *, shared: bool = False, **opts: Any) -> ImageBackend:
    if name not in REGISTRY:
        raise KeyError(f"unknown backend {name!r}; have: {', '.join(REGISTRY)}")
    if not shared:
        return REGISTRY[name](**opts)
    key = _share_key(name, opts)
    with _SHARED_LOCK:
        backend = _SHARED.get(key)
        if backend is None:
            backend = _SHARED[key] = REGISTRY[name](**opts)
        return backend


def reset_shared_backends() -> None:
    """Drop the process-wide cache — for tests, which are one process building
    hundreds of runtimes and must not inherit another test's loaded weights."""
    with _SHARED_LOCK:
        _SHARED.clear()


__all__ = ["ImageBackend", "make_backend", "reset_shared_backends", "REGISTRY"]
