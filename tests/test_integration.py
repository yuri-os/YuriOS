"""End-to-end over the REAL brain (SPEC §2, §13), no models, no net.

The unit tests drive the tool loop with a stub state; this one proves the
actual reuse: `ToolBrain.build` constructs the Build #1 AppState
(assemble, FileMemoryStore, the corpus, the Vault-git spine) exactly as
`python -m yurios.world` does, a scripted chat model emits a [[set_timer]] marker, a
FakeToolRunner answers it — and a tool-bearing turn still ends as one corpus
line and one Vault commit, with the markers in the record. Standalone: seeded
fresh from the soul-src, no reference to ../01 or ../02.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("yurios.app.main")            # the brain

from yurios.desktop.voice.backends.fakes import FakeTTS   # noqa: E402
from yurios.desktop.voice.turn import TurnController      # noqa: E402
from yurios.world.avatar.controller import VrmController  # noqa: E402
from yurios.world.brain import ToolBrain                  # noqa: E402
from yurios.world.tools.fakes import SPECS, FakeToolRunner  # noqa: E402
from yurios.world.tools.guard import Guard                # noqa: E402
from yurios.world.tools.timers import TimerBoard          # noqa: E402

from .conftest import ScriptedChat                 # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SOUL_SRC = ROOT / "soul-src"

MARKER = '[[set_timer {"minutes": 10, "label": "tea"}]]'


class FakeUtility:
    async def complete(self, messages, **params):
        return '{"ops": []}'                # no partner-model changes, valid JSON


class FakeEmbedder:
    dim = 8

    def embed(self, texts):
        return [[float((len(t) + i) % 5) for i in range(self.dim)] for t in texts]


@pytest.fixture
def vault(tmp_path):
    """Seed a throwaway Vault from the SOUL — the new-user path."""
    if not (SOUL_SRC / "soul.yaml").exists():
        pytest.skip("soul-src missing")
    dst = tmp_path / "vault"
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "seed_vault.py"),
                        "--soul", str(SOUL_SRC), "--vault", str(dst)],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    return dst


async def test_tool_turn_over_the_real_brain(vault, cfg, clock):
    cfg = cfg.model_copy(update={
        "vault_dir": vault, "embed_dim": 8,
        "corpus_dir": vault.parent / "corpus",
        "trace_dir": vault.parent / "traces"})
    chat = ScriptedChat([
        ["[happy] ", "Tea? ", "On it. ", MARKER],
        ["Ten ", "minutes ", "— ", "[tender] ", "I'll ", "call ", "you."],
    ])
    guard = Guard(rates_per_min={"set_timer": 6, "play_music": 6, "get_weather": 4},
                  log_dir=cfg.tool_log_dir, clock=clock)
    timers = TimerBoard(clock)
    controller = VrmController()
    runner = FakeToolRunner()

    brain = ToolBrain.build(cfg, guard=guard, timers=timers,
                            controller=controller, chat_model=chat,
                            utility_model=FakeUtility(), embedder=FakeEmbedder())
    brain.set_tools(runner, list(SPECS))
    sid = brain.resolve_session(None)
    tc = TurnController(brain=brain, tts=FakeTTS(), filler_bank=None,
                        mask_latency=False, trace_dir=cfg.trace_dir)

    events = [ev async for ev in tc.run_turn(sid, "set a tea timer, ten minutes")]
    kinds = [e.kind for e in events]

    # the turn completed as one seamless OutEvent stream, tool call and all
    assert kinds[-1] == "done"
    texts = [e.text for e in events if e.kind == "audio"]
    assert any("On it" in t for t in texts)            # the lead-in (pass 1)
    assert any("call you" in t for t in texts)         # the continuation (pass 2)
    assert not any("[[" in (t or "") for t in texts)   # the marker never spoken
    assert any(e.kind == "expression" and e.expression == "happy" for e in events)

    # the prompt carried the ## TOOLS directive, built from discovery (§7.4)
    assert "## TOOLS" in chat.calls[0][0]["content"]
    assert "set_timer" in chat.calls[0][0]["content"]

    # …and the situation block (§2.5): the injected clock's time, the
    # embodiment truth — she may know she is an AI; she is never bodiless
    import datetime
    system = chat.calls[0][0]["content"]
    assert "## THE SITUATION RIGHT NOW" in system
    assert datetime.datetime.fromtimestamp(clock.now()).strftime("%H:%M") in system
    assert "Never say you have no body" in system

    # the tool ran, was guarded + audited, and the host realised it (§7.5)
    assert runner.calls == [("set_timer", {"minutes": 10, "label": "tea"})]
    assert (cfg.tool_log_dir / "calls.jsonl").exists()
    assert [t.label for t in timers.pending()] == ["tea"]

    # ONE corpus line — with the model-verbatim record: markers AND result (§7.4)
    corpus = (cfg.corpus_dir / "turns.jsonl").read_text().strip().splitlines()
    assert len(corpus) == 1
    assert "[[set_timer" in corpus[0] and "set_timer →" in corpus[0]

    # and the Vault recorded the turn as exactly one new git commit (B1 §6.5)
    log = subprocess.run(["git", "-C", str(vault), "log", "--oneline"],
                         capture_output=True, text=True).stdout
    assert sum("turn" in l for l in log.splitlines()) == 1


async def test_the_same_marker_twice_in_one_turn_runs_once(vault, cfg, clock):
    """The two-selfies bug, over the real pass loop (§7.3).

    A start-don't-await result (`status: started`) carries nothing she can see,
    so the continuation reads as though the call never landed and she emits the
    identical marker again. Both passed the old guard — the rate limit is a
    burst of two by design, and the per-turn cap of two only bounded how many
    duplicates got through — and the chat got two photos, two timers, two of
    whatever she reached for. The second call must never reach the runner.
    """
    cfg = cfg.model_copy(update={
        "vault_dir": vault, "embed_dim": 8,
        "corpus_dir": vault.parent / "corpus",
        "trace_dir": vault.parent / "traces"})
    chat = ScriptedChat([
        ["On ", "it. ", MARKER],
        ["Just ", "a ", "moment. ", MARKER],       # the same ask, re-emitted
        ["There ", "— ", "ten ", "minutes."],
    ])
    guard = Guard(rates_per_min={"set_timer": 6}, log_dir=cfg.tool_log_dir,
                  clock=clock)
    timers = TimerBoard(clock)
    runner = FakeToolRunner()
    brain = ToolBrain.build(cfg, guard=guard, timers=timers,
                            controller=VrmController(), chat_model=chat,
                            utility_model=FakeUtility(), embedder=FakeEmbedder())
    brain.set_tools(runner, list(SPECS))
    sid = brain.resolve_session(None)
    tc = TurnController(brain=brain, tts=FakeTTS(), filler_bank=None,
                        mask_latency=False, trace_dir=cfg.trace_dir)

    events = [ev async for ev in tc.run_turn(sid, "set a tea timer, ten minutes")]

    assert events[-1].kind == "done"
    assert runner.calls == [("set_timer", {"minutes": 10, "label": "tea"})]
    assert [t.label for t in timers.pending()] == ["tea"]      # one timer, not two

    # the duplicate is on the record as a refusal, and she was told in the
    # continuation — so she can speak to it instead of reaching a third time
    audit = [json.loads(l) for l in
             (cfg.tool_log_dir / "calls.jsonl").read_text().strip().splitlines()]
    assert [a["verdict"] for a in audit] == ["ok", "denied: already done this turn"]
    assert "already done this turn" in chat.calls[-1][-1]["content"]


async def test_a_barged_in_turn_leaves_nothing_in_her_memory(vault, cfg, clock):
    """The other half of "a turn that didn't happen leaves no trace" (§4.4).

    `stream_reply` writes the user's line into the session window before the
    first token — the model has to see it — while only `persist` writes her
    half. Cut the turn off in between and, without the rollback, the window
    keeps a question she never answered: the very next prompt reads it as still
    open and she answers it a second time, folded into the new turn. That is
    what the chat panel showed as two `you` bubbles and one merged reply.
    """
    cfg = cfg.model_copy(update={
        "vault_dir": vault, "embed_dim": 8,
        "corpus_dir": vault.parent / "corpus"})
    chat = ScriptedChat([
        ["Yes, ", "I ", "hear ", "you. ", "Every ", "word."],   # turn 1 — cut off
        ["Seoul. ", "You ", "told ", "me ", "yesterday."],      # turn 2
    ])
    brain = ToolBrain.build(
        cfg, guard=Guard(rates_per_min={}, log_dir=cfg.tool_log_dir, clock=clock),
        timers=TimerBoard(clock), controller=VrmController(),
        chat_model=chat, utility_model=FakeUtility(), embedder=FakeEmbedder())
    sid = brain.resolve_session(None)
    tc = TurnController(brain=brain, tts=FakeTTS(), filler_bank=None,
                        mask_latency=False)

    # turn 1: she gets a sentence out, then the user talks (or types) over her
    events = []
    async for ev in tc.run_turn(sid, "hello, can you hear me?"):
        events.append(ev)
        if ev.kind == "audio":
            tc.cancel()
    assert events[-1].kind == "cancelled"
    brain.abandon(sid)                       # what every cancel path now does

    # turn 2: the prompt must carry no sign of the turn that didn't happen
    async for _ in tc.run_turn(sid, "where do I live?"):
        pass
    window = [m["content"] for m in chat.calls[1] if m["role"] != "system"]
    assert not any("can you hear me" in c for c in window)
    assert any("where do I live" in c for c in window)

    # …and the rollback is only ever the abandoned turn's own line: turn 2
    # committed, so its exchange stays put for turn 3
    async for _ in tc.run_turn(sid, "and what did I say?"):
        pass
    window = [m["content"] for m in chat.calls[2] if m["role"] != "system"]
    assert any("where do I live" in c for c in window)
    assert any("You told me yesterday" in c for c in window)


async def test_ambient_stream_over_the_real_brain_never_persists(vault, cfg, clock):
    cfg = cfg.model_copy(update={
        "vault_dir": vault, "embed_dim": 8,
        "corpus_dir": vault.parent / "corpus"})
    chat = ScriptedChat([["[relaxed] ", "Still ", "raining…"]])
    brain = ToolBrain.build(
        cfg, guard=Guard(rates_per_min={}, log_dir=cfg.tool_log_dir, clock=clock),
        timers=TimerBoard(clock), controller=VrmController(),
        chat_model=chat, utility_model=FakeUtility(), embedder=FakeEmbedder())
    sid = brain.resolve_session(None)
    tc = TurnController(brain=brain, tts=FakeTTS(), filler_bank=None,
                        mask_latency=False)

    events = [ev async for ev in tc.run_turn(
        sid, "", persist=False,
        tokens=brain.stream_ambient(sid, "((one line about the rain))"))]
    assert events[-1].kind == "done"
    assert any(e.kind == "audio" for e in events)
    corpus = cfg.corpus_dir / "turns.jsonl"
    assert not corpus.exists() or not corpus.read_text().strip()
