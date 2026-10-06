"""Hands in the loop (SPEC §26, as amended) — the default-off proof, and the
five questions §26 actually deferred.

The capability is a tool call nobody asked for, at four in the morning, with
nobody in the room. Every test here is one of the ways that goes wrong: it
speaks, it repeats, it spends conversation's rate limit, it spends money the
budget already said no to, or it keeps going after somebody flipped the switch.
"""
from __future__ import annotations

import asyncio
import json
import logging

import pytest

from yurios.kernel.clock import Clock
from yurios.mind import loop as mind_loop
from yurios.mind.hands import (CHEAP, EXPENSIVE, HANDS, Hands, describe_hands,
                               klass, native_call, parse_intent,
                               strip_native_calls)
from yurios.world.tools.fakes import FakeToolRunner

from .conftest import ScriptedUtility, make_mind, run_mind


def hands_cfg(cfg, allow="write_note", **extra):
    """Both switches on and an explicit allowlist — the only configuration in
    which any of this does anything at all."""
    return cfg.model_copy(update={"mind_tools_enabled": True,
                                  "mind_tool_allowlist": allow, **extra})


def audit_lines(rig):
    path = rig.mind.cfg.tool_log_dir / "calls.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


async def work(rig, ticks=1):
    """Tick past the consider cooldown, so each tick is a fresh working step."""
    traces = []
    for _ in range(ticks):
        traces.append(await rig.mind.tick())
        rig.clock.advance(rig.mind.cfg.mind_consider_cooldown_s + 60)
    return traces


# --- the default-off proof -------------------------------------------------------

async def test_switches_off_the_tool_step_appraisal_never_wins(cfg, seeded_vault):
    """On any input, ever. This is the property everything else rests on."""
    utility = ScriptedUtility(*['use write_note {"path": "n.md", "text": "x"}'] * 8)
    cfg = cfg.model_copy(update={"mind_tools_enabled": False})
    rig = make_mind(cfg, seeded_vault, utility=utility, tools=FakeToolRunner())
    rig.mind.goals.add("tidy my notes", kind="task", priority=0.95)
    rig.mind.goals.add("think about the shed", kind="task", priority=0.9)
    rig.mind.bus.post("task_completion", {"task": "something"}, source="host")
    rig.say("hello", reply="I'll look into that tonight.")

    traces = await work(rig, ticks=6)
    for trace in traces:
        assert not trace["decided"]["intention"].startswith("tool_step:")
        assert trace["decided"]["hands"]["available"] == []
        # off means invisible: not even a runner-up explaining itself
        assert trace["decided"]["hands"]["blocked"] == ""
        for a in trace["appraised"]:
            if a["what"].startswith("tool_step"):
                assert a["score_to_act"] < rig.mind.cfg.mind_act_threshold
    assert audit_lines(rig) == [], "a hand she may not use is never dispatched"


async def test_the_house_switch_on_but_the_allowlist_empty_is_still_off(
        cfg, seeded_vault):
    """Turning the capability on and choosing which hands are two decisions."""
    rig = make_mind(cfg, seeded_vault,
                    utility=ScriptedUtility('use write_note {"path": "n.md"}'),
                    tools=FakeToolRunner())
    rig.mind.cfg = hands_cfg(cfg, allow="")
    rig.mind.hands.cfg = rig.mind.cfg
    rig.mind.goals.add("tidy my notes", kind="task", priority=0.95)
    trace = (await work(rig))[0]
    assert not rig.mind.hands.enabled
    assert trace["decided"]["hands"]["blocked"] == "no hand is on MIND_TOOL_ALLOWLIST"
    assert audit_lines(rig) == []


async def test_a_character_switched_off_cannot_use_the_house_capability(
        cfg, seeded_vault):
    """The kill switch, and the second of the two switches in series."""
    rig = make_mind(cfg, seeded_vault, utility=ScriptedUtility(
        'use write_note {"path": "n.md", "text": "x"}'),
        tools=FakeToolRunner())
    rig.mind.cfg = hands_cfg(cfg)
    rig.mind.hands.cfg = rig.mind.cfg
    rig.mind.set_hands_enabled(False)
    rig.mind.goals.add("tidy my notes", kind="task", priority=0.95)
    trace = (await work(rig))[0]
    assert "switched off" in trace["decided"]["hands"]["blocked"]
    assert audit_lines(rig) == []


# --- a hand that does work --------------------------------------------------------

def rig_with_hands(cfg, vault, *lines, allow="write_note", tools=None,
                   utility=None, **extra):
    runner = tools if tools is not None else FakeToolRunner()
    rig = make_mind(cfg, vault, utility=utility or ScriptedUtility(*lines),
                    tools=runner)
    rig.mind.cfg = hands_cfg(cfg, allow=allow, **extra)
    rig.mind.hands.cfg = rig.mind.cfg
    from yurios.mind.hands import build_guard
    rig.mind.hands.guard = build_guard(rig.mind.cfg, rig.clock)
    rig.runner = runner
    return rig


async def test_a_desk_hand_is_a_step_of_a_goal_and_lands_in_the_audit(
        cfg, seeded_vault):
    rig = rig_with_hands(
        cfg, seeded_vault,
        'use write_note {"path": "goals/shed.md", "text": "measure it first"}')
    goal = rig.mind.goals.add("plan the shed", kind="task", priority=0.95)

    trace = (await work(rig))[0]
    assert trace["decided"]["intention"].startswith("tool_step:")
    assert trace["acted"]["tool"] == "write_note"
    assert rig.runner.calls[0][0] == "write_note"

    line = audit_lines(rig)[-1]
    assert line["verdict"] == "ok"
    # one honest record of what her hands did, with the kind that tells the two
    # apart — "what did she reach for on her own" is a filter, not an inference
    assert line["origin"] == "mind_tool"
    assert line["tick_id"] == trace["tick_id"]
    # …and the goal that wanted it (principle 7) is the one that advanced
    assert rig.mind.goals.get(goal.id).state == "active"
    assert rig.mind.goals.get(goal.id).steps == 1


class _SlowRunner(FakeToolRunner):
    """A hand whose answer never comes back in time — the shape a slow disk
    takes from the host's side of the wire."""

    async def call(self, tool, args):
        self.calls.append((tool, dict(args)))
        raise TimeoutError()


async def test_a_hand_that_timed_out_says_so_in_the_trace_and_the_audit(
        cfg, seeded_vault):
    """One call, two records, and they used to disagree: `error` with a blank
    result in `calls.jsonl`, `write_note (ok)` in the tick trace, and
    `error ()` on her desk — a timeout nobody could read as one."""
    rig = rig_with_hands(
        cfg, seeded_vault,
        'use write_note {"path": "goals/shed.md", "text": "measure it first"}',
        tools=_SlowRunner())
    goal = rig.mind.goals.add("plan the shed", kind="task", priority=0.95)

    trace = (await work(rig))[0]
    assert trace["acted"]["verdict"] == "error"
    assert trace["acted"]["result"].endswith("write_note (error)")

    line = audit_lines(rig)[-1]
    assert line["verdict"] == "error"
    assert line["result"].startswith("timed out after ")
    desk = (seeded_vault / "workspace").rglob("*.md")
    assert any("error (timed out after " in p.read_text() for p in desk)
    # a failed step is still a step: the goal advanced, it did not vanish
    assert rig.mind.goals.get(goal.id).steps == 1


async def test_the_step_that_writes_the_file_can_be_the_step_that_finishes(
        cfg, seeded_vault):
    """Found live: she put "goal complete" inside the `append_note` text, where
    nothing reads it, and a finished goal parked for twelve hours. Her reason
    rides beside the call now, and it is what the desk and the lifecycle read."""
    rig = rig_with_hands(
        cfg, seeded_vault,
        'think that is the last of it — goal complete\n'
        'use write_note {"path": "notes/tiles.md", "text": "heat gun"}')
    goal = rig.mind.goals.add("work out the tiles", kind="task", priority=0.95)

    trace = (await work(rig))[0]
    assert trace["acted"]["tool"] == "write_note"
    assert rig.mind.goals.get(goal.id).state == "done"
    desk = rig.mind.vault.read(f"workspace/goals/{goal.id}.md")
    assert "that is the last of it" in desk, "the why survives, not just the result"


async def test_a_list_notes_catalog_is_kept_whole_on_the_goal_desk(
        cfg, seeded_vault):
    """Found live: 160 characters of pretty JSON was one diary file, and the
    next tick retried list_notes for days."""
    listing = json.dumps({
        "count": 19, "shown": 19, "truncated": False,
        "files": [{"path": f"diary/2026-08-{i:02d}.md", "bytes": 900}
                  for i in range(9, 28)]})
    rig = rig_with_hands(
        cfg, seeded_vault,
        'use list_notes {"folder": "diary"}',
        allow="list_notes",
        tools=FakeToolRunner(results={"list_notes": listing}))
    goal = rig.mind.goals.add("see the diary", kind="task", priority=0.95)
    await work(rig)
    desk = rig.mind.vault.read(f"workspace/goals/{goal.id}.md")
    assert "diary/2026-08-27.md" in desk
    assert "count" in desk


async def test_the_same_call_is_not_re_dispatched_every_tick(cfg, seeded_vault):
    """`Guard.turn()` is one dedupe scope per reply, and the mind has ticks."""
    line = 'use write_note {"path": "n.md", "text": "the same thing"}'
    # a step chains until she thinks, so each tick is one reach and one thought
    rig = rig_with_hands(cfg, seeded_vault, line, "think noted", line,
                         "think noted", line, "think noted")
    rig.mind.goals.add("keep a note", kind="task", priority=0.95)

    traces = await work(rig, ticks=3)
    calls = [c for c in rig.runner.calls if c[0] == "write_note"]
    assert len(calls) == 1, f"one call, not {len(calls)}"
    denials = [a for a in audit_lines(rig) if a["verdict"].startswith("denied")]
    assert any("cooldown" in a["verdict"] for a in denials)
    # …and a refused reach reads as a refused reach in the trace, not as a tick
    # where she happened to think instead — only one of the two is a reason to
    # go and change a knob
    assert traces[1]["acted"]["verdict"] == "denied"
    assert traces[1]["acted"]["tool"] == "write_note"


