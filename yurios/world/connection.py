"""Whether her model's server answers, said to every room (SPEC §31.5).

`providers/reach.py` knows how to ask a server and how to start a local one;
this is the runtime's side of it — what was last heard, who is told, and what
happens next:

  - **At boot, before her providers are built.** That order is the point: with
    the GGUF fallback on, an `lm_studio/…` model whose server is down at build
    time is built as a llama.cpp model instead, and stays one. So the server is
    asked first, and a local one we know how to run is started and waited for
    (MODEL_SERVER_AUTOSTART), and only then does the brain build.
  - **A sticky `connection` event.** Rooms opened later learn it on attach, the
    banner in every room follows it (web/shared/connection.js), and /api/health
    names it as a degradation — the §10 rule that cross-surface state is an
    event, not a poll.
  - **After a failed turn**, the server is asked again, so "her reply failed"
    turns into "LM Studio isn't running" rather than a stack trace.
  - **While a server is down**, it is asked again on a slow beat, so LM Studio
    opened by hand clears the banner on its own. Coming back calls
    `on_recovered`, which is where the runtime moves her off the fallback and
    pins her models (world/main.py).

Thread-safe: the boot check runs on the character's build thread, everything
after it on the event loop.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable
from dataclasses import replace

from yurios.app.providers import reach
from yurios.app.providers.reach import DOWN, FAILED, OK, STARTING, TROUBLE, Link

log = logging.getLogger("world.connection")

#: How often a down server is asked again. A local GET is nothing, and a person
#: who has just opened LM Studio should not wait long for the banner to notice.
WATCH_INTERVAL_S = 10.0
#: How long a start waits for the server to answer — a cold LM Studio app is
#: tens of seconds; past this it is a failure that says so.
START_WAIT_S = 60.0

#: Where a turn's exception came from, by module, when it is the model's fault
#: and not, say, a tool's — only these turn a failure into a connection state.
_MODEL_ERROR_MODULES = ("litellm", "httpx", "httpcore", "openai",
                        "yurios.app.providers")


def _model_error(exc: BaseException | None) -> BaseException | None:
    seen = 0
    while exc is not None and seen < 8:
        if type(exc).__module__.startswith(_MODEL_ERROR_MODULES):
            return exc
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return None


class ConnectionWatch:
    """The last word on each server she depends on, and the loop that keeps it
    true while one of them is down."""

    def __init__(self, cfg, hub, *,
                 fallback: Callable[[], str] = lambda: "",
                 on_recovered: Callable[[list[Link]], Awaitable[None]] | None = None,
                 interval_s: float = WATCH_INTERVAL_S):
        self.cfg = cfg
        self.hub = hub
        self.fallback = fallback
        self.on_recovered = on_recovered
        self.interval_s = interval_s
        self._lock = threading.Lock()
        self._links: list[Link] = []
        self._seams = {"chat": False, "utility": False, "embed": False}
        self._failure = ""             # a turn failure the probe could not explain
        self._busy = ""                # "checking" | "starting" while a request runs
        self._watch: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._asks: set[asyncio.Task] = set()   # re-asks a failed turn started

    # ---- what is known ------------------------------------------------------

    @property
    def tracking(self) -> bool:
        """Whether any server is behind the seams this runtime built."""
        return any(self._seams.values())

    def snapshot(self) -> dict:
        with self._lock:
            links = [replace(link) for link in self._links]
            failure, busy = self._failure, self._busy
        if failure:
            links = [replace(link, state=FAILED, detail=failure)
                     if "chat" in link.roles and link.state not in TROUBLE else link
                     for link in links]
        state = reach.worst(links) if links else OK
        bad = next((link for link in sorted(links, key=lambda li: reach.SEVERITY.index(li.state))
                    if link.state != OK), None)
        out = {"state": state, "busy": busy,
               "links": [link.as_dict() for link in links],
               "detail": bad.detail if bad and bad.state in TROUBLE | {STARTING} else "",
               "fallback": ""}
        # She still answers through the built-in runner while LM Studio is away
        # — which is worth saying in the same breath, since it is why a turn
        # may be slow or come back in a different voice.
        if state in TROUBLE:
            out["fallback"] = self.fallback() or ""
        return out

    def is_down(self, provider: str) -> bool:
        """Whether the last word on *provider*'s server was that nothing answered."""
        with self._lock:
            return any(li.provider == provider and li.state == DOWN for li in self._links)

    def degradations(self) -> list[str]:
        snap = self.snapshot()
        return [f"model server: {snap['detail']}"] if snap["state"] in TROUBLE else []

    def _publish(self) -> dict:
        snap = self.snapshot()
        self.hub.publish("connection", snap, sticky="connection")
        return snap

    def _set(self, links: list[Link]) -> list[Link]:
        """Store *links*; return the ones that were down (or being started) and
        answer now. A server coming back also retires the turn failure it
        caused — the banner must not go on blaming a server that is up."""
        with self._lock:
            before = {(li.provider, li.endpoint): li.state for li in self._links}
            self._links = links
            back = [li for li in links if li.state == OK
                    and before.get((li.provider, li.endpoint)) in (DOWN, STARTING)]
            if back:
                self._failure = ""
        return back

    # ---- boot (the build thread) --------------------------------------------

    def boot(self, *, chat: bool, utility: bool, embed: bool, board=None,
             autostart: bool = True) -> None:
        """Ask every server her built seams need; start a local one that is down.

        Blocking — the character's build thread, before the providers exist."""
        self._seams = {"chat": chat, "utility": utility, "embed": embed}
        links = reach.links_for(self.cfg, chat=chat, utility=utility, embed=embed)
        if not links:
            self._publish()
            return
        names = ", ".join(sorted({li.label for li in links}))
        if board is not None:
            board.declare("server", "mind · model server")
            board.start("server", detail=names)
        links = reach.check(links)
        for i, link in enumerate(links):
            if link.state == DOWN and autostart and link.startable:
                if board is not None:
                    board.start("server", detail=f"starting {link.label}…")
                with self._lock:
                    self._links = links[:i] + [replace(link, state=STARTING,
                                               detail=f"starting {link.label}…")] + links[i + 1:]
                self._publish()
                links[i] = reach.start(link, wait_s=START_WAIT_S)
        self._set(links)
        snap = self._publish()
        if board is not None:
            if snap["state"] in TROUBLE:
                board.done("server", state="failed", detail=snap["detail"][:120])
            else:
                board.done("server", detail=" · ".join(li.detail for li in links)[:120])

    # ---- the event loop ------------------------------------------------------

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        """Where to run the watch from — set once start_async is on the loop,
        and the watch begun if boot left a server down."""
        self._loop = loop
        if self._needs_watch():
            self._ensure_watch()

    def _needs_watch(self) -> bool:
        with self._lock:
            return any(li.state == DOWN for li in self._links)

    def _ensure_watch(self) -> None:
        if self._loop is None or (self._watch and not self._watch.done()):
            return
        self._watch = self._loop.create_task(self._watch_loop(), name="connection-watch")

    async def _watch_loop(self) -> None:
        while self._needs_watch():
            await asyncio.sleep(self.interval_s)
            await self.recheck(quiet=True)

    async def recheck(self, *, quiet: bool = False) -> dict:
        """Ask every server again. `quiet` is the watch's own beat: no `busy`
        flag for the room to show, since nobody pressed anything."""
        if not self.tracking:
            return self._publish()
        if not quiet:
            self._mark_busy("checking")
        try:
            links = reach.links_for(self.cfg, **self._seams)
            links = await asyncio.to_thread(reach.check, links)
            back = self._set(links)
        finally:
            if not quiet:
                self._mark_busy("")
        snap = self._publish()
        if back:
            await self._recovered(back)
            snap = self._publish()
        if self._needs_watch():
            self._ensure_watch()
        return snap

    async def start(self) -> dict:
        """Start every local server that is down and that we know how to run,
        wait for each, and say where that left her. A server that is not ours
        to start is re-asked, so the room's button is also "check again"."""
        if not self.tracking:
            return self._publish()
        self._mark_busy("starting")
        try:
            links = reach.links_for(self.cfg, **self._seams)
            links = await asyncio.to_thread(reach.check, links)
            for i, link in enumerate(links):
                if link.state == DOWN and link.startable:
                    with self._lock:
                        self._links = [replace(li, state=STARTING,
                                               detail=f"starting {li.label}…")
                                       if li.endpoint == link.endpoint
                                       and li.provider == link.provider else li
                                       for li in links]
                    self._publish()
                    links[i] = await asyncio.to_thread(reach.start, link,
                                                       wait_s=START_WAIT_S)
            back = self._set(links)
        finally:
            self._mark_busy("")
        snap = self._publish()
        if back:
            await self._recovered(back)
            snap = self._publish()
        if self._needs_watch():
            self._ensure_watch()
        return snap

    def _mark_busy(self, what: str) -> None:
        with self._lock:
            self._busy = what
        self._publish()

    async def _recovered(self, links: list[Link]) -> None:
        log.info("model server back: %s", ", ".join(li.detail for li in links))
        if self.on_recovered is None:
            return
        try:
            await self.on_recovered(links)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("could not move her back onto the recovered server")

    # ---- what her turns say --------------------------------------------------

    def turn_failed(self, exc: BaseException | None) -> None:
        """A turn through her model raised. If it was the model's doing, keep
        the reason, and ask the servers whether that is why. Callable from the
        loop only; never raises."""
        cause = _model_error(exc)
        if cause is None or not self.tracking:
            return
        text = str(cause).strip().splitlines()[0] if str(cause).strip() else type(cause).__name__
        with self._lock:
            self._failure = f"her last reply failed: {text[:200]}"
        if self._loop is not None:
            task = self._loop.create_task(self.recheck(quiet=True),
                                          name="connection-recheck")
            self._asks.add(task)                 # held, or it may be collected
            task.add_done_callback(self._asks.discard)
        else:
            self._publish()

    def turn_ok(self) -> None:
        with self._lock:
            had = bool(self._failure)
            self._failure = ""
        if had:
            self._publish()

    async def stop(self) -> None:
        task, self._watch = self._watch, None
        pending = [t for t in [task, *self._asks] if t is not None and not t.done()]
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
