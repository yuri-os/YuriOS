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

def rig_with_hands(cfg, vault, *lines, allow="write_note", tools=None, **extra):
    runner = tools if tools is not None else FakeToolRunner()
    rig = make_mind(cfg, vault, utility=ScriptedUtility(*lines), tools=runner)
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


async def test_her_bucket_is_not_conversations_bucket(cfg, seeded_vault):
    """A night of autonomous work must not leave the morning's request denied."""
    rig = rig_with_hands(cfg, seeded_vault,
                         *[f'use write_note {{"path": "n{i}.md", "text": "x"}}'
                           for i in range(6)],
                         mind_tool_calls_per_day=50)
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

    rig = make_mind(cfg, seeded_vault)
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
