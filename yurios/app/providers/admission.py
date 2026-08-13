"""Process-wide admission control for model inference.

Every provider on an event loop shares one gate.  Keeping gates loop-local avoids
binding asyncio primitives to a loop other than the one using them while still
making independently constructed chat and utility providers contend globally.
"""
from __future__ import annotations

import asyncio
import threading
import weakref


DEFAULT_ACTIVE = 1
DEFAULT_QUEUE = 8


class InferenceBusy(RuntimeError):
    """Raised when inference is active and its bounded waiting queue is full."""


class InferenceAdmission:
    """An asyncio inference gate with bounded active and waiting populations."""

    def __init__(self, active: int = DEFAULT_ACTIVE, queue: int = DEFAULT_QUEUE):
        if active < 1:
            raise ValueError("active must be at least 1")
        if queue < 0:
            raise ValueError("queue must not be negative")
        self._semaphore = asyncio.Semaphore(active)
        self._queue_limit = queue
        self._waiting = 0

    @property
    def waiting(self) -> int:
        return self._waiting

    async def __aenter__(self) -> InferenceAdmission:
        if self._semaphore.locked():
            if self._waiting >= self._queue_limit:
                raise InferenceBusy("inference queue is full")
            self._waiting += 1
            try:
                await self._semaphore.acquire()
            finally:
                self._waiting -= 1
        else:
            await self._semaphore.acquire()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        self._semaphore.release()


_gates: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, InferenceAdmission]
_gates = weakref.WeakKeyDictionary()
_gates_lock = threading.Lock()


def inference_admission() -> InferenceAdmission:
    """Return the shared inference gate for the running event loop."""
    loop = asyncio.get_running_loop()
    with _gates_lock:
        gate = _gates.get(loop)
        if gate is None:
            gate = InferenceAdmission()
            _gates[loop] = gate
        return gate