async def test_the_daily_cap_denies_exactly_once_per_attempt_and_audits_it(
        cfg, seeded_vault):
    """A cap, not a governor: checked *before* dispatch, and it refuses."""
    lines = [f'use write_note {{"path": "n{i}.md", "text": "x"}}' for i in range(4)]
    rig = rig_with_hands(cfg, seeded_vault, *lines, mind_tool_calls_per_day=2)
    rig.mind.goals.add("keep notes", kind="task", priority=0.95,
                       meta={"steps": -20})   # plenty of step budget

    await work(rig, ticks=4)
    ok = [a for a in audit_lines(rig) if a["verdict"] == "ok"]
    assert len(ok) == 2, "the cap is absolute"
    assert len([c for c in rig.runner.calls if c[0] == "write_note"]) == 2
    # …and the block is visible where a person looks for it
    assert any("spent" in a["verdict"] for a in audit_lines(rig)) or \
        rig.mind.hands.offer(state="IDLE", pressure=0.0,
                             user_present=False).reason.endswith("are spent")


async def test_a_fumbled_call_is_handed_back_not_run_or_booked(cfg, seeded_vault):
    """Run with `{}`, a fumble failed at the server and booked `{}` in the
    ledger: six hours of "she already did this" for every later fumble."""
    rig = rig_with_hands(
        cfg, seeded_vault,
        'use write_note {"path": "goals/shed.md", text: oops}',
        'use write_note {"path": "goals/shed.md", "text": "measure it first"}',
        "think written")
    rig.mind.goals.add("plan the shed", kind="task", priority=0.95)

    await work(rig)
    assert [c[0] for c in rig.runner.calls] == ["write_note"], "only the good one ran"
    fumble = audit_lines(rig)[0]
    assert fumble["verdict"].startswith("denied: the arguments are not valid JSON")
    assert not any(fp.endswith("\0{}") for fp in rig.mind.hands.ledger)
    assert audit_lines(rig)[-1]["verdict"] == "ok"


async def test_her_bucket_is_not_conversations_bucket(cfg, seeded_vault):
    """A night of autonomous work must not leave the morning's request denied."""
    rig = rig_with_hands(cfg, seeded_vault,
                         *[f'use write_note {{"path": "n{i}.md", "text": "x"}}'
                           for i in range(6)],
                         mind_tool_calls_per_day=50, tool_rate_mind_desk=4)
    # conversation's guard, with its own bucket for the same tool
    rig.mind.brain.guard.allow("write_note", 20)
    rig.mind.goals.add("keep notes", kind="task", priority=0.95,
                       meta={"steps": -20})

    # spend the mind's bucket flat inside one minute (no clock advance)
    for i in range(6):
        rig.mind.hands.ledger.clear()
        await rig.mind.tick()
        rig.mind.considered.clear()

    mine = [a for a in audit_lines(rig) if a["origin"] == "mind_tool"]
    assert any(a["verdict"] == "denied: rate limit" for a in mine), \
        "her own bucket is what runs out"
    ok, why = rig.mind.brain.guard.check("write_note", {"path": "x.md"})
    assert ok, f"conversation is untouched by it — got {why!r}"


# --- the expensive class ------------------------------------------------------------

async def test_budget_pressure_sheds_the_expensive_hands_and_keeps_the_cheap(
        cfg, seeded_vault):
    rig = rig_with_hands(cfg, seeded_vault, allow="write_note,research",
                         search_backend="fake", mind_tool_pressure_ceiling=0.5)
    over = rig.mind.hands.offer(state="DORMANT", pressure=0.9, user_present=False)
    assert over.tools == ("write_note",), "expensive hands wait for the budget"
    under = rig.mind.hands.offer(state="DORMANT", pressure=0.1, user_present=False)
    assert set(under.tools) == {"write_note", "research"}
    # …and she is told which ones wait, and on what
    assert over.held == ("research",) and "budget" in over.held_why
    assert under.held == () and under.waiting() == ""


async def test_expensive_hands_wait_for_an_empty_room(cfg, seeded_vault):
    rig = rig_with_hands(cfg, seeded_vault, allow="write_note,research",
                         search_backend="fake")
    watched = rig.mind.hands.offer(state="IDLE", pressure=0.0, user_present=True)
    assert watched.tools == ("write_note",)
    alone = rig.mind.hands.offer(state="IDLE", pressure=0.0, user_present=False)
    assert "research" in alone.tools


async def test_a_hand_that_waits_is_named_to_her_not_left_out(cfg, seeded_vault):
    """Left off the list, a web hand read as one she does not have: live, she
    wrote "I don't have a web-browsing tool" on her desk about a search that
    was only waiting for the room to empty. Named, it is one she has later."""
    from yurios.mind.goalwork import work_system
    rig = rig_with_hands(cfg, seeded_vault, allow="write_note,web_search",
                         search_backend="fake")
    goal = rig.mind.goals.add("find out about the tiles", kind="task")
    watched = rig.mind.hands.offer(state="IDLE", pressure=0.0, user_present=True)
    assert watched.held == ("web_search",)

    system = work_system(rig.mind, goal, watched, False)
    assert "use write_note" in system
    assert "use web_search" not in system, "still not offered this step"
    assert "Also yours, but not this step: web_search" in system
    assert "until the room is empty" in system
    # …and a reach for it anyway is refused with the same reason, not a state
    ok, why = rig.mind.hands.check("web_search", {"query": "tiles"}, state="IDLE",
                                   pressure=0.0, user_present=True)
    assert not ok and why == ("web_search is held back — they wait until the "
                              "room is empty")


async def test_a_step_reads_back_what_it_just_wrote(cfg, seeded_vault):
    """The ledger stops a goal repeating a call hourly; reading her own desk is
    not that loop. Live, a step wrote a note and was refused the read that
    checked it — "360 min of cooldown left" — because a failed read of the
    same path, a moment earlier, was booked like a web search."""
    note = '{"path": "notes/n.md"}'
    rig = rig_with_hands(
        cfg, seeded_vault,
        f"use read_note {note}",
        'use write_note {"path": "notes/n.md", "text": "x"}',
        f"use read_note {note}",
        "think it landed",
        f"use read_note {note}",
        "think still there",
        allow="write_note,read_note")
    rig.mind.goals.add("keep a note", kind="task", priority=0.95,
                       meta={"steps": -20})

    await work(rig, ticks=2)
    assert [c[0] for c in rig.runner.calls] == \
        ["read_note", "write_note", "read_note", "read_note"]
    assert not [a for a in audit_lines(rig) if a["verdict"].startswith("denied")]
    # the write is still on the ledger; the reads never were
    assert rig.mind.hands.cooling("write_note",
                                  {"path": "notes/n.md", "text": "x"}) > 0
    assert rig.mind.hands.cooling("read_note", {"path": "notes/n.md"}) == 0.0
    assert rig.mind.hands.spent["count"] == 4, "every read still counts to the cap"


async def test_a_hand_the_house_never_installed_is_not_on_the_allowlist(
        cfg, seeded_vault):
    """`SEARCH_BACKEND=off`'s rule: unadvertised, not merely denied."""
    rig = rig_with_hands(cfg, seeded_vault, allow="research,take_selfie",
                         search_backend="off", selfie_backend="off")
    assert rig.mind.hands.allowlist == ()
    assert not rig.mind.hands.enabled


def _offer_while_talking(cfg, seeded_vault, **extra):
    rig = rig_with_hands(cfg, seeded_vault, allow="write_note,research", **extra)
    return rig.mind.hands.offer(state="ENGAGED", pressure=0.0, user_present=True)


async def test_a_local_utility_model_stands_down_while_she_is_talking(
        cfg, seeded_vault):
    """One model on this machine cannot reply and run her hands at once."""
    for model in ("lm_studio/local", "ollama/qwen3", "gguf/someone/model"):
        engaged = _offer_while_talking(cfg, seeded_vault, utility_model=model)
        assert not engaged, model
        assert "mid-conversation" in engaged.reason


async def test_a_hosted_utility_model_keeps_the_cheap_hands_while_she_is_talking(
        cfg, seeded_vault):
    """OpenRouter (and a bare id, which is OpenRouter) is not her GPU."""
    for model in ("openrouter/deepseek/deepseek-v4-flash", "deepseek/deepseek-v4-flash",
                  "openai/gpt-5", "NONE", ""):
        engaged = _offer_while_talking(cfg, seeded_vault, utility_model=model)
        assert engaged.tools == ("write_note",), model
        assert engaged.reason == ""
        assert "research" not in engaged.tools


async def test_mind_tools_during_chat_overrides_where_the_utility_model_runs(
        cfg, seeded_vault):
    local = _offer_while_talking(cfg, seeded_vault, utility_model="lm_studio/local",
                                 mind_tools_during_chat="on")
    assert local.tools == ("write_note",)
    hosted = _offer_while_talking(
        cfg, seeded_vault, utility_model="openrouter/deepseek/deepseek-v4-flash",
        mind_tools_during_chat="off")
    assert not hosted
    assert "mid-conversation" in hosted.reason
    # a typo is auto, and auto on a local model still stands down
    typo = _offer_while_talking(cfg, seeded_vault, utility_model="gguf/someone/model",
                                mind_tools_during_chat="sometimes")
    assert not typo and "mid-conversation" in typo.reason


async def test_she_writes_a_note_during_a_conversation_when_utility_is_hosted(
        cfg, seeded_vault):
    rig = rig_with_hands(
        cfg, seeded_vault,
        'use write_note {"path": "notes/x.md", "text": "while we talk"}',
        utility_model="openrouter/deepseek/deepseek-v4-flash")
    rig.mind.goals.add("jot this down", kind="task", priority=0.95)
    rig.mind.turn_started()

    trace = await rig.mind.tick()

    assert trace["activity_state"] == "ENGAGED"
    assert trace["decided"]["hands"]["blocked"] == ""
    assert "write_note" in trace["decided"]["hands"]["available"]
    assert trace["decided"]["intention"].startswith("tool_step:")
    assert trace["acted"]["tool"] == "write_note"


