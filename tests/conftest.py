"""Shared fixtures. The whole suite runs offline (SPEC §13): fake voice backends
(B2 §3), a fake tool runner, an in-memory MCP session,
and a VirtualClock for everything timed."""
from __future__ import annotations

import dotenv
import pytest

# Unpair the developer's `.env` from the suite, before anything can read it.
# `import litellm` calls `load_dotenv()`, which copies this machine's `.env` into
# `os.environ` for the rest of the process — so the test file that first reaches
# the brain hands every later one real values (CONTEXT_LENGTH, a live Telegram
# token…) through the environment, behind `Config(_env_file=None)`'s back. conftest
# is imported before any test module, and litellm binds the name at ITS import, so
# stubbing it here is what keeps the suite offline (SPEC §13) whatever order the
# files run in. (`.env` still reaches a Config that asks for it by path.)
dotenv.load_dotenv = lambda *a, **kw: False

from yurios.desktop.config import Config as VoiceConfig  # noqa: E402,F401 (re-export habit)
from yurios.world.avatar.controller import VrmController
from yurios.world.clock import VirtualClock
from yurios.world.config import Config
from yurios.world.tools.guard import Guard
from yurios.world.tools.timers import TimerBoard


@pytest.fixture(scope="session")
def _empty_installation(tmp_path_factory):
    return tmp_path_factory.mktemp("installation")


@pytest.fixture(autouse=True)
def fresh_park_gate():
    """One process-wide park door is right for a host and wrong for a test run.

    `world/vram.shared_gate` is a module singleton because four characters
    share one card; pytest is one process running hundreds of runtimes, so a
    gate a failed test left shut would silently hold every later one at the
    door. Each test gets a clean one."""
    from yurios.world.vram import reset_shared_gate
    reset_shared_gate()
    yield
    reset_shared_gate()


@pytest.fixture(autouse=True)
def installation_elsewhere(_empty_installation, monkeypatch):
    """Keep the CLI's idea of "the installation" off this machine's real one.

    `yurios` commands address the installation the package was installed from
    (daemon.install_root), not the working directory — on a developer's machine
    that is this checkout, with the `.env` conftest works so hard to keep out of
    the suite. A test that calls a command directly would write to it. So the
    suite points at an empty installation instead; a test that needs one with
    contents in it names its own with YURIOS_ROOT, and the two tests about
    finding the real one set or clear the variable themselves. It is deliberately
    not `tmp_path`: tests assert on what is and isn't in there.
    """
    monkeypatch.setenv("YURIOS_ROOT", str(_empty_installation))


@pytest.fixture
def clock() -> VirtualClock:
    return VirtualClock()


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config(
        # the developer's own `.env` (real API keys, tuned mind_* knobs) must
        # never leak into a test's idea of the code's defaults (SPEC §13)
        _env_file=None,
        tts_backend="fake", stt_backend="fake", vad_backend="fake",
        mask_latency=False, tools_backend="fake",
        selfie_backend="mock", selfie_dir=tmp_path / "selfies",
        vault_dir=tmp_path / "vault", db_path=tmp_path / "mvw.db",
        corpus_dir=tmp_path / "corpus", trace_dir=tmp_path / "traces",
        tool_log_dir=tmp_path / "tool-logs",
        upload_dir=tmp_path / "uploads",
        # Whether her model can be shown a picture (§35) is a question you
        # answer by asking the provider, and the suite is offline — so it is
        # answered here with the documented override instead, which short-
        # circuits before any request. A test about pictures says `on`; the
        # probe itself is exercised over a MockTransport (test_pictures.py).
        chat_image_input="off",
        # channels stay off no matter what the machine's .env pairs (§10.5) —
        # the suite must never start a real adapter or touch a real API
        telegram_bot_token="", telegram_chat_id="")


@pytest.fixture
def guard(cfg, clock) -> Guard:
    return Guard(rates_per_min={"set_timer": 6, "play_music": 6, "list_notes": 20},
                 log_dir=cfg.tool_log_dir, clock=clock)


@pytest.fixture
def timers(clock) -> TimerBoard:
    return TimerBoard(clock)


class SpyController(VrmController):
    """A VrmController that also journals every command for assertions."""

    def __init__(self):
        super().__init__()
        self.commands: list[dict] = []

    def _send(self, cmd, sticky=None):
        self.commands.append(cmd)
        super()._send(cmd, sticky=sticky)

    def kinds(self) -> list[str]:
        return [c["type"] for c in self.commands]


@pytest.fixture
def controller() -> SpyController:
    return SpyController()


class ScriptedChat:
    """A chat model whose stream yields one scripted token list per pass, and
    records the messages of every call — the tool loop's test double."""

    def __init__(self, passes: list[list[str]]):
        import asyncio
        self.passes = list(passes)
        self.calls: list[list[dict]] = []
        # fires when pass i starts streaming — lets a test time a barge-in
        self.pass_started = [asyncio.Event() for _ in passes]

    async def stream(self, messages, **params):
        import asyncio
        i = len(self.calls)
        self.calls.append([dict(m) for m in messages])
        if i < len(self.pass_started):
            self.pass_started[i].set()
        tokens = self.passes[i] if i < len(self.passes) else []
        for tok in tokens:
            yield tok
            await asyncio.sleep(0)     # a real await point — cancellation lands here


