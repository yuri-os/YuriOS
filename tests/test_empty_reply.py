"""A reply that comes back empty is a failed turn, with its reason said.

It used to be rolled back and then returned as a success with no message in it:
the page drew nothing, the log said nothing, and from the chair she had simply
ignored you. The reason was on the stream all along — a `finish_reason`, a
reasoning pass that spent the whole budget, a refusal filed beside the content —
and nothing read it. These pin the tally that reads it (providers/usage.py), the
provider that keeps it, and the two doors a text turn comes back through."""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
from starlette.testclient import TestClient                   # noqa: E402

from yurios.app.providers.usage import ACTED, LAST_STREAM, StreamTally  # noqa: E402
from yurios.desktop.voice.backends.fakes import FakeBrain     # noqa: E402
from yurios.world.main import create_app                      # noqa: E402
from yurios.world.turns import EmptyReply                     # noqa: E402

from tests.test_channels import ScriptedTelegram, signal_types, tg, update  # noqa: E402


def chunk(content=None, *, reasoning=None, finish=None, refusal=None, usage=None):
    """One streamed chunk, shaped like LiteLLM's (attributes, not keys)."""
    delta = SimpleNamespace(content=content, reasoning_content=reasoning,
                            refusal=refusal)
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=finish)], usage=usage)


def tally_of(*chunks, max_tokens=3200) -> StreamTally:
    tally = StreamTally(max_tokens)
    for c in chunks:
        choices = c["choices"] if isinstance(c, dict) else c.choices
        delta = (choices[0]["delta"] if isinstance(c, dict) else choices[0].delta) \
            if choices else None
        text = (delta.get("content") if isinstance(delta, dict)
                else getattr(delta, "content", None)) or ""
        tally.note(c, text)
    return tally


# ---- the tally ---------------------------------------------------------------

def test_a_stream_that_spoke_has_no_reason():
    assert tally_of(chunk("hi"), chunk(finish="stop")).reason() == ""


def test_a_budget_spent_thinking_says_so_and_names_the_knob():
    usage = SimpleNamespace(completion_tokens=3200, prompt_tokens=9000,
                            completion_tokens_details=SimpleNamespace(
                                reasoning_tokens=3200))
    tally = tally_of(chunk(reasoning="Let me weigh this. " * 50),
                     chunk(finish="length"), SimpleNamespace(choices=[], usage=usage))
    assert "ran out of room thinking" in tally.reason()
    assert "3200-token" in tally.reason() and "MAX_REPLY_TOKENS" in tally.reason()
    detail = tally.detail()
    assert "finish_reason=length" in detail and "reasoning_tokens=3200" in detail
    assert "reasoning_chars=950" in detail


def test_a_filtered_reply_reads_as_a_refusal():
    assert tally_of(chunk(finish="content_filter")).reason() \
        == "the model refused to answer"


def test_a_refusal_filed_beside_the_content_is_quoted():
    tally = tally_of(chunk(refusal="I can't help with "), chunk(refusal="that."),
                     chunk(finish="stop"))
    assert tally.reason() == "the model refused to answer: “I can't help with that.”"


def test_plain_silence_is_plain():
    assert tally_of(chunk(finish="stop")).reason() == "the model sent back no text"


def test_llama_cpp_dict_chunks_are_read_too():
    tally = tally_of({"choices": [{"delta": {}, "finish_reason": "length"}]},
                     max_tokens=512)
    assert "whole 512-token" in tally.reason()


# ---- the provider leaves it for the task that iterated it --------------------

async def test_the_litellm_stream_leaves_its_tally_in_the_callers_context(monkeypatch):
    from yurios.app.providers.openrouter import LiteLLMChatModel

    async def fake(**kwargs):
        async def stream():
            yield chunk(reasoning="hmm")
            yield chunk(finish="length")
        return stream()

    monkeypatch.setattr("litellm.acompletion", fake)
    LAST_STREAM.set(None)
    said = [t async for t in LiteLLMChatModel("some/model").stream(
        [{"role": "user", "content": "hi"}], max_tokens=64)]
    assert said == []
    tally = LAST_STREAM.get()
    assert tally is not None and "whole 64-token" in tally.reason()