async def test_she_does_not_reach_for_a_hand_during_a_conversation_on_a_local_model(
        cfg, seeded_vault):
    rig = rig_with_hands(
        cfg, seeded_vault,
        'use write_note {"path": "notes/x.md", "text": "while we talk"}',
        utility_model="lm_studio/local")
    rig.mind.goals.add("jot this down", kind="task", priority=0.95)
    rig.mind.turn_started()

    trace = await rig.mind.tick()

    assert trace["activity_state"] == "ENGAGED"
    assert trace["decided"]["hands"]["available"] == []
    assert "mid-conversation" in trace["decided"]["hands"]["blocked"]
    assert not trace["decided"]["intention"].startswith("tool_step:")


# --- the landing rule ----------------------------------------------------------------

async def test_a_dispatched_run_parks_the_goal_and_task_completion_wakes_it(
        cfg, seeded_vault):
    """`waiting` is the state the whole return path exists for."""
    rig = rig_with_hands(cfg, seeded_vault,
                         'use research {"topic": "tide tables", "depth": 2}',
                         "think and now I know.",
                         allow="research", search_backend="fake",
                         # longer than `work()` advances, so this test is about
                         # the ordinary return path and not about the safety net
                         # (which has a test of its own, below)
                         mind_dispatch_timeout_s=6 * 3600.0)
    goal = rig.mind.goals.add("find out about the tides", kind="task",
                              priority=0.95)
    await work(rig)

    parked = rig.mind.goals.get(goal.id)
    assert parked.state == "waiting"
    assert parked.dispatched["tool"] == "research"
    assert goal.id in rig.mind.wakeups, "and a floor under how long it waits"

    # …and while it waits, it is not re-appraised and not re-dispatched
    trace = (await work(rig))[0]
    assert not trace["decided"]["intention"].startswith("tool_step:")

    rig.mind.bus.post("task_completion",
                      {"task": "reading up on tide tables", "kind": "research",
                       "goal_id": goal.id, "deliver": "vault", "pages": 2},
                      source="research")
    await rig.mind.tick()
    woken = rig.mind.goals.get(goal.id)
    assert woken.state == "active"
    assert woken.dispatched == {}
    assert rig.post.proactive() == [], "the product is not a delivery (§18)"
    day_files = list((seeded_vault / "memory" / "episodic").glob("*.md"))
    assert any("not in the chat" in p.read_text() for p in day_files)


async def test_a_stranded_goal_is_woken_rather_than_lost(cfg, seeded_vault):
    """`task_completion` is the ordinary way back. This is the safety net for
    the run that died without posting one."""
    rig = rig_with_hands(cfg, seeded_vault,
                         'use research {"topic": "tide tables"}',
                         "think picking it up myself.",
                         allow="research", search_backend="fake",
                         mind_dispatch_timeout_s=600.0)
    goal = rig.mind.goals.add("find out about the tides", kind="task",
                              priority=0.95)
    await work(rig)
    assert rig.mind.goals.get(goal.id).state == "waiting"

    rig.clock.advance(1200)
    await rig.mind.tick()
    assert rig.mind.goals.get(goal.id).state == "active"
    day_files = list((seeded_vault / "memory" / "episodic").glob("*.md"))
    assert any("never came back" in p.read_text() for p in day_files)


async def test_an_autonomous_call_at_3am_with_a_page_open_still_does_not_speak(
        cfg, seeded_vault):
    """Gate 2 is the only thing that may reach the user, and it is not here."""
    rig = rig_with_hands(
        cfg, seeded_vault,
        'use write_note {"path": "notes/3am.md", "text": "an idea"}')
    rig.speak.connected = True                     # a page IS open
    rig.clock.advance(18 * 3600)                   # …at 03:00
    rig.mind.goals.add("write down the idea", kind="task", priority=0.95)

    await work(rig)
    assert rig.post.proactive() == []
    assert not any(c["delivered"] for c in rig.speak.calls)


# --- the kill switch ------------------------------------------------------------------

async def test_revoking_hands_mid_run_denies_the_next_call_and_audits_it(
        cfg, seeded_vault):
    rig = rig_with_hands(cfg, seeded_vault,
                         'use write_note {"path": "a.md", "text": "one"}',
                         "think that is one",
                         'use write_note {"path": "b.md", "text": "two"}',
                         "think and two")
    rig.mind.goals.add("keep notes", kind="task", priority=0.95,
                       meta={"steps": -20})
    await work(rig)
    assert len([c for c in rig.runner.calls if c[0] == "write_note"]) == 1

    rig.mind.set_hands_enabled(False)              # the switch, mid-flight
    await work(rig)
    assert len([c for c in rig.runner.calls if c[0] == "write_note"]) == 1, \
        "nothing after the revoke is dispatched"
    # nothing was cancelled and nothing was hidden: the denial is a line
    assert rig.mind.hands.offer(state="IDLE", pressure=0.0,
                                user_present=False).reason


# --- a restart: the mind starts before her hands do (SPEC §26.3) -------------------

def rig_mid_spawn(cfg, vault, **extra):
    """Her hands configured and granted, the tool server still coming up: the
    runner is not on the brain yet and discovery has not answered."""
    rig = rig_with_hands(cfg, vault, **extra)
    rig.mind.brain.set_tools(None, [])
    settled = asyncio.Event()
    rig.mind.set_hands_boot(settled)
    return rig, settled


def first_tick_offer(rig):
    """Stand in for `tick` and record what the first one would have been
    offered, then stop the loop — the offer is the thing under test."""
    seen = []

    async def tick():
        seen.append(rig.mind.hands.offer(state="DORMANT", pressure=0.0,
                                         user_present=False))
        raise asyncio.CancelledError
    rig.mind.tick = tick
    return seen


async def test_hands_on_their_way_are_starting_not_absent(cfg, seeded_vault):
    """The trace said "no tool server is running" on the first tick after
    every restart, about a server that was eighteen seconds from ready."""
    rig, settled = rig_mid_spawn(cfg, seeded_vault)
    offer = rig.mind.hands.offer(state="DORMANT", pressure=0.0,
                                 user_present=False)
    assert not offer and offer.reason == "her hands are still starting"

    settled.set()                              # discovery answered: it failed
    offer = rig.mind.hands.offer(state="DORMANT", pressure=0.0,
                                 user_present=False)
    assert offer.reason == "no tool server is running"


async def test_the_first_tick_waits_for_her_hands_to_arrive(
        cfg, seeded_vault, monkeypatch):
    rig, settled = rig_mid_spawn(cfg, seeded_vault)
    monkeypatch.setattr(rig.clock, "sleep", Clock().sleep)  # a wait that waits
    seen = first_tick_offer(rig)
    task = asyncio.create_task(rig.mind.run())
    for _ in range(5):
        await asyncio.sleep(0)
    assert seen == [], "the first tick must not appraise before discovery answers"

    rig.mind.brain.set_tools(rig.runner, [])   # _start_tools wires, then settles
    settled.set()
    try:
        await asyncio.wait_for(task, timeout=5)
    except asyncio.CancelledError:
        pass
    assert "write_note" in seen[0].tools


async def test_a_hung_spawn_does_not_stop_her_heart(cfg, seeded_vault, caplog):
    """The wait is bounded: past it the tick goes ahead handless, and says why."""
    rig, _ = rig_mid_spawn(cfg, seeded_vault)
    seen = first_tick_offer(rig)
    before = rig.clock.now()
    with caplog.at_level(logging.WARNING, logger="mind.loop"):
        try:
            await rig.mind.run()
        except asyncio.CancelledError:
            pass
    assert rig.clock.now() - before == mind_loop.HANDS_BOOT_WAIT_S
    assert seen[0].reason == "her hands are still starting"
    assert "has not answered" in caplog.text


async def test_a_character_without_hands_does_not_wait_for_them(
        cfg, seeded_vault):
    rig, _ = rig_mid_spawn(cfg, seeded_vault)
    rig.mind.set_hands_enabled(False)
    seen = first_tick_offer(rig)
    before = rig.clock.now()
    try:
        await rig.mind.run()
    except asyncio.CancelledError:
        pass
    assert rig.clock.now() == before
    assert len(seen) == 1


# --- the pieces, unit-wise --------------------------------------------------------------

def test_cost_classes_are_a_table_not_a_guess():
    assert klass("write_note") == "cheap"
    assert klass("research") == "expensive"
    assert klass("rm") == ""


def test_every_hand_is_described_and_shaped():
    """The one table's whole point. A hand with no `does` is a name nobody
    filling in MIND_TOOL_ALLOWLIST can find the meaning of; one with no `args`
    is a malformed line in the prompt she answers."""
    for name, hand in HANDS.items():
        assert hand.klass in ("cheap", "expensive"), name
        assert hand.does and not hand.does.endswith("."), name
        assert hand.args.startswith("{") and hand.args.endswith("}"), name
        assert hand.needs in ("", "SEARCH_BACKEND", "SELFIE_BACKEND"), name
    assert set(CHEAP) | set(EXPENSIVE) == set(HANDS), "the classes partition it"
    assert not set(CHEAP) & set(EXPENSIVE)


def test_the_vocabulary_says_what_this_machine_can_actually_offer(cfg):
    """`describe_hands` is what both settings surfaces read: the answer to "I
    ticked research and nothing happens" is SEARCH_BACKEND, which is nowhere
    near MIND_TOOL_ALLOWLIST."""
    off = cfg.model_copy(update={"search_backend": "off", "selfie_backend": "on"})
    by_name = {hand["name"]: hand for hand in describe_hands(off)}
    assert by_name["write_note"] == {"name": "write_note", "klass": "cheap",
                                     "does": by_name["write_note"]["does"],
                                     "needs": "", "available": True}
    assert by_name["research"]["available"] is False
    assert by_name["research"]["needs"] == "SEARCH_BACKEND"
    assert by_name["take_selfie"]["available"] is True
    # with no config at hand the catalogue is still the whole vocabulary
    assert [h["name"] for h in describe_hands()] == list(HANDS)


