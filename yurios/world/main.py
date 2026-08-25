"""YuriOS — the server entrypoint (SPEC §1, §15).

One process, one origin: the Build #1 brain and Build #2 voice loop,
the `EventHub` carrying every host→frontend event, the MCP tool loop, the
timer board, the selfie lab — Build #4's whole body, unchanged — and behind it
the thing Build #4 only pretended to have: **the mind** (`mind/`), an always-on
cognitive tick loop fed by the inbound `SignalBus`, holding the same strings
the idle machine used to. Run:

    python -m yurios.world                 # reads HOST/PORT from .env (§11)

The voice stack loads off-thread (B2's pattern — her body renders in seconds,
her voice follows), but only for as long as somebody is in one of her rooms
(`voicestack.py`, SPEC §9.9): on a node hosting a registry of characters,
warming every autostarted one's Kokoro/whisper/silero at boot cost gigabytes
nobody was listening to. The async machinery (tool runner, timers, the mind)
starts on FastAPI startup so it lives on the server's event loop.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Awaitable, Callable

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders

from yurios.desktop.voice.fillers import FillerBank
from yurios.desktop.voice.ws_limits import VoiceConnectionLimiter, uvicorn_ws_options

from yurios.mind.loop import MindLoop
from yurios.mind.promptlog import PromptLog
from yurios.mind.signals import SignalBus
from yurios.mind.workspace import DESK_TOOLS, SKILL_TOOLS
from yurios.models import is_configured

from .avatar.controller import VrmController
from .boot import BootBoard
from .brain import ToolBrain
from .brain_protocol import AutonomousBrain
from .channels.manager import ChannelManager
from yurios.app.conversation import ConversationLog
from ..kernel.clock import Clock
from .config import Config
from .context import ContextMeter, short_tokens
from ..kernel.hub import EventHub
from .inbox import Inbox
from .turns import TextTurns
from .research import Researcher
from .selfies import SelfieLab, build_forge
from .uploads import Uploads
from .situation import render_visual_situation
from .tools.client import MultiToolRunner, load_servers
from .tools.fetch import build_fetcher
from .tools.guard import Guard
from .tools.search import build_provider
from .tools.timers import TimerBoard
from .voicestack import VoiceStack

log = logging.getLogger("world.main")
WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"
DIST_DIR = WEB_DIR / "dist"   # Vite build output (cd web && npm run build); served at / (§3)
#: How much of the conversation the in-memory ring holds (SPEC §2.6).
RING_SIZE = 200
#: What one `GET /api/history` hands a page that asks for no particular window.
HISTORY_PAGE = 100
#: The most one call may ask for — a client cannot turn this route into a dump.
HISTORY_MAX = 200


class Runtime:
    """Everything a connection needs, built once (B2 §2's Runtime, extended)."""

    def __init__(self, cfg: Config, *, brain=None, chat_model=None,
                 utility_model=None, embedder=None, tool_runner=None,
                 clock: Clock | None = None,
                 controller: VrmController | None = None):
        self.cfg = cfg
        self.voice_ws_limiter = VoiceConnectionLimiter(cfg.voice_ws_max_connections)
        # Injected models/brains are test and embedding seams with a working model
        # behind them; only a normal fresh runtime is intentionally unconfigured.
        self.model_configured = bool(
            chat_model is not None or brain is not None
            or is_configured(cfg.chat_model))
        self.clock = clock or Clock()
        # the one outbound bus (SPEC §10): chat, drafts, and the puppet channel
        # all fan out here; /api/events drains it. An injected controller (the
        # tests' spy) is re-pointed at the app's hub so it joins the same bus.
        self.hub = EventHub()
        self.controller = controller or VrmController(hub=self.hub)
        self.controller.hub = self.hub
        # the transcript ring (SPEC §2.6 — YuriOS parity): what /api/history
        # backfills and every `message` event appends to. Still just the visible
        # chat — her *memory* is the Vault, and nothing here is ever read back
        # into a prompt — but no longer only in memory: `app/conversation.py`
        # keeps the same entries on disk, so the ring wakes up holding the end
        # of the last conversation instead of opening her room onto a blank
        # column. The ring stays the live copy and the archive stays the deep
        # one; the log is what /api/history pages back through, a screenful on
        # load and a handful at a time behind the button at the top.
        #
        # One file, two readers: the same log is where the §7.1 window comes
        # from (`app/sessions.py`). A line is one row carrying both columns —
        # what the page drew and what the model produced — because they are one
        # line, and keeping two files in step was a bug waiting to happen.
        self.chatlog = ConversationLog(cfg.vault_dir)
        self.transcript: list[dict] = self.chatlog.tail(RING_SIZE)
        # how full her context window is (SPEC §11): the masthead readout, fed by
        # the chat provider on every model pass and published as a sticky
        # `context` event. CONTEXT_LENGTH names the initial ceiling; a direct GGUF
        # provider or LM Studio probe replaces it with the window actually in use.
        self.context = ContextMeter(self.hub, limit=cfg.context_length,
                                    reserve=cfg.max_reply_tokens,
                                    trace_dir=cfg.trace_dir,
                                    max_trace_bytes=cfg.mind_trace_max_bytes)
        self.stopping = asyncio.Event()        # ends open SSE streams on shutdown
        # the boot log the UI shows while she wakes (SPEC §6.4). Voice services
        # are declared here and resolved on the warm-up thread; tools/mind on
        # the event loop (start_async); selfies is known now. /api/boot serves it.
        self.boot = BootBoard(who=cfg.character_id)
        rates = {"set_timer": cfg.tool_rate_timer,
                 "play_music": cfg.tool_rate_music}
        if cfg.selfie_backend != "off":        # absent from the allowlist = no hand (§7.3)
            rates["take_selfie"] = cfg.tool_rate_selfie
            rates["show_picture"] = cfg.tool_rate_picture
        if cfg.search_backend != "off":        # …and the same rule for the web (§7.7)
            rates["web_search"] = cfg.tool_rate_search
            rates["read_page"] = cfg.tool_rate_read
            rates["research"] = cfg.tool_rate_research
        # her desk (§34.2), the same allowlist rule once more. The rate is
        # generous because these are local file writes with no cost and no
        # outside party — the bucket is here to catch a loop, not to ration.
        if cfg.workspace_enabled:
            for tool in DESK_TOOLS:
                rates[tool] = cfg.tool_rate_desk
        if cfg.skills_enabled:
            for tool in SKILL_TOOLS:
                rates[tool] = cfg.tool_rate_desk
        # the self-edit door (§23). Advertised only where the mind runs, because
        # the queue it writes into is only read there — and rationed hard: this
        # is the one hand that reaches at who she is, and a proposal a minute
        # is not deliberation, it is a loop with a git history.
        if cfg.mind_enabled:
            rates["propose_edit"] = cfg.tool_rate_selfedit
        self.guard = Guard(rates_per_min=rates,
                           log_dir=cfg.tool_log_dir, clock=self.clock,
                           max_bytes=cfg.tool_log_max_bytes)
        self.timers = TimerBoard(self.clock)
        # Pictures *in* (SPEC §35): the shelf a photo you send her lands on,
        # and whether the model she is speaking through can be sent one at all.
        # The store is built unconditionally and creates nothing until the first
        # save; `image_input` is settled on the event loop (start_async), because
        # answering it means asking her provider and a constructor may not wait
        # on a network. Until then the composer sees `false` — a paperclip that
        # appears a second late is better than one that appears and errors.
        self.uploads = Uploads(cfg.upload_dir, max_px=cfg.chat_image_max_px,
                               keep=cfg.upload_keep)
        self.image_input = False
        self.image_input_status = "not probed yet"
        # her camera (SPEC §7.6): the forge behind the SelfieLab. Built
        # even when tools are faked (tests inject a fake runner but still want
        # the realisation path); "off" leaves her without one.
        self.selfies: SelfieLab | None = None
        self.selfies_status = "off"
        # The park window's door (§7.6, world/vram.py): shut while a render has
        # borrowed her brain's VRAM, so a turn arriving mid-render queues here
        # instead of loading the chat model back onto the card the render is
        # still filling. Built unconditionally — every turn waits on it, and
        # with no camera it simply never closes.
        #
        # Process-wide, not one per runtime. A host runs every character in
        # this process and they share one GPU and one LM Studio server, so a
        # door that only her own turns wait at is not a door: it was Yuri's
        # dream call that loaded the model back onto the card Adia's render was
        # holding, and both parks logged success while doing it.
        from .vram import shared_gate
        self.park_gate = shared_gate()
        if cfg.selfie_backend != "off":
            forge, self.selfies_status = build_forge(cfg)
            from .vram import LLMParker
            # The floor comes off the backend that was actually built, not off
            # the config: SELFIE_BACKEND=diffusers resolves to either local
            # camera depending on the checkpoint, and a degrade-to-mock says
            # None — nothing resident, nothing to park for.
            self.selfies = SelfieLab(forge, clock=self.clock,
                                     post=self.post_message,
                                     speak=self.speak_ambient,
                                     notify=self.hub.publish,
                                     parker=LLMParker(
                                         cfg,
                                         resident_free_gib=forge.backend.RESIDENT_FREE_GIB,
                                         gate=self.park_gate),
                                     quiet=self.wait_turns_idle,
                                     situation=self.visual_situation,
                                     signal=self.post_signal)
        # Her reading desk (SPEC §7.7): the same start-don't-await shape as the
        # camera, and the place a fetched page turns into a shelved document.
        # The knowledge store is passed as a GETTER because it belongs to the
        # MindLoop, which is built later (start_async) and not at all when she
        # is mindless — see world/research.py.
        self.research: Researcher | None = None
        self.research_status = "off"
        #: server name -> calls/minute for the tools it turns out to offer,
        #: filled from mcp-servers.json and spent at discovery (start_async).
        self._external_rates: dict[str, int] = {}
        if cfg.search_backend != "off":
            search = build_provider(cfg.search_backend, base_url=cfg.searxng_url,
                                    language=cfg.search_language,
                                    safesearch=cfg.search_safesearch)
            fetcher = build_fetcher(
                "fake" if cfg.search_backend == "fake" else "http",
                timeout=cfg.fetch_timeout_s, max_bytes=cfg.fetch_max_bytes)
            self.research = Researcher(
                search, fetcher, clock=self.clock,
                post=self.post_message, speak=self.speak_ambient,
                knowledge=lambda: self.mind.knowledge if self.mind else None,
                notify=self.hub.publish, signal=self.post_signal)
            self.research_status = cfg.search_backend
        # Whether the providers behind her voice are ours to rebuild (SPEC §31.4).
        # An injected brain or an injected model belongs to the caller — a live
        # model swap moves the Config knobs and leaves the object alone.
        self._owns_models = brain is None and chat_model is None
        # `brain` is injectable for the same reason as B2's: the route tests run
        # against a FakeBrain (no Vault, no SQLite). The real one is a ToolBrain —
        # the BrainAdapter with the §7 tool loop wrapped around it. Building it
        # loads the embedding model that indexes her memory (SPEC §3) — with the
        # sentence-transformers default that's a cold torch model on the CPU, as
        # slow to wake as the voice stack — so surface it in the boot panel first
        # (it loads here, before the voice warm-up thread even starts). A
        # server-backed embedder (ollama / lm_studio) has no local weights to
        # load; it still gets a line so the panel names what indexes her.
        if brain is not None:
            self.brain = brain                 # injected (tests): no embedder loads
        else:
            self._pin_lmstudio_models(cfg, chat_model, utility_model, embedder)
            if not self.model_configured:
                self.boot.declare("models", "mind · language model", state="skipped",
                                  detail="choose a model to connect")
            self.boot.declare("embed", "memory · embedding model")
            if embedder is None:
                from yurios.app.main import _default_embedder
                self.boot.start("embed", detail=cfg.embed_model)
                try:
                    embedder = _default_embedder(cfg)
                except Exception as e:
                    self.boot.done("embed", state="failed", detail=str(e)[:80])
                    raise
                self.boot.done("embed",
                               detail=f"{cfg.embed_model} · {cfg.embed_dim}d")
            else:
                self.boot.done("embed", detail="injected")
            self.brain = ToolBrain.build(
                cfg, guard=self.guard, timers=self.timers,
                controller=self.controller, selfies=self.selfies,
                research=self.research, chat_model=chat_model,
                utility_model=utility_model, embedder=embedder)
        #: Whether this brain can carry a mind, asked once instead of one
        #: attribute at a time (world/brain_protocol.py). A `ToolBrain` can; the
        #: conversational fake a route test injects cannot, and everything the
        #: mind would have wired is skipped rather than half-wired.
        self.autonomous = isinstance(self.brain, AutonomousBrain)
        # the meter reads the prompt where every path funnels through: the chat
        # provider itself (reply, greeting, ambient, each tool-loop pass)
        self._wire_context_meter()
        # …and the prompt log records what was actually in it (SPEC §24.2). Wired
        # here rather than from the mind, because greetings and chat turns happen
        # whether or not the loop is running, and they are half the record.
        if self.autonomous:
            self.brain.set_prompt_log(PromptLog.from_config(cfg, self.clock))
        self._tool_runner = tool_runner        # injected, or built at startup
        self.tools_status = "off"
        self.tool_count = 0
        # the inbound inbox (SPEC §16): everything that happens to her becomes a
        # typed signal here, and the mind's SENSE drains it. Producers (the
        # voice route, the events route, a landed timer) post facts; the loop
        # decides what they mean.
        self.signals = SignalBus(self.clock, log_dir=cfg.trace_dir,
                                 max_bytes=cfg.mind_signal_max_bytes)
        self.mind: MindLoop | None = None
        self.mind_status = "disabled"
        # Her half of the two switches behind her hands (§26.1). Held here
        # because the host seeds it from her record before there is a mind to
        # tell, and because a revoke has to survive the mind being rebuilt.
        self._hands_granted = True
        # the channel seam (SPEC §10.5): one text-turn runner shared by every
        # non-voice medium, and the manager that runs the in-process channel
        # adapters (Telegram now; WhatsApp / a game-engine NPC API later).
        self.turns = TextTurns(self)
        self.channels = ChannelManager.from_config(cfg)
        self.channels_status = "off"
        # …and where her initiative waits when none of those media is listening
        # (world/inbox.py). The transcript ring above is in-memory and shows only
        # to an open page; a reach-out decided at 3am with nobody home has to
        # survive both the empty room and the restart that follows it.
        self.inbox = Inbox(cfg.vault_dir)

        # declare the boot services in the order they should read down the panel.
        # The voice stack declares its own three (plus fillers) as it is built;
        # tools/mind are resolved in start_async; selfies is settled already, so
        # it lands terminal now.
        #
        # Her voice is the heaviest thing this process holds and only `/ws/voice`
        # wants it, so it loads when someone enters one of her rooms and goes when
        # the last of them leaves (SPEC §9.9, world/voicestack.py). On a node with
        # a registry that is the difference between one resident voice stack and
        # one per autostarted character.
        self.voice = VoiceStack(cfg, self.boot)
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
        self._ambient: dict[str, Callable[[str], Awaitable[bool]]] = {}
        # in-flight turn count + its idle gate (§7.6): the selfie lab's parker
        # must never unload her LLM while a turn streams from it.
        self._turns_in_flight = 0
        self.turns_idle = asyncio.Event()
        self.turns_idle.set()
        self._tasks: list[asyncio.Task] = []
        self._mind_task: asyncio.Task | None = None
        self.loop: asyncio.AbstractEventLoop | None = None   # set at startup

        # Sessions already greeted this run. She speaks first on arrival (§9.8),
        # but a *reconnect* is not a new arrival — and during a voice warm several
        # connections release together, so without this they would all greet.
        self.greeted: set[str] = set()

    # The voice stack used to be four attributes on the Runtime and everything
    # downstream reads them by name (the route, /api/health, the desktop route's
    # shape). It moved behind `self.voice` when it became load-on-demand; these
    # keep every reader working and reading the *current* stack rather than a
    # stale reference to weights that have since been freed.

    @property
    def tts(self):
        return self.voice.tts

    @property
    def stt(self):
        return self.voice.stt

    @property
    def vad(self):
        return self.voice.vad

    @property
    def filler_bank(self) -> FillerBank | None:
        return self.voice.filler_bank

    @property
    def tts_name(self) -> str:
        return self.voice.tts_name

    @property
    def stt_name(self) -> str:
        return self.voice.stt_name

    @property
    def vad_name(self) -> str:
        return self.voice.vad_name

    @property
    def voice_ready(self) -> threading.Event:
        return self.voice.ready

    def _wire_context_meter(self) -> None:
        """Point the chat provider at the meter, if this brain has a real one.

        One attachment covers every path she speaks through — reply, greeting,
        ambient self-talk, each pass of the tool loop — because they all end at
        the same `state.chat.stream()`. An injected test brain (no AppState) or a
        fake model (no `meter` attribute) simply leaves the gauge at zero."""
        chat = getattr(getattr(self.brain, "state", None), "chat", None)
        if chat is not None and hasattr(chat, "meter"):
            chat.meter = self.context
            direct_limit = getattr(chat, "context_limit", 0)
            if direct_limit:
                self.context.set_limit(direct_limit, "direct gguf")

    def _probe_context_window(self, cfg) -> str:
        """Ask LM Studio how big the window her chat model is loaded with is.

        Worth a call even when CONTEXT_LENGTH said: what .env asked for and what
        the server seated are not the same claim — a model somebody loaded by
        hand keeps the window they gave it, and one too big for the card comes
        back smaller. Only an lm_studio/… route has a local server to ask. Returns a short label for the boot panel, "" when nothing was
        learned."""
        if not cfg.chat_model.startswith("lm_studio/"):
            return f"{short_tokens(cfg.context_length)} ctx" if cfg.context_length else ""
        from yurios.app.providers.lmstudio import probe_context
        found = probe_context(cfg.lmstudio_base_url,
                              cfg.chat_model.split("/", 1)[1])
        # A window smaller than CONTEXT_LENGTH is news, not a fault: someone
        # loaded that model in LM Studio themselves and we run in what they
        # loaded (providers/lmstudio.ensure_resident — reloading 16 GB of
        # weights to widen a window is not ours to decide). The pinning path
        # says so already and knows whose load it was, so this only speaks for
        # the case nobody covered: preload off, LM Studio JIT-loading in
        # whatever its own config says. Once, at info — this runs per character
        # and on every model swap, and a warning that fires four times a boot
        # for a working setup teaches people to read past warnings.
        if not cfg.lmstudio_preload and found["loaded"] and cfg.context_length \
                and found["loaded"] != cfg.context_length:
            log.info("LM Studio has %s in a %d-token window, not the "
                     "CONTEXT_LENGTH=%d in .env — the gauge shows what she "
                     "actually has; LM Studio's own load config is what sets "
                     "it while LMSTUDIO_PRELOAD is off",
                     cfg.chat_model, found["loaded"], cfg.context_length)
        if not found["loaded"]:
            if cfg.context_length:                # asked for one; trust it
                return f"{short_tokens(cfg.context_length)} ctx"
            # Only the window she is actually LOADED in can go on the gauge. The
            # model's maximum is a different number entirely — often 30× bigger —
            # and showing it would promise room that isn't there. Say so instead.
            if found["max"]:
                log.info("LM Studio has not said what window %s is loaded in; it "
                         "can do up to %s tokens — set CONTEXT_LENGTH in .env to "
                         "pick one and put it on the gauge",
                         cfg.chat_model, found["max"])
            return ""
        self.context.set_limit(found["loaded"], "lm studio")
        return f"{short_tokens(found['loaded'])} ctx"

    def _pin_lmstudio_models(self, cfg, chat_model, utility_model, embedder) -> None:
        """Load her LM Studio models and pin them there, before the first turn.

        The brain does this too (app.main._preload_lmstudio) — it has to, since
        Build #1 and the desktop boot without this panel. Doing it here as well
        buys the panel: a cold 6 GB model off disk is a minute of silence, and the
        enter gate should say which model it is waiting for rather than hang. The
        second call is a no-op by then (already resident costs one GET).

        Why pin at all: chat and embeddings share one LM Studio server, whose JIT
        loader evicts the previously JIT-loaded model to serve the next request —
        so every turn used to unload one to load the other (see
        providers/lmstudio.ensure_resident)."""
        from yurios.app.main import _lmstudio_ids, _preload_lmstudio

        chat = chat_model is None or utility_model is None
        if not cfg.lmstudio_preload:
            if chat:
                self._probe_context_window(cfg)   # the readout still wants a ceiling
            return
        ids = _lmstudio_ids(cfg, chat=chat, embed=embedder is None)
        if not ids:
            return
        self.boot.declare("models", "mind · language models")
        self.boot.start("models", detail=f"{len(ids)} on LM Studio")
        pinned = _preload_lmstudio(cfg, chat=chat, embed=embedder is None)
        # the window they were loaded with — the number the masthead gauge and
        # every "context size exceeded" both hang off (SPEC §11)
        window = self._probe_context_window(cfg) if chat else ""
        if pinned:
            detail = f"{len(pinned)} resident · no idle unload"
            self.boot.done("models", detail=f"{detail} · {window}" if window else detail)
        else:
            # she still runs: LM Studio JIT-loads per request, slowly (§3.1)
            self.boot.done("models", state="failed", detail="none pinned — see the log")

    # ---- pictures you send her (SPEC §35) ----

    async def save_upload(self, data: bytes):
        """Put one sent picture on the shelf, off the event loop.

        Decoding, orienting and re-encoding a phone photo is real CPU work, and
        this loop is also carrying a token stream and the SSE fan-out — the same
        reason TTS synthesis runs in a thread (desktop/voice/turn.py)."""
        return await asyncio.to_thread(self.uploads.save, data)

    # ---- the inbound inbox (SPEC §16) ----

    def post_signal(self, type_: str, payload: dict | None = None,
                    source: str = "host"):
        """Put one fact on the bus for SENSE to find.

        A bound method rather than `self.signals.post` handed round directly,
        because the two off-turn workers (the camera, the reading desk) are
        built before the bus is and would capture a name that doesn't exist
        yet. It also keeps the rule that producers post facts and never call
        into the mind: this is the whole of the seam they get.
        """
        return self.signals.post(type_, payload, source=source)

    # ---- the transcript (SPEC §2.6) ----

    def post_message(self, role: str, text: str, *, image_url: str | None = None,
                     proactive: bool = False, channel: str | None = None,
                     client_id: str | None = None,
                     selfie_id: str | None = None,
                     report_path: str | None = None,
                     report_title: str | None = None,
                     report_job: str | None = None,
                     unheard: bool = False,
                     session_id: str | None = None) -> dict:
        """Commit one chat entry: append the ring, publish the `message` event.
        `proactive` marks lines she spoke unprompted (greeting, ambient, a
        finished selfie) — the YuriOS flag, same meaning. `channel` names the
        medium a turn arrived through (cli, telegram, …; None = this origin's
        own frontends) so channels can filter their own echoes (SPEC §10.5).

        `unheard` files the entry in her inbox as well (world/inbox.py): she said
        this on her own initiative and may have said it to an empty room, so it
        waits on disk until somebody has actually been in to see it. It is set
        by the caller rather than inferred here, because only the caller knows:
        `hub.subscribers` counts channel adapters too, so "no subscribers" stops
        meaning "nobody is home" the moment Telegram is configured. The mind's
        SUGGEST line and its undeliverable SPEAK say so explicitly (§18.3); a
        greeting never does, because a greeting is answered *to* somebody who
        just arrived.

        `report_path`/`report_title`/`report_job` name a document on her desk
        that this line is *about* — a night's report a DREAM job was told to
        deliver (§18.2a). The line is the whole message; the path is what the
        chat view turns into a card you can open, the same way `image_url` is
        what it turns into a picture."""
        # `session_id` is what lets this line and the model's own version of it
        # be one row (app/conversation.py) rather than two files' worth of the
        # same sentence. Only the turn paths pass one: a selfie, a digest or a
        # mind reach-out belongs on the page and in no prompt's window (§9.9).
        drawn = self.chatlog.undrawn(session_id, role) if session_id else None
        entry: dict = {"id": (drawn or {}).get("id") or uuid.uuid4().hex[:8],
                       # dedup key: a page may see a message live AND in its
                       # /api/history backfill (a race the client resolves by
                       # id, not by guessing)
                       "role": role, "text": text,
                       "ts": datetime.datetime.fromtimestamp(
                           self.clock.now()).isoformat(timespec="seconds")}
        if image_url:
            entry["image_url"] = image_url
        if proactive:
            entry["proactive"] = True
        if channel:
            entry["channel"] = channel
        if client_id:
            entry["client_id"] = client_id
        if selfie_id:
            entry["selfie_id"] = selfie_id
        if report_path:
            entry["report_path"] = report_path
            entry["report_title"] = report_title or report_path
            entry["report_job"] = report_job or ""
        if unheard:
            # on the wire as well as on disk: the notification channel filters on
            # it, and an open chat view uses it to clear the badge for a line it
            # just rendered — being in the room is what "seen" means (§32.5).
            entry["unheard"] = True
        if session_id:
            entry["session_id"] = session_id
        self.transcript.append(entry)
        del self.transcript[:-RING_SIZE]
        # …and the same entry to disk, so the column survives the next restart
        # (app/conversation.py). Verbatim and unconditional: a line that is only
        # in the ring is a line the next boot draws as nothing. `drawn` means the
        # window already has this line — the greeting, whose text the brain
        # appends before this posts it — so the page's half patches that row
        # instead of adding a second copy of the same sentence.
        if drawn is not None:
            self.chatlog.attach_drawn(drawn["id"], entry)
        else:
            self.chatlog.add(entry)
        if unheard:
            # before the publish: the notification channel and an open page both
            # react to the event, and both want a badge that already exists.
            self.inbox.add(entry)
        self.hub.publish("message", entry)
        return entry

    def history(self, *, limit: int = HISTORY_PAGE,
                before: str | None = None) -> dict:
        """One window of the visible conversation, oldest first (SPEC §2.6).

        What `GET /api/history` answers. Two shapes, one method: with no
        `before` it is the *end* of the conversation — what a page opening
        backfills, and after a restart that is the disk copy rather than an
        empty ring. With `before` it is the entries immediately **older** than
        that message id, which is what the button at the top of the column asks
        for, six at a time.

        `has_more` says whether the walk can continue, so the client can retire
        the button when the archive runs out instead of offering a press that
        returns nothing. An unknown `before` — an id compacted off the end of
        the log — is the same answer as reaching the floor: nothing older, and
        no more to ask for.
        """
        entries = self._visible()
        if before:
            cut = next((i for i, e in enumerate(entries)
                        if e.get("id") == before), None)
            if cut is None:
                return {"messages": [], "has_more": False}
            entries = entries[:cut]
        limit = max(1, min(int(limit), HISTORY_MAX))
        window = entries[-limit:]
        return {"messages": window, "has_more": len(window) < len(entries)}

    def _visible(self) -> list[dict]:
        """The whole conversation a page may draw, oldest first: the archive,
        then whatever the ring holds that never reached it.

        The two are normally the archive being a superset of the ring, and this
        merges them by id anyway, for the case where they are not — a Vault on
        a read-only mount, or no Vault at all (the bare-runtime tests). The log
        is best-effort by design (app/conversation.py); the ring is not, and a
        failed write must not take a line off the screen it is already on.
        """
        entries = self.chatlog.entries()
        if not entries:
            return list(self.transcript)
        known = {e.get("id") for e in entries}
        # Both are oldest-first, and anything the ring holds that the archive
        # does not is newer than all of it — the log only ever fails forward.
        return entries + [e for e in self.transcript if e.get("id") not in known]

    def spoken_line(self, message_id: str) -> str | None:
        """The words of one line **she** said, by transcript id (SPEC §9.11).

        What the replay button resolves to. The voice socket carries the id and
        never the words, and this is why: the only thing that can come back out
        of it is something she already said, in this room, to this person. A
        wire that took text would be a text-to-speech endpoint wearing her
        voice, which is a different feature and not the one anybody asked for.

        Three places to look, because a page shows lines from all of them. The
        ring is memory and holds the last couple of hundred; the inbox is the
        disk copy of what she said into an empty room (§18.4), and after a
        restart it is the only copy still on screen. The log is the deep one,
        and it has to be here: the walk back at the top of the column (§2.6)
        pages through it, so without it every line older than the ring came back
        with a speaker button that answered "that line has fallen out of the
        transcript" — a control that is drawn precisely where it cannot work.
        Newest first in each — an id is unique, so the order is only about
        finding it sooner. Her lines only: `role` is the ring's field and inbox
        rows have no role at all, since everything in that file is hers.
        """
        for entries in (self.transcript, self.inbox.entries(),
                        self.chatlog.entries()):
            for entry in reversed(entries):
                if entry.get("id") != message_id:
                    continue
                if entry.get("role", "assistant") == "user":
                    return None
                text = (entry.get("text") or "").strip()
                return text or None
        return None

    # ---- ambient speech seam (B4 §8.4; today's obligations are SPEC §8, §9) ----

    def attach_ambient(self, session_id: str,
                       inject: Callable[[str], Awaitable[bool]]) -> None:
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

    def turn_started(self, proactive: bool = False) -> None:
        self._turns_in_flight += 1
        self.turns_idle.clear()
        if self.mind:
            self.mind.turn_started(proactive=proactive)

    def turn_ended(self) -> None:
        self._turns_in_flight = max(0, self._turns_in_flight - 1)
        if self._turns_in_flight == 0:
            self.turns_idle.set()
        if self.mind:
            self.mind.turn_ended()

    def visual_situation(self) -> str:
        """Where she actually is, as a camera would see it (§7.6). The selfie
        lab's gap-filler: what she doesn't describe comes from here rather than
        from a dice roll, so an unprompted shot at 2am in the rain looks like
        2am in the rain."""
        return render_visual_situation(self.clock, controller=self.controller)

    async def wait_turns_idle(self) -> None:
        """Block until nothing is talking to her brain. The selfie lab's VRAM
        parker is the one caller (§7.6): unloading her LLM while a turn is
        still streaming from it kills that stream mid-reply — the draft
        vanishes from the chat as if the turn were cancelled. A parked render
        always waits for a quiet moment first.

        Two counters, because there are two ways to be mid-sentence with her.
        A turn is one; the mind loop's off-turn utility calls (§15 — dream
        jobs, consolidation, knowledge extraction) are the other, and they run
        at exactly the hour the camera is busiest, since a dreamt selfie is
        started by one of those jobs and the next job starts before it lands.
        Evicting under either one kills it."""
        await self.turns_idle.wait()
        await self.park_gate.wait_idle()

    # ---- async lifecycle (runs on the server's event loop) ----

    async def _probe_image_input(self) -> None:
        """Settle `image_input` from the provider (app/providers/vision.py).

        The status string beside it is the point as much as the flag: "text
        only" with no reason attached is what sends someone to the docs looking
        for a switch that was never the problem. It shows in /api/health and,
        when the answer is no, in the boot panel."""
        from yurios.app.providers.vision import probe
        try:
            self.image_input, self.image_input_status = await probe(self.cfg)
        except Exception as e:                 # noqa: BLE001 — never a boot failure
            log.warning("could not tell whether %s takes images (%s) — assuming "
                        "text only", self.cfg.chat_model, e)
            self.image_input, self.image_input_status = False, f"probe failed: {e}"
        log.info("pictures to her: %s (%s)",
                 "on" if self.image_input else "off", self.image_input_status)
        # Sticky, so a page that opens an hour from now learns it on subscribe
        # and a model swapped live (retune) reaches every open room at once —
        # the §10 rule that cross-surface state is an event, not a poll.
        self.hub.publish("capabilities",
                         {"image_input": self.image_input,
                          "detail": self.image_input_status},
                         sticky="capabilities")

    async def start_async(self) -> None:
        self.loop = asyncio.get_running_loop()
        # the render thread closes/opens the gate from off-loop; tell it where
        self.park_gate.bind(self.loop)
        # Can she be shown a picture? (SPEC §35) Her provider is asked once, at
        # boot, and the answer rides the `hello` event to every room — that is
        # what puts the paperclip in the composer, or leaves it out. Never fatal:
        # a probe that cannot reach the server answers "text only", which is a
        # room without a paperclip, not a room without her.
        await self._probe_image_input()
        # the hands (SPEC §7.2): spawn/connect, discover, wire — or degrade.
        # tools_backend=off, a missing `mcp` install, or a dead server all leave
        # her hand-less but talking; /api/health says which happened.
        runner = self._tool_runner
        if runner is None and self.cfg.tools_backend == "mcp":
            from .tools.client import McpToolRunner
            runner = McpToolRunner(env={
                "TIMER_MAX_MINUTES": str(self.cfg.timer_max_minutes),
                # off = the tool isn't even advertised: no hand, not a dead one
                "SELFIE_ENABLED": "0" if self.cfg.selfie_backend == "off" else "1",
                # the contract side builds its description from the SAME merged
                # book the host renders from (world/selfies.py) — overlay and
                # its tool_hint included — so the two can never disagree
                "SELFIE_TEMPLATES_EXTRA": self.cfg.selfie_templates_extra,
                "SELFIE_TEMPLATES": self.cfg.selfie_templates,
                # the web hands (§7.7) — off means unadvertised, same rule
                "SEARCH_BACKEND": self.cfg.search_backend,
                "SEARXNG_URL": self.cfg.searxng_url,
                "SEARCH_RESULTS": str(self.cfg.search_results),
                "SEARCH_LANGUAGE": self.cfg.search_language,
                "SEARCH_SAFESEARCH": str(self.cfg.search_safesearch),
                "FETCH_TIMEOUT_S": str(self.cfg.fetch_timeout_s),
                "FETCH_MAX_BYTES": str(self.cfg.fetch_max_bytes),
                "RESEARCH_MAX_PAGES": str(self.cfg.research_max_pages),
                # her desk (§34.2). The path IS the sandbox root, so this is
                # also what scopes the hands to *this* character's vault — and
                # an unset one leaves the desk tools unadvertised entirely.
                "VAULT_DIR": str(self.cfg.vault_dir),
                "WORKSPACE_ENABLED": "1" if self.cfg.workspace_enabled else "0",
                "SKILLS_ENABLED": "1" if self.cfg.skills_enabled else "0",
                # §23: unadvertised without a mind, because the queue it writes
                # into is only ever read by the loop and the inner-life panel.
                "SELFEDIT_ENABLED": "1" if self.cfg.mind_enabled else "0",
            })
            # …plus anybody else's hands (§7.2). With no MCP_SERVERS file this
            # is skipped entirely and she runs on her own server alone, exactly
            # as before — the wrapper only appears when there is something to
            # wrap. A file that won't parse is loud and then ignored: third-
            # party hands are an addition, never a reason she boots without her
            # own.
            try:
                extra = load_servers(self.cfg.mcp_servers) if self.cfg.mcp_servers else []
            except Exception as e:
                log.warning("MCP_SERVERS (%s) couldn't be read — running on her "
                            "own server alone: %s", self.cfg.mcp_servers, e)
                extra = []
            if extra:
                children = [("yurios", runner)]
                for entry in extra:
                    children.append((entry["name"],
                                     McpToolRunner(command=entry["command"],
                                                   env=entry["env"])))
                    self._external_rates[entry["name"]] = (
                        entry["rate"] if entry["rate"] is not None
                        else self.cfg.tool_rate_external)
                runner = MultiToolRunner(children)
        elif runner is None and self.cfg.tools_backend == "fake":
            from .tools.fakes import FakeToolRunner
            runner = FakeToolRunner()
        if runner is not None and not self.autonomous:
            runner = None                      # injected test brain has no hands
        if runner is not None:
            self.boot.start("tools", detail=self.cfg.tools_backend)
            try:
                specs = await runner.start()
                # Discovery is the allowlist for tools nobody here could name in
                # advance (§7.3) — that is, a third-party server's. Hers are
                # deliberately NOT admitted this way: the rates in __init__ are
                # her allowlist, and they encode decisions discovery can't see
                # (SELFIE_BACKEND=off leaves the camera out of the buckets, and
                # the fake runner advertises it regardless). Auto-admitting
                # everything would quietly hand back the hands config took away.
                #
                # Only `MultiToolRunner` keeps `started` as the (name, child)
                # list this walks; a single runner uses the same attribute for
                # a plain "did I come up" bool. Iterating that raised
                # `TypeError: 'bool' object is not iterable` — inside the except
                # below, so `TOOLS_BACKEND=fake` booted her handless with one
                # warning and no discovery ever ran.
                servers = getattr(runner, "started", None)
                for name, child in (servers if isinstance(servers, list) else []):
                    if name == "yurios":
                        continue
                    rate = self._external_rates.get(name, self.cfg.tool_rate_external)
                    for spec in specs:
                        if runner.server_of(spec.name) == name and \
                                self.guard.allow(spec.name, rate):
                            log.info("tools: %s admitted at %d/min (from the %r "
                                     "server)", spec.name, rate, name)
                self.brain.set_tools(runner, specs)
                self._tool_runner = runner
                self.tool_count = len(specs)
                self.tools_status = ("fake" if type(runner).__name__ == "FakeToolRunner"
                                     else "mcp")
                detail = f"{self.tools_status} · {len(specs)} tools"
                if isinstance(runner, MultiToolRunner):
                    detail += f" · {len(runner.started)} servers"
                    for name, why in runner.failures.items():
                        log.warning("tools: %s is not mounted (%s)", name, why)
                self.boot.done("tools", detail=detail)
            except Exception as e:
                # peeled out of its task groups — the wrapper's own message is
                # "unhandled errors in a TaskGroup", which names nothing (§7.2)
                from .tools.client import start_failure
                why = start_failure(e)
                log.warning("tool backend failed — she has no hands this run: %s", why)
                self.tools_status = f"failed: {why}"
                self.tool_count = 0
                self._tool_runner = None
                self.boot.done("tools", state="failed", detail=why[:80])
        elif self.cfg.tools_backend != "off":
            # declared pending but no runner (e.g. a test brain) — settle it
            self.boot.done("tools", state="skipped", detail="no hands")

        self.controller.set_rain(self.cfg.rain_intensity)   # the room's weather (§6.2)

        self._tasks.append(asyncio.create_task(self.timers.run(),
                                               name="timer-board"))
        # the mind (SPEC §15): built over the real brain's stores. An injected
        # test brain (no AppState) leaves her mindless but talking — the route
        # suites exercise the wires without the loop.
        if self.cfg.mind_enabled and not self.model_configured:
            self.mind_status = "waiting for model selection"
            self.boot.done("mind", state="skipped", detail="choose a language model")
        elif self.cfg.mind_enabled and self.autonomous:
            try:
                self.mind = MindLoop(self.cfg, self.clock, bus=self.signals,
                                     brain=self.brain, controller=self.controller,
                                     timers=self.timers, hub=self.hub,
                                     speak=self.speak_ambient,
                                     post_message=self.post_message,
                                     park_gate=self.park_gate)
                self.mind.set_hands_enabled(self._hands_granted)
                self.mind_status = "running"
                self.boot.done("mind", detail=f"running · {self.mind.activity.state}")
                self._mind_task = asyncio.create_task(self.mind.run(), name="mind")
                self._tasks.append(self._mind_task)
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
        await self.voice.close()               # cancel a pending unload; free the weights
        await self.channels.stop_all()
        if self.selfies is not None:
            await self.selfies.close()
        if self.research is not None:
            await self.research.close()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._tool_runner is not None:
            try:
                await self._tool_runner.close()
            except Exception:
                log.exception("tool runner close failed")
        store = getattr(getattr(self.brain, "state", None), "store", None)
        index = getattr(store, "index", None)
        if index is not None:
            try:
                index.close()
            except Exception:
                log.exception("memory index close failed")

    # ---- her brain settings, changed while she is talking (SPEC §31.4) ----

    async def retune(self, wanted: dict) -> dict:
        """Move this runtime onto another model (or route, or key) in place.

        `wanted` is the character's *effective* brain settings — her record's
        overrides already resolved against the host's `.env`
        (`host.config_for_character`), so this method never has to know that
        characters exist. Only the fields that actually differ move; the answer
        names them, so the caller can say "nothing changed" honestly.

        The swap itself is synchronous and instant: the next model call — the
        very next token if a turn is streaming — goes to the new provider. What
        *isn't* instant is pinning a local model into LM Studio's memory, so that
        runs behind the answer; until it lands, LM Studio JIT-loads her the slow
        way, which is late, not broken (§3.1)."""
        from . import rewire

        changes = rewire.differences(self.cfg, wanted)
        state = getattr(self.brain, "state", None) if self._owns_models else None
        applied = rewire.apply(state, self.cfg, changes, meter=self.context)
        if applied and self._owns_models:
            # The flag /api/onboarding and /api/health report is a snapshot
            # taken at boot; a model chosen live (a switchboard override lands
            # here, no restart) must retire the first-run chooser — and a
            # cleared override must revive it.
            self.model_configured = is_configured(self.cfg.chat_model)
        if "chat_model" in applied:
            # A different model has different senses (§35). Re-asked here, on
            # the swap, so the composer's paperclip appears or disappears with
            # the model rather than at the next restart.
            await self._probe_image_input()
        if applied and self.cfg.chat_model.startswith("lm_studio/"):
            task = asyncio.create_task(self._repin_lmstudio(), name="lmstudio-repin")
            self._tasks.append(task)           # cancelled with the rest on shutdown…
            task.add_done_callback(self._forget_task)    # …and forgotten once done
        return {"applied": applied, "chat_model": self.cfg.chat_model,
                "utility_model": self.cfg.utility_model}

    def _forget_task(self, task: asyncio.Task) -> None:
        """Drop a finished one-shot task from the shutdown list, whichever order
        it and `stop_async`'s clear() happen in."""
        try:
            self._tasks.remove(task)
        except ValueError:
            pass

    async def _repin_lmstudio(self) -> None:
        """Load the newly chosen LM Studio model and re-read the window it got.

        Off the event loop and off the answer's critical path: a cold model off
        disk is a minute, and she is mid-conversation. The context gauge's
        ceiling belongs to whichever model is loaded *now*, so it is re-probed
        here rather than left showing the old model's window."""
        try:
            if self.cfg.lmstudio_preload:
                from yurios.app.main import _preload_lmstudio
                await asyncio.to_thread(_preload_lmstudio, self.cfg,
                                        chat=True, embed=False)
            await asyncio.to_thread(self._probe_context_window, self.cfg)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("could not pin the newly selected model in LM Studio")

    async def set_mind_enabled(self, enabled: bool) -> None:
        """Pause/resume autonomy without taking conversation offline."""
        if enabled:
            if self.mind is None:
                if not self.autonomous:
                    raise RuntimeError("this brain has no autonomy state")
                self.mind = MindLoop(
                    self.cfg, self.clock, bus=self.signals, brain=self.brain,
                    controller=self.controller, timers=self.timers, hub=self.hub,
                    speak=self.speak_ambient, post_message=self.post_message,
                    park_gate=self.park_gate)
            if self._mind_task is None or self._mind_task.done():
                self._mind_task = asyncio.create_task(self.mind.run(), name="mind")
                self._tasks.append(self._mind_task)
            self.mind_status = "running"
            return
        if self._mind_task is not None and not self._mind_task.done():
            self._mind_task.cancel()
            await asyncio.gather(self._mind_task, return_exceptions=True)
        self._mind_task = None
        self.mind_status = "paused"

    def set_hands_enabled(self, enabled: bool) -> None:
        """Grant or revoke this character's autonomous hands, live (§26, amended).

        Synchronous and never restarts her: the whole value of a kill switch is
        that it lands before the next tick, and one that needed a rebuild would
        be a setting wearing a switch's clothes. A revoke cancels nothing
        already dispatched and denies everything after it, in the audit.

        Recorded even when there is no mind yet: `start()` seeds this from her
        record before `start_async` builds one, and a mind rebuilt later (a
        model change, a restart of the loop alone) must not come back with the
        hands she was refused.
        """
        self._hands_granted = bool(enabled)
        if self.mind is not None:
            self.mind.set_hands_enabled(enabled)


# uvicorn waits this long for open connections to drain on Ctrl+C before it
# cancels them (SPEC §10). The one long-lived connection is the /api/events SSE
# stream; it watches server.should_exit and bows out within ~1 s, so this cap is
# only the safety net for a wedged client. Without any cap uvicorn's default is
# an *unbounded* wait — which is the Ctrl+C hang.
SHUTDOWN_GRACE_SECONDS = 5


class _RawAssetNoCache:
    """Pure-ASGI header shim: mark the unhashed, served-raw paths no-cache
    (revalidate every load) without buffering the body the way BaseHTTPMiddleware
    would. See the note at its registration in create_app for why this isn't
    @app.middleware.

    Not the Vite bundle: dist/assets/* filenames carry a content hash, so a
    changed file is a changed URL and the old body can be cached forever. These
    paths keep their names across edits, which is exactly when a browser's
    heuristic freshness serves yesterday's script."""

    PREFIXES = ("/models/", "/js/", "/shared/", "/live2d/")

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith(self.PREFIXES):
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
        timeout_graceful_shutdown=SHUTDOWN_GRACE_SECONDS,
        **uvicorn_ws_options(cfg)))
    app.state.server = server
    return server


