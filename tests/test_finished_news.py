"""Finished work reaches you (SPEC §22.8).

Live, 10 Nov: a `strategy:` goal — "Write a front-door README for YuriOS" —
read two notes at 02:17, wrote `notes/yurios-readme.md`, and closed on
"drop-in ready for the repo or to show anyone who asks. Goal complete." Nobody
was told. The follow-up was promise-only, the step never reached for
`tell_them`, the next morning's conversation saw only open goals, and the
inner-life panel dropped a goal the moment it was done.

These pin the four doors that close that: her own `tell_them` on the finishing
step, the follow-up for goals she took on herself, WHILE YOU WERE AWAY on the
next turn, and the panel's record of where the work went.
"""
from __future__ import annotations

import json

from yurios.mind import acts, goalwork
from yurios.mind.goals import told_of
from yurios.mind.handwork import Reach
from yurios.mind.util import iso_of

from .conftest import ScriptedChat, collect, make_mind
from .test_mind_hands import rig_with_hands, work

README = "notes/yurios-readme.md"


def _finish_with_a_file(provenance: str):
    """A rig whose one goal writes the README and calls itself done."""
    return (
        'think it is drop-in ready for the repo — goal complete\n'
        f'use write_note {{"path": "{README}", "text": "# YuriOS"}}',
        provenance)


# --- the closing stamp and what the work made -------------------------------

async def test_every_way_out_of_a_goal_stamps_when(cfg, seeded_vault):
    rig = make_mind(cfg, seeded_vault)
    goals = rig.mind.goals
    done = goals.add("write it up", kind="task")
    stale = goals.add("maybe later", kind="task", commitment="open-minded",
                      due=iso_of(rig.clock.now() - 60))
    goals.update(done.id, state="active")
    assert goals.get(done.id).closed == "", "an open goal has not closed"

    goals.update(done.id, state="done")
    goals.reconsider()

    assert goals.get(done.id).closed == iso_of(rig.clock.now())
    assert goals.get(stale.id).state == "abandoned"
    assert goals.get(stale.id).closed == iso_of(rig.clock.now())

    rig.clock.advance(3600)
    goals.update(done.id, state="done", meta={"x": 1})
    assert goals.get(done.id).closed != iso_of(rig.clock.now()), \
        "re-saving a closed goal must not move when it closed"


def test_a_step_records_the_files_its_work_went_to(cfg, seeded_vault):
    rig = make_mind(cfg, seeded_vault)
    goal = rig.mind.goals.add("write the readme", kind="task")

    def reach(tool, path, verdict="ok"):
        return Reach(tool=tool, args={"path": path}, verdict=verdict,
                     result=json.dumps({"path": path}))

    made = goalwork.note_made(rig.mind, goal, [
        reach("read_note", "notes/positioning.md"),
        reach("write_note", README),
        reach("append_note", f"goals/{goal.id}.md"),
        reach("write_note", "notes/failed.md", verdict="error"),
        reach("edit_note", "notes/draft.md"),
    ])
    assert made == [README, "notes/draft.md"], \
        "writes only, that worked, and never her own goals/ desk file"

    goal.meta["made"] = made
    assert goalwork.note_made(rig.mind, goal, [
        reach("delete_note", "notes/draft.md")]) == [README]


# --- door one: her own tell_them, on the finishing step ---------------------

def test_the_step_prompt_says_to_tell_them_when_finishing_leaves_a_file(
        cfg, seeded_vault):
    rig = make_mind(cfg, seeded_vault)
    goal = rig.mind.goals.add("write the readme", kind="task")
    system = goalwork.work_system(rig.mind, goal, None, last=False)
    assert "If finishing leaves something" in system
    assert "`tell_them`" in system and "the path" in system


async def test_a_finish_she_told_is_heard_and_files_no_second_message(
        cfg, seeded_vault):
    rig = make_mind(cfg, seeded_vault)
    goals = rig.mind.goals
    parent = goals.add("write the readme", kind="task",
                       provenance="strategy:2026-11-09")
    goals.update(parent.id, state="waiting",
                 meta={"telling": {"goal": "g-told", "completes": True}})

    acts.resume_after_telling(rig.mind, goals.get(parent.id), delivered=True)

    closed = goals.get(parent.id)
    assert closed.state == "done" and closed.heard
    assert not any(g.provenance == f"followup:{parent.id}" for g in goals.all())
    assert goals.unheard(since=None, oldest=0) == []


