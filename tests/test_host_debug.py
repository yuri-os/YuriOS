"""/api/characters/{id}/debug/* — the mind debug page's read API (SPEC §24.3).

Two properties matter more than any individual payload here. First, every one
of these reads files, so a character who is stopped — or crashed, which is when
you actually want to look — is still fully inspectable. Second, they must be
declared *above* the runtime dispatcher mount, or they silently fall through to
the child app; the first test catches that by asking with nothing running.

This file also backfills `/journal`, `/log` and `/context-history`, which the
dashboard has shipped against since the drawer existed and which had no tests.
"""
from __future__ import annotations

import json
import subprocess

import pytest
from fastapi.testclient import TestClient

from yurios.characters import CharacterRegistry
from yurios.world.config import Config
from yurios.world.host import create_host_app

from .test_host import fake_character_app, record


# --- fixtures -----------------------------------------------------------------

def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def seed(rec, *, ticks=3, calls=1, prompts=1):
    """A character with a little of everything on disk."""
    write_jsonl(rec.paths.traces / "ticks.jsonl", [
        {"tick_id": f"t-{i}", "ts": f"2026-08-0{i + 1}T10:00:00",
         "activity_state": "IDLE" if i % 2 else "ENGAGED",
         "sensed": [{"type": "user_message", "id": f"sig-{i}"}],
         "appraised": [], "decided": {"intention": "REST", "runners_up": []},
         "acted": {"what": None, "result": "rest"}, "interrupt": {}}
        for i in range(ticks)])
    write_jsonl(rec.paths.traces / "activity.jsonl", [
        {"ts": "2026-08-01T09:00:00", "at": 1.0, "from": None, "to": "IDLE",
         "reason": "boot", "cadence_s": 60.0},
        {"ts": "2026-08-01T09:05:00", "at": 2.0, "from": "IDLE", "to": "ENGAGED",
         "reason": "user_turn", "cadence_s": 2.0}])
    write_jsonl(rec.paths.traces / "signals.jsonl", [
        {"id": "sig-0", "type": "user_message", "ts": "2026-08-01T10:00:00",
         "payload": {"text": "hi"}, "source": "voice"},
        {"id": "sig-9", "type": "timer", "ts": "2026-08-01T10:01:00",
         "payload": {"label": "tea"}, "source": "host"}])
    write_jsonl(rec.paths.traces / "context.jsonl", [
        {"timestamp": "2026-08-01T10:00:00", "source": "usage", "used": 900,
         "limit": 8000, "pct": 0.11}])
    write_jsonl(rec.paths.tool_logs / "calls.jsonl", [
        {"ts": 1786021010.0, "call_id": f"call-{i}", "tool": "take_selfie",
         "args": {"look": "cozy"}, "verdict": "ok", "duration_ms": 800.0,
         "result": '{"status": "started"}', "corr_id": "c-abc",
         "origin": "chat_turn", "session_id": "s-1", "turn_index": 2,
         "tick_id": "t-0"}
        for i in range(calls)])
    write_jsonl(rec.paths.selfies / "generations.jsonl", [
        {"image": "shot.png", "backend": "fake", "model": "m", "seed": 7,
         "prompt": "a cozy photo", "created_at": "2026-08-01T10:00:05",
         "corr_id": "c-abc", "selfie_id": "sf-1"}])
    write_jsonl(rec.paths.traces / "prompts.jsonl", [
        {"id": f"pr-{i}", "ts": f"2026-08-0{i + 1}T11:00:00", "at": float(i),
         "kind": "ambient", "corr_id": "c-abc", "tick_id": "t-0",
         "session_id": None, "turn_index": None, "model": "m",
         "messages": [{"role": "system", "content": "you are her"},
                      {"role": "user", "content": "((murmur))"}],
         "messages_ref": None, "completion": "mm.", "n_messages": 2,
         "tokens_in": 8, "tokens_out": 2, "truncated": False}
        for i in range(prompts)])
    write_jsonl(rec.paths.corpus / "utility.jsonl", [
        {"timestamp": "2026-08-01T10:00:00", "kind": "extract", "applied": True,
         "quarantined": False},
        {"timestamp": "2026-08-01T10:01:00", "kind": "summarise",
         "applied": False, "quarantined": True}])
    episodic = rec.paths.vault / "memory" / "episodic"
    episodic.mkdir(parents=True, exist_ok=True)
    (episodic / "2026-08-01.md").write_text(
        "### 10:00  you: hi  ⇄  yuri: hello\n### 10:05  [she] tidied the shelf\n",
        encoding="utf-8")
    (rec.paths.vault / "state").mkdir(parents=True, exist_ok=True)
    (rec.paths.vault / "state" / "activity.json").write_text(
        '{"state": "IDLE", "cadence_s": 60.0, "last_user_msg": 1.0}',
        encoding="utf-8")
    return rec


