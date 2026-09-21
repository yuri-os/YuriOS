"""The joined graph behind the mind debug page's workspace (SPEC §24.4).

What is under test is the difference between *recorded* and *inferred*: a link
the files state (a shared tick_id, a corr_id, a turn_id, a commit that names its
tick) against one matched by time (a reach-out and the call just before it, a
journal line and the tick around its minute). The page draws them differently
and must, so the build has to label them right, and every inferred link has to
carry its reason.

The rest is what a debug surface owes a stopped character: the rolled `.1`
generation is read, a fallback id survives rotation, a window stops reading at
its edge, and nothing is written — not even the conversation log's one-time
upgrade.
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
import time

from yurios.app.conversation import read_entries
from yurios.mind.util import jsonl_reverse
from yurios.world import debug_graph

from .test_host import record
from .test_host_debug import client, get, write_jsonl  # noqa: F401  (fixture)

NOW = dt.datetime(2026, 8, 10, 12, 0, 0).timestamp()


def iso(offset_s: float) -> str:
    """A naive house-local stamp `offset_s` before NOW, as the traces write it."""
    return dt.datetime.fromtimestamp(NOW - offset_s).isoformat(timespec="seconds")


def tick(tid, ago, *, what="speak", intention="reach_out:hello", **extra):
    return {"tick_id": tid, "ts": iso(ago), "activity_state": "IDLE",
            "sensed": [], "appraised": [{"what": intention, "score_to_act": 0.7,
                                         "why": "priority 0.6"}],
            "decided": {"intention": intention, "runners_up": []},
            "acted": {"what": what, "result": "sent"}, "interrupt": {}, **extra}


def message(mid, ago, **fields):
    """One drawn line in the conversation log, the way `post_message` writes it."""
    return {"id": mid, "role": "assistant", "text": "hello", "ts": iso(ago), "d": 1,
            **fields}


def build(rec, days=None):
    return debug_graph.build(rec, days=days, char_name="Yuri", user_name="Grant",
                             act_threshold=0.4, now=NOW)


def reach(rec, **message_fields):
    """A reach-out delivered at NOW-100, composed by a call 2s earlier inside
    the owning tick, with an older tick that must not be taken for its cause."""
    write_jsonl(rec.paths.traces / "ticks.jsonl", [tick("t-older", 200), tick("t-owner", 90)])
    write_jsonl(rec.paths.traces / "prompts.jsonl", [
        {"id": "pr-compose", "ts": iso(102), "kind": "compose", "tick_id": "t-owner",
         "corr_id": "c-work", "model": "m"}])
    write_jsonl(rec.paths.vault / "state" / "conversation.jsonl",
                [message("m-1", 100, proactive=True, **message_fields)])


def said(graph):
    return [link for link in graph["links"] if link["rel"] == "said"]


# --- recorded and inferred ------------------------------------------------------

def test_a_reach_outs_cause_is_the_composing_call_and_its_tick(tmp_path):
    rec = record(tmp_path)
    reach(rec)
    graph = build(rec)
    links = said(graph)
    assert {link["a"] for link in links} == {"pr-compose", "t-owner"}, \
        "the older tick is not the cause just for being a tick"
    assert all(link["confidence"] == "inferred" for link in links), \
        "nothing in the files names the message's call: it is matched by time"
    story = next(s for s in graph["stories"] if s["kind"] == "reach")
    assert "Inferred association" in story["why"]
    assert "2s before delivery" in story["why"]


def test_a_shared_corr_id_makes_the_same_links_recorded(tmp_path):
    rec = record(tmp_path)
    reach(rec, corr_id="c-work")
    assert all(link["confidence"] == "explicit" for link in said(build(rec)))


def test_a_message_that_names_its_tick_needs_no_composing_call(tmp_path):
    rec = record(tmp_path)
    reach(rec, tick_id="t-owner")
    (rec.paths.traces / "prompts.jsonl").unlink()
    [edge] = said(build(rec))
    assert (edge["a"], edge["confidence"]) == ("t-owner", "explicit")


def test_with_nothing_recorded_the_tick_is_looked_for_after_delivery(tmp_path):
    """ACT delivers first and REFLECT traces the tick after it, so the fallback
    looks forward in time, never back at whatever tick came before."""
    rec = record(tmp_path)
    reach(rec)
    (rec.paths.traces / "prompts.jsonl").unlink()
    [edge] = said(build(rec))
    assert (edge["a"], edge["confidence"]) == ("t-owner", "inferred")


def test_every_inferred_link_says_why_it_was_matched(tmp_path):
    rec = record(tmp_path)
    reach(rec)
    episodic = rec.paths.vault / "memory" / "episodic"
    episodic.mkdir(parents=True)
    minute = dt.datetime.fromtimestamp(NOW - 90).strftime("%H:%M")
    (episodic / f"{dt.datetime.fromtimestamp(NOW).date()}.md").write_text(
        f"### {minute}  [she] tidied the shelf\n### {minute}  you: hi  ⇄  yuri: hey\n",
        encoding="utf-8")
    graph = build(rec)
    inferred = [link for link in graph["links"] if link["confidence"] == "inferred"]
    assert {link["rel"] for link in inferred} >= {"said", "wrote"}
    assert all(link["reason"] and "Recorded" not in link["reason"] for link in inferred)
    journal = [e for e in graph["events"] if e["kind"] == "journal"]
    assert [e["title"] for e in journal] == ["tidied the shelf"], \
        "only her own lines: the lines written together are the chat lane's"


def test_a_reply_joins_the_exact_call_that_wrote_it(tmp_path):
    """The conversation log keeps a reply's turn_id on its late `raw_for` half;
    the prompt index points at the same turn. That is a recorded join."""
    rec = record(tmp_path)
    write_jsonl(rec.paths.traces / "prompts.jsonl", [
        {"id": "pr-turn", "ts": iso(61), "kind": "chat_turn", "model": "m",
         "messages_ref": {"file": "corpus/turns.jsonl", "id": "turn-7"}}])
    write_jsonl(rec.paths.vault / "state" / "conversation.jsonl", [
        {"id": "u-1", "role": "user", "text": "hi", "ts": iso(70), "d": 1},
        {"id": "a-1", "role": "assistant", "text": "hey", "ts": iso(60), "d": 1},
        {"raw_for": "a-1", "w": 1, "turn_id": "turn-7", "raw": "[happy] hey"}])
    graph = build(rec)
    [edge] = [link for link in graph["links"] if link["rel"] == "turn"]
    assert (edge["a"], edge["b"], edge["confidence"]) == ("pr-turn", "chat-a-1", "explicit")
    exchange = next(s for s in graph["stories"] if s["kind"] == "chat")
    assert "pr-turn" in exchange["nodes"]


def test_a_commit_that_names_its_tick_is_joined_to_it(tmp_path):
    rec = record(tmp_path)
    write_jsonl(rec.paths.traces / "ticks.jsonl", [
        tick("t-9", 30, what="goalwork", intention="goal:tidy")])
    vault = rec.paths.vault
    vault.mkdir(parents=True, exist_ok=True)
    run = lambda *a: subprocess.run(["git", "-C", str(vault), *a],  # noqa: E731
                                    capture_output=True, text=True, check=True)
    run("init", "-q")
    run("config", "user.email", "v@localhost")
    run("config", "user.name", "vault")
    run("config", "commit.gpgsign", "false")
    (vault / "goals.md").write_text("- [ ] tidy\n", encoding="utf-8")
    run("add", "-A")
    run("commit", "-q", "-m", "tick t-9: goal:tidy")
    graph = debug_graph.build(rec, days=None, now=time.time())
    [commit] = [e for e in graph["events"] if e["kind"] == "commit"]
    assert commit["ref"].startswith("#/vault/commit/")
    [edge] = [link for link in graph["links"] if link["rel"] == "commit"]
    assert {edge["a"], edge["b"]} == {"t-9", commit["id"]}
    assert edge["confidence"] == "explicit"


# --- what a tick is, and what REST is --------------------------------------------

def test_rest_is_density_not_events(tmp_path):
    rec = record(tmp_path)
    write_jsonl(rec.paths.traces / "ticks.jsonl", [
        {"tick_id": "t-rest", "ts": iso(50), "decided": {"intention": "REST"},
         "acted": {"result": "rest"}}])
    graph = build(rec)
    assert graph["events"] == []
    [bucket] = graph["rest_density"]
    assert bucket["n"] == 1 and bucket["first_t"] == bucket["last_t"] == NOW - 50
    assert graph["stats"]["rest"] == 1


def test_tick_event_can_shape_rest_for_the_detail_page():
    row = {"tick_id": "t-rest", "ts": iso(50), "activity_state": "DORMANT",
           "sensed": [],
           "appraised": [{"what": "tool_step:nights", "score_to_act": 0.24,
                          "why": "priority 0.4"}],
           "decided": {"intention": "REST", "runners_up": []},
           "acted": {"result": "rest"}}
    assert debug_graph.tick_event(row, {}, {}) is None
    ev = debug_graph.tick_event(row, {}, {}, include_rest=True)
    assert ev["bucket"] == "REST"
    assert ev["detail"]["appraised"][0]["score"] == 0.24


def test_a_decision_explains_itself_against_her_own_threshold(tmp_path):
    rec = record(tmp_path)
    write_jsonl(rec.paths.traces / "ticks.jsonl", [tick("t-1", 10)])
    graph = debug_graph.build(rec, days=None, act_threshold=0.55, now=NOW)
    [story] = [s for s in graph["stories"] if s["kind"] == "tick"]
    assert "Gate 1 fires at 0.55" in story["why"]
    assert graph["meta"]["act_threshold"] == 0.55


# --- reading the files -------------------------------------------------------------

def test_fallback_ids_come_from_content_so_rotation_cannot_shift_them(tmp_path):
    rec = record(tmp_path)
    rows = [{"at": NOW - 200, "from": "IDLE", "to": "ENGAGED"},
            {"at": NOW - 100, "from": "ENGAGED", "to": "IDLE"}]
    path = rec.paths.traces / "activity.jsonl"
    write_jsonl(path, rows)
    before = {e["id"] for e in build(rec)["events"] if e["t"] == NOW - 100}
    write_jsonl(path, rows[1:])
    after = {e["id"] for e in build(rec)["events"]}
    assert before == after


def test_the_rolled_generation_is_read_and_its_overlap_kept_once(tmp_path):
    rec = record(tmp_path)
    live = rec.paths.traces / "signals.jsonl"
    write_jsonl(live.with_name("signals.jsonl.1"), [
        {"id": "sig-old", "type": "timer", "ts": iso(500), "payload": {"label": "tea"}},
        {"id": "sig-both", "type": "timer", "ts": iso(400), "payload": {}}])
    write_jsonl(live, [{"id": "sig-both", "type": "timer", "ts": iso(400), "payload": {}},
                       {"id": "sig-new", "type": "wakeup", "ts": iso(10), "payload": {}}])
    ids = [e["id"] for e in build(rec)["events"]]
    assert ids == ["sig-old", "sig-both", "sig-new"], "oldest first, one of each"


def test_a_window_reads_back_to_its_edge_and_no_further(tmp_path):
    rec = record(tmp_path)
    write_jsonl(rec.paths.traces / "signals.jsonl", [
        {"id": "sig-ancient", "type": "timer", "ts": iso(10 * 86400)},
        {"id": "sig-week", "type": "timer", "ts": iso(3 * 86400)},
        {"id": "sig-fresh", "type": "timer", "ts": iso(60)}])
    assert [e["id"] for e in build(rec, days=1)["events"]] == ["sig-fresh"]
    assert [e["id"] for e in build(rec, days=7)["events"]] == ["sig-week", "sig-fresh"]
    graph = build(rec, days=7)
    assert graph["meta"]["window"] == {"days": 7, "since": NOW - 7 * 86400, "until": NOW}
    assert graph["meta"]["range"][0] <= NOW - 7 * 86400, "the range spans the window asked for"


def test_goals_come_from_her_own_store_with_the_ticks_that_served_them(tmp_path):
    rec = record(tmp_path)
    (rec.paths.vault).mkdir(parents=True, exist_ok=True)
    (rec.paths.vault / "goals.md").write_text(
        f"- [ ] (g-abc123) tidy the shelf | kind: task | priority: 0.6 | from: self | "
        f"created: {iso(300)} | meta: {json.dumps({'rationale': 'it is a mess'})}\n",
        encoding="utf-8")
    write_jsonl(rec.paths.traces / "ticks.jsonl", [
        tick("t-g", 30, what="goalwork", intention="goal:tidy the shelf")])
    graph = build(rec)
    [goal] = graph["goals"]
    assert (goal["title"], goal["from"], goal["meta"]["rationale"]) == \
        ("tidy the shelf", "self", "it is a mess")
    assert goal["ticks"] == ["t-g"]
    assert any(link["rel"] == "goal" and {link["a"], link["b"]} == {"g-abc123", "t-g"}
               for link in graph["links"])


def test_reading_the_conversation_writes_nothing(tmp_path):
    """A handle's constructor upgrades legacy stores and marks the file
    untracked; a debug read of a stopped character must do neither."""
    vault = tmp_path / "vault"
    write_jsonl(vault / "state" / "conversation.jsonl", [
        {"id": "a-1", "role": "assistant", "text": "hi", "ts": iso(5), "d": 1},
        {"id": "a-2", "role": "assistant", "text": "not drawn", "ts": iso(4)},
        {"raw_for": "a-1", "w": 1, "turn_id": "turn-1"}])
    (vault / "state" / "sessions.json").write_text('{"sessions": {}}', encoding="utf-8")
    before = sorted(p.name for p in (vault / "state").iterdir())
    rows = read_entries(vault)
    assert [(r["id"], r.get("turn_id")) for r in rows] == [("a-1", "turn-1")]
    assert sorted(p.name for p in (vault / "state").iterdir()) == before


def test_the_reverse_reader_is_newest_first_and_skips_a_torn_line(tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_text('{"n": 1}\n{"n": 2}\n{"n": 3', encoding="utf-8")
    assert [r["n"] for r in jsonl_reverse(path)] == [2, 1]
    assert list(jsonl_reverse(tmp_path / "absent.jsonl")) == []


# --- over the host ---------------------------------------------------------------

def test_the_graph_route_reads_a_stopped_character(client):  # noqa: F811
    write_jsonl(client.record.paths.traces / "ticks.jsonl", [
        {**tick("t-now", 0), "ts": dt.datetime.now().isoformat(timespec="seconds")}])
    body = get(client, "/graph", days=1)
    assert [e["id"] for e in body["events"]] == ["t-now"]
    assert body["meta"]["name"] == "Yuri"
    assert body["meta"]["act_threshold"] == 0.4
    assert get(client, "/graph", days=0)["meta"]["window"]["days"] is None, "0 is everything"


def test_a_tick_detail_carries_the_graphs_account_of_it(client):  # noqa: F811
    write_jsonl(client.record.paths.traces / "ticks.jsonl.1", [tick("t-rolled", 60)])
    body = get(client, "/ticks/t-rolled")
    assert body["tick"]["tick_id"] == "t-rolled", "found in the rolled generation"
    assert body["event"]["kind"] == "tick"
    assert body["why"].startswith(dt.datetime.fromtimestamp(NOW - 60).strftime("%a"))
    assert "DECIDE committed to «reach_out:hello»" in body["why"]
    assert body["act_threshold"] == 0.4
