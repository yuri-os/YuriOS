"""The conversation on disk, and the walk back through it (SPEC §2.6, §7.1).

The first bug these cover: the visible conversation lived in a 200-entry
in-memory ring, so every restart — a config change, a crash, a machine that
slept — opened her room onto a blank column. What you said last night was still
in the Vault, which is her memory; it was simply nowhere on the screen, and
"what did we settle on?" had nothing to scroll back to.

The second is the fix's own: the file that solved it was a *second* per-message
store, beside the `transcript[]` `sessions.json` already kept for the §7.1
window. One line, two files, two chances to disagree. They are one row now,
carrying both columns — what the page drew and what the model produced — and the
tests below pin both readers to it.

The third is the walk: a page draws the end of the conversation and one button
at the top asks for the six lines before it, a press at a time.
"""
from __future__ import annotations

import json

import pytest

from yurios.app.conversation import MAX_ENTRIES, SLACK, ConversationLog

pytest.importorskip("fastapi")
from starlette.testclient import TestClient                  # noqa: E402

from yurios.desktop.voice.backends.fakes import FakeBrain    # noqa: E402
from yurios.app.sessions import SessionStore                   # noqa: E402
from yurios.world.main import RING_SIZE, create_app            # noqa: E402


def boot(cfg):
    """One runtime on this vault, the way the daemon builds it. Called twice on
    the same `cfg` it is a restart: the second one is a fresh process's ring."""
    return create_app(cfg, brain=FakeBrain()).state.rt


# ---- the store -------------------------------------------------------------

def test_the_conversation_survives_the_process(tmp_path):
    """The whole point. A fresh process, the same vault, the same words."""
    log = ConversationLog(tmp_path)
    log.add({"id": "m1", "role": "user", "text": "what did we call it?"})
    log.add({"id": "m2", "role": "assistant", "text": "the long way round."})
    restored = ConversationLog(tmp_path).entries()
    assert [e["text"] for e in restored] == ["what did we call it?",
                                             "the long way round."]


def test_a_restored_line_is_not_a_downgraded_one(tmp_path):
    """The entry goes down verbatim, so the card, the picture and the tag all
    come back with it — a report you never opened is still a report (§18.2a)."""
    log = ConversationLog(tmp_path)
    log.add({"id": "r1", "role": "assistant", "proactive": True,
             "text": "I read the tape while you were out.",
             "report_path": "reports/market-brief/2026-08-20.md",
             "report_title": "Overnight market brief"})
    row = ConversationLog(tmp_path).entries()[0]
    assert row["proactive"] is True
    assert row["report_title"] == "Overnight market brief"


def test_an_entry_with_no_id_is_not_filed(tmp_path):
    """The id is the dedup key a client resolves live-and-backfill by. A row
    without one cannot be reconciled with anything, so it is not written."""
    log = ConversationLog(tmp_path)
    log.add({"role": "user", "text": "nowhere to file this"})
    assert ConversationLog(tmp_path).entries() == []


def test_a_torn_tail_line_is_skipped_not_fatal(tmp_path):
    """Appends are not fsynced on purpose — this is the draw buffer for a chat
    column, not memory — so a crash can leave half a line. It costs that line
    and nothing else."""
    log = ConversationLog(tmp_path)
    log.add({"id": "m1", "text": "whole"})
    with open(log.path, "a", encoding="utf-8") as f:
        f.write('{"id": "m2", "text": "half a li')
    assert [e["id"] for e in ConversationLog(tmp_path).entries()] == ["m1"]


def test_the_archive_has_a_floor(tmp_path):
    """Past the cap the oldest lines fall off. The corpus is the archive; this
    is how far back the column can be scrolled."""
    log = ConversationLog(tmp_path)
    for i in range(MAX_ENTRIES + SLACK + 5):
        log.add({"id": f"m{i}", "text": f"line {i}"})
    entries = ConversationLog(tmp_path).entries()
    assert len(entries) <= MAX_ENTRIES + SLACK          # compacted, not unbounded
    assert entries[-1]["id"] == f"m{MAX_ENTRIES + SLACK + 4}"   # the newest kept
    assert entries[0]["id"] != "m0"                     # …and the oldest let go


def test_compaction_does_not_happen_once_per_message(tmp_path):
    """A rewrite costs a full read and write; the slack is what keeps it off
    the path every committed line takes."""
    log = ConversationLog(tmp_path)
    for i in range(MAX_ENTRIES + 10):
        log.add({"id": f"m{i}", "text": "x"})
    with open(log.path, encoding="utf-8") as f:
        assert sum(1 for _ in f) == MAX_ENTRIES + 10   # still over the cap


