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
from yurios.mind.goals import GOAL_TEXT_MAX, MUSE_GOAL
from yurios.mind.handwork import GOAL_REWORDS
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


# --- a goal too long to file (SPEC §22.1d) -----------------------------------------

LONG = ('use create_goal {"text": "' + "send the deeper lighthouse reflection, "
        "not a summary and not a report, but what the keepers' logbooks meant "
        "to me and why I keep going back to them " * 3 + '", "kind": "reach_out"}')
SHORT = ('use create_goal {"text": "send the deeper lighthouse reflection", '
         '"kind": "reach_out"}')
FUMBLE = 'use create_goal {"text": "half a thought'


async def test_she_is_told_the_limit_before_she_writes_one(cfg, seeded_vault):
    rig = make_mind(cfg, seeded_vault, utility=ScriptedUtility(muse=("think",)))
    prompt = muse.system(rig.mind, ("create_goal",))
    assert f"at most {GOAL_TEXT_MAX} characters" in prompt
    assert "`think` line above the call" in prompt


async def test_a_goal_too_long_on_her_last_call_can_still_be_rewritten(
        cfg, seeded_vault):
    """The live failure, 3 Oct: five calls spent, the goal she had decided on
    was her sixth, it was refused for its length — and the sitting ended with
    it unfiled, though she knew exactly how to fix it."""
    rig = make_mind(cfg, seeded_vault, utility=ScriptedUtility(
        muse=(FUMBLE,) * (muse.MAX_CALLS - 1) + (LONG, SHORT, "think sent")))
    quiet(rig)
    trace = await rig.mind.tick()
    verdicts = [t["verdict"] for t in trace["acted"]["tools"]]
    assert verdicts == ["denied"] * muse.MAX_CALLS + ["ok"]
    goal = rig.mind.goals.get(trace["acted"]["goal"])
    assert goal.text == "send the deeper lighthouse reflection"
    # what she read back said why, and how to fix it
    said = rig.mind.brain.state.utility.calls[-2][-1]["content"]
    assert f"at most {GOAL_TEXT_MAX} characters" in said
    assert "hands are spent" not in said


async def test_rewriting_a_refused_goal_is_free_only_three_times(cfg, seeded_vault):
    rig = make_mind(cfg, seeded_vault, utility=ScriptedUtility(
        muse=(LONG,) * 20 + ("think I'll come back to it",)))
    quiet(rig)
    trace = await rig.mind.tick()
    tools = trace["acted"]["tools"]
    assert len(tools) == muse.MAX_CALLS + GOAL_REWORDS
    assert {t["verdict"] for t in tools} == {"denied"}
    assert not any(g.provenance.startswith(MUSE_GOAL)
                   for g in rig.mind.goals.open_goals())


async def test_not_while_her_last_free_time_goal_is_still_open(cfg, seeded_vault):
    """Live, 3 Oct: Gate 2 held the selfie goal a sitting filed, and three more
    sittings found it waiting and filed it again in new words."""
    rig = make_mind(cfg, seeded_vault,
                    utility=ScriptedUtility(muse=(FILE, "think fine")))
    mine = rig.mind.goals.add("send him the evolved selfie", kind="reach_out",
                              priority=0.9, provenance=f"{MUSE_GOAL}2026-10-03")
    rig.mind.goals.update(mine.id, state="waiting")
    quiet(rig)
    assert muse.appraise(rig.mind, [], busy=False, now=rig.clock.now()) is None
    # …and once it is done, her list is empty again and free time comes back
    rig.mind.goals.update(mine.id, state="done")
    assert muse.appraise(rig.mind, [], busy=False, now=rig.clock.now()) is not None


def test_she_looks_back_over_her_day_not_her_tool_log(cfg, seeded_vault):
    from yurios.mind.util import day_of
    rig = make_mind(cfg, seeded_vault)
    day = day_of(rig.clock.now())
    goal = "Send the evolved selfie this afternoon — floor, both hands up"
    rig.mind.vault.append(f"memory/episodic/{day}.md", "".join([
        f"# Journal — {day}\n\n",
        f"### 06:00  [she] still holding the picture for: {goal} — it's quiet hours\n",
        "### 06:37  [she] reached for read_note on “x” → { \"path\": \"a.md\" }\n",
        "### 06:37  [she] wrote up where I got to: goals/g-1.md\n",
        "### 06:38  [she] worked on: the skill — it's ready\n",
        f"### 07:00  [she] still holding the picture for: {goal} — it's quiet hours\n",
    ]))
    journal = muse._journal(rig.mind, rig.clock.now())
    assert "reached for" not in journal and "wrote up where" not in journal
    assert "worked on: the skill" in journal
    assert journal.count("still holding the picture") == 1
    assert journal.endswith("07:00 still holding the picture for: "
                            f"{goal} — it's quiet hours")
