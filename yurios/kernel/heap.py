"""Handing freed weights back to the OS — `malloc_trim`, behind a guard.

Every local model this process can hold — a render pipeline (SPEC §7.6a), the
voice stack, a parked GGUF context — is read into host RAM on a worker thread,
and glibc serves those allocations from per-thread arenas. `free` keeps the
pages inside the arena for reuse rather than returning them, so a teardown that
drops every Python reference, collects, and empties the CUDA cache can still
leave the process holding the whole model. Observed as ~15 GB (most of it
swapped out) thirteen hours after a selfie pipeline's second unload; one
`malloc_trim(0)` gave back 13 GB.

It lives in the kernel because the forge, the voice stack and the providers
all unload weights, and none of them should reach sideways into another for
it. Standard library only, like the rest of this package. Off Linux, or on a
libc without the symbol, it does nothing.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from typing import Any, Optional

log = logging.getLogger("kernel.heap")


def _libc() -> Optional[Any]:
    """The process's own C library, or None where it can't be reached."""
    try:
        return ctypes.CDLL(None)
    except OSError:
        return None


def trim_heap() -> bool:
    """Ask glibc to return freed heap pages to the OS; True when it ran.

    Call it after the references are gone and `gc.collect()` has run — it can
    only give back what is already free. It walks every arena, so it belongs on
    a worker thread, never on the event loop.
    """
    if not sys.platform.startswith("linux"):
        return False                    # macOS/Windows allocators return on their own
    libc = _libc()
    trim = getattr(libc, "malloc_trim", None) if libc is not None else None
    if trim is None:
        return False                    # not glibc (musl, …)
    trim.argtypes = [ctypes.c_size_t]
    trim.restype = ctypes.c_int
    try:
        trim(0)
    except Exception:                   # never let a courtesy call fail a teardown
        log.debug("malloc_trim failed", exc_info=True)
        return False
    return True