def test_a_backend_that_is_off_is_a_dropped_hand_with_a_reason(cfg, clock, caplog):
    """It was dropped silently, which is how "she never researches" became
    unanswerable. And it is said once, not once per tick."""
    hands = Hands(cfg=hands_cfg(cfg, allow="write_note,research,rm -rf",
                                search_backend="off"), clock=clock)
    with caplog.at_level("WARNING"):
        assert hands.allowlist == ("write_note",)
        assert hands.allowlist == ("write_note",)
    said = [r.getMessage() for r in caplog.records]
    assert len(said) == 2, "one line per bad name, not one per read"
    assert any("SEARCH_BACKEND" in line and "research" in line for line in said)
    assert any("rm -rf" in line and "write_note" in line for line in said),         "an unknown name is told what the known ones are"


def test_an_unparseable_line_is_a_thought_not_an_error():
    """Nobody is waiting, so failing safe means failing towards thinking."""
    assert parse_intent("use write_note {oh no", allowed=("write_note",)).kind == "use"
    assert parse_intent("use rm {}", allowed=("write_note",)).kind == "think"
    assert parse_intent("", allowed=("write_note",)).kind == "think"
    thought = parse_intent("think the grout needs doing", allowed=())
    assert thought.kind == "think" and thought.text == "the grout needs doing"


def test_arguments_may_run_past_the_line_the_call_is_on():
    """Live, 28 Sep – 2 Oct: a night's `write_note` with its text written in
    paragraphs parsed as `{}`, failed at the server, and booked `{}` in the
    cooldown ledger — so every later one was "she already did this"."""
    allowed = ("write_note", "list_notes")
    for reply in ('use write_note {"path": "a.md",\n  "text": "one"}',
                  'use write_note {"path": "a.md", "text": "# A\nbody\nmore"}',
                  'think saving it\nuse write_note\n\n{"path": "a.md", "text": "one"}'):
        intent = parse_intent(reply, allowed=allowed)
        assert intent.args["path"] == "a.md" and not intent.fumbled, reply
    assert parse_intent('use write_note {"path": "a.md", "text": "a\nb"}',
                        allowed=allowed).args["text"] == "a\nb"
    bare = parse_intent("use list_notes\nand then I'll look {maybe}", allowed=allowed)
    assert (bare.args, bare.fumbled) == ({}, ""), "prose below is not arguments"


def test_a_fumbled_call_says_why_rather_than_running_empty():
    intent = parse_intent("use write_note {oh no", allowed=("write_note",))
    assert intent.kind == "use" and intent.args == {}
    assert "not valid JSON" in intent.fumbled


#: Verbatim from her prompt trace, 24 Sep: a compose call with no tools
#: declared, answered in DeepSeek's own call markup — and posted as a message.
DSML_READ = ('\n\n<｜DSML｜ calls>\n<｜DSML｜ invoke name="read_note">\n'
             '<｜DSML｜ parameter name="path" string="true">goals/g-ca9b706b5f2a.md'
             '</｜DSML｜ parameter>\n</｜DSML｜ invoke>\n</｜DSML｜ calls>')


def test_a_second_call_run_onto_the_line_does_not_eat_the_first_ones_args():
    reply = ('use list_notes {"folder": "notes"}'
             'use read_note {"path": "reports/a.md"}')
    intent = parse_intent(reply, allowed=("list_notes", "read_note"))
    assert (intent.tool, intent.args) == ("list_notes", {"folder": "notes"})
    assert intent.said == 'use list_notes {"folder": "notes"}'


def test_what_she_said_ends_at_the_call_that_runs():
    """SPEC §26.2: her reason and the call stay; a second call and anything
    below the first go, whether on its line, under it, or spread over lines."""
    allowed = ("read_note", "write_note")
    two = parse_intent('think I need both\nuse read_note {"path": "a.md"}\n'
                       'use read_note {"path": "b.md"}', allowed=allowed)
    assert two.said == 'think I need both\nuse read_note {"path": "a.md"}'
    made_up = parse_intent('use read_note {"path": "a.md"}\nResult: it says hi',
                           allowed=allowed)
    assert made_up.said == 'use read_note {"path": "a.md"}'
    pretty = parse_intent('saving\nuse write_note {\n  "path": "a.md",\n'
                          '  "text": "one"\n}\nuse read_note {"path": "a.md"}',
                          allowed=allowed)
    assert pretty.said == ('saving\nuse write_note {\n  "path": "a.md",\n'
                           '  "text": "one"\n}')
    # a fumbled call keeps the whole answer: the mistake is the point
    assert parse_intent("use write_note {oh no", allowed=allowed).said == ""


#: Verbatim shape from her prompt trace, 29 Sep (`t-d576030bfd7c`): a whole
#: chain written as one answer, each `use` run onto the sentence before it.
GLUED_CHAIN = (
    "think Grant is here and it's late — but this goal is mine.\n\n"
    "Let me read what I actually gathered."
    'use read_note {"path": "notes/sensual-language-craft.md"}'
    'use read_note {"path": "notes/desire-psychology.md"}'
    "think Okay — I have all three."
    'use write_skill {"name": "intimate-presence", "description": "d", '
    '"instructions": "i"}'
    "think Good — it's there.\n\n"
    "goal complete — step 1 of 3 done. Skill written, verified, ready to use.")


def test_a_call_run_onto_a_sentence_is_still_a_call():
    """No line began with `use`, so the whole chain parsed as one thought."""
    intent = parse_intent(GLUED_CHAIN, allowed=("read_note", "write_skill"))
    assert (intent.kind, intent.tool) == ("use", "read_note")
    assert intent.args == {"path": "notes/sensual-language-craft.md"}
    # her reason is the sentence the call was glued to, and nothing after it —
    # least of all the "goal complete" written before anything had run
    assert intent.text.endswith("Let me read what I actually gathered.")
    assert "goal complete" not in intent.text


def test_prose_about_a_hand_is_not_a_reach_for_it():
    """Mid-line it takes the brace; a longer word ending in "use" is a word."""
    for words in ("think I'll use read_note to check it later",
                  'think I could reuse read_note {"path": "a.md"} tomorrow'):
        assert parse_intent(words, allowed=("read_note",)).kind == "think"
    capital = parse_intent('Use read_note {"path": "a.md"}', allowed=("read_note",))
    assert (capital.kind, capital.tool) == ("use", "read_note")


def test_a_hand_she_was_not_offered_is_named_on_the_thought():
    """So a done-mark written beside it is not read as a finish (goalwork)."""
    intent = parse_intent('think that settles it — goal complete\n'
                          'use web_search {"query": "x"}',
                          allowed=("read_note",))
    assert intent.kind == "think" and intent.unrun == ("web_search",)
    # a word that is not a hand is not one she reached for
    assert parse_intent("use my head {really}", allowed=()).unrun == ()


async def test_a_glued_chain_runs_its_calls_instead_of_closing_on_them(
        cfg, seeded_vault):
    """The live failure end to end: every call ran, and only the answer she
    gave once they had closes the goal."""
    rig = rig_with_hands(
        cfg, seeded_vault, GLUED_CHAIN,
        'think read the second.use read_note {"path": "notes/desire-psychology.md"}',
        'think now the skill.use write_skill {"name": "intimate-presence", '
        '"description": "d", "instructions": "i"}',
        "think it's written — goal complete",
        allow="read_note,write_skill")
    goal = rig.mind.goals.add("turn the research into a skill", kind="task",
                              priority=0.95)
    trace = (await work(rig))[0]
    assert [c[0] for c in rig.runner.calls] == ["read_note", "read_note",
                                                "write_skill"]
    assert trace["acted"]["tools"][-1]["tool"] == "write_skill"
    assert rig.mind.goals.get(goal.id).state == "done"


async def test_a_second_call_she_was_not_answered_never_goes_back_to_her(
        cfg, seeded_vault):
    """Live, 4 Oct: two `read_note`s in one answer, the first run, and the
    whole answer sent back with one result under it — so she wrote the second
    result herself, 2,066 tokens of a note she never read."""
    utility = ScriptedUtility(
        'think check both\nuse read_note {"path": "diary/a.md"}\n'
        'use read_note {"path": "free-time/b.md"}',
        "think that's enough")
    rig = rig_with_hands(cfg, seeded_vault, allow="read_note", utility=utility)
    rig.mind.goals.add("look back at yesterday", kind="task", priority=0.95)
    await work(rig)
    assert [c[1]["path"] for c in rig.runner.calls] == ["diary/a.md"]
    asked = utility.calls[-1]
    hers = [m["content"] for m in asked if m["role"] == "assistant"]
    assert hers[-1].endswith('use read_note {"path": "diary/a.md"}')
    assert not any("free-time/b.md" in m["content"] for m in asked)


async def test_a_finish_beside_a_call_that_never_ran_leaves_the_goal_open(
        cfg, seeded_vault):
    """"goal complete" next to a hand she was not offered is a narrated finish."""
    rig = rig_with_hands(
        cfg, seeded_vault,
        'think found it all — goal complete\nuse web_search {"query": "tiles"}',
        allow="write_note")
    goal = rig.mind.goals.add("work out the tiles", kind="task", priority=0.95)
    trace = (await work(rig))[0]
    assert rig.runner.calls == []
    assert trace["acted"]["state"] == "active"
    assert rig.mind.goals.get(goal.id).state == "active"
    desk = rig.mind.vault.read(f"workspace/goals/{goal.id}.md")
    assert "web_search I wrote out never ran" in desk


async def test_a_finish_above_a_call_past_the_cap_leaves_the_goal_open(
        cfg, seeded_vault):
    """The call that would have finished it was dropped, so it did not."""
    rig = rig_with_hands(
        cfg, seeded_vault,
        'use write_note {"path": "notes/a.md", "text": "one"}',
        'think that is the last of it — goal complete\n'
        'use write_note {"path": "notes/b.md", "text": "two"}',
        tool_max_calls_per_turn=1)
    goal = rig.mind.goals.add("write both notes", kind="task", priority=0.95)
    await work(rig)
    assert [c[1]["path"] for c in rig.runner.calls] == ["notes/a.md"]
    assert rig.mind.goals.get(goal.id).state == "active"