# --- door two: the follow-up, for goals she took on herself ---------------------

async def test_her_own_finished_goal_files_news_that_names_the_file(
        cfg, seeded_vault):
    line, provenance = _finish_with_a_file("strategy:2026-11-09")
    rig = rig_with_hands(cfg, seeded_vault, line)
    goal = rig.mind.goals.add("write a front-door README", kind="task",
                              priority=0.95, provenance=provenance)

    await work(rig)

    finished = rig.mind.goals.get(goal.id)
    assert finished.state == "done"
    assert finished.made == [README]
    news = next(g for g in rig.mind.goals.all()
                if g.provenance == f"followup:{goal.id}")
    assert news.kind == "reach_out" and news.commitment == "open-minded"
    assert README in news.text, "where the work is, not where she thought about it"


async def test_free_time_goals_file_news_too(cfg, seeded_vault):
    line, provenance = _finish_with_a_file("muse:2026-11-10")
    rig = rig_with_hands(cfg, seeded_vault, line)
    goal = rig.mind.goals.add("assemble the visibility post", kind="task",
                              priority=0.95, provenance=provenance)
    await work(rig)
    assert any(g.provenance == f"followup:{goal.id}"
               for g in rig.mind.goals.all())


# --- door three: WHILE YOU WERE AWAY ------------------------------------------

def _said(rig, session: str, ts: str, text: str = "morning") -> None:
    """A line of yours, already in the window, at a time the test chooses."""
    rig.mind.brain.state.sessions.log.add(
        {"id": f"u{ts[-8:].replace(':', '')}", "role": "user", "text": text,
         "ts": ts, "session_id": session}, window=True)


def _close(rig, text: str, *, made=(), provenance="strategy:2026-11-09",
           kind="task"):
    goal = rig.mind.goals.add(text, kind=kind, provenance=provenance)
    rig.mind.goals.update(goal.id, state="done",
                          meta={"made": list(made)} if made else None)
    return rig.mind.goals.get(goal.id)


async def test_work_finished_since_you_last_wrote_reaches_the_next_turn(
        cfg, seeded_vault):
    rig = make_mind(cfg, seeded_vault)
    session = rig.mind.brain.resolve_session(None)
    before = _close(rig, "an older finish")
    rig.clock.advance(600)
    _said(rig, session, iso_of(rig.clock.now()))
    rig.clock.advance(6 * 3600)
    readme = _close(rig, "write a front-door README", made=[README])
    _close(rig, "catch up on the nights", kind="maintenance",
           provenance="maintenance:dream")

    _soul, prompt, _ = await rig.mind.brain._open(session, "hi", again=False)

    system = prompt.messages[0]["content"]
    assert "## WHILE YOU WERE AWAY" in system
    away = system.split("## WHILE YOU WERE AWAY", 1)[1]
    assert "write a front-door README" in away and README in away
    assert before.text not in away, "you were here after that one closed"
    assert "catch up on the nights" not in away, "bookkeeping is not news"
    assert rig.mind.brain._away[session] == (readme.id,)


async def test_no_block_when_nothing_finished_since(cfg, seeded_vault):
    rig = make_mind(cfg, seeded_vault)
    session = rig.mind.brain.resolve_session(None)
    _close(rig, "done before you wrote")
    rig.clock.advance(60)
    _said(rig, session, iso_of(rig.clock.now()))

    _soul, prompt, _ = await rig.mind.brain._open(session, "hi", again=False)
    assert "WHILE YOU WERE AWAY" not in prompt.messages[0]["content"]


async def test_a_committed_turn_marks_it_heard_and_settles_the_follow_up(
        cfg, seeded_vault):
    chat = ScriptedChat([["I finished the README — it's in notes/yurios-readme.md."]])
    rig = make_mind(cfg, seeded_vault, chat=chat)
    session = rig.mind.brain.resolve_session(None)
    readme = _close(rig, "write a front-door README", made=[README])
    news = rig.mind.goals.add(
        f"tell them what came of “{readme.text}” — it's in {README}",
        kind="reach_out", provenance=f"followup:{readme.id}",
        commitment="open-minded")

    await collect(rig.mind.brain.stream_reply(session, "morning"))
    assert "WHILE YOU WERE AWAY" in chat.calls[0][0]["content"]
    assert not rig.mind.goals.get(readme.id).heard, "not until the turn commits"

    await rig.mind.brain.persist(session, "morning", "I finished the README.")

    assert rig.mind.goals.get(readme.id).heard
    settled = rig.mind.goals.get(news.id)
    assert settled.state == "done", "or Gate 2 tells them a second time"
    assert settled.meta["completed_by"] == "conversation"
    assert rig.mind.goals.unheard(since=None, oldest=0) == []