def test_the_log_is_untracked(tmp_path):
    """It changes on every sentence either of you says, and the words are
    already in the corpus and the journal. Committing it would put one commit
    per turn in the diary (app/conversation.py's header)."""
    log = ConversationLog(tmp_path)
    log.add({"id": "m1", "text": "hi"})
    assert "conversation.jsonl" in (tmp_path / "state" / ".gitignore").read_text()


def test_it_shares_the_ignore_file_with_the_inbox(tmp_path):
    """Both write into `state/.gitignore`; whichever gets there second must
    append rather than clobber."""
    from yurios.world.inbox import Inbox
    Inbox(tmp_path).add({"id": "i1", "ts": "2026-08-21T04:00:00", "text": "hey"})
    ConversationLog(tmp_path).add({"id": "m1", "text": "hi"})
    ignored = (tmp_path / "state" / ".gitignore").read_text()
    assert "inbox.json" in ignored and "conversation.jsonl" in ignored


def test_no_vault_is_a_working_no_op(tmp_path):
    """Losing the scrollback is never a reason to fail a turn."""
    log = ConversationLog(None)
    log.add({"id": "m1", "text": "hi"})
    assert log.entries() == [] and log.active is False


# ---- the ring that wakes up holding it -------------------------------------

def test_the_ring_wakes_up_holding_the_end_of_the_last_conversation(cfg):
    first = boot(cfg)
    first.post_message("user", "did the parcel come?")
    first.post_message("assistant", "it did — it's by the door.")

    second = boot(cfg)          # a restart, same vault
    assert [m["text"] for m in second.transcript] == ["did the parcel come?",
                                                      "it did — it's by the door."]


def test_a_restored_line_can_still_be_read_out(cfg):
    """§9.11 resolves a replay out of the transcript by id, so a line the ring
    only has because it was restored has to resolve like any other."""
    first = boot(cfg)
    said = first.post_message("assistant", "it's by the door.")
    second = boot(cfg)
    assert second.spoken_line(said["id"]) == "it's by the door."


def test_a_line_the_log_never_got_is_still_on_screen(cfg, monkeypatch):
    """The log is best-effort; the ring is not. A failed write must not take a
    line off the column it is already on."""
    rt = boot(cfg)
    rt.post_message("user", "the one that landed")
    monkeypatch.setattr(rt.chatlog, "add", lambda entry: None)
    rt.post_message("assistant", "the one that didn't")
    assert [m["text"] for m in rt.history()["messages"]] == ["the one that landed",
                                                            "the one that didn't"]


# ---- the walk back ---------------------------------------------------------

@pytest.fixture
def talked(cfg):
    """Twenty lines of conversation, oldest first."""
    rt = boot(cfg)
    for i in range(20):
        rt.post_message("user" if i % 2 == 0 else "assistant", f"line {i}")
    return rt


def test_a_page_opens_on_the_end_of_it(talked):
    window = talked.history(limit=6)
    assert [m["text"] for m in window["messages"]] == [f"line {i}" for i in range(14, 20)]
    assert window["has_more"] is True


def test_the_button_walks_back_six_at_a_time(talked):
    page = talked.history(limit=6)
    older = talked.history(limit=6, before=page["messages"][0]["id"])
    assert [m["text"] for m in older["messages"]] == [f"line {i}" for i in range(8, 14)]
    assert older["has_more"] is True


def test_the_walk_stops_at_the_floor(talked):
    anchor = talked.history(limit=6)["messages"][0]["id"]
    seen = []
    while True:
        page = talked.history(limit=6, before=anchor)
        seen = page["messages"] + seen
        if not page["has_more"]:
            break
        anchor = page["messages"][0]["id"]
    assert [m["text"] for m in seen] == [f"line {i}" for i in range(14)]


def test_an_anchor_the_log_no_longer_holds_is_the_floor(talked):
    """An id compacted off the end — or an inbox row older than the log file
    itself — answers the same as reaching the bottom: nothing, and no more."""
    assert talked.history(limit=6, before="nosuchid") == {"messages": [],
                                                          "has_more": False}


def test_the_walk_is_not_a_dump(talked):
    """A client cannot turn the route into a transcript export."""
    assert len(talked.history(limit=10_000)["messages"]) <= 20


# ---- over the wire ---------------------------------------------------------

@pytest.fixture
def client(cfg):
    cfg = cfg.model_copy(update={"tools_backend": "off", "mind_enabled": False})
    app = create_app(cfg, brain=FakeBrain())
    with TestClient(app) as c:
        c.app = app
        yield c


