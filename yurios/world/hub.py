"""The event hub (SPEC §4, §10) — the single outbound bus to the frontends.

This is YuriOS's `EventHub` (host/http_api.py), ported: every host→frontend
event — chat messages, drafts, puppet commands, scene changes — is one typed
JSON dict fanned out to every subscriber, drained by the `/api/events` SSE
route. One bus, many event types, instead of one socket per concern; the shape
Build #4 converges on as it grows toward the YuriOS runtime (SPEC §14).

Two rules carried over from the pieces it replaces:

  - **A stalled client loses events; it never blocks the publisher** (the
    YuriOS EventHub rule, previously enforced per-viewer in `VrmController`):
    puts are non-blocking, a full queue drops.
  - **Sticky replay** (previously `VrmController._sticky`): appearance state —
    rain, music, material tints — is remembered under a key and replayed to
    every new subscriber, so a reload doesn't reset the scene.

Publishes are safe from the event loop *or* a worker thread (the TTS synth
thread, a demo script): off-loop calls hop in via `call_soon_threadsafe`.
Before any subscriber has ever attached there is no loop to hop to — sticky
state is still recorded, live fan-out is skipped (nobody is listening).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Hashable, List, Optional

log = logging.getLogger("world.hub")


class EventHub:
    """Fan-out of typed host events to every attached frontend."""

    def __init__(self, max_queue: int = 256):
        self._max_queue = max_queue
        self._queues: List[asyncio.Queue] = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # sticky state, replayed on subscribe (SPEC §4): key → last event
        self.sticky: Dict[Hashable, Dict[str, Any]] = {}

    # ---- publish (any thread) ----

    def publish(self, type_: str, payload: Dict[str, Any],
                sticky: Optional[Hashable] = None) -> None:
        """Fan one event out to every subscriber. `sticky` keys the event for
        replay to future subscribers (the last event per key wins)."""
        event = {"type": type_, **payload}
        if sticky is not None:
            self.sticky[sticky] = event
        loop = self._loop
        if loop is None or loop.is_closed():
            return                                # no subscriber has ever attached
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._fan(event)
        else:                                     # a worker thread (TTS, a script)
            loop.call_soon_threadsafe(self._fan, event)

    def _fan(self, event: Dict[str, Any]) -> None:
        for q in list(self._queues):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # a stalled client loses events; it never blocks the publisher
                log.debug("subscriber queue full; dropping %s", event.get("type"))

    # ---- subscribe (on the loop; called by the /api/events route) ----

    def subscribe(self) -> asyncio.Queue:
        """Register a subscriber; returns its queue, pre-loaded with sticky state."""
        self._loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=self._max_queue)
        for event in self.sticky.values():
            q.put_nowait(event)
        self._queues.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._queues:
            self._queues.remove(q)

    @property
    def subscribers(self) -> int:
        return len(self._queues)
