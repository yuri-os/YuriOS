"""The transcript on disk, and the walk back through it (SPEC §2.6).

The bug these cover: the visible conversation lived in a 200-entry in-memory
ring, so every restart — a config change, a crash, a machine that slept — opened
her room onto a blank column. What you said last night was still in the Vault,
which is her memory; it was simply nowhere on the screen, and "what did we
settle on?" had nothing to scroll back to.

The other half is the walk: a page draws the end of the conversation and one
button at the top asks for the six lines before it, a press at a time.
"""
from __future__ import annotations

import json

import pytest

from yurios.world.chatlog import MAX_ENTRIES, SLACK, ChatLog

pytest.importorskip("fastapi")
from starlette.testclient import TestClient                  # noqa: E402

from yurios.desktop.voice.backends.fakes import FakeBrain    # noqa: E402
from yurios.world.main import create_app                      # noqa: E402


def boot(cfg):
    """One runtime on this vault, the way the daemon builds it. Called twice on
    the same `cfg` it is a restart: the second one is a fresh process's ring."""
    return create_app(cfg, brain=FakeBrain()).state.rt


# ---- the store -------------------------------------------------------------

def test_the_conversation_survives_the_process(tmp_path):
    """The whole point. A fresh process, the same vault, the same words."""
    log = ChatLog(tmp_path)
    log.add({"id": "m1", "role": "user", "text": "what did we call it?"})
    log.add({"id": "m2", "role": "assistant", "text": "the long way round."})
    restored = ChatLog(tmp_path).entries()
    assert [e["text"] for e in restored] == ["what did we call it?",
                                             "the long way round."]


def test_a_restored_line_is_not_a_downgraded_one(tmp_path):
    """The entry goes down verbatim, so the card, the picture and the tag all
    come back with it — a report you never opened is still a report (§18.2a)."""
    log = ChatLog(tmp_path)
    log.add({"id": "r1", "role": "assistant", "proactive": True,
             "text": "I read the tape while you were out.",
             "report_path": "reports/market-brief/2026-08-20.md",
             "report_title": "Overnight market brief"})
    row = ChatLog(tmp_path).entries()[0]
    assert row["proactive"] is True
    assert row["report_title"] == "Overnight market brief"


def test_an_entry_with_no_id_is_not_filed(tmp_path):
    """The id is the dedup key a client resolves live-and-backfill by. A row
    without one cannot be reconciled with anything, so it is not written."""
    log = ChatLog(tmp_path)
    log.add({"role": "user", "text": "nowhere to file this"})
    assert ChatLog(tmp_path).entries() == []


def test_a_torn_tail_line_is_skipped_not_fatal(tmp_path):
    """Appends are not fsynced on purpose — this is the draw buffer for a chat
    column, not memory — so a crash can leave half a line. It costs that line
    and nothing else."""
    log = ChatLog(tmp_path)
    log.add({"id": "m1", "text": "whole"})
    with open(log.path, "a", encoding="utf-8") as f:
        f.write('{"id": "m2", "text": "half a li')
    assert [e["id"] for e in ChatLog(tmp_path).entries()] == ["m1"]


def test_the_archive_has_a_floor(tmp_path):
    """Past the cap the oldest lines fall off. The corpus is the archive; this
    is how far back the column can be scrolled."""
    log = ChatLog(tmp_path)
    for i in range(MAX_ENTRIES + SLACK + 5):
        log.add({"id": f"m{i}", "text": f"line {i}"})
    entries = ChatLog(tmp_path).entries()
    assert len(entries) <= MAX_ENTRIES + SLACK          # compacted, not unbounded
    assert entries[-1]["id"] == f"m{MAX_ENTRIES + SLACK + 4}"   # the newest kept
    assert entries[0]["id"] != "m0"                     # …and the oldest let go


def test_compaction_does_not_happen_once_per_message(tmp_path):
    """A rewrite costs a full read and write; the slack is what keeps it off
    the path every committed line takes."""
    log = ChatLog(tmp_path)
    for i in range(MAX_ENTRIES + 10):
        log.add({"id": f"m{i}", "text": "x"})
    with open(log.path, encoding="utf-8") as f:
        assert sum(1 for _ in f) == MAX_ENTRIES + 10   # still over the cap


def test_the_log_is_untracked(tmp_path):
    """It changes on every sentence either of you says, and the words are
    already in the corpus and the journal. Committing it would put one commit
    per turn in the diary (world/chatlog.py's header)."""
    log = ChatLog(tmp_path)
    log.add({"id": "m1", "text": "hi"})
    assert "transcript.jsonl" in (tmp_path / "state" / ".gitignore").read_text()


def test_it_shares_the_ignore_file_with_the_inbox(tmp_path):
    """Both write into `state/.gitignore`; whichever gets there second must
    append rather than clobber."""
    from yurios.world.inbox import Inbox
    Inbox(tmp_path).add({"id": "i1", "ts": "2026-08-21T04:00:00", "text": "hey"})
    ChatLog(tmp_path).add({"id": "m1", "text": "hi"})
    ignored = (tmp_path / "state" / ".gitignore").read_text()
    assert "inbox.json" in ignored and "transcript.jsonl" in ignored


def test_no_vault_is_a_working_no_op(tmp_path):
    """Losing the scrollback is never a reason to fail a turn."""
    log = ChatLog(None)
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