async def test_a_stream_closed_early_by_a_tool_call_leaves_nothing(monkeypatch):
    """A tool marker ends the pass mid-stream: that is not silence."""
    from yurios.app.providers.openrouter import LiteLLMChatModel

    async def fake(**kwargs):
        async def stream():
            yield chunk("[[write_note {}]]")
            yield chunk(finish="stop")
        return stream()

    monkeypatch.setattr("litellm.acompletion", fake)
    LAST_STREAM.set(None)
    stream = LiteLLMChatModel("some/model").stream([{"role": "user", "content": "x"}])
    assert await stream.__anext__() == "[[write_note {}]]"
    await stream.aclose()
    assert LAST_STREAM.get() is None


# ---- the turn: a failure, said out loud --------------------------------------

class ThinkingBrain(FakeBrain):
    """Spends the whole budget thinking, the way the provider reports it."""

    async def stream_reply(self, session_id, text):
        tally = StreamTally(3200)
        tally.note(chunk(reasoning="a long deliberation"), "")
        tally.note(chunk(finish="length"), "")
        tally.settle()
        return
        yield                                   # pragma: no cover


def make_app(cfg, brain):
    cfg = cfg.model_copy(update={"tools_backend": "off", "mind_enabled": False})
    return create_app(cfg, brain=brain)


def test_an_empty_reply_is_a_502_that_says_why_and_leaves_no_trace(cfg, caplog):
    brain = ThinkingBrain()
    app = make_app(cfg, brain)
    with TestClient(app) as c, caplog.at_level(logging.WARNING, "world.turns"):
        r = c.post("/api/chat", json={"text": "hello", "client_id": "browser-1"})
        assert r.status_code == 502
        detail = r.json()["detail"]
        assert detail.startswith("she didn't answer — she ran out of room thinking")
        assert detail.endswith("Send it again to retry.")
        rt = app.state.rt
        assert [m["role"] for m in rt.transcript] == ["user"]   # your line stays
        assert brain.abandon_calls                             # …out of her window
        assert brain.persisted is None
        assert ("turn_committed", "api") not in signal_types(rt)
    (line,) = [r for r in caplog.records if "came back empty" in r.getMessage()]
    assert "finish_reason=length" in line.getMessage()


def test_a_brain_with_no_tally_still_fails_with_words(cfg):
    class Mute(FakeBrain):
        async def stream_reply(self, session_id, text):
            yield "[happy] "                    # a tag and nothing to show

    with TestClient(make_app(cfg, Mute())) as c:
        r = c.post("/api/chat", json={"text": "hello"})
    assert r.status_code == 502
    assert "her reply had nothing in it to show" in r.json()["detail"]


class HandOnly(FakeBrain):
    """Reached for a hand and had nothing to add after it, the way a reasoning
    model often ends the pass that follows a tool call."""

    async def stream_reply(self, session_id, text, image=None):
        ACTED.set(True)                             # ToolBrain._execute, on "ok"
        self.tool_outcomes = [{"tool": "take_selfie", "args": {},
                               "verdict": "ok", "result": "{}"}]
        tally = StreamTally(8192)
        tally.note(chunk(finish="stop"), "")
        tally.settle()
        return
        yield                                       # pragma: no cover


def test_a_hand_with_no_words_after_it_is_a_turn_not_a_failure(cfg):
    """The act already happened — the render is running, the timer is set. A
    502 would roll back her record of it and ask for a retry that does it
    twice; the turn commits, with nothing to draw."""
    brain = HandOnly()
    app = make_app(cfg, brain)
    with TestClient(app) as c:
        r = c.post("/api/chat", json={"text": "send me a picture"})
        assert r.status_code == 200
        assert r.json()["message"] is None
        rt = app.state.rt
        assert [m["role"] for m in rt.transcript] == ["user"]   # nothing drawn
        assert brain.persisted is not None                     # …but remembered
        assert ("turn_committed", "api") in signal_types(rt)