def create_app(cfg: Config | None = None, *, brain=None, chat_model=None,
               utility_model=None, embedder=None, tool_runner=None,
               clock: Clock | None = None,
               controller: VrmController | None = None,
                manage_lifespan: bool = True,
                mount_frontend: bool = True,
                protect_access: bool = True,
                limit_http_body: bool = True) -> FastAPI:
    cfg = cfg or Config()
    rt = Runtime(cfg, brain=brain, chat_model=chat_model,
                 utility_model=utility_model, embedder=embedder,
                 tool_runner=tool_runner, clock=clock, controller=controller)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if manage_lifespan:
            await rt.start_async()
        yield
        if manage_lifespan:
            await rt.stop_async()

    app = FastAPI(title="YuriOS", docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)
    app.state.rt = rt
    from yurios.app.providers.admission import InferenceAdmission
    app.state.turn_admission = InferenceAdmission(active=1, queue=2)

    if limit_http_body:
        from yurios.security import install_http_boundaries
        install_http_boundaries(app)

    if protect_access:
        from yurios.security import install_owner_security
        install_owner_security(app, cfg)

    # StaticFiles sends ETag/Last-Modified but no Cache-Control, so browsers —
    # and the desktop window's persistent cache (§6.5) — apply heuristic
    # freshness and can keep serving a stale body after the file on disk changes
    # — a body under web/models/, or an edited script under web/js/ or
    # web/shared/ that the Live2D page loads by its unhashed name.
    # no-cache = still cached, but revalidated every load: a 304 normally, the
    # new bytes the moment the file on disk differs.
    #
    # A pure-ASGI shim, deliberately NOT @app.middleware("http"): that decorator
    # is a BaseHTTPMiddleware, which re-streams *every* response — including the
    # long-lived /api/events SSE body (routes/events.py) — through an internal
    # memory stream, and on shutdown-cancel that surfaces as a noisy
    # "Exception in ASGI application". Rewriting one header on http.response.start
    # leaves streaming bodies untouched.
    app.add_middleware(_RawAssetNoCache)

    from yurios.desktop.routes import settings as b2_settings

    from .routes import channels, chat, events, health, live2d, mind, voice_ws
    from .routes import gallery, inbox, onboarding, uploads
    app.include_router(health.router)
    app.include_router(onboarding.router)
    app.include_router(events.router)
    app.include_router(voice_ws.router)
    # the sanctuary's channel switches (SPEC §10.5): the telegram sending
    # toggle lives beside the controls that reach the same runtime.
    app.include_router(channels.router)
    # what she said while nobody was listening (SPEC §18.4): the pending run the
    # chat view shows on entry, and the desktop shell's notification stream.
    app.include_router(inbox.router)
    # the text-turn seam over HTTP (SPEC §10.5): what the CLI chat — and any
    # future remote frontend — drives instead of the voice socket.
    app.include_router(chat.router)
    # …and the picture that can come with one (SPEC §35): the composer puts the
    # file here first, then names it in the turn.
    app.include_router(uploads.router)
    # the inner-life surface (SPEC §24.3): journal, goals, pending self-edits,
    # the tick trace — what converts autonomy from creepy to an inner life.
    app.include_router(mind.router)
    # …and the shelf of everything her camera has made (SPEC §7.6): the chat
    # column's third panel reads the forge's ledger through here, and writes
    # back the one thing the ledger cannot know — whether the shot was any good.
    app.include_router(gallery.router)
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
    # The _RawAssetNoCache shim above keeps /models/ revalidated.
    app.mount("/models", StaticFiles(directory=WEB_DIR / "models", html=True),
              name="models")
    # The settings panel's one shared source (SPEC §11): web/shared/settings.{js,css},
    # served raw so BOTH the bundled VRM app and the raw Live2D client load the
    # exact same file — one codepath for the .env editor, no per-frontend copy.
    app.mount("/shared", StaticFiles(directory=WEB_DIR / "shared"), name="shared")
    # …and the same deal one directory over, for the two frontend scripts that
    # aren't the settings panel: web/js/chat.js and web/js/boot.js are classic,
    # dependency-free IIFEs precisely so BOTH pages can run them (see their file
    # headers). The VRM page gets them bundled by Vite; the raw Live2D client
    # asks for /js/chat.js by path — which, without this mount, falls through to
    # the dist mount below and 404s, since the build emits only hashed assets/.
    # Serving web/js raw is what makes that path real in a built deploy.
    app.mount("/js", StaticFiles(directory=WEB_DIR / "js"), name="js")
    # The sanctuary app itself is the Vite build (web/dist, → §3). check_dir=False
    # so a fresh checkout that hasn't run `npm run build` still boots — / just
    # 404s until then, and the warning tells them what to run — instead of raising
    # at mount time and taking the whole server (and the test suite) down with it.
    if mount_frontend and not (DIST_DIR / "index.html").exists():
        log.warning("frontend not built — run `cd web && npm install && npm run build`; "
                    "serving %s (/ will 404 until then)", DIST_DIR)
    if mount_frontend:
        app.mount("/", StaticFiles(directory=DIST_DIR, html=True, check_dir=False),
                  name="web")
    return app


def app() -> FastAPI:
    """uvicorn factory: `uvicorn world.main:app --factory`."""
    return create_app()
