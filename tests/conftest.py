"""Shared fixtures. The whole suite runs offline (SPEC §13): fake voice backends
(B2 §3), a fake tool runner, an in-memory MCP session, MockTransport weather,
and a VirtualClock for everything timed."""
from __future__ import annotations

import pytest

from yurios.desktop.config import Config as VoiceConfig  # noqa: F401 (re-export habit)
from yurios.world.avatar.controller import VrmController
from yurios.world.clock import VirtualClock
from yurios.world.config import Config
from yurios.world.tools.guard import Guard
from yurios.world.tools.timers import TimerBoard


@pytest.fixture
def clock() -> VirtualClock:
    return VirtualClock()


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config(
        tts_backend="fake", stt_backend="fake", vad_backend="fake",
        mask_latency=False, tools_backend="fake",
        selfie_backend="mock", selfie_dir=tmp_path / "selfies",
        vault_dir=tmp_path / "vault", db_path=tmp_path / "mvw.db",
        corpus_dir=tmp_path / "corpus", trace_dir=tmp_path / "traces",
        tool_log_dir=tmp_path / "tool-logs",
        # channels stay off no matter what the machine's .env pairs (§10.5) —
        # the suite must never start a real adapter or touch a real API
        telegram_bot_token="", telegram_chat_id="")


@pytest.fixture
def guard(cfg, clock) -> Guard:
    return Guard(rates_per_min={"set_timer": 6, "play_music": 6, "get_weather": 4},
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
                   specs=None, selfies=None):
    """A ToolBrain over a stub state — unit tests drive _stream_with_tools
    directly; the full path is pinned in test_integration.py."""
    from yurios.world.brain import ToolBrain
    from yurios.world.tools.fakes import SPECS
    tb = ToolBrain(StubState(chat), cfg, guard=guard, timers=timers,
                   controller=controller, selfies=selfies)
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
    honest DREAM summary (keeps the lines someone flagged with 'remember')."""

    async def complete(self, messages, **params):
        system = messages[0].get("content", "") if messages else ""
        if "durable facts" in system:
            body = messages[1].get("content", "") if len(messages) > 1 else ""
            keep = [l.split("  ", 1)[-1] for l in body.splitlines()
                    if l.startswith("### ") and "remember" in l.lower()]
            return "\n".join(keep[:3])
        if "working note" in system.lower():
            return "sat with it; noted one next step."
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

    def __call__(self, role, text, *, image_url=None, proactive=False):
        entry = {"role": role, "text": text, "ts": self.clock.now(),
                 "proactive": proactive}
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


def make_mind(cfg, vault, clock=None, *, chat=None, seed=7) -> MindRig:
    """The real ToolBrain (fake models) + the real MindLoop, on a VirtualClock."""
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
                            utility_model=FakeUtility(), embedder=FakeEmbedder())
    speak = SpeakRecorder(clock)
    post = PostRecorder(clock)
    mind = MindLoop(cfg, clock, bus=SignalBus(clock), brain=brain,
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
