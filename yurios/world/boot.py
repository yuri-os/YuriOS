"""The startup status board (SPEC §2, §6.4) — the kernel-boot log the UI shows
while she wakes.

The slow part of boot is the local voice stack: Kokoro TTS, faster-whisper and
silero load cold on the CPU and can take a minute (world.main._warm_voice), so a
fresh page would otherwise sit on the enter gate with no sign of life. This board
records each service as it moves pending → loading → ready | failed | skipped,
with how long it took, and `/api/boot` serves the snapshot. The web boot panel
(web/js/boot.js) polls it — deliberately *not* the /api/events bus, because that
stream only opens after the enter gesture (SPEC §6.4), and the whole point is to
show progress *before* she's ready to be entered.

Thread-safe on purpose: the voice models warm on a worker thread while tools and
the mind come up on the event loop, so both writers touch one lock.
"""
from __future__ import annotations

import threading
import time
from typing import Callable

PENDING = "pending"
LOADING = "loading"
READY = "ready"
FAILED = "failed"
SKIPPED = "skipped"

_DONE_STATES = (READY, FAILED, SKIPPED)


class BootBoard:
    """An ordered set of boot services and their live state (SPEC §2)."""

    def __init__(self, *, clock: Callable[[], float] = time.perf_counter):
        self._clock = clock
        self._t0 = clock()
        self._lock = threading.Lock()
        self._svc: dict[str, dict] = {}
        self._order: list[str] = []

    def declare(self, key: str, label: str, *,
                state: str = PENDING, detail: str = "") -> None:
        """Register a service so it shows in the list from the first paint. A
        service known-resolved at construction (selfies, a disabled backend) can
        declare straight into a terminal state."""
        with self._lock:
            if key not in self._svc:
                self._order.append(key)
            self._svc[key] = {"key": key, "label": label, "state": state,
                              "detail": detail, "seconds": None, "_start": None}

    def start(self, key: str, detail: str = "") -> None:
        with self._lock:
            s = self._svc[key]
            s["state"] = LOADING
            if detail:
                s["detail"] = detail
            s["_start"] = self._clock()

    def done(self, key: str, *, state: str = READY, detail: str = "") -> None:
        with self._lock:
            s = self._svc[key]
            s["state"] = state
            if detail:
                s["detail"] = detail
            if s["_start"] is not None and s["seconds"] is None:
                s["seconds"] = round(self._clock() - s["_start"], 1)

    def unresolved(self, keys) -> list[str]:
        """Which of `keys` are declared but still pending/loading — used to
        settle stages a failure skipped so the panel never hangs."""
        with self._lock:
            return [k for k in keys
                    if k in self._svc and self._svc[k]["state"] not in _DONE_STATES]

    def snapshot(self) -> dict:
        """The wire shape /api/boot returns and web/js/boot.js renders."""
        with self._lock:
            services = [{k: v for k, v in self._svc[key].items()
                         if not k.startswith("_")}
                        for key in self._order]
            elapsed = round(self._clock() - self._t0, 1)
        done = bool(services) and all(s["state"] in _DONE_STATES for s in services)
        return {"services": services, "done": done, "elapsed": elapsed}
