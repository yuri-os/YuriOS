"""Free time (SPEC §22.7): an empty list is not a stop condition.

Found live, 29 Sep: her one goal closed at 02:14 and every tick for the next
seventeen hours was `DORMANT → REST` — nothing sensed, nothing appraised, and
nothing that ever could be until somebody spoke to her. These pin the way out
and the fences around it: it happens, it decides something, it files that as a
goal she then works, and it never crowds out work that is already in progress.
"""
from __future__ import annotations

import json

from yurios.mind import muse
from yurios.mind.goals import MUSE_GOAL
from yurios.mind.policy import DORMANT, ENGAGED

from .conftest import ScriptedUtility, make_mind, run_mind

FILE = ('think He kept coming back to the lighthouse keepers — I want to know '
        'more before we talk again.\n'
        'use create_goal {"text": "read up on the lighthouse keepers of the '
        'Hebrides", "kind": "task"}')


def quiet(rig, *, state=DORMANT):
    """Nobody here, nothing said for a long while."""
    rig.mind.activity.state = state
    rig.mind.activity.last_user_msg = rig.clock.now() - 6 * 3600
    rig.mind._last_turn_end = rig.clock.now() - 6 * 3600


def free_ticks(traces):
    return [t for t in traces if t["decided"]["intention"] == "muse"]


async def test_an_empty_list_is_free_time_not_rest(cfg, seeded_vault):
    """The live failure: nothing on her list, nothing sensed — she still acts."""
    rig = make_mind(cfg, seeded_vault,
                    utility=ScriptedUtility(muse=(FILE, "think that's it")))
    quiet(rig)
    trace = await rig.mind.tick()
    assert trace["decided"]["intention"] == "muse"
    assert trace["acted"]["what"] == "muse"
    goal = rig.mind.goals.get(trace["acted"]["goal"])
    assert goal is not None and goal.provenance.startswith(MUSE_GOAL)
    assert goal.state == "pending" and goal.commitment == "open-minded"
    # her reason is the plan the goal's first step starts from (§22.4)
    assert "lighthouse keepers" in goal.meta["rationale"]


async def test_the_goal_she_chose_is_worked_on_the_next_tick(cfg, seeded_vault):
    rig = make_mind(cfg, seeded_vault,
                    utility=ScriptedUtility("think started a reading list",
                                            muse=(FILE, "think that's it")))
    quiet(rig)
    first = await rig.mind.tick()
    rig.clock.advance(rig.mind.cadence())
    second = await rig.mind.tick()
    assert second["decided"]["intention"].startswith("goal:")
    assert rig.mind.goals.get(first["acted"]["goal"]).steps == 1


async def test_filing_works_with_her_hands_off(cfg, seeded_vault):
    """Deciding what to do is not a hand: the night files without one too."""
    rig = make_mind(cfg, seeded_vault,
                    utility=ScriptedUtility(muse=(FILE, "think that's it")))
    assert not rig.mind.hands.enabled or not rig.mind.hands.offer(
        state=DORMANT, pressure=0.0, user_present=False)
    quiet(rig)
    trace = await rig.mind.tick()
    assert trace["acted"]["goal"]
    audit = (rig.mind.cfg.tool_log_dir / "calls.jsonl")
    if rig.mind.hands.guard is not None:
        lines = [json.loads(x) for x in audit.read_text().splitlines() if x]
        assert any(x["tool"] == "create_goal" and x["verdict"] == "ok"
                   for x in lines), "decided on her own is still on the record"


async def test_a_thought_alone_is_kept(cfg, seeded_vault):
    rig = make_mind(cfg, seeded_vault, utility=ScriptedUtility(
        muse=("think I keep circling the same three notes. Tomorrow.",)))
    quiet(rig)
    trace = await rig.mind.tick()
    assert trace["acted"]["result"] == "free time: thought it over"
    desk = rig.mind.workspace.read("free-time/2026-07-06.md", default="")
    assert "same three notes" in desk
    # …and the next sitting is shown it, so she does not have it again
    assert "same three notes" in muse.context(rig.mind, rig.clock.now())


async def test_one_goal_per_sitting(cfg, seeded_vault):
    second = 'use create_goal {"text": "tidy the recipes folder by cuisine"}'
    rig = make_mind(cfg, seeded_vault, utility=ScriptedUtility(
        muse=(FILE, second, "think fine")))
    quiet(rig)
    trace = await rig.mind.tick()
    verdicts = [t["verdict"] for t in trace["acted"]["tools"]]
    assert verdicts == ["ok", "denied"]
    assert len([g for g in rig.mind.goals.open_goals()
                if g.provenance.startswith(MUSE_GOAL)]) == 1


