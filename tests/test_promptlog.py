"""traces/prompts.jsonl — the record of every context window she was given.

Before this sink, `corpus/turns.jsonl` held the assembled prompt for committed
conversational turns and nothing else. Self-talk, the arrival greeting, a
reach-out, goal work and DREAM consolidation all called a model and left no
trace of what they were asked. These pin that the gap is closed, that a chat
turn still points at the corpus rather than duplicating it, and that a sink
which fails never takes the turn down with it.
"""
from __future__ import annotations

from yurios.mind.promptlog import PromptLog
from yurios.mind.util import jsonl_tail
from yurios.world import correlate

from .conftest import ScriptedChat, collect, make_mind


def rows(mind_or_log):
    log = getattr(mind_or_log, "prompt_log", mind_or_log)
    return jsonl_tail(log.path, 200)


# --- the sink itself ----------------------------------------------------------

def test_a_record_carries_what_the_detail_view_shows(tmp_path, clock):
    log = PromptLog(tmp_path, clock)
    log.record(kind=correlate.UTILITY, model="tiny",
               messages=[{"role": "system", "content": "be brief"},
                         {"role": "user", "content": "the goal: tidy the shelf"}],
               completion="noted one next step.")
    row = rows(log)[0]
    assert row["kind"] == "utility" and row["model"] == "tiny"
    assert [m["role"] for m in row["messages"]] == ["system", "user"]
    assert row["n_messages"] == 2
    assert row["tokens_in"] > 0 and row["tokens_out"] > 0
    assert row["truncated"] is False
    assert row["id"].startswith("pr-") and row["at"] == clock.now()


def test_a_record_stamps_the_work_that_caused_it(tmp_path, clock):
    log = PromptLog(tmp_path, clock)
    with correlate.scope(kind=correlate.TICK, tick_id="t-77") as tick:
        with correlate.scope(kind=correlate.DREAM):
            log.record(kind=correlate.DREAM, messages=[{"role": "user", "content": "x"}])
    row = rows(log)[0]
    assert (row["tick_id"], row["corr_id"]) == ("t-77", tick.corr_id)


def test_one_enormous_message_is_truncated_not_left_to_eat_the_log(tmp_path, clock):
    """A knowledge drop that swallowed a PDF would otherwise put a megabyte on
    one line, and rotation would throw the whole history away to make room."""
    log = PromptLog(tmp_path, clock, max_chars=100)
    log.record(kind=correlate.KNOWLEDGE,
               messages=[{"role": "user", "content": "y" * 5_000}])
    row = rows(log)[0]
    assert row["truncated"] is True
    assert len(row["messages"][0]["content"]) == 101      # 100 + the ellipsis


def test_capture_can_be_turned_off_entirely(tmp_path, clock):
    log = PromptLog(tmp_path, clock, enabled=False)
    assert log.record(kind=correlate.UTILITY, messages=[]) is None
    assert not log.path.exists()


