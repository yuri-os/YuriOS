"""A goal step's `create_goal` files a goal, and says which (SPEC §22.1c).

The tool server only validates — `goals.md` belongs to the host — and a reply's
call is filed by `ToolBrain._execute`. A goal step's call never went through
there: she read `{"status": "ready"}`, the audit said ok, the desk said she had
reached for it, and nothing was filed.
"""
from __future__ import annotations

import json

from yurios.mind.goals import GOAL_TEXT_MAX, STEP_GOAL
from yurios.mind.handwork import LoopHands

from .conftest import ScriptedUtility
from .test_mind_hands import audit_lines, rig_with_hands, work


def _use(text, kind="task"):
    return "use create_goal " + json.dumps({"text": text, "kind": kind})


def _rig(cfg, vault, *lines, allow="create_goal,write_note", **extra):
    rig = rig_with_hands(cfg, vault, *lines, allow=allow, **extra)
    rig.utility = rig.mind.brain.state.utility
    assert isinstance(rig.utility, ScriptedUtility)
    return rig


def _children(rig, parent):
    return [g for g in rig.mind.goals.open_goals()
            if g.provenance == f"{STEP_GOAL}{parent.id}"]


def _read_back(rig) -> str:
    """What the step was handed after its call — the second message it read."""
    step = [c for c in rig.utility.calls
            if "advancing one of your own goals" in c[0]["content"].lower()]
    return step[-1][-1]["content"]


async def test_a_step_files_a_real_goal_and_reads_back_its_id(cfg, seeded_vault):
    rig = _rig(cfg, seeded_vault,
               _use("ask Sam which wood the shed should be", "reach_out"),
               "Split that off; back to the measurements.")
    parent = rig.mind.goals.add("plan the shed", kind="task", priority=0.95)

    trace = (await work(rig))[0]
    assert trace["acted"]["tool"] == "create_goal"
    [child] = _children(rig, parent)
    assert child.text == "ask Sam which wood the shed should be"
    assert child.kind == "reach_out"
    # filed like the night's own (§22.1b): disposable, and under a promise
    assert child.commitment == "open-minded" and child.due
    assert child.priority < 0.7

    back = _read_back(rig)
    assert '"status": "created"' in back and child.id in back
    line = audit_lines(rig)[-1]
    assert line["tool"] == "create_goal" and line["verdict"] == "ok"
    assert child.id in line["result"] and '"ready"' not in line["result"]
    desk = rig.mind.workspace.read(f"goals/{parent.id}.md")
    assert f"filed a goal of its own: “{child.text}” ({child.id})" in desk


async def test_one_open_child_per_goal_and_the_refusal_spends_nothing(
        cfg, seeded_vault):
    rig = _rig(cfg, seeded_vault, _use("measure the shed floor"), "ok",
               _use("buy the shed paint"), "ok")
    parent = rig.mind.goals.add("plan the shed", kind="task", priority=0.95)
    await work(rig)
    [first] = _children(rig, parent)
    calls = len(rig.runner.calls)

    await work(rig)
    assert _children(rig, parent) == [first]
    assert len(rig.runner.calls) == calls, "refused before it was dispatched"
    line = audit_lines(rig)[-1]
    assert line["verdict"].startswith("denied: this goal already has one open")
    assert first.id in _read_back(rig)


async def test_a_goal_filed_by_a_step_files_no_further_goal(cfg, seeded_vault):
    rig = _rig(cfg, seeded_vault, _use("sand the shed door"), "ok")
    child = rig.mind.goals.add("measure the shed floor", kind="task",
                               priority=0.95, provenance=f"{STEP_GOAL}g-parent")
    await work(rig)
    assert _children(rig, child) == []
    assert audit_lines(rig)[-1]["verdict"].startswith(
        "denied: this goal was itself filed from another goal")


