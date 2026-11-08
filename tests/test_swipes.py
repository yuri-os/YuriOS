"""Another reply in her last one's place (SPEC §39).

Three seams carry the feature, and these pin each one. The log keeps every
reply she gave on one line and folds the standing one onto the row, so the
window, the page and a rating read the right reply without knowing there are
others. The runner asks her again from the same line of yours and draws the
answer *in* her line. And what she remembers saying — the journal line, the
index row — follows the reply that stands, while the corpus keeps them all and
your next line records which one you kept.
"""
from __future__ import annotations

import json

import pytest

from tests.conftest import FakeEmbedder, FakeUtility, ScriptedChat
from yurios.app.conversation import ConversationLog, swipeable
from yurios.app.corpus import CorpusLogger, export
from yurios.app.sessions import SessionStore

pytest.importorskip("fastapi")
from starlette.testclient import TestClient                  # noqa: E402

from yurios.desktop.voice.backends.fakes import FakeBrain    # noqa: E402
from yurios.world.brain import ToolBrain                     # noqa: E402
from yurios.world.main import create_app                     # noqa: E402
from yurios.world.tools.guard import Guard                   # noqa: E402
from yurios.world.tools.timers import TimerBoard             # noqa: E402
from yurios.world.turns import NotSwipeable                  # noqa: E402


def jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


# ---- the log: one line, several replies ------------------------------------

def exchange(log: ConversationLog, sid: str = "s1") -> str:
    """Your line and her reply, both drawn and admitted, the way a turn writes
    them. Returns her line's id."""
    log.add({"id": "u1", "role": "user", "text": "tea or coffee?",
             "ts": "2026-11-09T10:00:00", "session_id": sid})
    log.attach_raw("u1", None)
    log.add({"id": "h1", "role": "assistant", "text": "Tea.",
             "ts": "2026-11-09T10:00:05", "session_id": sid, "channel": "web"})
    log.attach_raw("h1", "[happy] Tea.", "turn-a")
    return "h1"


def test_another_reply_stands_in_the_same_line(tmp_path):
    log = ConversationLog(tmp_path)
    exchange(log)
    log.add_swipe("h1", "Coffee, then.")
    log.attach_raw("h1", "[tender] Coffee, then.", "turn-b")

    row = log.line("h1")
    assert (row["text"], row["raw"], row["turn_id"]) == \
        ("Coffee, then.", "[tender] Coffee, then.", "turn-b")
    assert (row["swipe"], row["swipes"]) == (1, 2)
    # the window continues from the reply that stands, and only that one
    assert [m["content"] for m in log.window("s1", 10)] == \
        ["tea or coffee?", "[tender] Coffee, then."]
    assert len(log.entries()) == 2                 # still one line of hers

    log.pick("h1", 0)
    row = log.line("h1")
    assert (row["text"], row["turn_id"], row["swipe"]) == ("Tea.", "turn-a", 0)
    assert log.window("s1", 10)[-1]["content"] == "[happy] Tea."


def test_a_reply_still_landing_does_not_borrow_the_last_ones_record(tmp_path):
    """Drawn first, filed a moment later: in between, the line must not carry
    the previous reply's corpus id, or a rating would land on the wrong one."""
    log = ConversationLog(tmp_path)
    exchange(log)
    log.add_swipe("h1", "Coffee, then.")
    row = log.line("h1")
    assert "turn_id" not in row and "raw" not in row


def test_a_pick_out_of_range_changes_nothing(tmp_path):
    log = ConversationLog(tmp_path)
    exchange(log)
    log.pick("h1", 3)
    assert log.line("h1")["text"] == "Tea."


def test_the_replies_survive_compaction(tmp_path):
    log = ConversationLog(tmp_path)
    exchange(log)
    log.add_swipe("h1", "Coffee, then.")
    log.attach_raw("h1", "[tender] Coffee, then.", "turn-b")
    with log._lock:
        log._compact()
    log.pick("h1", 0)
    assert log.line("h1")["turn_id"] == "turn-a"
    log.pick("h1", 1)
    assert log.line("h1")["raw"] == "[tender] Coffee, then."


def test_only_her_last_reply_can_be_asked_again(tmp_path):
    log = ConversationLog(tmp_path)
    exchange(log)
    assert swipeable(log.entries()) == {"h1"}
    assert log.last_exchange()[1]["id"] == "h1"

    log.add({"id": "u2", "role": "user", "text": "tea it is",
             "ts": "2026-11-09T10:01:00", "session_id": "s1"})
    log.attach_raw("u2", None)
    assert swipeable(log.entries()) == set()
    assert log.last_exchange() is None

    # that turn failed — but your line is still on the page under hers, so
    # her reply is not the last word any more than it was a moment ago
    log.unwind("u2")
    assert swipeable(log.entries()) == set()