class StubState:
    """The minimum of Build #1's AppState the tool loop touches."""

    def __init__(self, chat):
        self.chat = chat


def make_toolbrain(cfg, guard, timers, controller, chat, runner=None,
                   specs=None, selfies=None, research=None):
    """A ToolBrain over a stub state — unit tests drive _stream_with_tools
    directly; the full path is pinned in test_integration.py."""
    from yurios.world.brain import ToolBrain
    from yurios.world.tools.fakes import SPECS
    tb = ToolBrain(StubState(chat), cfg, guard=guard, timers=timers,
                   controller=controller, selfies=selfies, research=research)
    if runner is not None:
        tb.set_tools(runner, specs if specs is not None else list(SPECS))
    return tb


async def collect(agen) -> list[str]:
    return [t async for t in agen]


# ---- the mind's sim harness (SPEC §27) --------------------------------------
# A VirtualClock + the REAL brain (fake models) drive the real tick
# loop through simulated days in milliseconds. Signals in, trace records out.

import datetime  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SIM_START = datetime.datetime(2026, 7, 6, 9, 0)     # a Monday morning, local


class FakeUtility:
    """Answers like a real utility model: empty partner ops, and a dumb but
    honest DREAM summary (keeps the lines someone flagged with 'remember').

    The §21.2 dream jobs get answers too, each recognisable enough that a test
    can assert the right prompt reached the model — a fake that returned the
    same string to every job would let two jobs swap prompts unnoticed."""

    async def complete(self, messages, **params):
        system = messages[0].get("content", "") if messages else ""
        low = system.lower()
        if "reviewing possible commitments" in low:
            import json
            payload = json.loads(messages[1].get("content", "{}"))
            candidates = payload.get("candidates", [])
            if not candidates:
                return '{"goal":null}'
            candidate = candidates[0]
            text = candidate.get("text", "")
            from yurios.mind.goals import promise_kind
            kind = promise_kind(text, str(candidate.get("provenance", "promise:")))
            return json.dumps({"goal": {
                "text": text, "kind": kind,
                "rationale": "the reply leaves this work unresolved",
                "success": "the stated work is completed",
                "candidates": [0],
            }})
        if "durable facts" in system:
            body = messages[1].get("content", "") if len(messages) > 1 else ""
            keep = [l.split("  ", 1)[-1] for l in body.splitlines()
                    if l.startswith("### ") and "remember" in l.lower()]
            return "\n".join(keep[:3])
        if "working note" in low:
            return "sat with it; noted one next step."
        if "diary entry" in low:
            return "A quiet one. The rain kept up all afternoon."
        if "taking stock of your own goals" in low:
            return "Two of these are the same thing. Do the sailing one first."
        if "want a picture of" in low:
            return "Sat by the window with the lamp low, chin on my hand."
        return '{"ops": []}'


class FakeEmbedder:
    """Deterministic bag-of-words hashing: texts sharing words land near each
    other, so retrieval order is meaningful offline (crc32, never hash() —
    which is salted per process)."""

    dim = 32

    def embed(self, texts):
        import re
        import zlib
        out = []
        for t in texts:
            v = [0.0] * self.dim
            for w in re.findall(r"[a-z0-9']+", (t or "").lower()):
                v[zlib.crc32(w.encode()) % self.dim] += 1.0
            out.append(v)
        return out


class CannedChat:
    """A chat model that answers every stream with the same line — enough for
    a mind that composes murmurs and reach-outs across simulated days."""

    def __init__(self, line: str = "[tender] Hey — how did it go?"):
        self.line = line
        self.calls: list[list[dict]] = []

    async def stream(self, messages, **params):
        self.calls.append([dict(m) for m in messages])
        for tok in self.line.split(" "):
            yield tok + " "


class SpeakRecorder:
    """Runtime.speak_ambient's stand-in: records cues, answers per `connected`."""

    def __init__(self, clock):
        self.clock = clock
        self.connected = False
        self.calls: list[dict] = []

    async def __call__(self, cue: str) -> bool:
        self.calls.append({"cue": cue, "ts": self.clock.now(),
                           "delivered": self.connected})
        return self.connected


class PostRecorder:
    """Runtime.post_message's stand-in: the chat transcript, recorded."""

    def __init__(self, clock):
        self.clock = clock
        self.messages: list[dict] = []

    def __call__(self, role, text, *, image_url=None, proactive=False, **kw):
        # **kw absorbs the routing fields a late-arriving message carries back
        # (channel, client_id, selfie_id) — a recorder that rejected them would
        # fail on exactly the callers that need them most.
        entry = {"role": role, "text": text, "ts": self.clock.now(),
                 "proactive": proactive, **kw}
        self.messages.append(entry)
        return entry

    def proactive(self):
        return [m for m in self.messages if m["proactive"]]


