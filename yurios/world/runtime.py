"""How a `Runtime` is assembled — the four heavy things and the boot panel.

`Runtime.__init__` is one object's worth of state (the bus, the guard, the
transcript ring, the timers, the inbox, the task list) plus four subsystems that
are each a real build: the tool allowlist behind her `Guard`, her camera, her
reading desk, and her brain — the last of which loads an embedding model and
may pin gigabytes into LM Studio before it returns.

Those four had grown to two thirds of a 273-line constructor, and the effect was
that the *shape* of a Runtime — what a connection actually gets — could not be
read without reading all of it. They live here now, as functions. What stays in
the constructor is the assignment and the order, which is the part that has to
be read in sequence: the guard exists before the brain that spends it, the park
gate before the camera that shuts it, the camera and the desk before the brain
that is handed both.

Each builder takes the half-built `Runtime` and reads what it needs off it —
these are not general-purpose factories and pretending otherwise would mean
threading eight arguments through to say the same thing. Two of them hand it
bound methods (`post_message`, `speak_ambient`) that reach state built *after*
them; that is deliberate and pre-existing, and it works because those are
callbacks a worker invokes later, never at build time.

`rt` is deliberately unannotated. Naming its type means importing `Runtime`,
and `tests/test_layering.py` reads a `TYPE_CHECKING` import off the parse tree
like any other — correctly, since a cycle you only pay for at type-check time
is still a cycle in the dependency graph.
"""
from __future__ import annotations

import logging

from yurios.mind.workspace import DESK_TOOLS, SKILL_TOOLS

from .brain import ToolBrain
from .config import Config
from .context import short_tokens
from .research import Researcher
from .selfies import SelfieLab, build_forge
from .tools.fetch import build_fetcher
from .tools.search import build_provider

log = logging.getLogger("world.runtime")


def tool_rates(cfg: Config) -> dict[str, int]:
    """The allowlist her `Guard` is built from: tool name → calls per minute.

    Absence is the refusal (SPEC §7.3). A hand she may not use is not rated at
    zero and denied on the way past — it is simply not in this dict, which is
    the same rule `list_tools` follows on the server side: no hand, not a dead
    one. So every `if` below decides existence, not a budget.
    """
    rates = {"set_timer": cfg.tool_rate_timer,
             "play_music": cfg.tool_rate_music}
    if cfg.selfie_backend != "off":
        rates["take_selfie"] = cfg.tool_rate_selfie
        rates["show_picture"] = cfg.tool_rate_picture
    if cfg.search_backend != "off":            # …and the same rule for the web (§7.7)
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
    return rates


def build_camera(rt) -> tuple[SelfieLab | None, str]:
    """Her camera (SPEC §7.6): the forge behind the SelfieLab, and its status.

    Built even when tools are faked — tests inject a fake runner but still want
    the realisation path — and `off` leaves her without one. Requires
    `rt.park_gate`, which the constructor sets first because every turn waits
    on it whether or not there is a camera to shut it.
    """
    cfg = rt.cfg
    if cfg.selfie_backend == "off":
        return None, "off"
    forge, status = build_forge(cfg)
    from .vram import LLMParker
    # The floor comes off the backend that was actually built, not off
    # the config: SELFIE_BACKEND=diffusers resolves to either local
    # camera depending on the checkpoint, and a degrade-to-mock says
    # None — nothing resident, nothing to park for.
    lab = SelfieLab(forge, clock=rt.clock,
                    post=rt.post_message,
                    speak=rt.speak_ambient,
                    notify=rt.hub.publish,
                    parker=LLMParker(
                        cfg,
                        resident_free_gib=forge.backend.RESIDENT_FREE_GIB,
                        gate=rt.park_gate),
                    quiet=rt.wait_turns_idle,
                    situation=rt.visual_situation,
                    signal=rt.post_signal)
    return lab, status


def build_reading(rt) -> tuple[Researcher | None, str]:
    """Her reading desk (SPEC §7.7) — the same start-don't-await shape as the
    camera, and the place a fetched page turns into a shelved document.

    The knowledge store is passed as a GETTER because it belongs to the
    MindLoop, which is built later (`start_async`) and not at all when she is
    mindless — see world/research.py.
    """
    cfg = rt.cfg
    if cfg.search_backend == "off":
        return None, "off"
    search = build_provider(cfg.search_backend, base_url=cfg.searxng_url,
                            language=cfg.search_language,
                            safesearch=cfg.search_safesearch)
    fetcher = build_fetcher(
        "fake" if cfg.search_backend == "fake" else "http",
        timeout=cfg.fetch_timeout_s, max_bytes=cfg.fetch_max_bytes)
    desk = Researcher(
        search, fetcher, clock=rt.clock,
        post=rt.post_message, speak=rt.speak_ambient,
        knowledge=lambda: rt.mind.knowledge if rt.mind else None,
        notify=rt.hub.publish, signal=rt.post_signal)
    return desk, cfg.search_backend