def test_a_line_she_says_on_her_own_ends_it_and_a_chip_does_not(tmp_path):
    """The newest line *on the page*, not only in the window: a reach-out or a
    photo drawn under her reply is said after it, though no prompt sees it."""
    log = ConversationLog(tmp_path)
    exchange(log)
    log.add({"id": "t1", "role": "tool", "text": "checked the time",
             "ts": "2026-11-09T10:00:06"})
    assert swipeable(log.entries()) == {"h1"}       # a hand she used: not a line
    log.add({"id": "m1", "role": "assistant", "text": "thinking of you",
             "ts": "2026-11-09T10:30:00", "proactive": True})
    assert swipeable(log.entries()) == set()
    assert log.last_exchange() is None


def test_a_newer_exchange_in_another_session_ends_it(tmp_path):
    """A page that opens mints its own session, but the column is one
    conversation: a reply with a newer exchange drawn under it is not the last
    word, whichever session that exchange was in."""
    log = ConversationLog(tmp_path)
    exchange(log, "s1")
    log.add({"id": "u2", "role": "user", "text": "new room, hi",
             "ts": "2026-11-09T11:00:00", "session_id": "s2"})
    log.attach_raw("u2", None)
    log.add({"id": "h2", "role": "assistant", "text": "Hi.",
             "ts": "2026-11-09T11:00:03", "session_id": "s2"})
    log.attach_raw("h2", "Hi.", "turn-c")
    assert swipeable(log.entries()) == {"h2"}
    assert log.last_exchange()[0]["id"] == "u2"


# ---- the whole path, over the real brain -----------------------------------

ASK = "what should I drink"


@pytest.fixture
def room(cfg, seeded_vault, clock, controller):
    cfg = cfg.model_copy(update={
        "vault_dir": seeded_vault, "embed_dim": FakeEmbedder.dim,
        "tools_backend": "off", "mind_enabled": False})
    chat = ScriptedChat([
        ["[happy] ", "Tea?"],
        ["[tender] ", "Coffee, ", "then."],
        ["Mm. ", "Good."],
    ])
    brain = ToolBrain.build(
        cfg, guard=Guard(rates_per_min={}, log_dir=cfg.tool_log_dir, clock=clock),
        timers=TimerBoard(clock), controller=controller, chat_model=chat,
        utility_model=FakeUtility(), embedder=FakeEmbedder())
    app = create_app(cfg, brain=brain)
    return app, brain, chat


def journal_lines(vault) -> list[str]:
    return [line for day in sorted((vault / "memory" / "episodic").glob("*.md"))
            for line in day.read_text(encoding="utf-8").splitlines()
            if ASK in line]


