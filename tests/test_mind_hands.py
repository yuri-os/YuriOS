"""Hands in the loop (SPEC §26, as amended) — the default-off proof, and the
five questions §26 actually deferred.

The capability is a tool call nobody asked for, at four in the morning, with
nobody in the room. Every test here is one of the ways that goes wrong: it
speaks, it repeats, it spends conversation's rate limit, it spends money the
budget already said no to, or it keeps going after somebody flipped the switch.
"""
from __future__ import annotations

import json

from yurios.mind.hands import Hands, klass, parse_intent
from yurios.world.tools.fakes import FakeToolRunner

from .conftest import ScriptedUtility, make_mind


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


async def test_the_same_call_is_not_re_dispatched_every_tick(cfg, seeded_vault):
    """`Guard.turn()` is one dedupe scope per reply, and the mind has ticks."""
    line = 'use write_note {"path": "n.md", "text": "the same thing"}'
    rig = rig_with_hands(cfg, seeded_vault, line, line, line)
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


async def test_expensive_hands_wait_for_an_empty_room(cfg, seeded_vault):
    rig = rig_with_hands(cfg, seeded_vault, allow="write_note,research",
                         search_backend="fake")
    watched = rig.mind.hands.offer(state="IDLE", pressure=0.0, user_present=True)
    assert watched.tools == ("write_note",)
    alone = rig.mind.hands.offer(state="IDLE", pressure=0.0, user_present=False)
    assert "research" in alone.tools


async def test_a_hand_the_house_never_installed_is_not_on_the_allowlist(
        cfg, seeded_vault):
    """`SEARCH_BACKEND=off`'s rule: unadvertised, not merely denied."""
    rig = rig_with_hands(cfg, seeded_vault, allow="research,take_selfie",
                         search_backend="off", selfie_backend="off")
    assert rig.mind.hands.allowlist == ()
    assert not rig.mind.hands.enabled


async def test_she_is_never_offered_a_hand_while_she_is_talking_to_you(
        cfg, seeded_vault):
    rig = rig_with_hands(cfg, seeded_vault)
    engaged = rig.mind.hands.offer(state="ENGAGED", pressure=0.0,
                                   user_present=True)
    assert not engaged
    assert "mid-conversation" in engaged.reason


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
                         'use write_note {"path": "b.md", "text": "two"}')
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


# --- the pieces, unit-wise --------------------------------------------------------------

def test_cost_classes_are_a_table_not_a_guess():
    assert klass("write_note") == "cheap"
    assert klass("research") == "expensive"
    assert klass("rm") == ""


def test_an_unparseable_line_is_a_thought_not_an_error():
    """Nobody is waiting, so failing safe means failing towards thinking."""
    assert parse_intent("use write_note {oh no", allowed=("write_note",)).kind == "use"
    assert parse_intent("use rm {}", allowed=("write_note",)).kind == "think"
    assert parse_intent("", allowed=("write_note",)).kind == "think"
    thought = parse_intent("think the grout needs doing", allowed=())
    assert thought.kind == "think" and thought.text == "the grout needs doing"


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