def build_brain(rt, *, chat_model, utility_model, embedder) -> ToolBrain:
    """The real brain: a `BrainAdapter` with the §7 tool loop wrapped round it.

    Only reached when nothing was injected — a route test's `FakeBrain` is
    assigned straight across, which is the point of the seam (no Vault, no
    SQLite, no embedder). Building this one loads the embedding model that
    indexes her memory (SPEC §3): with the sentence-transformers default that
    is a cold torch model on the CPU, as slow to wake as the voice stack, and
    it happens *here*, before the voice warm-up thread has even started. So it
    is surfaced in the boot panel first. A server-backed embedder (ollama /
    lm_studio) has no local weights to load; it still gets a line, so the panel
    names what indexes her.
    """
    from yurios.app.main import _default_embedder    # lazy: torch lives behind it
    cfg = rt.cfg
    pin_lmstudio(rt, chat_model, utility_model, embedder)
    if not rt.model_configured:
        rt.boot.declare("models", "mind · language model", state="skipped",
                        detail="choose a model to connect")
    rt.boot.declare("embed", "memory · embedding model")
    if embedder is None:
        rt.boot.start("embed", detail=cfg.embed_model)
        try:
            embedder = _default_embedder(cfg)
        except Exception as e:
            rt.boot.done("embed", state="failed", detail=str(e)[:80])
            raise
        rt.boot.done("embed", detail=f"{cfg.embed_model} · {cfg.embed_dim}d")
    else:
        rt.boot.done("embed", detail="injected")
    return ToolBrain.build(
        cfg, guard=rt.guard, timers=rt.timers,
        controller=rt.controller, selfies=rt.selfies,
        research=rt.research, chat_model=chat_model,
        utility_model=utility_model, embedder=embedder)


def pin_lmstudio(rt, chat_model, utility_model, embedder) -> None:
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
    cfg = rt.cfg
    chat = chat_model is None or utility_model is None
    if not cfg.lmstudio_preload:
        if chat:
            probe_context_window(rt, cfg)     # the readout still wants a ceiling
        return
    ids = _lmstudio_ids(cfg, chat=chat, embed=embedder is None)
    if not ids:
        return
    rt.boot.declare("models", "mind · language models")
    rt.boot.start("models", detail=f"{len(ids)} on LM Studio")
    pinned = _preload_lmstudio(cfg, chat=chat, embed=embedder is None)
    # the window they were loaded with — the number the masthead gauge and
    # every "context size exceeded" both hang off (SPEC §11)
    window = probe_context_window(rt, cfg) if chat else ""
    if pinned:
        detail = f"{len(pinned)} resident · no idle unload"
        rt.boot.done("models", detail=f"{detail} · {window}" if window else detail)
    else:
        # she still runs: LM Studio JIT-loads per request, slowly (§3.1)
        rt.boot.done("models", state="failed", detail="none pinned — see the log")


def probe_context_window(rt, cfg: Config) -> str:
    """Ask LM Studio how big the window her chat model is loaded with is.

    Worth a call even when CONTEXT_LENGTH said: what .env asked for and what
    the server seated are not the same claim — a model somebody loaded by
    hand keeps the window they gave it, and one too big for the card comes
    back smaller. Only an lm_studio/… route has a local server to ask. Returns
    a short label for the boot panel, "" when nothing was learned."""
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
    rt.context.set_limit(found["loaded"], "lm studio")
    return f"{short_tokens(found['loaded'])} ctx"


def declare_services(rt) -> None:
    """Fill the boot panel, in the order it should read down (SPEC §6.4).

    Each line is one thing she wakes with and the state it is already in. The
    voice stack declares its own three (plus fillers) as it is built; tools and
    mind resolve later on the event loop (`start_async`), so they land pending;
    selfies is settled by now and lands terminal.
    """
    cfg = rt.cfg
    rt.boot.declare(
        "tools", "hands · tool server",
        state="skipped" if cfg.tools_backend == "off" else "pending")
    rt.boot.declare(
        "selfies", "camera · selfie forge",
        state="skipped" if cfg.selfie_backend == "off" else "ready",
        detail="off" if cfg.selfie_backend == "off" else rt.selfies_status)
    rt.boot.declare(
        "mind", "mind · autonomy engine",
        state="pending" if cfg.mind_enabled else "skipped")
    rt.boot.declare(
        "channels", "channels · outside mediums",
        state="pending" if rt.channels.configured else "skipped",
        detail="" if rt.channels.configured else "none configured")