def test_a_reach_in_deepseek_markup_is_the_call_she_meant():
    """No tools are ever declared, so DeepSeek sometimes writes its native
    markup instead of the `use` line. It is still a reach, not a thought —
    journalled as a thought it went into her memory verbatim."""
    intent = parse_intent("I need to see where I left it." + DSML_READ,
                          allowed=("read_note",))
    assert (intent.kind, intent.tool) == ("use", "read_note")
    assert intent.args == {"path": "goals/g-ca9b706b5f2a.md"}
    assert intent.text == "I need to see where I left it."
    typed = parse_intent(
        '<｜DSML｜function_calls>\n<｜DSML｜invoke name="set_timer">\n'
        '<｜DSML｜parameter name="minutes" string="false">5</｜DSML｜parameter>\n'
        '</｜DSML｜invoke>\n</｜DSML｜function_calls>', allowed=("set_timer",))
    assert typed.args == {"minutes": 5}
    refused = parse_intent(DSML_READ, allowed=("write_note",))
    assert refused.kind == "think" and "DSML" not in refused.text


def test_native_call_markup_never_survives_as_words():
    assert native_call(DSML_READ) == ("read_note",
                                      {"path": "goals/g-ca9b706b5f2a.md"})
    assert strip_native_calls(DSML_READ) == ""
    assert strip_native_calls("Hi. [shy] x | y") == "Hi. [shy] x | y"
    # a clipped block ends at the blank line, not at the end of the text
    clipped = ('before <｜DSML｜ calls>\n<｜DSML｜ invoke name="read_note">\n'
               '\nthe next entry')
    assert strip_native_calls(clipped) == "before \n\nthe next entry"


def test_a_reach_keeps_the_reason_she_wrote_beside_it():
    """A 12B model answers with both lines; the reasoning is the half that has
    to survive. Verbatim from a live run: dropping it made step 3 redo step 2."""
    intent = parse_intent(
        'think I don\'t have the exact quote yet, so I\'ll leave a placeholder.\n'
        'use write_note {"path": "notes/q.md", "text": "..."}',
        allowed=("write_note",))
    assert intent.kind == "use" and intent.tool == "write_note"
    assert intent.text == ("I don't have the exact quote yet, so I'll leave a "
                           "placeholder.")


def test_what_she_writes_below_a_call_is_not_her_reason_for_it():
    """Verbatim shape from GLM, 25 Sep: a result invented before the call ran."""
    intent = parse_intent(
        "think time to search\n"
        'use web_search {"query": "Artemis September 2026"}Result:\n\n'
        "1. Artemis III crew continues training\n"
        "think found it all — goal complete",
        allowed=("web_search",))
    assert (intent.kind, intent.tool) == ("use", "web_search")
    assert intent.text == "time to search"


def test_the_done_mark_is_read_off_a_reach_as_well_as_a_thought():
    """The step that finishes a goal is often the one that writes the file."""
    intent = parse_intent(
        'think that is the whole of it — goal complete\n'
        'use append_note {"path": "notes/q.md", "text": "..."}',
        allowed=("append_note",))
    assert intent.kind == "use"
    assert "goal complete" in intent.text.lower()


def test_the_catalog_names_only_the_offered_hands(cfg, clock):
    hands = Hands(cfg=hands_cfg(cfg, allow="write_note,read_note"), clock=clock)
    catalog = hands.catalog(("write_note",))
    assert "use write_note" in catalog
    assert "read_note" not in catalog, "off means invisible, per hand too"
    assert hands.catalog(()) == ""


def test_the_ledger_survives_a_restart(cfg, clock):
    hands = Hands(cfg=hands_cfg(cfg), clock=clock)
    hands.spend("write_note", {"path": "n.md"})
    assert hands.cooling("write_note", {"path": "n.md"}) > 0

    reborn = Hands(cfg=hands_cfg(cfg), clock=clock)
    reborn.load(hands.snapshot())
    assert reborn.cooling("write_note", {"path": "n.md"}) > 0
    assert reborn.spent["count"] == 1
    # a near-miss is her changing her mind, not a repeat (Guard._fingerprint)
    assert reborn.cooling("write_note", {"path": "other.md"}) == 0.0


# --- the landing rule, second half: what the gallery was a dead end for -------
#
# `_deliver: "vault"` is right that the lab must not post its own product, and
# it was the whole rule — so a picture she took because you asked for it could
# be described forever and shown never, and "show me" came back as a path to a
# goal file. These are the four places the photo now has to survive.

SHOT = "/selfies/1789648668-e3110ba4.png"


def _completion(goal_id, **over):
    payload = {"task": "a selfie she took", "kind": "selfie", "id": "e3110ba4",
               "goal_id": goal_id, "deliver": "vault", "image_url": SHOT,
               "detail": "the window seat, the lamp on the left"}
    return {**payload, **over}


async def test_a_finished_photo_is_kept_by_the_goal_that_asked_for_it(
        cfg, seeded_vault):
    """The render ends and the picture lands ON the goal (§18.2a). The lab
    still posts nothing — that half of the rule is untouched."""
    rig = rig_with_hands(cfg, seeded_vault,
                         *["think still looking at it."] * 4, allow="")
    goal = rig.mind.goals.add("send him the photo I promised", kind="task",
                              priority=0.95, provenance="promise:her-own-words")
    rig.mind.goals.update(goal.id, state="waiting",
                          meta={"dispatched": {"tool": "take_selfie"}})

    rig.mind.bus.post("task_completion", _completion(goal.id), source="selfies")
    await rig.mind.tick()

    woken = rig.mind.goals.get(goal.id)
    assert woken.state != "waiting", "the completion still wakes the goal"
    assert woken.product["image_url"] == SHOT
    assert woken.product["selfie_id"] == "e3110ba4"
    assert rig.post.proactive() == [], "and the lab still posts nothing (§18.2a)"
    day_files = list((seeded_vault / "memory" / "episodic").glob("*.md"))
    assert any("mine to send now" in p.read_text() for p in day_files), \
        "the journal line must not say 'not in the chat' about a photo she can send"


async def test_the_step_after_a_photo_lands_is_told_it_has_it(cfg, seeded_vault):
    """Live, 5 Oct: the step after the render landed saw "started a selfie"
    and `take_selfie (ok)` and nothing else, so she took it again — and the
    retake displaced the first, which was never sent."""
    from yurios.mind import goalwork

    rig = rig_with_hands(cfg, seeded_vault,
                         *["think still looking at it."] * 4, allow="")
    goal = rig.mind.goals.add("take the raincheck selfie", kind="task")
    rig.mind.goals.update(goal.id, state="waiting",
                          meta={"dispatched": {"tool": "take_selfie"}})
    assert "WHAT CAME BACK" not in await goalwork.context(rig.mind, goal)

    rig.mind.bus.post("task_completion", _completion(goal.id), source="selfies")
    await rig.mind.tick()

    shown = await goalwork.context(rig.mind, rig.mind.goals.get(goal.id))
    assert "WHAT CAME BACK FOR THIS" in shown
    assert "the window seat, the lamp on the left" in shown
    assert "not sent yet" in shown


async def test_a_retake_says_which_picture_it_displaced(cfg, seeded_vault):
    """One picture rides on a goal; the one a retake pushes off it must leave
    a line behind rather than vanish into the gallery."""
    rig = rig_with_hands(cfg, seeded_vault,
                         *["think still looking at it."] * 4, allow="")
    goal = rig.mind.goals.add("take the raincheck selfie", kind="task")
    rig.mind.goals.update(goal.id, state="waiting", meta={
        "dispatched": {"tool": "take_selfie"},
        "product": {"image_url": "/selfies/first.png", "selfie_id": "first"}})

    rig.mind.bus.post("task_completion", _completion(goal.id), source="selfies")
    await rig.mind.tick()

    assert rig.mind.goals.get(goal.id).product["image_url"] == SHOT
    day_files = list((seeded_vault / "memory" / "episodic").glob("*.md"))
    assert any("replaces /selfies/first.png" in p.read_text() for p in day_files)
    assert any("finished something I'd started: a selfie she took — the window "
               "seat, the lamp on the left" in p.read_text() for p in day_files), \
        "the journal line says which photo, not just that there was one"


async def test_a_failed_render_leaves_the_goal_holding_nothing(cfg, seeded_vault):
    """`task_completion` means the work is over, never that it worked (§16.3).
    A goal that thinks it is holding a picture would reach out with a dead url."""
    rig = rig_with_hands(cfg, seeded_vault,
                         *["think still looking at it."] * 4, allow="")
    goal = rig.mind.goals.add("send him the photo I promised", kind="task",
                              priority=0.95, provenance="promise:her-own-words")
    rig.mind.goals.update(goal.id, state="waiting",
                          meta={"dispatched": {"tool": "take_selfie"}})

    rig.mind.bus.post("task_completion",
                      _completion(goal.id, error="OutOfMemoryError",
                                  image_url=""),
                      source="selfies")
    await rig.mind.tick()

    assert rig.mind.goals.get(goal.id).product == {}


def _strays(rig):
    return [g for g in rig.mind.goals.all()
            if g.provenance == "followup:e3110ba4"]


async def test_a_picture_no_goal_holds_is_handed_to_a_reach_out(cfg, seeded_vault):
    """A night job's hands name no goal (§18.2a). Live, 6 Oct: the stock-take
    took the raincheck selfie at 02:10 and it sat in the gallery for good,
    because only a goal could ever have carried it to Gate 2."""
    rig = rig_with_hands(cfg, seeded_vault,
                         *["think still looking at it."] * 4, allow="")
    rig.mind.bus.post("task_completion", _completion(None, by="mind"),
                      source="selfies")
    await rig.mind.tick()

    [heir] = _strays(rig)
    assert heir.kind == "reach_out" and heir.commitment == "single-minded"
    assert heir.held_picture == SHOT
    assert heir.product["detail"] == "the window seat, the lamp on the left"
    assert rig.post.proactive() == [], "the lab's rule holds: Gate 2 sends it"

    # the same completion seen twice is one errand
    rig.mind.bus.post("task_completion", _completion(None, by="mind"),
                      source="selfies")
    await rig.mind.tick()
    assert len(_strays(rig)) == 1