@pytest.fixture
def seeded_vault(tmp_path):
    """A throwaway Vault seeded from the SOUL — the new-user path."""
    if not (ROOT / "soul-src" / "soul.yaml").exists():
        pytest.skip("soul-src missing")
    dst = tmp_path / "vault"
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "seed_vault.py"),
                        "--soul", str(ROOT / "soul-src"), "--vault", str(dst)],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    return dst


class MindRig:
    def __init__(self, mind, clock, speak, post, timers, controller, chat):
        self.mind = mind
        self.clock = clock
        self.speak = speak
        self.post = post
        self.timers = timers
        self.controller = controller
        self.chat = chat

    def say(self, text: str, reply: str = "Mm. I'm here.") -> None:
        """One committed exchange, as the forked voice route tees it."""
        self.mind.bus.post("user_message", {"text": text}, source="voice")
        self.mind.bus.post("turn_committed", {"text": text, "reply": reply},
                           source="voice")

    def proactive_messages(self):
        out = [m for m in self.post.proactive()]
        out += [c for c in self.speak.calls
                if c["delivered"] and "reach out" in c["cue"]]
        return out


class ScriptedUtility:
    """A utility model that answers goal-work steps from a script, and hands
    everything else to `FakeUtility`.

    Goal work is one utility call that may emit one structured intent
    (mind/hands.py), so scripting *that* line is how a test drives a
    mind-initiated tool call without a model. Everything else the loop asks for
    — the dream jobs, the partner ops — still gets the honest fake, or one
    night's consolidation would break every hands test."""

    def __init__(self, *lines: str):
        self.lines = list(lines)
        self.calls: list[list[dict]] = []
        self.fallback = FakeUtility()

    async def complete(self, messages, **params):
        self.calls.append([dict(m) for m in messages])
        system = (messages[0].get("content", "") if messages else "").lower()
        if "advancing one of your own goals" in system and self.lines:
            return self.lines.pop(0)
        return await self.fallback.complete(messages, **params)


def make_mind(cfg, vault, clock=None, *, chat=None, seed=7,
              utility=None, tools=None, specs=None, bus=None) -> MindRig:
    """The real ToolBrain (fake models) + the real MindLoop, on a VirtualClock.

    `tools` wires a ToolRunner onto the brain the way `Runtime.start_async`
    does, which is what her own hands need to exist at all (mind/hands.py reads
    the runner off the brain). Left out, she is handless — the shipped default,
    and what every test written before the hands assumes.

    `bus` hands a second mind the *same* queue, which is what a loop switched
    off and on again gets: a rebuilt MindLoop on a SignalBus that never went
    away. Left out, each rig gets its own, which is what a restart gets."""
    from yurios.world.brain import ToolBrain
    from yurios.world.hub import EventHub
    from yurios.world.tools.guard import Guard
    from yurios.world.tools.timers import TimerBoard

    from yurios.mind.loop import MindLoop
    from yurios.mind.signals import SignalBus

    clock = clock or VirtualClock(start=SIM_START.timestamp())
    cfg = cfg.model_copy(update={
        "vault_dir": vault, "embed_dim": FakeEmbedder.dim, "mind_seed": seed,
        "corpus_dir": vault.parent / "corpus",
        "trace_dir": vault.parent / "traces",
        "tool_log_dir": vault.parent / "tool-logs"})
    chat = chat or CannedChat()
    guard = Guard(rates_per_min={}, log_dir=cfg.tool_log_dir, clock=clock)
    timers = TimerBoard(clock)
    controller = SpyController()
    brain = ToolBrain.build(cfg, guard=guard, timers=timers,
                            controller=controller, chat_model=chat,
                            utility_model=utility or FakeUtility(),
                            embedder=FakeEmbedder())
    if tools is not None:
        from yurios.world.tools.fakes import SPECS
        brain.set_tools(tools, list(SPECS) if specs is None else specs)
    speak = SpeakRecorder(clock)
    post = PostRecorder(clock)
    mind = MindLoop(cfg, clock, bus=bus or SignalBus(clock), brain=brain,
                    controller=controller, timers=timers, hub=EventHub(),
                    speak=speak, post_message=post)
    return MindRig(mind, clock, speak, post, timers, controller, chat)


async def run_mind(rig: MindRig, *, hours: float,
                   max_ticks: int = 4000) -> list[dict]:
    """Advance the sim: tick → advance virtual time by the regulated cadence.
    Days of behaviour, checkable in seconds (SPEC §27)."""
    end = rig.clock.now() + hours * 3600
    traces = []
    while rig.clock.now() < end and len(traces) < max_ticks:
        traces.append(await rig.mind.tick())
        rig.clock.advance(max(rig.mind.cadence(), 1.0))
        rig.timers.poll()                   # countdowns land before the next tick
    return traces