async def test_a_goal_she_already_carries_is_named_not_filed(cfg, seeded_vault):
    rig = _rig(cfg, seeded_vault,
               _use("research good cedar suppliers for the garden shed roof"), "ok")
    carried = rig.mind.goals.add("research cedar suppliers for the shed roof",
                                 kind="task", priority=0.3)
    parent = rig.mind.goals.add("plan the garden shed", kind="task", priority=0.95)
    await work(rig)
    assert _children(rig, parent) == []
    back = _read_back(rig)
    assert '"status": "existing"' in back and carried.id in back
    desk = rig.mind.workspace.read(f"goals/{parent.id}.md")
    assert "already carrying it: “research cedar suppliers" in desk


async def test_a_part_that_quotes_its_parent_is_still_filed(cfg, seeded_vault):
    """The rewording test skips the parent, which a part names by nature."""
    rig = _rig(cfg, seeded_vault,
               _use("price the timber for the garden shed plan"), "ok")
    parent = rig.mind.goals.add("make the garden shed plan with the timber",
                                kind="task", priority=0.95)
    await work(rig)
    assert len(_children(rig, parent)) == 1


async def test_the_filing_switch_refuses_it(cfg, seeded_vault):
    rig = _rig(cfg, seeded_vault, _use("measure the shed floor"), "ok",
               mind_goal_filing_enabled=False)
    parent = rig.mind.goals.add("plan the shed", kind="task", priority=0.95)
    await work(rig)
    assert _children(rig, parent) == []
    assert rig.runner.calls == []
    assert audit_lines(rig)[-1]["verdict"] == (
        "denied: filing goals of your own is switched off")


async def test_the_nights_work_is_not_a_goal(cfg, seeded_vault):
    rig = _rig(cfg, seeded_vault, _use("consolidate last night's memory"), "ok")
    parent = rig.mind.goals.add("plan the shed", kind="task", priority=0.95)
    await work(rig)
    assert _children(rig, parent) == []
    assert "the night already does that" in audit_lines(rig)[-1]["verdict"]


def test_a_night_job_is_not_offered_it(cfg, seeded_vault):
    rig = _rig(cfg, seeded_vault)
    assert "create_goal" in rig.mind.hands.offer(
        state=rig.mind.activity.state, pressure=0.0, user_present=False).tools
    offer = LoopHands(rig.mind).offer()
    assert "create_goal" not in offer.tools and "write_note" in offer.tools

    only = _rig(cfg, seeded_vault, allow="create_goal")
    offer = LoopHands(only.mind).offer()
    assert not offer and offer.reason


def test_the_store_holds_one_open_child_per_parent(cfg, seeded_vault):
    rig = _rig(cfg, seeded_vault)
    a = rig.mind.goals.add("measure the floor", provenance=f"{STEP_GOAL}g-1")
    assert rig.mind.goals.add("buy paint", provenance=f"{STEP_GOAL}g-1").id == a.id
    assert rig.mind.goals.add("buy paint", provenance=f"{STEP_GOAL}g-2").id != a.id


async def test_a_goal_too_long_is_refused_unspent_and_rewritten_free(
        cfg, seeded_vault):
    """§22.1d: the shape is checked before dispatch, and fixing it is not a call
    — so a step with one call left still files the goal it meant to."""
    rig = _rig(cfg, seeded_vault, _use("measure the shed floor " * 12),
               _use("measure the shed floor"), "Split off; back to the plans.",
               tool_max_calls_per_turn=1)
    parent = rig.mind.goals.add("plan the shed", kind="task", priority=0.95)
    calls = len(rig.runner.calls)

    await work(rig)
    [child] = _children(rig, parent)
    assert child.text == "measure the shed floor"
    assert len(rig.runner.calls) == calls + 1, "the long one never reached a server"
    refused, filed = audit_lines(rig)[-2:]
    assert refused["verdict"].startswith(
        f"denied: a goal is one line of at most {GOAL_TEXT_MAX} characters")
    assert filed["verdict"] == "ok"