async def test_a_goal_that_closed_before_its_picture_landed_still_hands_it_on(
        cfg, seeded_vault):
    rig = rig_with_hands(cfg, seeded_vault,
                         *["think still looking at it."] * 4, allow="")
    goal = rig.mind.goals.add("take the raincheck selfie", kind="task")
    rig.mind.goals.set_state(goal.id, "done")
    rig.mind.bus.post("task_completion", _completion(goal.id, by="mind"),
                      source="selfies")
    await rig.mind.tick()
    assert [g.held_picture for g in _strays(rig)] == [SHOT]


@pytest.mark.parametrize("over", [
    {"by": "owner"},                     # rendered from the gallery: theirs already
    {},                                  # nobody said it was hers
    {"by": "mind", "deliver": "chat"},   # already in the conversation
    {"by": "mind", "error": "OutOfMemoryError", "image_url": ""},
], ids=["owner", "unstamped", "in-chat", "failed"])
async def test_only_her_own_unsent_picture_gets_an_errand(cfg, seeded_vault, over):
    rig = rig_with_hands(cfg, seeded_vault,
                         *["think still looking at it."] * 4, allow="")
    rig.mind.bus.post("task_completion", _completion(None, **over),
                      source="selfies")
    await rig.mind.tick()
    assert _strays(rig) == []


async def test_a_promise_that_made_a_photo_hands_it_to_a_goal_that_can_send_it(
        cfg, seeded_vault):
    """The gap all three stranded photographs fell through: the goal that made
    the picture ended, the goal that could deliver it began, and nothing
    crossed — the news half offered a path to `goals/g-….md` instead."""
    rig = rig_with_hands(cfg, seeded_vault,
                         *["think goal complete — it's taken."] * 6, allow="")
    goal = rig.mind.goals.add("send him the photo I promised", kind="task",
                              priority=0.95, provenance="promise:her-own-words")
    rig.mind.goals.update(goal.id, state="waiting",
                          meta={"dispatched": {"tool": "take_selfie"}})
    rig.mind.bus.post("task_completion", _completion(goal.id), source="selfies")

    await work(rig, ticks=3)

    assert rig.mind.goals.get(goal.id).state == "done"
    followup = next(g for g in rig.mind.goals.all()
                    if g.provenance == f"followup:{goal.id}")
    assert followup.kind == "reach_out"
    assert followup.product["image_url"] == SHOT
    assert ".md" not in followup.text, \
        f"a picture, not a path to read about one: {followup.text}"
    assert followup.commitment == "single-minded", \
        "a promised photo is the promise; it does not expire as news does"


async def test_a_chat_goal_that_finishes_holding_a_photo_hands_it_on(
        cfg, seeded_vault):
    """`user:chat` is how "set a goal for …" arrives, and it is not a promise.

    She rendered the shot, the lab left it on the goal, and the next step
    said "goal complete" — including when that sentence was wrong about the
    chat. `offer_to_tell` used to return before it ever looked at the
    picture, so the work closed and Gate 2 had nothing left to send.
    """
    utility = ScriptedUtility(*["think goal complete — it's in the chat."] * 6)
    rig = make_mind(cfg, seeded_vault, utility=utility)
    goal = rig.mind.goals.add(
        "flash the shot he asked for", kind="task", priority=0.95,
        provenance="user:chat",
        meta={"product": {"image_url": SHOT, "selfie_id": "26cae5c8",
                          "detail": "the window, the lamp"}})

    await work(rig, ticks=3)

    assert rig.mind.goals.get(goal.id).state == "done"
    followup = next(g for g in rig.mind.goals.all()
                    if g.provenance == f"followup:{goal.id}")
    assert followup.kind == "reach_out"
    assert followup.product["image_url"] == SHOT
    assert followup.commitment == "single-minded"
    assert ".md" not in followup.text
    assert rig.mind.goals.get(goal.id).meta.get("offered") == followup.id


async def test_a_chat_goal_with_nothing_to_show_files_no_news(
        cfg, seeded_vault):
    """The prose half stays promise-only. A cup of coffee he asked for in
    chat is on the desk where he left it; finishing it is not an interrupt."""
    utility = ScriptedUtility(*["think goal complete."] * 6)
    rig = make_mind(cfg, seeded_vault, utility=utility)
    goal = rig.mind.goals.add(
        "make the coffee", kind="task", priority=0.95, provenance="user:chat")

    await work(rig, ticks=3)

    assert rig.mind.goals.get(goal.id).state == "done"
    assert not any(g.provenance == f"followup:{goal.id}"
                   for g in rig.mind.goals.all())


async def test_a_photo_already_delivered_to_chat_is_not_offered_again(
        cfg, seeded_vault):
    """The completion still records the product, but `deliver: chat` means the
    lab already posted it. Finishing the linked goal must not send it twice."""
    utility = ScriptedUtility(*["think goal complete."] * 6)
    rig = make_mind(cfg, seeded_vault, utility=utility)
    goal = rig.mind.goals.add(
        "flash the shot he asked for", kind="task", priority=0.95,
        provenance="user:chat")
    rig.mind.goals.update(goal.id, state="waiting",
                          meta={"dispatched": {"tool": "take_selfie"}})
    rig.mind.bus.post(
        "task_completion", _completion(goal.id, deliver="chat"),
        source="selfies")

    await work(rig, ticks=3)

    completed = rig.mind.goals.get(goal.id)
    assert completed.state == "done"
    assert completed.product["image_url"] == SHOT
    assert completed.product["deliver"] == "chat"
    assert not any(g.provenance == f"followup:{goal.id}"
                   for g in rig.mind.goals.all())


async def test_letting_the_goal_go_does_not_let_the_photo_go_with_it(
        cfg, seeded_vault):
    """She gave up on the goal. The picture is still real and still unseen."""
    from yurios.mind.util import iso_of
    rig = rig_with_hands(cfg, seeded_vault,
                         *["think nothing more to add."] * 6,
                         allow="", mind_goal_max_steps=1)
    goal = rig.mind.goals.add(
        "show him the one I promised", kind="task", priority=0.95,
        commitment="open-minded", due=iso_of(rig.clock.now() - 3600),
        provenance="promise:her-own-words",
        meta={"product": {"image_url": SHOT, "selfie_id": "e3110ba4"}})

    await work(rig)

    assert rig.mind.goals.get(goal.id).state == "abandoned"
    followup = next(g for g in rig.mind.goals.all()
                    if g.provenance == f"followup:{goal.id}")
    assert followup.product["image_url"] == SHOT


async def test_gate_2_arrives_holding_the_picture_not_a_sentence_about_it(
        cfg, seeded_vault):
    """The end of it: she passes Gate 2 and the photo is *in the message*.

    Before this, every delivery Gate 2 had was text — so the one thing the
    whole goal existed to hand over was the one thing that could not travel.
    """
    import datetime

    rig = make_mind(cfg, seeded_vault)
    rig.say("send me the one you promised", reply="When you're back. I mean it.")
    await rig.mind.tick()
    rig.mind.bus.post("user_absent", {}, source="frontend")
    due = datetime.datetime(2026, 7, 7, 18, 0)
    rig.mind.goals.add(
        "send them the picture I took for them — the window seat, the lamp",
        kind="reach_out", priority=0.8, due=due.isoformat(timespec="seconds"),
        commitment="single-minded", provenance="followup:g-6233dc189e71",
        meta={"product": {"image_url": SHOT, "selfie_id": "e3110ba4",
                          "detail": "the window seat, the lamp on the left"}})

    await run_mind(rig, hours=40)

    proactive = rig.post.proactive()
    assert proactive, "she never reached out at all"
    carried = [m for m in proactive if m.get("image_url") == SHOT]
    assert carried, f"Gate 2 passed and the picture stayed behind: {proactive}"
    assert carried[0].get("selfie_id") == "e3110ba4"
    assert all(m.get("unheard") for m in carried), \
        "a photo sent into an empty room is exactly what the inbox is for"
    g = next(g for g in rig.mind.goals.all() if g.provenance.startswith("followup:"))
    assert g.state == "done"


async def test_a_held_picture_is_never_let_go_of_quietly(cfg, seeded_vault):
    """Gate 2's SILENT branch drops a stale reach-out, because news keeps
    badly. A promised photo is not news — dropping it is the disappearance."""
    import datetime

    # a threshold that holds it: SILENT is the branch under test
    rig = make_mind(cfg.model_copy(update={"mind_interrupt_threshold": 0.75}),
                    seeded_vault)
    rig.mind.bus.post("user_absent", {}, source="frontend")
    # due in the past and open to being let go: the exact shape SILENT drops
    due = datetime.datetime(2026, 7, 5, 18, 0)
    goal = rig.mind.goals.add(
        "send them the picture I took for them — the window seat, the lamp",
        kind="reach_out", priority=0.2, due=due.isoformat(timespec="seconds"),
        commitment="open-minded", provenance="followup:g-6233dc189e71",
        meta={"product": {"image_url": SHOT, "selfie_id": "e3110ba4"}})

    await run_mind(rig, hours=12)

    # The invariant is not that this goal survives — `reconsider()` may still
    # sweep it — but that the picture is never left with nobody holding it.
    holding = [g for g in rig.mind.goals.all()
               if g.state in ("pending", "active", "waiting")
               and g.product.get("image_url") == SHOT]
    assert holding, \
        "the picture was let go of quietly — this is the whole failure"
    assert goal.id in {g.id for g in holding} or any(
        g.provenance.startswith("followup:") for g in holding)


