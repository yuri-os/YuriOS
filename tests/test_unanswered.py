"""A line of yours that never got its answer (SPEC §2.6).

A turn that fails rolls back everywhere but the page: your line stays drawn,
because you said it. Before this it then sat there for good — marked "no reply"
in the tab that sent it, unmarked everywhere else and after a reload, and with
no way to send it again or take it back. These pin the three halves: the host
says which lines went unanswered (live and in history), sending one again moves
it to the bottom as a fresh turn, and deleting one forgets it — and only ever a
line nothing else remembers.
"""
from __future__ import annotations

import pytest

from tests.conftest import FakeEmbedder, FakeUtility, ScriptedChat
from yurios.app.conversation import ConversationLog, unanswered

pytest.importorskip("fastapi")
from starlette.testclient import TestClient                  # noqa: E402

from yurios.world.brain import ToolBrain                     # noqa: E402
from yurios.world.main import create_app                     # noqa: E402
from yurios.world.tools.guard import Guard                   # noqa: E402
from yurios.world.tools.timers import TimerBoard             # noqa: E402

ASK = "did the install take?"


# ---- the log ---------------------------------------------------------------

def test_only_a_drawn_line_of_yours_that_was_unwound_is_unanswered(tmp_path):
    log = ConversationLog(tmp_path)
    log.add({"id": "u1", "role": "user", "text": "hi", "session_id": "s"})
    log.attach_raw("u1", None)
    assert not unanswered(log.line("u1"))          # answered, or being
    log.unwind("u1")
    assert unanswered(log.line("u1"))
    log.add({"id": "h1", "role": "assistant", "text": "hey", "session_id": "s"})
    log.unwind("h1")
    assert not unanswered(log.line("h1"))          # hers is never one


def test_a_discarded_line_is_gone_and_stays_gone_through_compaction(tmp_path):
    log = ConversationLog(tmp_path)
    log.add({"id": "u1", "role": "user", "text": "hi", "session_id": "s"})
    log.add({"id": "u2", "role": "user", "text": "still there?", "session_id": "s"})
    log.discard("u1")
    assert [r["id"] for r in log.entries()] == ["u2"]
    assert log.line("u1") is None
    with log._lock:
        log._compact()
    assert [r["id"] for r in log.entries()] == ["u2"]


# ---- the whole path, over the real brain -------------------------------------

@pytest.fixture
def room(cfg, seeded_vault, clock, controller):
    cfg = cfg.model_copy(update={
        "vault_dir": seeded_vault, "embed_dim": FakeEmbedder.dim,
        "tools_backend": "off", "mind_enabled": False})
    chat = ScriptedChat([["[happy] ", "It took."], ["Yes."], ["Mm."]])
    brain = ToolBrain.build(
        cfg, guard=Guard(rates_per_min={}, log_dir=cfg.tool_log_dir, clock=clock),
        timers=TimerBoard(clock), controller=controller, chat_model=chat,
        utility_model=FakeUtility(), embedder=FakeEmbedder())
    app = create_app(cfg, brain=brain)
    events: list[tuple[str, dict]] = []
    publish = app.state.rt.hub.publish

    def spy(type_, payload, **kw):
        events.append((type_, dict(payload)))
        return publish(type_, payload, **kw)

    app.state.rt.hub.publish = spy
    return app, brain, chat, events


def _breaks(chat):
    healthy = chat.stream

    async def breaks(messages, **params):
        chat.calls.append([dict(m) for m in messages])
        raise RuntimeError("401 Unauthorized")
        yield ""                                   # pragma: no cover

    chat.stream = breaks
    return healthy


def _failed_line(c, chat) -> str:
    healthy = _breaks(chat)
    failed = c.post("/api/chat", json={"text": ASK, "channel": "web", "client_id": "c1"})
    assert failed.status_code == 502
    chat.stream = healthy
    chat.calls.pop()
    return next(m["id"] for m in c.get("/api/history").json()["messages"]
                if m["role"] == "user" and m["text"] == ASK)