async def test_the_cap_on_her_own_goals_is_shared_with_the_night(cfg, seeded_vault):
    rig = make_mind(cfg, seeded_vault,
                    utility=ScriptedUtility(muse=(FILE, "think fine")))
    for text in ("learn the tide tables", "sketch the harbour at dusk",
                 "find a poem about fog"):
        rig.mind.goals.add(text, kind="task", priority=0.1,
                           provenance="strategy:2026-07-05")
    quiet(rig)
    trace = await rig.mind.tick()
    assert trace["decided"]["intention"] == "muse"
    assert trace["acted"]["tools"] == [{"tool": "create_goal", "verdict": "denied"}]
    assert not any(g.provenance.startswith(MUSE_GOAL)
                   for g in rig.mind.goals.open_goals())


async def test_work_in_progress_is_not_crowded_out(cfg, seeded_vault):
    """A goal resting between its steps is what she is busy with."""
    rig = make_mind(cfg, seeded_vault, utility=ScriptedUtility(
        *["think a step"] * 4, muse=(FILE,)))
    quiet(rig)
    rig.mind.goals.add("finish the tide chart", kind="task", priority=0.9)
    worked = await rig.mind.tick()
    assert worked["decided"]["intention"].startswith("goal:")
    rig.clock.advance(rig.mind.cfg.mind_dormant_cadence_s)
    resting = await rig.mind.tick()
    assert resting["decided"]["intention"] == "REST"
    assert "muse" not in [a["what"] for a in resting["appraised"]]


async def test_a_message_gate_2_is_holding_is_not_work_in_progress(
        cfg, seeded_vault):
    """Quiet hours can hold a reach-out all night; that is not being busy."""
    rig = make_mind(cfg, seeded_vault,
                    utility=ScriptedUtility(muse=(FILE, "think fine")))
    held = rig.mind.goals.add("tell them about the lighthouse", kind="reach_out",
                              priority=0.9)
    quiet(rig)
    rig.mind.considered[held.id] = rig.clock.now() - 60
    trace = await rig.mind.tick()
    assert trace["decided"]["intention"] == "muse"


async def test_a_goal_stuck_under_gate_1_does_not_stop_her(cfg, seeded_vault):
    """Live: "catch up on the nights" at priority 0.4 scored 0.24 every tick."""
    rig = make_mind(cfg, seeded_vault,
                    utility=ScriptedUtility(muse=(FILE, "think fine")))
    rig.mind.goals.add("catch up on something small", kind="task", priority=0.4)
    quiet(rig)
    trace = await rig.mind.tick()
    assert trace["decided"]["intention"] == "muse"


async def test_not_while_talking_and_not_past_the_budget_line(cfg, seeded_vault):
    rig = make_mind(cfg, seeded_vault, utility=ScriptedUtility(muse=(FILE,)))
    quiet(rig, state=ENGAGED)
    assert muse.appraise(rig.mind, [], busy=False, now=rig.clock.now()) is None
    quiet(rig)
    assert muse.appraise(rig.mind, [], busy=False, now=rig.clock.now()) is not None
    rig.mind.budget.debit("x" * 4 * int(rig.mind.cfg.mind_daily_tokens * 0.8), "")
    assert muse.appraise(rig.mind, [], busy=False, now=rig.clock.now()) is None


async def test_zero_turns_it_off(cfg, seeded_vault):
    cfg = cfg.model_copy(update={"mind_muse_cooldown_s": 0})
    rig = make_mind(cfg, seeded_vault, utility=ScriptedUtility(muse=(FILE,)))
    quiet(rig)
    assert (await rig.mind.tick())["decided"]["intention"] == "REST"


async def test_the_cooldown_paces_it_and_survives_a_restart(cfg, seeded_vault):
    rig = make_mind(cfg, seeded_vault, utility=ScriptedUtility(
        muse=("think nothing yet",) * 20))
    quiet(rig)
    traces = await run_mind(rig, hours=6)
    sittings = free_ticks(traces)
    cooldown = rig.mind.cfg.mind_muse_cooldown_s
    assert 1 <= len(sittings) <= 6 * 3600 / cooldown + 1
    # the majority of ticks still rest (§15.1)
    rests = [t for t in traces if t["decided"]["intention"] == "REST"]
    assert len(rests) > len(traces) / 2

    again = make_mind(cfg, seeded_vault, clock=rig.clock,
                      utility=ScriptedUtility(muse=("think hm",)))
    assert again.mind.last_mused == rig.mind.last_mused