def test_asking_again_replaces_her_reply_everywhere_it_counts(room):
    app, brain, chat = room
    rt = app.state.rt
    vault, corpus = rt.cfg.vault_dir, rt.cfg.corpus_dir
    swipes: list[dict] = []
    publish = rt.hub.publish

    drafts: list[tuple[str, dict]] = []

    def spy(type_, payload, **kw):
        if type_ == "swipe":
            swipes.append(dict(payload))
        if type_ in ("draft", "draft_cancel"):
            drafts.append((type_, dict(payload)))
        return publish(type_, payload, **kw)

    rt.hub.publish = spy
    with TestClient(app) as c:
        first = c.post("/api/chat", json={"text": ASK, "channel": "web"}).json()
        sid, line = first["session_id"], first["message"]["id"]
        assert first["message"]["text"] == "Tea?"
        # a room drew the line before it was filed, so it learns from the bus
        # — not a reload — that the line can be asked again
        assert [(e["id"], e["regenerable"], e["rateable"]) for e in swipes] == \
            [(line, True, True)]
        last = c.get("/api/history").json()["messages"][-1]
        assert last["id"] == line and last["regenerable"] is True
        assert "alts" not in last

        assert drafts and all("again" not in d for _, d in drafts)
        drafts.clear()
        again = c.post("/api/regenerate", json={"id": line})
        assert again.status_code == 200, again.text
        # streamed *into* her line in every room: each draft names it, the
        # first before a word has arrived
        assert drafts[0] == ("draft", {"text": "", "again": line})
        assert drafts[-1] == ("draft", {"text": "Coffee, then.", "again": line})
        assert {t for t, _ in drafts} == {"draft"}
        assert all(d.get("again") == line for _, d in drafts)
        entry = again.json()["message"]
        assert entry["id"] == line                      # the same line, redrawn
        assert (entry["text"], entry["swipe"], entry["swipes"]) == \
            ("Coffee, then.", 1, 2)
        assert "alts" not in entry

        # asked from the same prompt: your line once, her old reply absent
        def said(call):
            return [m for m in call if m["role"] != "system"]
        assert said(chat.calls[1]) == said(chat.calls[0])
        assert not any("Tea?" in str(m.get("content")) for m in chat.calls[1])

        # the next prompt continues from the new reply, and the turn counted once
        assert [m["content"] for m in brain.state.sessions.window(sid, 10)] == \
            [ASK, "[tender] Coffee, then."]
        assert brain.state.sessions.get(sid)["turn_count"] == 1

        # the corpus keeps both; her memory keeps the one that stands
        turns = jsonl(corpus / "turns.jsonl")
        assert [t["completion"] for t in turns] == \
            ["[happy] Tea?", "[tender] Coffee, then."]
        assert turns[0]["turn_index"] == turns[1]["turn_index"]
        assert "again" in turns[1]["tags"]
        lines = journal_lines(vault)
        assert len(lines) == 1 and lines[0].endswith("[tender] Coffee, then.")
        chunk = brain.state.store.index.get(f"turn-{sid}-0")
        assert "Coffee" in chunk.text and "Tea?" not in chunk.text

        # swiping back is a pick, not a generation
        back = c.post("/api/swipe", json={"id": line, "swipe": 0})
        assert back.status_code == 200, back.text
        assert back.json()["message"]["text"] == "Tea?"
        assert len(chat.calls) == 2
        assert journal_lines(vault)[0].endswith("[happy] Tea?")
        assert brain.state.sessions.window(sid, 10)[-1]["content"] == "[happy] Tea?"

        # a thumb belongs to the reply that stands, not to the bubble
        assert c.post("/api/rate", json={"id": line, "thumbs": 1}).status_code == 200
        assert jsonl(corpus / "ratings.jsonl")[-1]["id"] == turns[0]["id"]

        # going on settles it: the kept reply is recorded, and it is no longer
        # the last thing said
        nxt = c.post("/api/chat", json={"text": "tea it is", "channel": "web",
                                         "session_id": sid})
        assert nxt.status_code == 200
        assert jsonl(corpus / "preferences.jsonl") == [{
            **jsonl(corpus / "preferences.jsonl")[0],
            "id": line, "chosen": turns[0]["id"], "rejected": [turns[1]["id"]]}]
        assert c.post("/api/regenerate", json={"id": line}).status_code == 409
        assert c.post("/api/swipe", json={"id": line, "swipe": 1}).status_code == 409
        old = next(m for m in c.get("/api/history").json()["messages"]
                   if m["id"] == line)
        assert "regenerable" not in old

    committed = [s for s in rt.signals.next(0, limit=64)[0]
                 if s.type == "turn_committed"]
    assert [s.payload["reply"] for s in committed] == \
        ["Tea?", "Coffee, then.", "Mm. Good."]


def test_a_redo_that_fails_puts_her_line_back_in_every_room(room):
    app, _brain, chat = room
    rt = app.state.rt
    events: list[tuple[str, dict]] = []
    publish = rt.hub.publish

    def spy(type_, payload, **kw):
        events.append((type_, dict(payload)))
        return publish(type_, payload, **kw)

    rt.hub.publish = spy
    with TestClient(app) as c:
        first = c.post("/api/chat", json={"text": ASK, "channel": "web"}).json()
        line = first["message"]["id"]
        async def breaks(messages, **params):
            yield "Half a "
            raise RuntimeError("model went away")
        chat.stream = breaks
        events.clear()
        assert c.post("/api/regenerate", json={"id": line}).status_code >= 500
        drafts = [e for e in events if e[0].startswith("draft")]
        assert drafts[0] == ("draft", {"text": "", "again": line})
        assert drafts[-1] == ("draft_cancel", {"again": line})
        assert not any(t == "swipe" for t, _ in events)
        # nothing changed: the line, its one reply, still the one to redo
        last = c.get("/api/history").json()["messages"][-1]
        assert (last["text"], last.get("swipes", 1), last["regenerable"]) == \
            ("Tea?", 1, True)