async def test_telegram_says_no_reply_came_back():
    tr = ScriptedTelegram()
    ch = tg(tr)

    async def empty(text, *, channel, session_id=None, image_id=None):
        raise EmptyReply("the model refused to answer")

    ch.rt.turns.run = empty
    await ch._handle_update(update())
    (note,) = tr.sent("sendMessage")
    assert note["text"] == ("(No reply came back — the model refused to answer. "
                            "Try sending it again?)")
    await ch._client.aclose()


# ---- the voice socket: the same failure, and nothing filed in her memory ------

def turn_controller(brain):
    from yurios.desktop.voice.backends.fakes import FakeTTS
    from yurios.desktop.voice.turn import TurnController
    return TurnController(brain=brain, tts=FakeTTS(), filler_bank=None,
                          mask_latency=False)


async def test_an_empty_spoken_reply_is_an_error_and_is_not_persisted(caplog):
    brain = FakeBrain(reply="")
    with caplog.at_level(logging.WARNING, "desktop.turn"):
        events = [ev async for ev in turn_controller(brain).run_turn("s", "hello")]
    assert events[-1].kind == "error"
    assert events[-1].detail["message"] == (
        "she didn't answer — her reply had nothing in it to say. "
        "Send it again to retry.")
    assert brain.persisted is None                  # no empty answer in her memory
    assert any("voice turn came back empty" in r.getMessage() for r in caplog.records)


async def test_the_voice_turn_reads_the_reason_off_its_producer_task():
    """The model is drained on a task of its own; the reason must survive it."""
    class Refused(FakeBrain):
        async def stream_reply(self, session_id, text, image=None):
            tally = StreamTally(8192)
            tally.note(chunk(finish="content_filter"), "")
            tally.settle()
            return
            yield                                   # pragma: no cover

    brain = Refused()
    events = [ev async for ev in turn_controller(brain).run_turn("s", "hello")]
    assert events[-1].kind == "error"
    assert "the model refused to answer" in events[-1].detail["message"]
    assert brain.persisted is None


async def test_a_spoken_turn_whose_hand_ran_commits_with_its_outcome(
        cfg, guard, timers, controller):
    """The real tool loop: a marker, the call, and a continuation with nothing
    in it. The timer is set, so the turn is done and persisted, not an error."""
    from tests.conftest import ScriptedChat, make_toolbrain
    from tests.test_turn_tools import LoopBrain
    from yurios.world.tools.fakes import FakeToolRunner

    chat = ScriptedChat([['[[set_timer {"minutes": 10}]]'], []])
    runner = FakeToolRunner()
    brain = LoopBrain(make_toolbrain(cfg, guard, timers, controller, chat,
                                     runner=runner))
    events = [ev async for ev in turn_controller(brain).run_turn("s1", "timer?")]
    assert events[-1].kind == "done"
    assert events[-1].tool_outcomes[0]["tool"] == "set_timer"
    assert runner.calls == [("set_timer", {"minutes": 10})]
    assert brain.persist_calls                      # her memory keeps the act


async def test_a_refused_hand_and_no_words_is_still_no_reply(
        cfg, guard, timers, controller):
    """Only a hand that *ran* makes silence a reply: a denied one did nothing,
    so sending it again repeats nothing."""
    from tests.conftest import ScriptedChat, make_toolbrain
    from tests.test_turn_tools import LoopBrain
    from yurios.world.tools.fakes import FakeToolRunner

    chat = ScriptedChat([['[[no_such_hand {}]]'], []])
    brain = LoopBrain(make_toolbrain(cfg, guard, timers, controller, chat,
                                     runner=FakeToolRunner()))
    events = [ev async for ev in turn_controller(brain).run_turn("s1", "hm?")]
    assert events[-1].kind == "error"
    assert brain.persist_calls == []


async def test_an_empty_opener_is_just_quiet():
    async def nothing():
        return
        yield                                       # pragma: no cover

    brain = FakeBrain(reply="")
    events = [ev async for ev in turn_controller(brain).run_turn(
        "s", "", persist=False, tokens=nothing())]
    assert events[-1].kind == "done"
