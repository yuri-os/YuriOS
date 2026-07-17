"""The scenario battery (SPEC §27.2): multi-day behaviour under a virtual
clock, asserted over the tick trace — silent for the right forty ticks, one
message, at the right one. "It felt right when I watched it for an evening"
is not a gate; days have to be checkable in seconds or the make-or-break
component ships untuned.
"""
from __future__ import annotations

import datetime

from .conftest import SIM_START, make_mind, run_mind


def _hours(rig) -> float:
    return (rig.clock.now() - SIM_START.timestamp()) / 3600


# --- the interview was Tuesday ------------------------------------------------

async def test_interview_tuesday_one_welljudged_reach_out(cfg, seeded_vault):
    rig = make_mind(cfg, seeded_vault)
    # Monday 09:00 — a short exchange, then the user is gone
    rig.say("the big interview is tomorrow evening. wish me luck",
            reply="You'll be great. Go get it.")
    await rig.mind.tick()
    rig.mind.bus.post("user_absent", {}, source="frontend")
    due = datetime.datetime(2026, 7, 7, 18, 0)          # Tuesday 18:00
    rig.mind.goals.add("ask how the interview went", kind="reach_out",
                       priority=0.8, due=due.isoformat(timespec="seconds"),
                       commitment="single-minded", provenance="promise:her-own-words")

    traces = await run_mind(rig, hours=40)

    proactive = rig.post.proactive()
    assert len(proactive) == 1, \
        f"exactly one reach-out, got {len(proactive)}: {proactive}"
    when = datetime.datetime.fromtimestamp(proactive[0]["ts"])
    lo = datetime.datetime(2026, 7, 7, 9, 0)
    hi = datetime.datetime(2026, 7, 7, 18, 30)
    assert lo <= when <= hi, f"reached out at {when}, not near the interview"
    # she considered it and chose quiet many times before speaking
    silents = [t for t in traces if t["interrupt"].get("outcome") == "SILENT"]
    assert len(silents) >= 3, "restraint should be visible in the trace"
    speaks = [t for t in traces if t["interrupt"].get("outcome") == "SPEAK"]
    assert len(speaks) == 1
    # …and the restraint is a *scored* decision, factors shown
    assert "availability" in silents[0]["interrupt"]["factors"]
    g = next(g for g in rig.mind.goals.all() if "interview" in g.text)
    assert g.state == "done"
    # nothing was ever spoken into the empty room
    assert not any(c["delivered"] for c in rig.speak.calls)


# --- user goes dark for the weekend ---------------------------------------------

async def test_dark_weekend_quiet_but_alive(cfg, seeded_vault):
    rig = make_mind(cfg, seeded_vault)
    # a Monday exchange lands in the episodic journal, as the real brain writes it
    from yurios.app.memory.store import Record
    await rig.mind.brain.state.store.remember(Record(
        session_id="s1", turn_index=0,
        user_msg="remember I hate mondays. see you next week",
        reply="Noted, and survived. Go.",
        ts=datetime.datetime(2026, 7, 6, 9, 0)))
    rig.say("remember I hate mondays. see you next week",
            reply="Noted, and survived. Go.")
    await rig.mind.tick()
    rig.mind.bus.post("user_absent", {}, source="frontend")

    traces = await run_mind(rig, hours=60)

    # she let you be: not one message, spoken or chat, while you were gone
    assert rig.post.proactive() == []
    assert not any(c["delivered"] for c in rig.speak.calls)
    # but she was alive: DREAM ran and consolidated Monday
    progress = seeded_vault / "state" / "dream_progress.json"
    assert progress.exists() and "2026-07-06" in progress.read_text()
    facts = (seeded_vault / "memory" / "semantic" / "facts.md").read_text()
    assert "mondays" in facts.lower()
    states = {t["activity_state"] for t in traces}
    assert "DORMANT" in states, "the drift down the cost ladder is visible"
    assert any(t["activity_state"] == "DREAM" for t in traces)
    # the vast majority of ticks end in REST
    rests = [t for t in traces if t["decided"]["intention"] == "REST"]
    assert len(rests) > len(traces) * 0.7
    # and the journal carries the night's work — the product surface
    day_files = sorted((seeded_vault / "memory" / "episodic").glob("*.md"))
    assert any("[she]" in p.read_text() for p in day_files)


# --- the machine sleeps ----------------------------------------------------------

async def test_suspend_gap_one_catchup_not_thirty_good_mornings(cfg, seeded_vault):
    rig = make_mind(cfg, seeded_vault)
    rig.say("goodnight", reply="Sleep well.")
    await rig.mind.tick()
    # the machine is off for 10 hours (no ticks at all)
    rig.clock.advance(10 * 3600)
    trace = await rig.mind.tick()
    gaps = [s for s in trace["sensed"] if s["type"] == "suspend_gap"]
    assert len(gaps) == 1, "one catch-up, not a pile of stale reactions"
    # and the catch-up journaled itself
    day = datetime.datetime.fromtimestamp(rig.clock.now()).strftime("%Y-%m-%d")
    journal = (seeded_vault / "memory" / "episodic" / f"{day}.md").read_text()
    assert "slept" in journal
    # the next tick does not re-sense the same gap
    trace2 = await rig.mind.tick()
    assert not [s for s in trace2["sensed"] if s["type"] == "suspend_gap"]


# --- her own promise becomes a goal she keeps -------------------------------------

async def test_promise_extraction_files_a_goal(cfg, seeded_vault):
    rig = make_mind(cfg, seeded_vault)
    rig.say("can you think about names for the cat?",
            reply="Ooh. I'll sleep on cat names and tell you tomorrow.")
    await rig.mind.tick()
    goals = rig.mind.goals.open_goals()
    assert any("sleep on cat names" in g.text for g in goals)
    g = next(g for g in goals if "cat names" in g.text)
    assert g.kind == "reach_out"
    assert g.provenance == "promise:her-own-words"
    assert g.due is not None                       # promises are time-bound
    # and the promise is journaled the moment it's made
    day = "2026-07-06"
    journal = (seeded_vault / "memory" / "episodic" / f"{day}.md").read_text()
    assert "I promised" in journal


# --- a timer is a promise: queued until deliverable ---------------------------------

async def test_timer_announce_queues_until_someone_can_hear(cfg, seeded_vault):
    rig = make_mind(cfg, seeded_vault)
    rig.timers.add(id="t1", label="tea", seconds=60.0)
    rig.clock.advance(61)
    rig.timers.poll()
    rig.speak.connected = False                    # nobody in the room
    await rig.mind.tick()
    assert rig.mind._pending_announce, "the promise stays queued"
    rig.speak.connected = True                     # a page attaches
    rig.clock.advance(30)
    await rig.mind.tick()
    assert not rig.mind._pending_announce
    assert any("tea" in c["cue"] for c in rig.speak.calls if c["delivered"])