def host(tmp_path, monkeypatch, *, running: bool):
    registry = CharacterRegistry(tmp_path)
    rec = record(tmp_path, "yuri", enabled=running)
    registry.add(rec)
    monkeypatch.setattr("yurios.world.host.hosting.create_app", fake_character_app)
    app = create_host_app(Config(data_dir=tmp_path), registry)
    client = TestClient(app)
    client.record = rec
    return client


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A character who is NOT running. That is the load-bearing case: `/api/mind`
    answers 503 without a loop, which is precisely when you want to read what
    happened, so every view here has to come off disk."""
    with host(tmp_path, monkeypatch, running=False) as c:
        yield c


@pytest.fixture
def live_client(tmp_path, monkeypatch):
    with host(tmp_path, monkeypatch, running=True) as c:
        yield c


def get(client, route, **params):
    """`route`, not `path` — `path=` is a query parameter on several of these."""
    response = client.get(f"/api/characters/yuri/debug{route}", params=params)
    assert response.status_code == 200, response.text
    return response.json()


# --- the two properties that matter --------------------------------------------

ALL = ["/overview", "/activity", "/ticks", "/signals", "/goals", "/self-edits",
       "/calls", "/selfies", "/prompts", "/prompts/days", "/economics",
       "/utility", "/memory", "/memory/chunks", "/vault/commits"]


def test_every_view_reads_disk_with_nothing_running(client):
    """Also catches the route being declared *below* the dispatcher mount: a
    fall-through would reach the child app and 404 instead."""
    seed(client.record)
    for path in ALL:
        assert client.get(f"/api/characters/yuri/debug{path}").status_code == 200, path


def test_a_character_who_never_ran_is_empty_not_broken(client):
    """No traces, no corpus, no vault. Every list answers with an empty page."""
    for path in ALL:
        body = get(client, path)
        if "items" in body:
            assert body["items"] == [], path


def test_an_unknown_character_is_still_a_404(client):
    assert client.get("/api/characters/nobody/debug/overview").status_code == 404


def test_the_host_never_claims_the_runtimes_mind_namespace(client):
    """Why the read API is `debug/` and not `mind/`: the dispatcher rewrites
    `/api/characters/{id}/<rest>` to the child app's `/api/<rest>`, so the
    sanctuary's inner-life panel already lives at `…/{id}/mind`. A host route
    declared there would take precedence over the mount and silently shadow a
    working surface with differently-shaped disk reads."""
    claimed = {r.path for r in client.app.routes
               if getattr(r, "path", "").startswith("/api/characters/{character_id}/mind")}
    assert claimed == set()


# --- the timeline ---------------------------------------------------------------

def test_the_activity_timeline_reads_her_own_log(client):
    seed(client.record)
    body = get(client, "/activity")
    assert [(r["from"], r["to"]) for r in body["items"]] \
        == [("IDLE", "ENGAGED"), (None, "IDLE")], "newest first"
    assert body["current"]["state"] == "IDLE"


# --- ticks ----------------------------------------------------------------------

def test_ticks_page_newest_first_without_overlap(client):
    write_jsonl(client.record.paths.traces / "ticks.jsonl", [
        {"tick_id": f"t-{i}", "ts": "2026-08-01T10:00:00", "activity_state": "IDLE",
         "sensed": [], "appraised": [], "decided": {}, "acted": {}, "interrupt": {}}
        for i in range(55)])
    seen = []
    for page in range(3):
        body = get(client, "/ticks", page=page, limit=25)
        seen += [r["tick_id"] for r in body["items"]]
        assert body["has_more"] is (page < 2)
        assert body["total"] == 55
    assert seen == [f"t-{i}" for i in range(54, -1, -1)]
    assert len(set(seen)) == 55


def test_ticks_can_be_filtered_by_state(client):
    seed(client.record, ticks=4)
    body = get(client, "/ticks", state="IDLE")
    assert body["items"] and all(r["activity_state"] == "IDLE" for r in body["items"])
    assert body["total"] is None, "a filtered count would mean a full pass"


def test_a_tick_detail_joins_what_it_caused(client):
    """The join the correlation id exists for."""
    seed(client.record)
    body = get(client, "/ticks/t-0")
    assert body["tick"]["tick_id"] == "t-0"
    assert [c["tool"] for c in body["calls"]] == ["take_selfie"]
    assert [p["kind"] for p in body["prompts"]] == ["ambient"]
    assert [s["id"] for s in body["signals"]] == ["sig-0"], "only what it sensed"


def test_a_tick_that_sensed_nothing_joins_nothing(client):
    """The overwhelmingly common tick: REST, empty inbox. It must still open."""
    write_jsonl(client.record.paths.traces / "ticks.jsonl", [
        {"tick_id": "t-quiet", "ts": "2026-08-01T10:00:00", "activity_state": "DORMANT",
         "sensed": [], "appraised": [], "decided": {"intention": "REST"},
         "acted": {"what": None, "result": "rest"}, "interrupt": {}}])
    body = get(client, "/ticks/t-quiet")
    assert body["signals"] == [] and body["calls"] == [] and body["prompts"] == []


def test_an_unknown_tick_is_a_404(client):
    seed(client.record)
    assert client.get("/api/characters/yuri/debug/ticks/t-nope").status_code == 404


# --- tools and photos -----------------------------------------------------------

def test_a_tool_call_carries_the_photo_it_produced(client):
    """A render lands minutes after the sentence that asked for it; without the
    corr_id the only way back is the clock."""
    seed(client.record)
    call = get(client, "/calls")["items"][0]
    assert call["corr_id"] == "c-abc" and call["origin"] == "chat_turn"
    assert call["selfie"]["image"] == "shot.png"
    assert call["selfie"]["url"] == "/api/characters/yuri/selfies/shot.png"
    assert call["selfie"]["prompt"] == "a cozy photo"


def test_calls_filter_by_correlation_id(client):
    seed(client.record)
    assert get(client, "/calls", corr_id="c-abc")["items"]
    assert get(client, "/calls", corr_id="c-nope")["items"] == []


# --- context windows ------------------------------------------------------------

def test_the_prompt_index_omits_the_expensive_field(client):
    seed(client.record)
    row = get(client, "/prompts")["items"][0]
    assert "messages" not in row, "a page of 25 must not carry 25 whole prompts"
    assert row["has_messages"] is True
    assert row["preview"] == "((murmur))"


def test_the_prompt_detail_carries_the_whole_context_window(client):
    seed(client.record)
    body = get(client, "/prompts/pr-0")
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert body["completion"] == "mm."


def test_a_chat_turn_detail_resolves_its_pointer_into_the_corpus(client):
    """Chat turns keep their body in corpus/turns.jsonl, where ratings join to
    it. The index row is a pointer; opening one has to follow it."""
    rec = client.record
    write_jsonl(rec.paths.corpus / "turns.jsonl", [
        {"id": "turn-1", "session_id": "s-1", "turn_index": 0,
         "messages": [{"role": "system", "content": "the whole assembled prompt"}],
         "completion": "hello there"}])
    write_jsonl(rec.paths.traces / "prompts.jsonl", [
        {"id": "pr-chat", "ts": "2026-08-01T12:00:00", "kind": "chat_turn",
         "messages": None, "n_messages": 1, "tokens_in": 6,
         "messages_ref": {"file": "corpus/turns.jsonl", "id": "turn-1"}}])

    index = get(client, "/prompts")["items"][0]
    assert index["has_messages"] is True

    body = get(client, "/prompts/pr-chat")
    assert body["messages"][0]["content"] == "the whole assembled prompt"
    assert body["resolved_from"] == "corpus/turns.jsonl"
    assert body["completion"] == "hello there"


def test_the_prompt_day_index_counts_by_kind(client):
    write_jsonl(client.record.paths.traces / "prompts.jsonl", [
        {"id": "pr-1", "ts": "2026-08-01T10:00:00", "kind": "ambient"},
        {"id": "pr-2", "ts": "2026-08-01T11:00:00", "kind": "ambient"},
        {"id": "pr-3", "ts": "2026-08-02T11:00:00", "kind": "chat_turn"}])
    body = get(client, "/prompts/days")
    assert [d["day"] for d in body["items"]] == ["2026-08-02", "2026-08-01"]
    assert body["items"][1] == {"day": "2026-08-01", "count": 2,
                                "kinds": {"ambient": 2}}


def test_days_is_not_swallowed_by_the_prompt_id_route(client):
    """`/prompts/days` and `/prompts/{id}` share a shape; declaration order is
    what keeps them apart."""
    seed(client.record)
    assert "items" in get(client, "/prompts/days")


# --- the vault ------------------------------------------------------------------

def git_vault(rec):
    vault = rec.paths.vault
    (vault / "soul").mkdir(parents=True, exist_ok=True)
    run = lambda *a: subprocess.run(["git", "-C", str(vault), *a],   # noqa: E731
                                    capture_output=True, text=True)
    run("init", "-q")
    run("config", "user.email", "v@localhost")
    run("config", "user.name", "vault")
    run("config", "commit.gpgsign", "false")
    (vault / "soul" / "USER.md").write_text("they like tea\n", encoding="utf-8")
    run("add", "-A")
    run("commit", "-q", "-m", "turn abcd1234:0")
    (vault / "soul" / "USER.md").write_text("they like tea and rain\n", encoding="utf-8")
    run("add", "-A")
    run("commit", "-q", "-m", "tick t-9: noticed the rain")
    return vault


def test_the_vault_history_is_the_record_of_what_changed(client):
    git_vault(client.record)
    body = get(client, "/vault/commits")
    assert [c["subject"] for c in body["items"]] == [
        "tick t-9: noticed the rain", "turn abcd1234:0"]
    assert body["total"] == 2
    assert body["items"][0]["files"][0]["path"] == "soul/USER.md"


def test_a_commit_shows_what_it_did_to_user_md(client):
    git_vault(client.record)
    sha = get(client, "/vault/commits")["items"][0]["sha"]
    body = get(client, f"/vault/commits/{sha}")
    assert "+they like tea and rain" in body["diff"]
    assert body["truncated"] is False


def test_one_file_can_be_read_now_and_as_it_was(client):
    git_vault(client.record)
    now = get(client, "/vault/file", path="soul/USER.md")
    assert now["text"] == "they like tea and rain\n"
    first = get(client, "/vault/commits")["items"][1]["sha"]
    then = get(client, "/vault/file", path="soul/USER.md", rev=first)
    assert then["text"] == "they like tea\n"


def test_a_files_own_history_is_available(client):
    git_vault(client.record)
    body = get(client, "/vault/history", path="soul/USER.md")
    assert len(body["items"]) == 2


def test_the_tree_lists_the_gitignored_working_state_too(client):
    """`state/*.json` is deliberately not committed; on a debug page it is
    exactly what you came to look at."""
    seed(client.record)
    git_vault(client.record)
    names = {e["name"] for e in get(client, "/vault/tree", path="state")["entries"]}
    assert "activity.json" in names


@pytest.mark.parametrize("bad", ["../../../etc/passwd", "/etc/passwd", "../..",
                                 ".git/config"])
def test_a_path_may_not_escape_the_vault(client, bad):
    git_vault(client.record)
    assert client.get("/api/characters/yuri/debug/vault/file",
                      params={"path": bad}).status_code == 400
    assert client.get("/api/characters/yuri/debug/vault/history",
                      params={"path": bad}).status_code == 400


@pytest.mark.parametrize("bad", ["--upload-pack=touch", "HEAD; rm -rf /", "not-hex"])
def test_a_commit_id_must_look_like_one_before_it_reaches_git(client, bad):
    git_vault(client.record)
    assert client.get(
        f"/api/characters/yuri/debug/vault/commits/{bad}").status_code in (400, 404)


# --- the recall index -----------------------------------------------------------

def test_an_absent_index_is_reported_not_raised(client):
    """chunks.db is gitignored and rebuildable; absent is the normal state."""
    body = get(client, "/memory/chunks")
    assert body == {"items": [], "page": 0, "limit": 50, "has_more": False,
                    "total": 0, "available": False}


def test_chunks_page_and_never_carry_their_embeddings(client):
    from yurios.app.memory.index import ChunkIndex
    db = client.record.paths.vault / "memory" / "index" / "chunks.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    index = ChunkIndex(db, 4)
    for i in range(30):
        index.upsert(id=f"c-{i:02d}", kind="turn" if i % 2 else "summary",
                     source_path="memory/episodic/2026-08-01.md", source_span=f"{i}",
                     text=f"a remembered thing number {i}",
                     embedding=[0.5, 0.5, 0.5, 0.5],
                     created_at=f"2026-08-01T10:{i:02d}:00")
    index.close()

    body = get(client, "/memory/chunks", limit=10)
    assert body["total"] == 30 and body["has_more"] is True
    assert len(body["items"]) == 10
    assert all("embedding" not in row for row in body["items"]), \
        "768 floats per row would dwarf the text they describe"

    filtered = get(client, "/memory/chunks", kind="turn")
    assert filtered["total"] == 15
    found = get(client, "/memory/chunks", q="number 7")
    assert [r["id"] for r in found["items"]] == ["c-07"]

    one = get(client, "/memory/chunks/c-07")
    assert one["dim"] == 4 and len(one["embedding_preview"]) == 4
    assert one["norm"] == pytest.approx(1.0)
    assert client.get("/api/characters/yuri/debug/memory/chunks/nope").status_code == 404


# --- overview and economics -----------------------------------------------------

def test_the_overview_says_what_is_on_disk_and_what_is_rolled_away(client):
    """The page reads only the live file, so it has to be able to say that
    older records exist and are not being shown."""
    seed(client.record)
    (client.record.paths.traces / "ticks.jsonl.1").write_text("{}\n", encoding="utf-8")
    body = get(client, "/overview")
    files = {f["name"]: f for f in body["files"]}
    assert files["ticks"]["rotated"] is True
    assert files["signals"]["rotated"] is False
    assert body["counts"]["ticks"] == 3
    assert body["activity"]["state"] == "IDLE"
    assert body["live"] is None, "she is not running; nothing may pretend otherwise"


def test_the_live_context_meter_is_reported_separately_when_she_is_up(live_client):
    """The one genuinely runtime-only value. Kept in its own field so nothing on
    the page can confuse it for history (SPEC §24.3)."""
    seed(live_client.record)
    body = live_client.get("/api/characters/yuri/debug/overview").json()
    assert body["live"] == {"context": {"used": 12, "limit": 100}}
    assert body["counts"]["ticks"] == 3, "history still comes off disk"


def test_economics_separates_what_was_applied_from_what_was_quarantined(client):
    seed(client.record)
    body = get(client, "/economics")
    assert body["utility"] == {
        "applied": 1, "quarantined": 1, "total": 2,
        "by_kind": {"extract": {"total": 1, "applied": 1, "quarantined": 0},
                    "summarise": {"total": 1, "applied": 0, "quarantined": 1}}}
    assert body["context"][0]["used"] == 900
    assert body["by_kind"]["ambient"]["calls"] == 1


def test_memory_reads_her_own_files(client):
    seed(client.record)
    body = get(client, "/memory")
    assert body["journal_days"] == ["2026-08-01"]
    assert body["chunks"]["available"] is False


# --- backfill: the three endpoints the dashboard already shipped against --------

def test_journal_pages_days_newest_first(client):
    episodic = client.record.paths.vault / "memory" / "episodic"
    episodic.mkdir(parents=True)
    for day in ("2026-08-01", "2026-08-02", "2026-08-03"):
        (episodic / f"{day}.md").write_text("### 10:00  you: hi  ⇄  yuri: hey\n",
                                            encoding="utf-8")
    body = client.get("/api/characters/yuri/journal").json()
    assert [d["day"] for d in body["days"]] == ["2026-08-03", "2026-08-02", "2026-08-01"]
    assert body["total"] == 3 and body["has_more"] is False
    assert all(d["count"] == 1 for d in body["days"])


def test_journal_day_returns_its_entries_newest_first(client):
    episodic = client.record.paths.vault / "memory" / "episodic"
    episodic.mkdir(parents=True)
    (episodic / "2026-08-01.md").write_text(
        "### 10:00  you: hi  ⇄  yuri: hey\n### 11:00  [she] tidied up\n",
        encoding="utf-8")
    body = client.get("/api/characters/yuri/journal",
                      params={"day": "2026-08-01"}).json()
    assert [e["time"] for e in body["entries"]] == ["11:00", "10:00"]
    assert body["entries"][0]["hers"] is True


def test_log_interleaves_ticks_and_tool_calls_by_time(client):
    """`_log_sort_key` reconciles two different time formats: ticks stamp a
    local ISO string, tool audits stamp the raw epoch float underneath. Get it
    wrong and the log reads as all ticks, then all calls."""
    rec = client.record
    write_jsonl(rec.paths.traces / "ticks.jsonl", [
        {"tick_id": "t-early", "ts": "2026-08-01T10:00:00", "acted": {}},
        {"tick_id": "t-late", "ts": "2026-08-01T10:10:00", "acted": {}}])
    import datetime
    at = lambda hhmm: datetime.datetime.fromisoformat(     # noqa: E731
        f"2026-08-01T{hhmm}:00").timestamp()
    write_jsonl(rec.paths.tool_logs / "calls.jsonl", [
        {"ts": at("10:05"), "tool": "list_notes", "verdict": "ok"}])

    entries = client.get("/api/characters/yuri/log").json()["entries"]
    assert [e.get("tick_id") or e.get("tool") for e in entries] \
        == ["t-early", "list_notes", "t-late"]


def test_context_history_reads_disk(client):
    seed(client.record)
    body = client.get("/api/characters/yuri/context-history").json()
    assert body["history"][0]["used"] == 900
    assert body["context"] == {"used": 0, "limit": None}, "nothing is running"


def test_context_history_overlays_the_meter_when_she_is_up(live_client):
    seed(live_client.record)
    body = live_client.get("/api/characters/yuri/context-history").json()
    assert body["context"] == {"used": 12, "limit": 100}
