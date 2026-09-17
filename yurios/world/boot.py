"""The startup status board (SPEC §2, §6.4) — the kernel-boot log the UI shows
while she wakes.

The slow parts of boot are local models: the embedder that indexes her memory,
the MCP tool server (a spawned process, a couple of seconds each), and — when a
room is opened — the voice stack, whose Kokoro TTS, faster-whisper and silero
load cold on the CPU and can take a minute (world/voicestack.py). A fresh page
would otherwise sit on the enter gate with no sign of life. This board records
each service as it moves pending → loading → ready | failed | skipped, with how
long it took, and `/api/boot` serves the snapshot. A service that only loads on
demand declares itself `skipped` and narrates itself later if it does run: the
gate polls until every service it waits on is terminal, so nothing may be left
pending for an event that might never come. One service it does *not* wait on
is the embedder (`blocking=False`): its weights are a GIL-bound import that
runs a good deal slower while five characters are building around it, and a
room that opened only when it landed made a sixty-second boot a two-and-a-half
minute one. A recall before it lands is an empty Vault (§2.4), which is a room
she is in, so the gate opens and her memory catches up behind
it. The web boot panel (web/js/boot.js) polls it —
deliberately *not* the /api/events bus, because that stream only opens after the
enter gesture (SPEC §6.4), and the whole point is to show progress *before*
she's ready to be entered.

Thread-safe on purpose: the voice models warm on a worker thread while tools and
the mind come up on the event loop, so both writers touch one lock.

Every move is also narrated to the log, prefixed `boot:`. The panel is only
visible once the server answers requests. A cold LM Studio chat model still
loads before that; the in-process embedder and the MCP tool server do not —
they warm without holding the rest of boot (SPEC §2.4, §7.2). The tool server
is what the gate waits on; the embedder narrates itself to the log and to a
panel already gone. Those lines are what `yurios start` watches to tell "still waking" from
"wedged" (yurios/cli.py), and what the log has to show afterwards to explain
where three minutes went.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

log = logging.getLogger("world.boot")

PENDING = "pending"
LOADING = "loading"
READY = "ready"
FAILED = "failed"
SKIPPED = "skipped"

_DONE_STATES = (READY, FAILED, SKIPPED)


class BootBoard:
    """An ordered set of boot services and their live state (SPEC §2)."""

    def __init__(self, *, who: str = "",
                 clock: Callable[[], float] = time.perf_counter):
        # `who` only ever reaches the log. A house of four characters boots four
        # of these into one file, and four identical "embedding model ready"
        # lines with nothing to tell them apart is a log that hides what it is
        # recording. The panel needs no such prefix: it is per character already.
        self._who = f"{who} · " if who else ""
        self._clock = clock
        self._t0 = clock()
        self._lock = threading.Lock()
        self._svc: dict[str, dict] = {}
        self._order: list[str] = []

    def declare(self, key: str, label: str, *,
                state: str = PENDING, detail: str = "",
                blocking: bool = True) -> None:
        """Register a service so it shows in the list from the first paint. A
        service known-resolved at construction (selfies, a disabled backend) can
        declare straight into a terminal state.

        `blocking=False` keeps the line on the board — it still narrates itself
        and still lands terminal — but leaves it out of `done`, so the enter
        gate does not wait on it (SPEC §6.4). Only for a service the rooms
        genuinely degrade around: the embedder, whose absence is an empty Vault
        and not a broken turn (§2.4).
        """
        with self._lock:
            if key not in self._svc:
                self._order.append(key)
            self._svc[key] = {"key": key, "label": label, "state": state,
                              "detail": detail, "seconds": None,
                              "_start": None, "_blocking": blocking}

    def start(self, key: str, detail: str = "") -> None:
        with self._lock:
            s = self._svc[key]
            s["state"] = LOADING
            if detail:
                s["detail"] = detail
            s["_start"] = self._clock()
            label, detail = s["label"], s["detail"]
        # outside the lock: logging can block on a slow handler, and the voice
        # thread is waiting on this same one
        log.info("boot: %s%s…%s", self._who, label,
                 f" ({detail})" if detail else "")

    def done(self, key: str, *, state: str = READY, detail: str = "") -> None:
        with self._lock:
            s = self._svc[key]
            s["state"] = state
            if detail:
                s["detail"] = detail
            if s["_start"] is not None and s["seconds"] is None:
                s["seconds"] = round(self._clock() - s["_start"], 1)
            label, detail, seconds = s["label"], s["detail"], s["seconds"]
        took = f" in {seconds:.0f}s" if seconds else ""
        line = (f"boot: {self._who}{label} {state}{took}"
                + (f" — {detail}" if detail else ""))
        (log.warning if state == FAILED else log.info)(line)

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
            waited_on = [self._svc[key] for key in self._order
                         if self._svc[key].get("_blocking", True)]
            elapsed = round(self._clock() - self._t0, 1)
        done = bool(waited_on) and all(s["state"] in _DONE_STATES for s in waited_on)
        return {"services": services, "done": done, "elapsed": elapsed}
