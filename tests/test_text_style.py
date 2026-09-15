"""Text is text (SPEC §9.7, §10.5): the spoken-style directive belongs to the
voice channel only, and a text keeps its line breaks. A texting companion told
"this is a spoken conversation" on every browser and Telegram turn cannot text
like one, and one whose three bubbles arrive glued into a sentence cannot
double-text at all."""
from __future__ import annotations

import pytest

from tests.conftest import CannedChat, FakeEmbedder, FakeUtility, collect
from yurios.world.brain import ToolBrain
from yurios.world.tools.guard import Guard
from yurios.world.tools.timers import TimerBoard

pytest.importorskip("fastapi")
from starlette.testclient import TestClient                   # noqa: E402

from yurios.desktop.voice.backends.fakes import FakeBrain     # noqa: E402
from yurios.world.main import create_app                      # noqa: E402


def make_brain(cfg, vault, chat, clock, controller):
    cfg = cfg.model_copy(update={
        "vault_dir": vault, "embed_dim": FakeEmbedder.dim,
        "corpus_dir": vault.parent / "corpus",
        "trace_dir": vault.parent / "traces",
        "tool_log_dir": vault.parent / "tool-logs"})
    return ToolBrain.build(
        cfg, guard=Guard(rates_per_min={}, log_dir=cfg.tool_log_dir, clock=clock),
        timers=TimerBoard(clock), controller=controller, chat_model=chat,
        utility_model=FakeUtility(), embedder=FakeEmbedder())


def system_of(chat: CannedChat) -> str:
    return chat.calls[-1][0]["content"]


# ---- the spoken-style directive is voice-only (§9.7) ------------------------

async def test_a_text_turn_is_not_told_it_is_spoken(cfg, seeded_vault, clock, controller):
    chat = CannedChat("[happy] hi")
    brain = make_brain(cfg, seeded_vault, chat, clock, controller)
    session = brain.resolve_session(None)
    with brain.turn_context(channel="browser"):
        await collect(brain.stream_reply(session, "hey"))
    assert "## VOICE\n" not in system_of(chat)
    assert "spoken conversation" not in system_of(chat)
    assert "## EXPRESSION" in system_of(chat)      # tags still drive a body


async def test_a_voice_turn_still_is(cfg, seeded_vault, clock, controller):
    chat = CannedChat("[happy] hi")
    brain = make_brain(cfg, seeded_vault, chat, clock, controller)
    session = brain.resolve_session(None)
    with brain.turn_context(channel="voice"):
        await collect(brain.stream_reply(session, "hey"))
    assert "## VOICE\n" in system_of(chat)
    assert "## EXPRESSION" in system_of(chat)


async def test_an_ambient_line_with_no_channel_is_text(cfg, seeded_vault, clock, controller):
    """A reach-out composed for the inbox has no channel; it is read, not heard."""
    chat = CannedChat("[tender] thinking of you")
    brain = make_brain(cfg, seeded_vault, chat, clock, controller)
    session = brain.resolve_session(None)
    await collect(brain.stream_ambient(session, "say hi"))
    assert "## VOICE\n" not in system_of(chat)


# ---- a text keeps its line breaks (§10.5) ------------------------------------

class LinesBrain(FakeBrain):
    """A brain that texts in three bubbles."""

    async def stream_reply(self, session_id, text, image=None):
        for tok in self.reply.split(" "):
            yield tok + " "


def test_a_multi_line_text_arrives_as_written(cfg):
    raw = "[happy] wait\n\ni did mean it\n\nokay?"
    cfg = cfg.model_copy(update={"tools_backend": "off", "mind_enabled": False})
    app = create_app(cfg, brain=LinesBrain(raw))
    with TestClient(app) as c:
        r = c.post("/api/chat", json={"text": "did you?", "channel": "browser"})
        assert r.status_code == 200, r.text
        assert r.json()["message"]["text"] == "wait\n\ni did mean it\n\nokay?"


# ---- no body on any screen (§2.5) --------------------------------------------

async def test_a_turn_with_no_body_on_screen_is_told_so(cfg, seeded_vault, clock, controller):
    chat = CannedChat("[happy] hi")
    brain = make_brain(cfg, seeded_vault, chat, clock, controller)
    brain.set_body_probe(lambda: False)
    session = brain.resolve_session(None)
    with brain.turn_context(channel="telegram"):
        await collect(brain.stream_reply(session, "hey"))
    assert "in your body right now" not in system_of(chat)
    assert "as text" in system_of(chat)