def test_the_route_serves_the_end_and_then_the_walk(client):
    rt = client.app.state.rt
    for i in range(12):
        rt.post_message("user", f"line {i}")

    page = client.get("/api/history", params={"limit": 6}).json()
    assert [m["text"] for m in page["messages"]] == [f"line {i}" for i in range(6, 12)]
    assert page["has_more"] is True

    older = client.get("/api/history",
                       params={"limit": 6, "before": page["messages"][0]["id"]}).json()
    assert [m["text"] for m in older["messages"]] == [f"line {i}" for i in range(6)]
    assert older["has_more"] is False


def test_the_route_refuses_a_nonsense_anchor(client):
    """`before` is a transcript id, and the shape of one is known."""
    assert client.get("/api/history", params={"before": "../../etc"}).status_code == 422
    assert client.get("/api/history", params={"limit": 0}).status_code == 422


def test_a_page_that_asks_for_nothing_in_particular_gets_the_end(client):
    rt = client.app.state.rt
    rt.post_message("assistant", "still here.")
    body = client.get("/api/history").json()
    assert body["messages"][-1]["text"] == "still here."
    assert body["has_more"] is False


def test_what_lands_on_disk_is_what_the_route_serves(client, tmp_path):
    rt = client.app.state.rt
    rt.post_message("user", "on the record")
    lines = [json.loads(line) for line in
             rt.chatlog.path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert lines[-1]["text"] == "on the record"
    assert lines[-1]["id"] == client.get("/api/history").json()["messages"][-1]["id"]


# ---- one line, two readers -------------------------------------------------

def test_a_turn_is_one_row_holding_both_versions_of_it(tmp_path):
    """The whole point of the merge. The page drew the sentences she spoke; the
    model must see what it actually produced, tags and narration intact. Those
    are two fields of one line, not two files."""
    log = ConversationLog(tmp_path)
    store = SessionStore(tmp_path, log)
    sid = store.create()

    log.add({"id": "a1", "role": "assistant", "text": "It's by the door.",
             "ts": "2026-08-25T09:00:00", "session_id": sid})      # what was drawn
    store.append_message(sid, "assistant", "[warm] *she nods* It's by the door.",
                         turn_id="t1")                             # what was made

    rows = log.entries()
    assert len(rows) == 1                       # one line, not two
    assert rows[0]["text"] == "It's by the door."
    assert store.window(sid, 10) == [
        {"role": "assistant", "content": "[warm] *she nods* It's by the door.",
         "ts": "2026-08-25T09:00:00"}]
    assert rows[0]["turn_id"] == "t1"           # …and the corpus join survives


def test_a_line_that_reads_the_same_both_ways_stores_it_once(tmp_path):
    """A user line without a picture note is identical on both sides. Storing
    the redundant copy would be paying bytes to say the same thing twice."""
    log = ConversationLog(tmp_path)
    store = SessionStore(tmp_path, log)
    sid = store.create()
    log.add({"id": "u1", "role": "user", "text": "did the parcel come?",
             "ts": "2026-08-25T09:00:00", "session_id": sid})
    store.append_message(sid, "user", "did the parcel come?")

    assert "raw" not in log.entries()[0]
    assert store.window(sid, 10)[0]["content"] == "did the parcel come?"


def test_the_greeting_arrives_in_the_other_order_and_is_still_one_line(tmp_path):
    """Her greeting reaches the window before it reaches the page — the brain
    appends its text, then the runtime posts it. Whichever half lands second has
    to find the row, or the same sentence is drawn twice."""
    log = ConversationLog(tmp_path)
    store = SessionStore(tmp_path, log)
    sid = store.create()

    store.append_message(sid, "assistant", "*settling* You found the signal.")
    assert log.entries() == []                  # nothing has drawn it yet

    row = log.undrawn(sid, "assistant")
    log.attach_drawn(row["id"], {"role": "assistant", "text": "You found the signal.",
                                 "ts": "2026-08-25T09:00:00", "proactive": True})

    drawn = log.entries()
    assert len(drawn) == 1
    assert drawn[0]["text"] == "You found the signal."
    assert store.window(sid, 10)[0]["content"] == "*settling* You found the signal."


def test_what_is_drawn_is_not_what_is_prompted_with(tmp_path):
    """Far more reaches the page than reaches the window: a murmur, a selfie, a
    digest, a mind reach-out. Membership is admitted by `append_message` and
    never inferred, or the first line written with a session id would quietly
    widen the next prompt (§9.9)."""
    log = ConversationLog(tmp_path)
    store = SessionStore(tmp_path, log)
    sid = store.create()
    log.add({"id": "m1", "role": "assistant", "text": "*to herself* mm.",
             "ts": "2026-08-25T09:00:00", "session_id": sid, "proactive": True})

    assert [r["text"] for r in log.entries()] == ["*to herself* mm."]
    assert store.window(sid, 10) == []


def test_a_barged_in_turn_leaves_the_window_and_stays_on_the_page(tmp_path):
    """§4.4: a turn that didn't happen must not leave an unanswered question for
    the next prompt. But you did say it, and a chat that quietly deletes what
    you typed is lying about what happened."""
    log = ConversationLog(tmp_path)
    store = SessionStore(tmp_path, log)
    sid = store.create()
    log.add({"id": "u1", "role": "user", "text": "actually, wait—",
             "ts": "2026-08-25T09:00:00", "session_id": sid})
    store.append_message(sid, "user", "actually, wait—")

    assert store.drop_last(sid, "user") is True
    assert store.window(sid, 10) == []                     # out of the window
    assert [r["text"] for r in log.entries()] == ["actually, wait—"]   # still drawn


def test_a_rollback_only_undoes_the_last_thing_in_the_window(tmp_path):
    """The old `drop_last`'s guard, kept: it undoes a line only when that line
    is what the window ends with."""
    log = ConversationLog(tmp_path)
    store = SessionStore(tmp_path, log)
    sid = store.create()
    store.append_message(sid, "user", "did it come?")
    store.append_message(sid, "assistant", "it did.")
    assert store.drop_last(sid, "user") is False
    assert len(store.window(sid, 10)) == 2


def test_build_one_writes_lines_nobody_draws(tmp_path):
    """`/api/chat` has no chat column to post to, so its lines are admitted to
    the window and never drawn. They must not turn up in somebody's transcript."""
    log = ConversationLog(tmp_path)
    store = SessionStore(tmp_path, log)
    sid = store.create()
    store.append_message(sid, "user", "hello?")
    assert log.entries() == []                             # nothing to draw
    assert store.window(sid, 10)[0]["content"] == "hello?"


# ---- the upgrade path ------------------------------------------------------

def test_it_adopts_a_conversation_that_predates_the_log(tmp_path):
    """Every vault written before this file has its conversation in
    `sessions.json`. Dropping those on upgrade would be the same bug the log
    exists to fix, one layer down."""
    state = tmp_path / "state"
    state.mkdir(parents=True)
    (state / "sessions.json").write_text(json.dumps({"sessions": {
        "a" * 32: {"created": "x", "last_active": "x", "turn_count": 1,
                   "transcript": [
                       {"role": "user", "content": "morning",
                        "ts": "2026-08-21T09:00:00+00:00"},
                       {"role": "assistant", "content": "morning yourself.",
                        "ts": "2026-08-21T09:00:04+00:00", "turn_id": "t1"}]},
        "b" * 32: {"created": "x", "last_active": "x", "turn_count": 1,
                   "transcript": [
                       {"role": "user", "content": "still up?",
                        "ts": "2026-08-20T23:00:00+00:00"}]},
    }}), encoding="utf-8")

    store = SessionStore(tmp_path)
    # merged by timestamp across sessions, so the column reads in the order it
    # was actually said rather than session by session
    assert [r["text"] for r in store.log.entries()] == [
        "still up?", "morning", "morning yourself."]
    assert store.window("a" * 32, 10)[0]["content"] == "morning"
    assert store.window("b" * 32, 10)[0]["content"] == "still up?"
    # …and the array it came from is emptied, so it cannot be adopted twice
    assert "transcript" not in json.loads(
        (state / "sessions.json").read_text())["sessions"]["a" * 32]


def test_adoption_is_idempotent(tmp_path):
    """The ids are derived from the session and the line's place in it, so a
    second run files nothing even if the array somehow comes back."""
    state = tmp_path / "state"
    state.mkdir(parents=True)
    rows = {"sessions": {"a" * 32: {"turn_count": 0, "transcript": [
        {"role": "user", "content": "again", "ts": "2026-08-21T09:00:00+00:00"}]}}}
    (state / "sessions.json").write_text(json.dumps(rows), encoding="utf-8")
    SessionStore(tmp_path)
    (state / "sessions.json").write_text(json.dumps(rows), encoding="utf-8")
    store = SessionStore(tmp_path)
    assert len(store.log.entries()) == 1


# ---- the walk back, read back out ------------------------------------------

def test_a_line_the_walk_paged_in_can_still_be_read_out(cfg):
    """§9.11 draws a speaker button on every line of hers the page shows, and
    §2.6 lets the column walk back past the ring. A line older than the ring
    resolved to nothing, so the button was drawn exactly where it could not
    work."""
    rt = boot(cfg)
    said = rt.post_message("assistant", "the long way round.")
    for i in range(RING_SIZE + 5):              # push it off the ring
        rt.post_message("user", f"filler {i}")
    assert said["id"] not in {m["id"] for m in rt.transcript}
    assert rt.spoken_line(said["id"]) == "the long way round."


def test_an_adopted_line_of_hers_is_drawn_as_a_line_not_as_tokens(tmp_path):
    """The old window store kept the model's own output, so her adopted lines
    arrive with `[tags]` and `*narration*` in them — and the column has never
    shown those. The page gets the line; the window keeps the tokens."""
    state = tmp_path / "state"
    state.mkdir(parents=True)
    (state / "sessions.json").write_text(json.dumps({"sessions": {"a" * 32: {
        "turn_count": 1, "transcript": [
            {"role": "assistant", "ts": "2026-08-21T09:00:00+00:00",
             "content": "[warm] *she looks up* It's by the door."}]}}}),
        encoding="utf-8")

    store = SessionStore(tmp_path)
    assert store.log.entries()[0]["text"] == "It's by the door."
    assert store.window("a" * 32, 10)[0]["content"] == \
        "[warm] *she looks up* It's by the door."


def test_the_old_chat_column_is_adopted_too(tmp_path):
    """`transcript.jsonl` held lines no window ever had — the greetings, the
    reach-outs. Dropping them on upgrade would lose exactly the scrollback the
    file was added to keep."""
    state = tmp_path / "state"
    state.mkdir(parents=True)
    (state / "transcript.jsonl").write_text(json.dumps({
        "id": "gr330001", "role": "assistant", "proactive": True,
        "text": "You found the signal.", "ts": "2026-08-21T08:59:00"}) + "\n",
        encoding="utf-8")

    log = ConversationLog(tmp_path)
    row = log.entries()[0]
    assert row["text"] == "You found the signal." and row["proactive"] is True
    assert log.window("a" * 32, 10) == []       # drawn, never prompted with
    assert not (state / "transcript.jsonl").exists()   # and the source retired


def test_the_two_old_stores_come_back_as_one_conversation(tmp_path):
    """Both at once, merged by timestamp — or the column comes back with the
    greeting stranded after the evening it opened."""
    state = tmp_path / "state"
    state.mkdir(parents=True)
    (state / "transcript.jsonl").write_text(json.dumps({
        "id": "gr330001", "role": "assistant", "text": "You found the signal.",
        "ts": "2026-08-21T08:59:00"}) + "\n", encoding="utf-8")
    (state / "sessions.json").write_text(json.dumps({"sessions": {"a" * 32: {
        "turn_count": 1, "transcript": [
            {"role": "user", "content": "morning", "ts": "2026-08-21T09:00:00"}]}}}),
        encoding="utf-8")

    assert [r["text"] for r in ConversationLog(tmp_path).entries()] == [
        "You found the signal.", "morning"]


def test_it_repairs_a_log_that_was_adopted_before_drawing_existed(tmp_path):
    """The build between the two stores and this one migrated `sessions.json`
    into the log and marked the rows for the window only. The result was every
    line in every prompt and not one on the page: the column the migration was
    supposed to save came up empty. Booting on it puts them back."""
    state = tmp_path / "state"
    state.mkdir(parents=True)
    (state / "conversation.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"id": "x1", "role": "user", "text": "did the parcel come?",
         "ts": "2026-08-24T09:00:00", "session_id": "a" * 32, "w": 1},
        {"id": "x2", "role": "assistant", "ts": "2026-08-24T09:00:04",
         "text": "[warm] *she looks up* it's by the door.",
         "session_id": "a" * 32, "w": 1},
    ]) + "\n", encoding="utf-8")

    log = ConversationLog(tmp_path)
    assert [r["text"] for r in log.entries()] == ["did the parcel come?",
                                                  "it's by the door."]
    # …and the window still gets the tokens it always had
    assert [m["content"] for m in log.window("a" * 32, 10)] == [
        "did the parcel come?", "[warm] *she looks up* it's by the door."]


def test_the_repair_leaves_a_healthy_log_alone(tmp_path):
    """It keys on a log where nothing was ever drawn, which no working version
    produces — so a normal one is not rewritten under it."""
    log = ConversationLog(tmp_path)
    log.add({"id": "m1", "role": "assistant", "text": "[warm] hello",
             "ts": "2026-08-24T09:00:00"})
    assert ConversationLog(tmp_path).entries()[0]["text"] == "[warm] hello"