async def test_a_parked_promise_does_not_park_the_picture_with_it(
        cfg, seeded_vault):
    """The exit her own promise actually took, and the one the rule missed.

    `g-6233dc189e71` — "Send the promised near-nude selfie … once the user
    answers the framing question" — is `single-minded`, `steps: 3`, `waiting`.
    It never reaches the let-go branch (that is `open-minded` only) and
    `reconsider` never sweeps it (same reason), so with the picture landed on
    it, it parks at the horizon holding a finished photograph no `task` goal
    can send, wakes twelve hours later, and parks again. Found by
    `scripts/live_check.py`, which drove the real lab and watched it happen.
    """
    rig = rig_with_hands(cfg, seeded_vault,
                         *["think nothing more I can do from here."] * 6,
                         allow="", mind_goal_max_steps=1)
    goal = rig.mind.goals.add(
        "send the one I promised once he answers", kind="task", priority=0.95,
        commitment="single-minded", provenance="promise:her-own-words",
        meta={"product": {"image_url": SHOT, "selfie_id": "e3110ba4",
                          "detail": "the window seat, the lamp on the left"}})

    await work(rig)

    parked = rig.mind.goals.get(goal.id)
    assert parked.state == "waiting", \
        f"this test is about the parked exit and she took another: {parked.state}"
    followup = next((g for g in rig.mind.goals.all()
                     if g.provenance == f"followup:{goal.id}"), None)
    assert followup is not None, \
        "she parked holding the picture and nothing was filed to send it"
    assert followup.kind == "reach_out"
    assert followup.product["image_url"] == SHOT
    assert parked.meta.get("offered") == followup.id, \
        "the goal that made it should say where the picture went"


async def test_the_picture_is_handed_on_once_however_many_exits_it_takes(
        cfg, seeded_vault):
    """Parked, sent, woken, finished — and not sent a second time.

    Every way out of a goal files the follow-up now, and one goal can take
    more than one of them. `already_carrying` dedupes on provenance but only
    across *open* goals, so once Gate 2 has delivered the picture and closed
    the errand, nothing would stop the next exit filing another one.
    """
    rig = rig_with_hands(cfg, seeded_vault,
                         "think nothing more I can do from here.",
                         "think that's it — goal complete.",
                         *["think nothing more."] * 4,
                         allow="", mind_goal_max_steps=1)
    goal = rig.mind.goals.add(
        "send the one I promised once he answers", kind="task", priority=0.95,
        commitment="single-minded", provenance="promise:her-own-words",
        meta={"product": {"image_url": SHOT, "selfie_id": "e3110ba4"}})

    await work(rig)                                   # …parks, and files one
    heir = next(g for g in rig.mind.goals.all()
                if g.provenance == f"followup:{goal.id}")
    rig.mind.goals.set_state(heir.id, "done")         # …Gate 2 sent it

    # …the wake lands (`acts.wake_goal`) and this time she finishes.
    rig.mind.goals.update(goal.id, state="active")
    rig.mind.considered.pop(goal.id, None)
    await work(rig)

    assert rig.mind.goals.get(goal.id).state == "done"
    heirs = [g for g in rig.mind.goals.all()
             if g.provenance == f"followup:{goal.id}"]
    assert len(heirs) == 1, \
        f"the same photograph was filed to be sent {len(heirs)} times: " \
        f"{[(g.id, g.state) for g in heirs]}"


# --- a reach-out gets ready with her hands before the gate (SPEC §18.2c) ------

def reach_rig(cfg, vault, *lines, allow="take_selfie,write_note"):
    """Hands on, the camera answering `started`, the room empty, and Gate 2
    opened: what is under test is the step before the gate, not its tuning."""
    rig = rig_with_hands(cfg, vault, *lines, allow=allow,
                         tools=FakeToolRunner(results={"take_selfie": {
                             "status": "started", "id": "e3110ba4"}}),
                         mind_interrupt_threshold=0.0)
    rig.mind.bus.post("user_absent", {}, source="frontend")
    return rig


async def test_a_reach_out_takes_the_picture_and_gate_2_sends_it(cfg, seeded_vault):
    """"Send him a selfie" was a `reach_out`, and a reach-out could only ever
    compose a sentence — so Gate 2 ruled on it all day and no picture existed.
    Now the goal reaches for the camera first, holds what comes back, and the
    delivery is the photograph."""
    rig = reach_rig(cfg, seeded_vault,
                    'think he asked for more like the last one — take it first\n'
                    'use take_selfie {"look": "by the window, warm lamp"}')
    goal = rig.mind.goals.add("send him a new selfie, like the one he liked",
                              kind="reach_out", priority=0.9,
                              provenance="user:chat")

    trace = (await work(rig))[0]
    assert trace["decided"]["intention"].startswith("tool_step:")
    assert rig.runner.calls[0][0] == "take_selfie"
    assert rig.mind.goals.get(goal.id).state == "waiting"
    assert trace["interrupt"] == {}, "nothing goes to the gate before the picture"
    assert rig.post.proactive() == []

    rig.mind.bus.post("task_completion", _completion(goal.id), source="selfies")
    await work(rig, ticks=2)

    carried = [m for m in rig.post.proactive() if m.get("image_url") == SHOT]
    assert carried, f"the picture never went: {rig.post.proactive()}"
    assert rig.mind.goals.get(goal.id).state == "done"
    assert len(rig.runner.calls) == 1, "one picture, taken once"


async def test_a_reach_out_with_nothing_to_fetch_goes_to_the_gate_the_same_tick(
        cfg, seeded_vault):
    rig = reach_rig(cfg, seeded_vault, "think nothing to fetch for this — ready to send")
    goal = rig.mind.goals.add("ask him how the interview went", kind="reach_out",
                              priority=0.9, provenance="user:chat")

    trace = (await work(rig))[0]
    assert trace["interrupt"], "ready, it is ruled on in the same intention"
    assert rig.mind.goals.get(goal.id).meta.get("prepared")
    assert rig.mind.goals.get(goal.id).state == "done"
    assert rig.runner.calls == []


async def test_a_reach_out_not_yet_ready_waits_for_its_next_step(cfg, seeded_vault):
    """Held back from the camera, she says so — and it is not sent without the
    picture it is about. Out of steps, it goes as it is."""
    rig = reach_rig(cfg, seeded_vault,
                    *["think the camera is held back; I'll come back to it"] * 3)
    goal = rig.mind.goals.add("send him a new selfie", kind="reach_out",
                              priority=0.9, provenance="user:chat")

    first = (await work(rig))[0]
    assert first["interrupt"] == {}
    assert rig.mind.goals.get(goal.id).state == "active"
    assert rig.post.proactive() == []

    await work(rig, ticks=3)
    assert rig.mind.goals.get(goal.id).meta.get("prepared"), \
        "past the horizon it goes to the gate rather than preparing forever"


async def test_a_reach_out_does_not_tell_or_file_while_getting_ready(
        cfg, seeded_vault):
    """`tell_them` would file a second message whose words skip the threshold;
    this goal *is* the message, and Gate 2 still rules on it."""
    rig = reach_rig(cfg, seeded_vault, 'use tell_them {"text": "hi you"}',
                    allow="write_note,create_goal")
    rig.mind.cfg = rig.mind.cfg.model_copy(update={"mind_interrupt_threshold": 0.99})
    goal = rig.mind.goals.add("ask him how the interview went", kind="reach_out",
                              priority=0.9, provenance="user:chat")
    await work(rig)
    assert not [g for g in rig.mind.goals.all()
                if g.provenance == f"told:{goal.id}"]
    from yurios.mind.goalwork import prepare_system
    from yurios.mind.hands import Offer
    system = prepare_system(rig.mind, goal, Offer(tools=("write_note",)), False)
    assert "tell_them" not in system


async def test_what_already_carries_its_content_skips_getting_ready(
        cfg, seeded_vault):
    from yurios.mind.goalwork import needs_preparing
    rig = reach_rig(cfg, seeded_vault)
    add = rig.mind.goals.add
    assert needs_preparing(rig.mind, add("send a selfie", kind="reach_out",
                                         provenance="user:chat"))
    assert not needs_preparing(rig.mind, add("x", kind="reach_out",
                                             provenance="followup:g-1"))
    assert not needs_preparing(rig.mind, add("y", kind="reach_out",
                                             provenance="told:g-1",
                                             meta={"say": "hi", "decided": True}))
    assert not needs_preparing(rig.mind, add("z", kind="reach_out",
                                             meta={"product": {"image_url": SHOT}}))
    assert not needs_preparing(rig.mind, add("w", kind="task"))


async def test_without_hands_a_reach_out_goes_straight_to_the_gate(
        cfg, seeded_vault):
    rig = make_mind(cfg.model_copy(update={"mind_interrupt_threshold": 0.0}),
                    seeded_vault)
    rig.mind.goals.add("ask him how the interview went", kind="reach_out",
                       priority=0.9, provenance="user:chat")
    trace = await rig.mind.tick()
    assert trace["decided"]["intention"].startswith("goal:")
    assert trace["interrupt"]


async def test_not_yet_parks_a_dated_reach_out_until_nearer_its_date(
        cfg, seeded_vault):
    """Her call that the moment has not come, honoured: parked until six
    hours before its date, without spending one of its steps — and with her
    hands off too, because that judgement needs no hands."""
    from yurios.mind.goalwork import NOT_YET_LEAD_H
    from yurios.mind.util import iso_of, ts_of_iso

    rig = make_mind(cfg.model_copy(update={"mind_interrupt_threshold": 0.0}),
                    seeded_vault,
                    utility=ScriptedUtility("think he hasn't had it yet — not yet"))
    goal = rig.mind.goals.add("ask how the interview went", kind="reach_out",
                              priority=0.9, provenance="promise:her-own-words",
                              due=iso_of(rig.clock.now() + 30 * 3600))
    trace = await rig.mind.tick()

    held = rig.mind.goals.get(goal.id)
    assert trace["interrupt"] == {}
    assert held.state == "waiting"
    assert held.steps == 0
    assert rig.mind.wakeups[goal.id] == \
        ts_of_iso(str(goal.due)) - NOT_YET_LEAD_H * 3600
    assert rig.post.proactive() == []


async def test_a_day_of_thinking_does_not_lock_the_camera(cfg, seeded_vault):
    """Yuri's afternoon of 3 Oct: 0.91 of the day's tokens spent on goal work,
    and the selfie she had been asked for held behind the token ceiling. The
    render is not tokens; the web hands, whose results are, still wait."""
    rig = reach_rig(cfg, seeded_vault,
                    'think he asked for another — take it\n'
                    'use take_selfie {"look": "by the window"}',
                    allow="take_selfie,research,write_note")
    rig.mind.cfg = rig.mind.cfg.model_copy(update={"search_backend": "fake"})
    rig.mind.hands.cfg = rig.mind.cfg
    rig.mind.budget.pressure = lambda: 0.91
    offer = rig.mind.hands.offer(state="DORMANT", pressure=0.91, user_present=False)
    assert "take_selfie" in offer.tools
    assert offer.held == ("research",) and "budget" in offer.held_why

    goal = rig.mind.goals.add("send him a new selfie", kind="reach_out",
                              priority=0.9, provenance="user:chat")
    await work(rig)
    assert rig.runner.calls[0][0] == "take_selfie"
    assert rig.mind.goals.get(goal.id).state == "waiting"


