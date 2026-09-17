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
from yurios.world.turns import _text_of                       # noqa: E402


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


def test_native_voice_marks_greetings_and_replies_as_spoken(cfg, seeded_vault):
    from yurios.desktop.brain import BrainAdapter
    from yurios.desktop.main import create_app as desktop_app

    cfg = cfg.model_copy(update={"vault_dir": seeded_vault,
                                 "embed_dim": FakeEmbedder.dim})
    chat = CannedChat("[happy] Hello there.")
    brain = BrainAdapter.build(cfg, chat_model=chat,
                               utility_model=FakeUtility(), embedder=FakeEmbedder())
    # A returning greeting calls the model; the authored cold open does not.
    brain.cold_open = lambda: None
    with TestClient(desktop_app(cfg, brain=brain)) as client:
        with client.websocket_connect("/ws/voice") as ws:
            ws.send_json({"type": "hello"})
            for request in (None, {"type": "text", "text": "Hello"}):
                if request:
                    ws.send_json(request)
                for _ in range(100):
                    event = ws.receive_json()
                    assert event["type"] != "error", event
                    if event["type"] == "done":
                        break
                else:
                    pytest.fail("voice turn did not finish")
                assert "## VOICE\n" in system_of(chat)


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


def test_a_text_keeps_her_narration(cfg):
    """`*she leans in*` is writing, not markup (§9.7).

    The parser's narration strip is the *spoken* path's rule — aloud she must
    not read her own stage directions. A text turn ran the same rule and it
    took words out of the middle of her sentences, because a `*…*` span is not
    always a stage direction on a line of its own: `Keeping *us* working.`
    reached the chat as "Keeping working." Tags still go (they drive her face);
    everything she wrote stays."""
    raw = ("[tender] That's you keeping me working. Keeping *us* working.\n\n"
           "[neutral] *She reaches for her desk, just to write one line down.*\n\n"
           "[shy] So next time I don't forget it has to *leave the room*.")
    cfg = cfg.model_copy(update={"tools_backend": "off", "mind_enabled": False})
    app = create_app(cfg, brain=LinesBrain(raw))
    with TestClient(app) as c:
        r = c.post("/api/chat", json={"text": "you fixed it?", "channel": "browser"})
        assert r.status_code == 200, r.text
        assert r.json()["message"]["text"] == (
            "That's you keeping me working. Keeping *us* working.\n\n"
            "*She reaches for her desk, just to write one line down.*\n\n"
            "So next time I don't forget it has to *leave the room*.")


def test_a_line_that_was_all_markup_leaves_no_hole():
    """What is left of a stripped-out line is its two newlines (§10.5).

    Some of what the parsers take out sits on a line of its own — a
    `[[append_note {…}]]` marker the tool loop already consumed, a `<think>`
    block — and removing it empties the line without removing it, so its
    newlines join the ones around it. Seen live in a reply with two
    four-newline gaps, one of them where a tool call she *successfully* made
    had been. The paragraph break she wrote survives; the hole does not."""
    assert _text_of(["So the next one arrives.\n\n", "", "\n\n",
                     "There. Written where I'll find it."]) == (
        "So the next one arrives.\n\nThere. Written where I'll find it.")
    # a single break is still a single break — three lines, three bubbles
    assert _text_of(["wait\n", "i did mean it\n\n", "okay?"]) == (
        "wait\ni did mean it\n\nokay?")


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