def test_a_failing_sink_never_breaks_the_call_it_observes(tmp_path, clock, monkeypatch):
    log = PromptLog(tmp_path, clock)
    monkeypatch.setattr("yurios.mind.promptlog.jsonl_append",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")))
    assert log.record(kind=correlate.UTILITY, messages=[]) is None   # must not raise


def test_from_config_honours_the_kill_switch(cfg, clock, tmp_path):
    off = cfg.model_copy(update={"trace_dir": tmp_path, "mind_prompt_capture": False})
    assert PromptLog.from_config(off, clock).enabled is False
    assert PromptLog.from_config(cfg, clock).enabled is True


# --- the mind's own calls -----------------------------------------------------

async def test_the_utility_seam_records_the_prompt_and_the_tick(cfg, seeded_vault):
    """One instrumentation point covers five callers — knowledge, dream and goal
    work are all handed `_utility` as their model seam."""
    rig = make_mind(cfg, seeded_vault)
    with correlate.scope(kind=correlate.TICK, tick_id="t-1"):
        with correlate.scope(kind=correlate.GOAL_WORK):
            await rig.mind._utility([
                {"role": "system", "content": "Write a short working note."},
                {"role": "user", "content": "The goal: water the plants"}])
    row = rows(rig.mind)[-1]
    assert row["kind"] == "goal_work", "labelled by the ACT that opened the scope"
    assert row["tick_id"] == "t-1"
    assert row["messages"][1]["content"] == "The goal: water the plants"
    assert row["completion"] and row["tier"] == "utility"


async def test_an_unlabelled_utility_call_still_lands(cfg, seeded_vault):
    rig = make_mind(cfg, seeded_vault)
    with correlate.scope(kind=correlate.TICK, tick_id="t-2"):
        await rig.mind._utility([{"role": "system", "content": "summarise"},
                                 {"role": "user", "content": "hello"}])
    assert rows(rig.mind)[-1]["kind"] == "utility"


async def test_a_tick_that_calls_no_model_writes_no_prompt(cfg, seeded_vault):
    rig = make_mind(cfg, seeded_vault)
    await rig.mind.tick()
    assert not rig.mind.prompt_log.path.exists() or rows(rig.mind) == []


# --- ambient speech: the half that was invisible -------------------------------

def speaking_brain(cfg, vault, chat=None):
    """A real ToolBrain (real soul, fake models) with the sink wired — the
    ambient path needs true prompt assembly, which the stub state cannot do."""
    rig = make_mind(cfg, vault, chat=chat)
    rig.mind.brain.set_prompt_log(rig.mind.prompt_log)
    return rig.mind.brain


async def test_self_talk_records_the_whole_assembled_prompt(cfg, seeded_vault):
    """stream_ambient is never persisted anywhere else — no corpus line, no
    transcript entry — so this sink is the only record it ever leaves."""
    brain = speaking_brain(cfg, seeded_vault)
    with correlate.scope(kind=correlate.AMBIENT):
        await collect(brain.stream_ambient("s-1", "((murmur something))"))
    row = rows(brain.prompt_log)[-1]
    assert row["kind"] == "ambient"
    assert row["cue"] == "((murmur something))"
    assert row["messages"] and row["messages"][0]["role"] == "system"
    assert "## PERSONA BACKBONE" in row["messages"][0]["content"]
    assert row["completion"]


async def test_a_reach_out_is_labelled_by_its_scope_not_by_the_seam(cfg, seeded_vault):
    brain = speaking_brain(cfg, seeded_vault)
    with correlate.scope(kind=correlate.COMPOSE):
        await collect(brain.stream_ambient("s-1", "((say the thing))"))
    assert rows(brain.prompt_log)[-1]["kind"] == "compose"


async def test_an_interrupted_ambient_line_still_records_what_it_was_asked(
        cfg, seeded_vault):
    """Recorded in `finally`, so a barge-in leaves the reasoning behind the
    half-sentence rather than nothing at all."""
    brain = speaking_brain(cfg, seeded_vault,
                           chat=ScriptedChat([["one ", "two ", "three"]]))
    stream = brain.stream_ambient("s-1", "((cue))")
    assert await anext(stream) == "one "
    await stream.aclose()                      # barge-in
    row = rows(brain.prompt_log)[-1]
    assert row["messages"], "the prompt survives the cancel"
    assert row["completion"] == "one "


# --- chat turns: a pointer, not a copy ----------------------------------------

async def test_a_chat_turn_points_at_the_corpus_instead_of_duplicating_it(
        cfg, seeded_vault, clock, tmp_path):
    """The corpus is the training asset and ratings.jsonl joins to its id.
    Copying whole prompts onto the hottest path would double its largest write
    to say nothing new — so the timeline gets an index row and a pointer."""
    rig = make_mind(cfg, seeded_vault)
    brain = rig.mind.brain
    brain.set_prompt_log(PromptLog(tmp_path, clock))

    session = brain.resolve_session(None)
    with brain.turn_context(channel="text", session_id=session):
        reply = "".join(await collect(brain.stream_reply(session, "hello there")))
        await brain.persist(session, "hello there", reply)

    row = rows(brain.prompt_log)[-1]
    assert row["kind"] == "chat_turn"
    assert row["messages"] is None, "the body lives in the corpus, not here"
    assert row["messages_ref"]["file"] == "corpus/turns.jsonl"
    assert row["messages_ref"]["id"], "and the pointer names which line"
    assert row["n_messages"] > 0 and row["tokens_in"] > 0
    assert row["session_id"] == session

    # the pointer resolves: that id is really in the corpus
    corpus = jsonl_tail(cfg.corpus_dir / "turns.jsonl", 50) \
        if (cfg.corpus_dir / "turns.jsonl").exists() else []
    corpus = corpus or jsonl_tail(seeded_vault.parent / "corpus" / "turns.jsonl", 50)
    assert any(c["id"] == row["messages_ref"]["id"] for c in corpus)