async def test_a_reach_out_whose_camera_waits_for_the_room_spends_no_step(
        cfg, seeded_vault):
    """Not ready with the camera held: no step spent, so the horizon cannot
    send "a new selfie" with none in it — and no morning park, because the
    room empties on its own schedule, not the budget's."""
    rig = reach_rig(cfg, seeded_vault,
                    "think the camera waits until he's gone; I'll come back to it")
    rig.mind.bus.post("user_present", {}, source="frontend")
    goal = rig.mind.goals.add("send him a new selfie", kind="reach_out",
                              priority=0.9, provenance="user:chat")
    trace = (await work(rig))[0]

    held = rig.mind.goals.get(goal.id)
    assert trace["decided"]["intention"].startswith("tool_step:")
    assert "take_selfie" not in trace["decided"]["hands"]["available"], \
        "the room holds the camera — the case under test"
    assert trace["interrupt"] == {}
    assert held.state == "active" and held.steps == 0
    assert goal.id not in rig.mind.wakeups
    assert rig.post.proactive() == [] and rig.runner.calls == []


async def test_a_reach_out_whose_web_hand_is_budget_held_waits_for_the_morning(
        cfg, seeded_vault):
    from yurios.mind.policy import next_open

    rig = reach_rig(cfg, seeded_vault,
                    "think I need to look it up first and the search is held",
                    allow="research,write_note")
    rig.mind.cfg = rig.mind.cfg.model_copy(update={"search_backend": "fake"})
    rig.mind.hands.cfg = rig.mind.cfg
    rig.mind.budget.pressure = lambda: 0.91
    goal = rig.mind.goals.add("tell him what the reviews say about that film",
                              kind="reach_out", priority=0.9,
                              provenance="user:chat")
    then = rig.clock.now()
    trace = (await work(rig))[0]

    held = rig.mind.goals.get(goal.id)
    assert trace["interrupt"] == {}
    assert held.state == "waiting" and held.steps == 0
    assert rig.mind.wakeups[goal.id] == next_open(then)
    assert rig.post.proactive() == []


async def test_a_step_is_one_desk_entry_however_many_calls_it_made(
        cfg, seeded_vault):
    """Live, 4 Oct: one step, seven entries in twenty seconds, and the desk
    read back from its last 3,000 characters showed the next step only that
    step's calls — so it re-checked work it had already done (SPEC §22.3)."""
    rig = rig_with_hands(
        cfg, seeded_vault,
        'think where did I leave it\nuse read_note {"path": "diary/a.md"}',
        'think so the plan goes in a note.\nuse write_note '
        '{"path": "notes/plan.md", "text": "the plan"}',
        'use edit_note {"path": "free-time/b.md", "old_text": "x", "new_text": "y"}',
        "think the plan is filed; the picture waits for him.",
        allow="read_note,write_note,edit_note",
        tools=FakeToolRunner(errors={"edit_note": "old_text does not appear"}))
    goal = rig.mind.goals.add("get the raincheck ready", kind="task",
                              priority=0.95)
    await work(rig)
    desk = rig.mind.vault.read(f"workspace/goals/{goal.id}.md")
    assert desk.count("\n## ") == 1, desk
    assert "— step 1" in desk
    words, _, listed = desk.partition("done in this step:")
    assert "the plan is filed; the picture waits for him." in words
    calls = [line for line in listed.splitlines() if line.startswith("- ")]
    assert calls[0] == "- read diary/a.md"                  # a read keeps no reason
    assert calls[1] == "- wrote notes/plan.md (so the plan goes in a note.)"
    assert calls[2].startswith("- edit_note free-time/b.md: ")
    assert "old_text does not appear" in calls[2]
    assert "reached for" not in desk and "get the raincheck ready" not in desk
    from yurios.mind.util import day_of
    journal = rig.mind.vault.read(f"memory/episodic/{day_of(rig.clock.now())}.md")
    assert journal.count("wrote up where I got to") == 1


def test_her_last_words_on_a_desk_leave_the_call_list_out():
    from yurios.mind.workspace import last_entry
    desk = ("\n## 2026-10-04T10:29:18 — step 3\n\nThe plan is filed.\n\n"
            "done in this step:\n- read diary/a.md\n- wrote notes/plan.md\n")
    assert last_entry(desk, 240) == "The plan is filed."


async def test_a_step_is_told_which_desk_file_is_its_own(cfg, seeded_vault):
    """Live, 4 Oct: nothing named the skill goal's desk file, and the desk
    listed five goals/<id>.md files by id alone — so its first step took the
    newest, the finished diary goal's, for its own and logged its progress
    there twice (SPEC §22.3)."""
    utility = ScriptedUtility("think the skill is updated.", "think step two.")
    rig = rig_with_hands(cfg, seeded_vault, allow="read_note", utility=utility)
    diary = rig.mind.goals.add("write today's diary entry", kind="task",
                               priority=0.1)
    rig.mind.goals.update(diary.id, state="done")
    rig.mind.workspace.append(f"goals/{diary.id}.md", "\n## 2026-10-03\n\nwrote it\n")
    skill = rig.mind.goals.add("update the intimate-presence skill", kind="task",
                               priority=0.95)
    await work(rig)
    first = utility.calls[0][1]["content"]
    # its own file, by name, even before it exists — and that it is kept for her
    assert f"its desk file: goals/{skill.id}.md" in first
    assert "never need to log your progress yourself" in first
    # every other goal file says whose it is
    assert (f"goals/{diary.id}.md (" in first
            and "— “write today's diary entry” (done)" in first)
    # …and on the next step, what she worked out arrives under its own path
    rig.clock.advance(rig.mind.cfg.mind_consider_cooldown_s + 1)
    await rig.mind.tick()
    second = utility.calls[-1][1]["content"]
    assert f"WHAT YOU HAVE ALREADY WORKED OUT ON THIS (goals/{skill.id}.md)" in second
    assert f"goals/{skill.id}.md (" in second and "— this goal" in second


# --- a night job's hands (§21.2, `max_hands`) ----------------------------------

@pytest.mark.parametrize("cap, chained", [
    (2, 2),         # the job's own number holds…
    (99, 3),        # …never past the house's TOOL_MAX_CALLS_PER_TURN
    (None, 3),      # a job that names none gets the house's
    (0, 0),         # and none is one plain call, with no hands block at all
])
async def test_a_night_job_chains_no_more_hands_than_its_cap(
        cfg, seeded_vault, cap, chained):
    """Each round resends the whole transcript, so a chain that will not stop
    costs the square of its length: fourteen `read_note`s once spent 158k
    tokens of a 200k day on one stock-take. The cap is what stops it."""
    from yurios.mind.handwork import LoopHands
    rig = rig_with_hands(cfg, seeded_vault, allow="read_note",
                         tool_max_calls_per_turn=3)
    sent: list[list[dict]] = []

    async def ask(messages):
        sent.append([dict(m) for m in messages])
        return 'use read_note {"path": "notes/a.md"}'

    messages = [{"role": "system", "content": "You are taking stock."},
                {"role": "user", "content": "the day"}]
    await LoopHands(rig.mind).run(messages, ask, cap=cap)
    assert len(rig.runner.calls) == chained
    assert len(sent) == chained + 1
    if cap == 0:
        assert sent[0][0]["content"] == "You are taking stock."
    else:
        assert f"Your limit for this answer is {chained}" in sent[0][0]["content"]


async def test_each_result_says_how_many_hands_are_left(cfg, seeded_vault):
    """Told only her total, a chain spends it all reading and reaches the
    answer with nothing left. The count beside each result is what lets her
    plan the rest."""
    from yurios.mind.handwork import LoopHands
    rig = rig_with_hands(cfg, seeded_vault, allow="read_note",
                         tool_max_calls_per_turn=16)
    sent: list[list[dict]] = []

    async def ask(messages):
        sent.append([dict(m) for m in messages])
        return 'use read_note {"path": "notes/a.md"}'

    await LoopHands(rig.mind).run(
        [{"role": "system", "content": "You are taking stock."},
         {"role": "user", "content": "the day"}], ask, cap=3)
    told = [s[-1]["content"] for s in sent[1:]]
    assert "You have 2 hands left" in told[0]
    assert "You have 1 hand left" in told[1]
    assert "hands are spent" in told[2] and "left" not in told[2]


# --- a goal step's hands (§26.2, `MIND_GOAL_MAX_HANDS`) -----------------------

@pytest.mark.parametrize("goal_hands, chained", [
    (2, 2),         # the step's own number holds…
    (99, 3),        # …never past the house's TOOL_MAX_CALLS_PER_TURN
    (0, 0),         # and none runs no hand; the step still ends on her thought
])
async def test_a_goal_step_chains_no_more_hands_than_its_cap(
        cfg, seeded_vault, goal_hands, chained):
    """A goal step used to get the reply's sixteen. On 7 Oct one the
    stock-take had filed read her desk ten times, listed it, and only then
    wrote its note — ~113k of a 200k day in one tick. Its own cap stops that,
    and the number in its prompt is the one that holds."""
    reads = [f'use read_note {{"path": "notes/{i}.md"}}' for i in range(8)]
    utility = ScriptedUtility(*reads)
    rig = rig_with_hands(cfg, seeded_vault, utility=utility, allow="read_note",
                         tool_max_calls_per_turn=3,
                         mind_goal_max_hands=goal_hands)
    goal = rig.mind.goals.add("go through my notes", kind="task", priority=0.95)
    await work(rig)
    assert len(rig.runner.calls) == chained
    assert rig.mind.goals.get(goal.id).steps == 1
    steps = [m for m in utility.calls
             if "advancing one of your own goals" in m[0]["content"].lower()]
    assert f"up to {chained} in this step" in steps[0][0]["content"]