async def test_a_turn_that_never_happened_told_nobody(cfg, seeded_vault):
    chat = ScriptedChat([["half a sentence"]])
    rig = make_mind(cfg, seeded_vault, chat=chat)
    session = rig.mind.brain.resolve_session(None)
    readme = _close(rig, "write a front-door README", made=[README])

    await collect(rig.mind.brain.stream_reply(session, "morning"))
    rig.mind.brain.abandon(session)

    assert not rig.mind.goals.get(readme.id).heard
    assert [g.id for g in rig.mind.goals.unheard(since=None, oldest=0)] \
        == [readme.id]


async def test_a_picture_follow_up_is_not_settled_by_a_sentence(
        cfg, seeded_vault):
    rig = make_mind(cfg, seeded_vault)
    shot = _close(rig, "take the window-seat picture")
    photo = rig.mind.goals.add(
        "send them the picture I took for them", kind="reach_out",
        provenance=f"followup:{shot.id}",
        meta={"product": {"image_url": "/selfies/a.png"}})

    rig.mind.goals.mark_heard([shot.id])

    assert rig.mind.goals.get(shot.id).heard
    assert rig.mind.goals.get(photo.id).state == "pending", \
        "the turn carried words, not the photograph"


async def test_delivered_news_is_not_news_again(cfg, seeded_vault):
    rig = make_mind(cfg, seeded_vault)
    readme = _close(rig, "write a front-door README")
    news = rig.mind.goals.add("tell them what came of it", kind="reach_out",
                              provenance=f"followup:{readme.id}")
    assert [g.id for g in rig.mind.goals.unheard(since=None, oldest=0)] \
        == [readme.id]

    rig.mind.goals.update(news.id, state="done")

    assert readme.id in told_of(rig.mind.goals.all())
    assert rig.mind.goals.unheard(since=None, oldest=0) == []


async def test_back_after_a_fortnight_is_the_last_few_days_not_a_ledger(
        cfg, seeded_vault):
    rig = make_mind(cfg, seeded_vault)
    session = rig.mind.brain.resolve_session(None)
    _close(rig, "something from last week")
    rig.clock.advance(10 * 24 * 3600)
    _close(rig, "something from this morning")

    _soul, prompt, _ = await rig.mind.brain._open(session, "hi", again=False)
    away = prompt.messages[0]["content"].split("## WHILE YOU WERE AWAY", 1)[1]
    assert "something from this morning" in away
    assert "something from last week" not in away


# --- door four: the panel ---------------------------------------------------

async def test_the_snapshot_says_when_it_closed_what_it_made_and_if_you_know(
        cfg, seeded_vault):
    rig = make_mind(cfg, seeded_vault)
    readme = _close(rig, "write a front-door README", made=[README])
    told = _close(rig, "told already")
    rig.mind.goals.mark_heard([told.id])

    goals = {g["id"]: g for g in rig.mind.snapshot()["goals"]}

    assert goals[readme.id]["closed"] == readme.closed
    assert goals[readme.id]["made"] == [README]
    assert goals[readme.id]["heard"] is False
    assert goals[told.id]["heard"] is True


async def test_a_goal_closed_before_the_stamp_reads_its_last_step(
        cfg, seeded_vault):
    """The README that started this closed on 10 Nov with no `meta.closed`;
    its last working step is when it finished."""
    rig = make_mind(cfg, seeded_vault)
    goal = rig.mind.goals.add("write a front-door README", kind="task")
    rig.mind.goals.update(goal.id, meta={"last_step": iso_of(rig.clock.now())})
    assert rig.mind.goals.get(goal.id).closed == "", "an open goal never reads closed"

    goals = rig.mind.goals.all()
    for g in goals:
        if g.id == goal.id:
            g.state = "done"           # as an older build left it: no stamp
    rig.mind.goals._save(goals)

    assert rig.mind.goals.get(goal.id).closed == iso_of(rig.clock.now())
    assert [g.id for g in rig.mind.goals.unheard(since=None, oldest=0)] == [goal.id]