def test_any_line_drawn_under_her_reply_settles_it(cfg):
    """A reach-out arriving under a reply you had her redo settles it like your
    next line would: the control leaves every room, and the reply that stood is
    recorded as the one kept — by standing, not by your choosing."""
    cfg = cfg.model_copy(update={"tools_backend": "off", "mind_enabled": False})
    app = create_app(cfg, brain=FakeBrain())
    rt = app.state.rt
    sessions = SessionStore(rt.cfg.vault_dir, rt.chatlog)
    sid = sessions.create()
    rt.post_message("user", "hi", session_id=sid, channel="web")
    sessions.append_message(sid, "user", "hi")
    her = rt.post_message("assistant", "Hey.", channel="web", session_id=sid)
    sessions.append_message(sid, "assistant", "[happy] Hey.", turn_id="t-1")
    rt.add_swipe(her["id"], "Hello.")
    sessions.revise_last(sid, "Hello.", turn_id="t-2")
    assert rt.swipe_target(her["id"])[2]["swipes"] == 2

    swipes: list[dict] = []
    publish = rt.hub.publish
    rt.hub.publish = lambda t, p, **kw: (swipes.append(dict(p)) if t == "swipe"
                                         else None, publish(t, p, **kw))[1]
    rt.post_message("tool", "looked at the clock", channel="web")
    assert swipes == []                          # a chip is not a line
    rt.post_message("assistant", "thinking of you", proactive=True)
    assert [(s["id"], s["regenerable"]) for s in swipes] == [(her["id"], False)]
    prefs = jsonl(rt.cfg.corpus_dir / "preferences.jsonl")
    assert [(p["chosen"], p["rejected"], p["by"]) for p in prefs] == \
        [("t-2", ["t-1"], "stood")]
    with pytest.raises(NotSwipeable):
        rt.swipe_target(her["id"])


def test_a_reply_that_cannot_be_asked_again_is_refused(cfg):
    cfg = cfg.model_copy(update={"tools_backend": "off", "mind_enabled": False})
    app = create_app(cfg, brain=FakeBrain())
    rt = app.state.rt
    sessions = SessionStore(rt.cfg.vault_dir, rt.chatlog)
    sid = sessions.create()
    you = rt.post_message("user", "hi", session_id=sid, channel="voice")
    sessions.append_message(sid, "user", "hi")
    sessions.append_message(sid, "assistant", "[happy] Hey.", turn_id="t-1")
    her = rt.post_message("assistant", "Hey.", channel="voice", session_id=sid)
    with pytest.raises(NotSwipeable):          # said aloud: not hers to redo
        rt.swipe_target(her["id"])
    with pytest.raises(NotSwipeable):          # your own line
        rt.swipe_target(you["id"])
    with TestClient(app) as c:
        assert c.post("/api/regenerate", json={"id": "nope"}).status_code == 404
        assert c.post("/api/regenerate", json={"id": her["id"]}).status_code == 409
        assert c.post("/api/swipe", json={"id": her["id"], "swipe": 0}).status_code == 409
        assert c.post("/api/swipe", json={"id": her["id"], "swipe": -1}).status_code == 422


# ---- the export -------------------------------------------------------------

def test_the_replies_you_chose_between_export_as_pairs(tmp_path):
    corpus = CorpusLogger(tmp_path)
    ids = [corpus.log_turn(session_id="s", turn_index=0,
                           messages=[{"role": "user", "content": "tea?"}],
                           completion=reply, model="m", card_version="1")
           for reply in ("Tea.", "Coffee.", "Water.")]
    corpus.log_preference("h1", chosen=ids[1], rejected=[ids[0], ids[2]])

    corpus.log_rating(ids[1], 1)
    rows, stats = export(tmp_path, fmt="dpo")
    assert (stats["preferences"], stats["up"]) == (1, 1)
    assert [(r["chosen"][0]["content"], r["rejected"][0]["content"])
            for r in rows] == [("Coffee.", "Tea."), ("Coffee.", "Water.")]
    assert rows[0]["prompt"] == [{"role": "user", "content": "tea?"}]

    raw, _ = export(tmp_path, fmt="raw")
    assert [r.get("preferred") for r in raw] == [False, True, False]


def test_a_later_choice_on_the_same_line_is_the_one_that_stands(tmp_path):
    corpus = CorpusLogger(tmp_path)
    a, b = (corpus.log_turn(session_id="s", turn_index=0, messages=[],
                            completion=reply, model="m", card_version="1")
            for reply in ("A", "B"))
    corpus.log_preference("h1", chosen=a, rejected=[b])
    corpus.log_preference("h1", chosen=b, rejected=[a])
    rows, _ = export(tmp_path, fmt="dpo")
    assert [r["chosen"][0]["content"] for r in rows] == ["B"]
