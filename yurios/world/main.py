"""YuriOS — the server entrypoint (SPEC §1, §15).

One process, one origin: the Build #1 brain and Build #2 voice loop,
the `EventHub` carrying every host→frontend event, the MCP tool loop, the
timer board, the selfie lab — Build #4's whole body, unchanged — and behind it
the thing Build #4 only pretended to have: **the mind** (`mind/`), an always-on
cognitive tick loop fed by the inbound `SignalBus`, holding the same strings
the idle machine used to. Run:

    python -m yurios.world                 # reads HOST/PORT from .env (§11)

The voice stack still warms off-thread (B2's pattern — her body renders in
seconds, her voice follows), and the async machinery (tool runner, timers, the
mind) starts on FastAPI startup so it lives on the server's event loop.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders

from yurios.desktop.main import build_stt, build_tts, build_vad
from yurios.desktop.voice.fillers import FillerBank

from yurios.mind.loop import MindLoop
from yurios.mind.signals import SignalBus

from .avatar.controller import VrmController
from .boot import BootBoard
from .brain import ToolBrain
from .channels.manager import ChannelManager
from .clock import Clock
from .config import Config
from .hub import EventHub
from .turns import TextTurns
from .selfies import SelfieLab, build_forge
from .tools.guard import Guard
from .tools.timers import TimerBoard

log = logging.getLogger("world.main")
WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"
DIST_DIR = WEB_DIR / "dist"   # Vite build output (cd web && npm run build); served at / (§3)


class Runtime:
    """Everything a connection needs, built once (B2 §2's Runtime, extended)."""

    def __init__(self, cfg: Config, *, brain=None, chat_model=None,
                 utility_model=None, embedder=None, tool_runner=None,
                 clock: Clock | None = None,
                 controller: VrmController | None = None):
        self.cfg = cfg
        self.clock = clock or Clock()
        # the one outbound bus (SPEC §10): chat, drafts, and the puppet channel
        # all fan out here; /api/events drains it. An injected controller (the
        # tests' spy) is re-pointed at the app's hub so it joins the same bus.
        self.hub = EventHub()
        self.controller = controller or VrmController(hub=self.hub)
        self.controller.hub = self.hub
        # the transcript ring (SPEC §2.6 — YuriOS parity): what /api/history
        # backfills and every `message` event appends to. In-memory on purpose;
        # her *memory* is the Vault, this is just the visible chat.
        self.transcript: list[dict] = []
        self.stopping = asyncio.Event()        # ends open SSE streams on shutdown
        # the boot log the UI shows while she wakes (SPEC §6.4). Voice services
        # are declared here and resolved on the warm-up thread; tools/mind on
        # the event loop (start_async); selfies is known now. /api/boot serves it.
        self.boot = BootBoard()
        rates = {"set_timer": cfg.tool_rate_timer,
                 "play_music": cfg.tool_rate_music,
                 "get_weather": cfg.tool_rate_weather}
        if cfg.selfie_backend != "off":        # absent from the allowlist = no hand (§7.3)
            rates["take_selfie"] = cfg.tool_rate_selfie
        self.guard = Guard(rates_per_min=rates,
                           log_dir=cfg.tool_log_dir, clock=self.clock)
        self.timers = TimerBoard(self.clock)
        # her camera (SPEC §7.6): the forge behind the SelfieLab. Built
        # even when tools are faked (tests inject a fake runner but still want
        # the realisation path); "off" leaves her without one.
        self.selfies: SelfieLab | None = None
        self.selfies_status = "off"
        if cfg.selfie_backend != "off":
            forge, self.selfies_status = build_forge(cfg)
            self.selfies = SelfieLab(forge, clock=self.clock,
                                     post=self.post_message,
                                     speak=self.speak_ambient)
        # `brain` is injectable for the same reason as B2's: the route tests run
        # against a FakeBrain (no Vault, no SQLite). The real one is a ToolBrain —
        # the BrainAdapter with the §7 tool loop wrapped around it.
        self.brain = brain or ToolBrain.build(
            cfg, guard=self.guard, timers=self.timers,
            controller=self.controller, selfies=self.selfies,
            chat_model=chat_model,
            utility_model=utility_model, embedder=embedder)
        self._tool_runner = tool_runner        # injected, or built at startup
        self.tools_status = "off"
        # the inbound inbox (SPEC §16): everything that happens to her becomes a
        # typed signal here, and the mind's SENSE drains it. Producers (the
        # voice route, the events route, a landed timer) post facts; the loop
        # decides what they mean.
        self.signals = SignalBus(self.clock, log_dir=cfg.trace_dir)
        self.mind: MindLoop | None = None
        self.mind_status = "disabled"
        # the channel seam (SPEC §10.5): one text-turn runner shared by every
        # non-voice medium, and the manager that runs the in-process channel
        # adapters (Telegram now; WhatsApp / a game-engine NPC API later).
        self.turns = TextTurns(self)
        self.channels = ChannelManager.from_config(cfg)
        self.channels_status = "off"

        # declare the boot services in the order they should read down the panel.
        # voice comes up on the warm-up thread below; tools/mind are resolved in
        # start_async; selfies is settled already, so it lands terminal now.
        self.boot.declare("tts", "voice · speech synthesis")
        self.boot.declare("stt", "voice · speech recognition")
        self.boot.declare("vad", "voice · voice activity")
        if cfg.mask_latency:
            self.boot.declare("fillers", "voice · filler phrases")
        self.boot.declare(
            "tools", "hands · tool server",
            state="skipped" if cfg.tools_backend == "off" else "pending")
        self.boot.declare(
            "selfies", "camera · selfie forge",
            state="skipped" if cfg.selfie_backend == "off" else "ready",
            detail="off" if cfg.selfie_backend == "off" else self.selfies_status)
        self.boot.declare(
            "mind", "mind · autonomy engine",
            state="pending" if cfg.mind_enabled else "skipped")
        self.boot.declare(
            "channels", "channels · outside mediums",
            state="pending" if self.channels.configured else "skipped",
            detail="" if self.channels.configured else "none configured")
        # per-connection ambient injectors (SPEC §15.5): session_id → coroutine fn.
        # The mind speaks *through a live voice connection* so barge-in and the
        # OutEvent stream work exactly as they do for a real turn.
        self._ambient: dict[str, object] = {}
        self._tasks: list[asyncio.Task] = []
        self.loop: asyncio.AbstractEventLoop | None = None   # set at startup

        # B2's voice warm-up, verbatim in shape (see desktop/main.py for why)
        self.tts = self.stt = self.vad = None
        self.tts_name = self.stt_name = self.vad_name = "loading"
        self.filler_bank: FillerBank | None = None
        self.greeted: set[str] = set()
        self.voice_ready = threading.Event()
        threading.Thread(target=self._warm_voice, daemon=True,
                         name="voice-warmup").start()

    def _warm_voice(self) -> None:
        # These local torch models (Kokoro TTS, faster-whisper, silero) load
        # cold on the CPU and can take a minute+ — the LLM runs elsewhere
        # (LMStudio/Ollama), so this thread is the real startup cost. Narrate
        # each stage so a slow boot doesn't look like a hang.
        t0 = time.perf_counter()
        log.info("voice: warming up (loading local models — this is the slow part)…")

        def _stage(key: str, what: str, backend: str, load):
            log.info("voice: loading %s (%s)…", what, backend)
            self.boot.start(key, detail=backend)
            start = time.perf_counter()
            try:
                obj, name = load()
            except Exception as e:                 # a failed stage marks its own
                self.boot.done(key, state="failed", detail=str(e)[:80])
                raise
            log.info("voice: %s ready [%s] in %.1fs", what, name, time.perf_counter() - start)
            self.boot.done(key, detail=name)
            return obj, name

        try:
            self.tts, self.tts_name = _stage("tts", "TTS", self.cfg.tts_backend, lambda: build_tts(self.cfg))
            self.stt, self.stt_name = _stage("stt", "STT", self.cfg.stt_backend, lambda: build_stt(self.cfg))
            self.vad, self.vad_name = _stage("vad", "VAD", self.cfg.vad_backend, lambda: build_vad(self.cfg))
            if self.cfg.mask_latency:
                log.info("voice: priming filler phrases (MASK_LATENCY)…")
                self.boot.start("fillers")
                filler_bank = FillerBank(tts=self.tts)
                try:
                    filler_bank.prime()
                    self.filler_bank = filler_bank
                    log.info("voice: fillers primed")
                    self.boot.done("fillers", detail="primed")
                except Exception:
                    log.exception("filler prime failed; masking disabled this run")
                    self.boot.done("fillers", state="failed", detail="prime failed")
        finally:
            # an earlier stage that raised leaves the later ones un-run; don't
            # let them hang the boot panel — settle any that never resolved.
            for key in self.boot.unresolved(("tts", "stt", "vad", "fillers")):
                self.boot.done(key, state="failed", detail="not reached")
            self.voice_ready.set()
            log.info("voice: ready — she can hear and speak (%.1fs total)",
                     time.perf_counter() - t0)

    # ---- the transcript (SPEC §2.6) ----

    def post_message(self, role: str, text: str, *, image_url: str | None = None,
                     proactive: bool = False, channel: str | None = None) -> dict:
        """Commit one chat entry: append the ring, publish the `message` event.
        `proactive` marks lines she spoke unprompted (greeting, ambient, a
        finished selfie) — the YuriOS flag, same meaning. `channel` names the
        medium a turn arrived through (cli, telegram, …; None = this origin's
        own frontends) so channels can filter their own echoes (SPEC §10.5)."""
        entry: dict = {"id": uuid.uuid4().hex[:8],   # dedup key: a page may see a
                       # message live AND in its /api/history backfill (a race
                       # the client resolves by id, not by guessing)
                       "role": role, "text": text,
                       "ts": datetime.datetime.fromtimestamp(
                           self.clock.now()).isoformat(timespec="seconds")}
        if image_url:
            entry["image_url"] = image_url
        if proactive:
            entry["proactive"] = True
        if channel:
            entry["channel"] = channel
        self.transcript.append(entry)
        del self.transcript[:-200]
        self.hub.publish("message", entry)
        return entry

    # ---- ambient speech seam (SPEC §8.4) ----

    def attach_ambient(self, session_id: str, inject) -> None:
        self._ambient[session_id] = inject

    def detach_ambient(self, session_id: str) -> None:
        self._ambient.pop(session_id, None)

    async def speak_ambient(self, cue: str) -> bool:
        """Offer the cue to each connected voice session; the first one free to
        speak takes it. False = nobody could (no client, or a turn in flight) —
        the caller decides whether that's a drop or a re-queue (§8.3)."""
        for session_id, inject in list(self._ambient.items()):
            try:
                if await inject(cue):
                    return True
            except Exception:
                log.exception("ambient inject failed (session %s)", session_id)
        return False

    # ---- engagement notifications from the voice route (SPEC §15.3) ----

    def turn_started(self) -> None:
        if self.mind:
            self.mind.turn_started()           # the ENGAGED preempt, from any state

    def turn_ended(self) -> None:
        if self.mind:
            self.mind.turn_ended()

    # ---- async lifecycle (runs on the server's event loop) ----

    async def start_async(self) -> None:
        self.loop = asyncio.get_running_loop()
        # the hands (SPEC §7.2): spawn/connect, discover, wire — or degrade.
        # tools_backend=off, a missing `mcp` install, or a dead server all leave
        # her hand-less but talking; /api/health says which happened.
        runner = self._tool_runner
        if runner is None and self.cfg.tools_backend == "mcp":
            from .tools.client import McpToolRunner
            runner = McpToolRunner(env={
                "TIMER_MAX_MINUTES": str(self.cfg.timer_max_minutes),
                "WEATHER_CITY": self.cfg.weather_city,
                "WEATHER_BACKEND": self.cfg.weather_backend,
                # off = the tool isn't even advertised: no hand, not a dead one
                "SELFIE_ENABLED": "0" if self.cfg.selfie_backend == "off" else "1",
            })
        elif runner is None and self.cfg.tools_backend == "fake":
            from .tools.fakes import FakeToolRunner
            runner = FakeToolRunner()
        if runner is not None and not hasattr(self.brain, "set_tools"):
            runner = None                      # injected test brain has no hands
        if runner is not None:
            self.boot.start("tools", detail=self.cfg.tools_backend)
            try:
                specs = await runner.start()
                self.brain.set_tools(runner, specs)
                self._tool_runner = runner
                self.tools_status = ("fake" if type(runner).__name__ == "FakeToolRunner"
                                     else "mcp")
                self.boot.done("tools", detail=f"{self.tools_status} · {len(specs)} tools")
            except Exception as e:
                log.warning("tool backend failed — she has no hands this run: %s", e)
                self.tools_status = f"failed: {e}"
                self._tool_runner = None
                self.boot.done("tools", state="failed", detail=str(e)[:80])
        elif self.cfg.tools_backend != "off":
            # declared pending but no runner (e.g. a test brain) — settle it
            self.boot.done("tools", state="skipped", detail="no hands")

        self.controller.set_rain(self.cfg.rain_intensity)   # the room's weather (§6.2)

        self._tasks.append(asyncio.create_task(self.timers.run(),
                                               name="timer-board"))
        # the mind (SPEC §15): built over the real brain's stores. An injected
        # test brain (no AppState) leaves her mindless but talking — the route
        # suites exercise the wires without the loop.
        if self.cfg.mind_enabled and hasattr(self.brain, "state"):
            try:
                self.mind = MindLoop(self.cfg, self.clock, bus=self.signals,
                                     brain=self.brain, controller=self.controller,
                                     timers=self.timers, hub=self.hub,
                                     speak=self.speak_ambient,
                                     post_message=self.post_message)
                self.mind_status = "running"
                self.boot.done("mind", detail=f"running · {self.mind.activity.state}")
                self._tasks.append(asyncio.create_task(self.mind.run(),
                                                       name="mind"))
            except Exception as e:  # noqa: BLE001 — she talks even mindless
                log.exception("mind failed to start")
                self.mind_status = f"failed: {e}"
                self.boot.done("mind", state="failed", detail=str(e)[:80])
        elif self.cfg.mind_enabled:
            self.boot.done("mind", state="skipped", detail="no brain state")

        # the channels (SPEC §10.5): each adapter polls its medium and renders
        # the hub; a failed channel leaves her reachable everywhere else.
        if self.channels.configured:
            self.boot.start("channels")
            detail, ok = await self.channels.start_all(self)
            self.channels_status = detail
            if ok:
                self.boot.done("channels", detail=detail)
            else:
                self.boot.done("channels", state="failed", detail=detail)

    async def stop_async(self) -> None:
        self.stopping.set()                    # open SSE streams end themselves
        await self.channels.stop_all()
        if self.selfies is not None:
            await self.selfies.close()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._tool_runner is not None:
            try:
                await self._tool_runner.close()
            except Exception:
                log.exception("tool runner close failed")


# uvicorn waits this long for open connections to drain on Ctrl+C before it
# cancels them (SPEC §10). The one long-lived connection is the /api/events SSE
# stream; it watches server.should_exit and bows out within ~1 s, so this cap is
# only the safety net for a wedged client. Without any cap uvicorn's default is
# an *unbounded* wait — which is the Ctrl+C hang.
SHUTDOWN_GRACE_SECONDS = 5


class _ModelsNoCache:
    """Pure-ASGI header shim: mark /models/ responses no-cache (revalidate every
    load) without buffering the body the way BaseHTTPMiddleware would. See the
    note at its registration in create_app for why this isn't @app.middleware."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith("/models/"):
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["Cache-Control"] = "no-cache"
            await send(message)

        await self.app(scope, receive, send_wrapper)


def build_server(app: FastAPI, cfg: Config):
    """The one configured uvicorn server, shared by every launch path (`python
    -m yurios.world`, the desktop window, the demo). Sets the graceful-shutdown cap and
    stashes the server on app.state so routes/events.py can watch should_exit —
    together these make a single Ctrl+C exit cleanly instead of hanging (§10)."""
    import uvicorn

    class _Server(uvicorn.Server):
        # uvicorn binds SIGINT/SIGTERM to handle_exit; the "Shutting down" INFO
        # log is muted at log_level="warning", so without this Ctrl+C looks like
        # a hang for the ~1 s the graceful stop takes. Print a notice on the
        # first press (not the second force-quit), then defer to uvicorn.
        def handle_exit(self, sig, frame):
            if not self.should_exit:
                print("\n  shutting down… (Ctrl+C again to force)", flush=True)
            super().handle_exit(sig, frame)

    server = _Server(uvicorn.Config(
        app, host=cfg.host, port=cfg.port, log_level="warning",
        timeout_graceful_shutdown=SHUTDOWN_GRACE_SECONDS))
    app.state.server = server
    return server


def create_app(cfg: Config | None = None, *, brain=None, chat_model=None,
               utility_model=None, embedder=None, tool_runner=None,
               clock: Clock | None = None,
               controller: VrmController | None = None) -> FastAPI:
    cfg = cfg or Config()
    rt = Runtime(cfg, brain=brain, chat_model=chat_model,
                 utility_model=utility_model, embedder=embedder,
                 tool_runner=tool_runner, clock=clock, controller=controller)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await rt.start_async()
        yield
        await rt.stop_async()

    app = FastAPI(title="YuriOS", docs_url=None, redoc_url=None,
                  lifespan=lifespan)
    app.state.rt = rt

    # StaticFiles sends ETag/Last-Modified but no Cache-Control, so browsers —
    # and the desktop window's persistent cache (§6.5) — apply heuristic
    # freshness and can keep serving a stale body after web/models/ changes.
    # no-cache = still cached, but revalidated every load: a 304 normally, the
    # new bytes the moment the file on disk differs.
    #
    # A pure-ASGI shim, deliberately NOT @app.middleware("http"): that decorator
    # is a BaseHTTPMiddleware, which re-streams *every* response — including the
    # long-lived /api/events SSE body (routes/events.py) — through an internal
    # memory stream, and on shutdown-cancel that surfaces as a noisy
    # "Exception in ASGI application". Rewriting one header on http.response.start
    # leaves streaming bodies untouched.
    app.add_middleware(_ModelsNoCache)

    from yurios.desktop.routes import settings as b2_settings

    from .routes import chat, events, health, live2d, mind, voice_ws
    app.include_router(health.router)
    app.include_router(events.router)
    app.include_router(voice_ws.router)
    # the text-turn seam over HTTP (SPEC §10.5): what the CLI chat — and any
    # future remote frontend — drives instead of the voice socket.
    app.include_router(chat.router)
    # the inner-life surface (SPEC §24.3): journal, goals, pending self-edits,
    # the tick trace — what converts autonomy from creepy to an inner life.
    app.include_router(mind.router)
    # The second body (SPEC §6.6): Build #2's Live2D client, under
    # web/live2d/ and served as-is — it speaks the same B2 §10 /ws/voice wire
    # the forked route preserves bit-for-bit, so the previous build's whole
    # body plugs in as just another client. Its two API needs come from the
    # settings router (called, not copied — it edits THIS build's
    # .env) and the re-aimed rig registry in routes/live2d.py.
    app.include_router(b2_settings.router)
    app.include_router(live2d.router)
    app.mount("/live2d", StaticFiles(directory=WEB_DIR / "live2d", html=True),
              name="live2d")
    # Her body + animations are large binaries kept out of the Vite bundle
    # (web/vite.config.js publicDir:false); serve them straight from web/models.
    # The _ModelsNoCache shim above keeps /models/ revalidated.
    app.mount("/models", StaticFiles(directory=WEB_DIR / "models", html=True),
              name="models")
    # The sanctuary app itself is the Vite build (web/dist, → §3). check_dir=False
    # so a fresh checkout that hasn't run `npm run build` still boots — / just
    # 404s until then, and the warning tells them what to run — instead of raising
    # at mount time and taking the whole server (and the test suite) down with it.
    if not (DIST_DIR / "index.html").exists():
        log.warning("frontend not built — run `cd web && npm install && npm run build`; "
                    "serving %s (/ will 404 until then)", DIST_DIR)
    app.mount("/", StaticFiles(directory=DIST_DIR, html=True, check_dir=False),
              name="web")
    return app


def app() -> FastAPI:
    """uvicorn factory: `uvicorn world.main:app --factory`."""
    return create_app()