def test_a_failed_turn_says_so_in_every_room_and_after_a_reload(room):
    app, _brain, chat, events = room
    with TestClient(app) as c:
        line = _failed_line(c, chat)
        assert ("unanswered", {"id": line}) in events
        row = next(m for m in c.get("/api/history").json()["messages"] if m["id"] == line)
        assert row["unanswered"] is True


def test_sending_it_again_moves_it_to_the_bottom_as_a_fresh_turn(room):
    app, brain, chat, events = room
    with TestClient(app) as c:
        line = _failed_line(c, chat)
        events.clear()
        again = c.post("/api/chat/resend", json={"id": line, "client_id": "c2"})
        assert again.status_code == 200, again.text
        body = again.json()
        assert body["message"]["text"] == "It took."
        assert body["user_message"]["text"] == ASK and body["user_message"]["id"] != line
        assert ("message_removed", {"id": line}) in events

        history = c.get("/api/history").json()["messages"]
        assert [m["text"] for m in history if m["role"] != "tool"] == [ASK, "It took."]
        assert not any(m.get("unanswered") for m in history)
        # she was asked once, with your line once — not the failed copy as well
        said = [m["content"] for m in chat.calls[-1] if m["role"] == "user"]
        assert sum(str(content).startswith(ASK) for content in said) == 1
        sid = body["session_id"]
        assert [m["content"] for m in brain.state.sessions.window(sid, 10)] == \
            [ASK, "[happy] It took."]
        # …and the line it replaced cannot be sent twice
        assert c.post("/api/chat/resend", json={"id": line}).status_code == 404


def test_a_resend_that_fails_again_leaves_one_unanswered_line(room):
    app, _brain, chat, _events = room
    with TestClient(app) as c:
        line = _failed_line(c, chat)
        _breaks(chat)
        assert c.post("/api/chat/resend", json={"id": line}).status_code == 502
        rows = [m for m in c.get("/api/history").json()["messages"] if m["role"] == "user"]
        assert len(rows) == 1 and rows[0]["unanswered"] and rows[0]["id"] != line


def test_deleting_it_forgets_it_and_frees_her_last_reply(room):
    app, _brain, chat, events = room
    with TestClient(app) as c:
        first = c.post("/api/chat", json={"text": "hello", "channel": "web"}).json()
        her = first["message"]["id"]
        line = _failed_line(c, chat)
        # your failed line sits under her reply, so it could not be redone…
        assert "regenerable" not in c.get("/api/history").json()["messages"][-2]
        events.clear()
        gone = c.post("/api/chat/discard", json={"id": line})
        assert gone.status_code == 200
        assert ("message_removed", {"id": line}) in events
        history = c.get("/api/history").json()["messages"]
        assert line not in {m["id"] for m in history}
        # …and now it is the last word again, in every room
        assert history[-1]["id"] == her and history[-1]["regenerable"] is True
        assert any(t == "swipe" and p["id"] == her and p["regenerable"] for t, p in events)


def test_an_answered_line_is_never_deleted_or_sent_again(room):
    app, _brain, _chat, _events = room
    with TestClient(app) as c:
        mine = c.post("/api/chat", json={"text": "hello", "channel": "web"}).json()["user_message"]
        her = c.get("/api/history").json()["messages"][-1]
        for path in ("/api/chat/discard", "/api/chat/resend"):
            assert c.post(path, json={"id": mine["id"]}).status_code == 409
            assert c.post(path, json={"id": her["id"]}).status_code == 409
            assert c.post(path, json={"id": "nope"}).status_code == 404
        assert mine["id"] in {m["id"] for m in c.get("/api/history").json()["messages"]}


def test_a_failure_after_her_reply_is_drawn_is_not_your_line_unanswered(room, monkeypatch):
    """Filing her reply can fail too, but the reply is on the page by then:
    offering to send your line again would ask her the same thing twice."""
    app, brain, _chat, events = room

    async def broken_persist(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(brain, "persist", broken_persist)
    with TestClient(app, raise_server_exceptions=False) as c:
        c.post("/api/chat", json={"text": "hello", "channel": "web"})
        assert not any(t == "unanswered" for t, _ in events)
        assert not any(m.get("unanswered") for m in c.get("/api/history").json()["messages"])
