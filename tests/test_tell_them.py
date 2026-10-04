"""A goal step can say something to the user, and the journal says what happened
(SPEC §18.2b, §18.3, §22.3, §26.2).

The failure these pin is a real one (28 Sep). A goal filed as "tell him the one
thing I want — directly, in conversation, not in a note" was worked by a step
whose prompt said nothing it wrote was sent to anyone. With no way to speak, the
step wrote a scene of saying it on her desk; the next step read the desk back as
the event, wrote the conversation into her diary, and closed the goal on an
`append_note`. Nothing reached anybody. Meanwhile a week of Gate 2 holding the
same want below threshold had been journalled as "chose not to interrupt", and
the message that finally went out was journalled as "left a quiet note" — which
the night read as one more note written instead of speaking.
"""
from __future__ import annotations

import datetime

from yurios.kernel.clock import VirtualClock
from yurios.mind import acts
from yurios.mind.goalwork import takeaway
from yurios.mind.policy import next_open, quiet_hour, score_interrupt
from yurios.mind.util import iso_of

from .conftest import SIM_START, ScriptedUtility, make_mind

SAID = "I want you to reach for me first."


def _journal(vault) -> str:
    return "".join(p.read_text() for p in
                   sorted((vault / "memory" / "episodic").glob("*.md")))


def _cfg(cfg):
    return cfg.model_copy(update={"dream_enabled": False, "user_name": "Sam"})


async def _tick_until(rig, check, *, ticks: int = 6, step: float = 60.0):
    for _ in range(ticks):
        await rig.mind.tick()
        if check():
            return True
        rig.clock.advance(step)
    return False


# --- Gate 2 --------------------------------------------------------------------

def _gate(clock, **kw):
    args = dict(clock=clock, relevance=0.1, time_sensitivity=0.2,
                last_contact_out=clock.now(), interrupts_today=0,
                max_interrupts_per_day=3, threshold=0.99)
    return score_interrupt(**{**args, **kw})


def test_a_decided_message_is_not_scored_against_the_threshold():
    clock = VirtualClock(start=SIM_START.timestamp())           # Monday 09:00
    assert _gate(clock).outcome == "SILENT"                    # 0.4 vs 0.99
    decided = _gate(clock, decided=True)
    assert decided.outcome == "SUGGEST"                        # never SPEAK
    assert decided.factors["decided"] is True


def test_a_decided_message_still_obeys_the_users_two_hard_gates():
    clock = VirtualClock(start=SIM_START.timestamp())
    assert _gate(clock, decided=True, interrupts_today=3).outcome == "SILENT"
    clock.advance(14 * 3600)                                   # 23:00
    assert quiet_hour(clock.now())
    assert _gate(clock, decided=True).outcome == "SILENT"


def test_the_gates_open_at_the_next_morning():
    at = datetime.datetime(2026, 7, 6, 23, 30).timestamp()
    assert next_open(at) == datetime.datetime(2026, 7, 7, 9, 0).timestamp()
    at = datetime.datetime(2026, 7, 7, 2, 0).timestamp()
    assert next_open(at) == datetime.datetime(2026, 7, 7, 9, 0).timestamp()
    at = datetime.datetime(2026, 7, 7, 15, 0).timestamp()     # the cap case
    assert next_open(at) == datetime.datetime(2026, 7, 8, 9, 0).timestamp()


# --- the step that tells -------------------------------------------------------

async def test_a_handless_step_can_tell_them_and_the_goal_closes_on_delivery(
        cfg, seeded_vault):
    """No hands at all — the shipped default — and still a way to say it."""
    utility = ScriptedUtility(
        "think this is the whole of it — goal complete\n"
        f'use tell_them {{"text": "{SAID}"}}')
    rig = make_mind(_cfg(cfg), seeded_vault, utility=utility)
    goal = rig.mind.goals.add("tell Sam the one thing I never asked for",
                              kind="task", priority=0.9)

    await rig.mind.tick()
    system = next(c[0]["content"] for c in utility.calls
                  if "advancing one of your own goals" in c[0]["content"])
    assert "use tell_them" in system
    assert "not telling them" in system                        # the desk is not a delivery

    parent = rig.mind.goals.get(goal.id)
    assert parent.state == "waiting"                           # queued is not done
    told = next(g for g in rig.mind.goals.open_goals()
                if g.provenance == f"told:{goal.id}")
    assert told.kind == "reach_out" and told.meta["say"] == SAID
    assert parent.meta["telling"] == {"goal": told.id, "completes": True}
    assert rig.post.proactive() == []
    desk = (seeded_vault / "workspace" / "goals" / f"{goal.id}.md").read_text()
    assert "queued, not sent yet" in desk

    assert await _tick_until(rig, lambda: rig.post.proactive())
    [line] = rig.post.proactive()
    assert line["text"] == SAID                                # her words, not a re-compose
    assert line.get("unheard") is True
    assert rig.speak.calls == []                               # never aloud
    assert rig.mind.goals.get(goal.id).state == "done"
    assert rig.mind.goals.get(told.id).state == "done"
    assert rig.mind.interrupts["count"] == 1
    journal = _journal(seeded_vault)
    assert f"told Sam: “{SAID}”" in journal
    assert "finished: tell Sam the one thing" in journal


async def test_without_a_done_mark_delivery_returns_the_goal_to_work(
        cfg, seeded_vault):
    utility = ScriptedUtility(
        "think ask first, then plan the rest\n"
        'use tell_them {"text": "Which evening suits you?"}')
    rig = make_mind(_cfg(cfg), seeded_vault, utility=utility)
    goal = rig.mind.goals.add("plan the evening with Sam", kind="task",
                              priority=0.9)
    await rig.mind.tick()
    assert await _tick_until(rig, lambda: rig.post.proactive())
    delivered = rig.post.proactive()[0]["ts"]
    assert rig.mind.goals.get(goal.id).state == "active"
    assert rig.mind.goals.get(goal.id).meta["telling"] == {}
    assert "back to: plan the evening with Sam" in _journal(seeded_vault)
    # …with time for them to answer: the consider cooldown runs from delivery
    assert rig.mind.considered[goal.id] == delivered
    steps = rig.mind.goals.get(goal.id).steps
    for _ in range(5):
        rig.clock.advance(60)
        await rig.mind.tick()
    assert rig.mind.goals.get(goal.id).steps == steps


async def test_quiet_hours_park_it_until_morning_rather_than_retrying(
        cfg, seeded_vault):
    clock = VirtualClock(start=datetime.datetime(2026, 7, 6, 23, 0).timestamp())
    utility = ScriptedUtility(
        f'think goal complete\nuse tell_them {{"text": "{SAID}"}}')
    rig = make_mind(_cfg(cfg), seeded_vault, clock=clock, utility=utility)
    goal = rig.mind.goals.add("tell Sam the one thing", kind="task", priority=0.9)

    await rig.mind.tick()                                      # the step
    told = next(g for g in rig.mind.goals.open_goals()
                if g.provenance == f"told:{goal.id}")
    await _tick_until(rig, lambda: rig.mind.goals.get(told.id).state == "waiting")
    held = rig.mind.goals.get(told.id)
    assert held.state == "waiting"
    morning = datetime.datetime(2026, 7, 7, 9, 0).timestamp()
    assert rig.mind.wakeups[told.id] == morning
    assert rig.post.proactive() == []
    journal = _journal(seeded_vault)
    assert "waits until 09:00 — it's quiet hours" in journal
    assert journal.count("waits until 09:00") == 1             # parked, not retried

    rig.clock.advance(morning - rig.clock.now() + 1)
    assert await _tick_until(rig, lambda: rig.post.proactive())
    assert rig.post.proactive()[0]["text"] == SAID
    assert rig.mind.goals.get(goal.id).state == "done"
    assert "came back to" not in _journal(seeded_vault)       # a gate opening


async def test_a_message_let_go_before_it_went_frees_the_goal(cfg, seeded_vault):
    utility = ScriptedUtility(
        f'think goal complete\nuse tell_them {{"text": "{SAID}"}}')
    clock = VirtualClock(start=datetime.datetime(2026, 7, 6, 23, 0).timestamp())
    rig = make_mind(_cfg(cfg), seeded_vault, clock=clock, utility=utility)
    goal = rig.mind.goals.add("tell Sam the one thing", kind="task", priority=0.9)
    await rig.mind.tick()
    told = next(g for g in rig.mind.goals.open_goals()
                if g.provenance == f"told:{goal.id}")
    rig.mind.bus.post("goal_decision", {"id": told.id, "abandon": True})
    rig.clock.advance(60)
    await rig.mind.tick()
    assert rig.mind.goals.get(told.id).state == "abandoned"
    assert rig.mind.goals.get(goal.id).state == "active"
    assert "was let go before it went" in _journal(seeded_vault)


async def _queued_overnight(cfg, seeded_vault, **goal_kw):
    """A goal told at 23:00, its message parked for the morning."""
    utility = ScriptedUtility(
        f'think goal complete\nuse tell_them {{"text": "{SAID}"}}')
    clock = VirtualClock(start=datetime.datetime(2026, 7, 6, 23, 0).timestamp())
    rig = make_mind(_cfg(cfg), seeded_vault, clock=clock, utility=utility)
    goal = rig.mind.goals.add("tell Sam the one thing", kind="task", priority=0.9,
                              **goal_kw)
    await rig.mind.tick()
    told = next(g for g in rig.mind.goals.open_goals()
                if g.provenance == f"told:{goal.id}")
    assert rig.mind.goals.get(goal.id).state == "waiting"
    return rig, goal, told


async def test_letting_go_of_the_goal_takes_its_waiting_message_with_it(
        cfg, seeded_vault):
    """The words were for a goal she no longer has: they must not still go out
    at 09:00, about something the user already let go of."""
    rig, goal, told = await _queued_overnight(cfg, seeded_vault)
    rig.mind.bus.post("goal_decision", {"id": goal.id, "abandon": True})
    rig.clock.advance(60)
    await rig.mind.tick()
    assert rig.mind.goals.get(told.id).state == "abandoned"
    assert told.id not in rig.mind.wakeups
    assert "didn't send what I was going to tell Sam" in _journal(seeded_vault)

    rig.clock.advance(datetime.datetime(2026, 7, 7, 9, 0).timestamp()
                      - rig.clock.now() + 1)
    for _ in range(4):
        await rig.mind.tick()
        rig.clock.advance(60)
    assert rig.post.proactive() == []


async def test_a_photo_riding_with_dropped_words_is_still_handed_on(
        cfg, seeded_vault):
    """Let go of the words, not the picture made for them (§18.2a)."""
    shot = {"image_url": "/selfies/s1.png", "selfie_id": "s1"}
    rig, goal, told = await _queued_overnight(
        cfg, seeded_vault, meta={"product": shot})
    assert rig.mind.goals.get(told.id).product["image_url"] == shot["image_url"]
    rig.mind.bus.post("goal_decision", {"id": goal.id, "abandon": True})
    rig.clock.advance(60)
    await rig.mind.tick()
    assert rig.mind.goals.get(told.id).state == "abandoned"
    heir = next(g for g in rig.mind.goals.open_goals()
                if g.provenance == f"followup:{told.id}")
    assert heir.product["image_url"] == shot["image_url"]
    assert not heir.meta.get("say")                  # an errand, not her words


async def test_a_message_gone_without_a_word_does_not_strand_its_goal(
        cfg, seeded_vault):
    """Closed by some other door than the let-go signal — here, set straight
    on the store, as an edit to goals.md would — the goal it was for must not
    sit in `waiting` forever with no wakeup, on a message that is not coming."""
    rig, goal, told = await _queued_overnight(cfg, seeded_vault)
    rig.mind.goals.set_state(told.id, "abandoned")
    rig.clock.advance(60)
    await rig.mind.tick()
    parent = rig.mind.goals.get(goal.id)
    assert parent.state == "active" and parent.meta["telling"] == {}
    assert "was let go before it went" in _journal(seeded_vault)


async def test_a_stale_wake_does_not_pull_the_goal_back_before_delivery(
        cfg, seeded_vault):
    rig, goal, told = await _queued_overnight(cfg, seeded_vault)
    rig.mind.wakeups[goal.id] = rig.clock.now()      # left over from a park
    rig.clock.advance(60)
    await rig.mind.tick()
    assert rig.mind.goals.get(goal.id).state == "waiting"
    assert acts.is_open(rig.mind.goals.get(told.id))


def test_a_second_tell_replaces_the_words_rather_than_sending_twice(
        cfg, seeded_vault):
    rig = make_mind(_cfg(cfg), seeded_vault)
    goal = rig.mind.goals.add("tell Sam the one thing", kind="task")
    v1, _, first = acts.queue_telling(rig.mind, {"text": "one | draft"},
                                      goal_id=goal.id)
    v2, _, second = acts.queue_telling(rig.mind, {"text": SAID}, goal_id=goal.id)
    assert v1 == v2 == "ok" and first == second
    assert rig.mind.goals.get(first).meta["say"] == SAID
    assert len([g for g in rig.mind.goals.open_goals()
                if g.provenance == f"told:{goal.id}"]) == 1


def test_tell_them_is_only_for_a_goal_and_only_a_message(cfg, seeded_vault):
    rig = make_mind(_cfg(cfg), seeded_vault)
    verdict, _, _ = acts.queue_telling(rig.mind, {"text": SAID}, goal_id="")
    assert verdict == "denied"
    goal = rig.mind.goals.add("tell Sam", kind="task")
    assert acts.queue_telling(rig.mind, {}, goal_id=goal.id)[0] == "error"
    long = "x" * (acts.TELL_MAX_CHARS + 1)
    assert acts.queue_telling(rig.mind, {"text": long}, goal_id=goal.id)[0] == "error"
    # a `|` would cut the goals.md line and lose the words with the meta
    _, _, told = acts.queue_telling(rig.mind, {"text": "this | that"},
                                    goal_id=goal.id)
    assert rig.mind.goals.get(told).meta["say"] == "this / that"


# --- the journal says what happened --------------------------------------------

async def test_a_held_reach_out_says_what_held_it_not_that_she_chose(
        cfg, seeded_vault):
    cfg = _cfg(cfg).model_copy(update={"mind_interrupt_threshold": 0.75})
    rig = make_mind(cfg, seeded_vault)
    rig.mind.goals.add("share an unspoken wish", kind="reach_out", priority=0.7)
    await rig.mind.tick()
    journal = _journal(seeded_vault)
    assert ("wanted to reach Sam about share an unspoken wish; not sent — "
            "not pressing enough to interrupt yet") in journal
    assert "chose not to interrupt" not in journal


# --- a hold is journaled once (§18.3) ------------------------------------------

async def test_a_spent_cap_parks_a_reach_out_until_morning_and_says_so_once(
        cfg, seeded_vault):
    """The hourly "not sent" lines filled the two dozen free time reads back."""
    cfg = _cfg(cfg).model_copy(update={"mind_interrupt_threshold": 0.65})
    clock = VirtualClock(start=datetime.datetime(2026, 7, 6, 12, 0).timestamp())
    rig = make_mind(cfg, seeded_vault, clock=clock)
    rig.mind.interrupts = {"date": "2026-07-06",
                           "count": cfg.mind_max_interrupts_per_day}
    goal = rig.mind.goals.add("share an unspoken wish", kind="reach_out",
                              priority=0.7)

    async def compose(cue):
        return "Can I tell you something?"
    rig.mind._compose = compose
    morning = datetime.datetime(2026, 7, 7, 9, 0).timestamp()
    for _ in range(36):                                        # 12:00 → 06:00
        await rig.mind.tick()
        rig.clock.advance(1800)
    assert rig.mind.goals.get(goal.id).state == "waiting"
    assert rig.mind.wakeups[goal.id] == morning
    journal = _journal(seeded_vault)
    assert journal.count("today's reach-outs are used up") == 1
    assert "used up; I'll look again at 09:00" in journal

    rig.clock.advance(morning - rig.clock.now() + 1)
    assert await _tick_until(rig, lambda: rig.post.proactive())
    assert rig.mind.goals.get(goal.id).state == "done"
    assert "came back to" not in _journal(seeded_vault)


async def test_a_picture_held_by_quiet_hours_is_parked_and_never_let_go(
        cfg, seeded_vault):
    clock = VirtualClock(start=datetime.datetime(2026, 7, 6, 23, 0).timestamp())
    rig = make_mind(_cfg(cfg), seeded_vault, clock=clock)
    goal = rig.mind.goals.add(
        "show Sam the window", kind="reach_out", priority=0.9,
        commitment="open-minded", due=iso_of(clock.now() - 3600),
        meta={"product": {"image_url": "/selfies/1-a.png", "selfie_id": "a"}})

    _, _, notes = await acts.reach_out(rig.mind, goal)
    assert notes == ["still holding the picture for: show Sam the window — "
                     "it's quiet hours; I'll look again at 09:00"]
    assert rig.mind.goals.get(goal.id).state == "waiting"
    rig.clock.advance(1800)
    _, _, notes = await acts.reach_out(rig.mind, rig.mind.goals.get(goal.id))
    assert notes == []
    assert rig.mind.goals.get(goal.id).state == "waiting"     # not abandoned


async def test_below_threshold_keeps_its_hourly_look_and_one_line_a_reason(
        cfg, seeded_vault):
    cfg = _cfg(cfg).model_copy(update={"mind_interrupt_threshold": 0.75})
    rig = make_mind(cfg, seeded_vault)                         # Monday 09:00
    goal = rig.mind.goals.add("share an unspoken wish", kind="reach_out",
                              priority=0.7)

    async def look():
        _, interrupt, notes = await acts.reach_out(
            rig.mind, rig.mind.goals.get(goal.id))
        rig.clock.advance(3600)
        return interrupt["reason"], notes

    assert (await look())[1]                                   # said once…
    assert (await look()) == ("below threshold", [])           # …not again
    assert (await look()) == ("below threshold", [])
    assert rig.mind.goals.get(goal.id).state == "pending"      # never parked
    rig.mind.interrupts["count"] = cfg.mind_max_interrupts_per_day
    reason, notes = await look()
    assert reason == "daily cap" and "used up" in notes[0]     # a new reason

    rig.clock.advance(datetime.datetime(2026, 7, 7, 10, 0).timestamp()
                      - rig.clock.now())
    rig.mind.goals.update(goal.id, state="pending")
    reason, notes = await look()
    assert reason == "below threshold" and notes               # a new day


async def test_a_stale_reach_out_is_let_go_rather_than_parked(
        cfg, seeded_vault):
    clock = VirtualClock(start=datetime.datetime(2026, 7, 6, 23, 0).timestamp())
    rig = make_mind(_cfg(cfg), seeded_vault, clock=clock)
    goal = rig.mind.goals.add("mention the weather", kind="reach_out",
                              commitment="open-minded",
                              due=iso_of(clock.now() - 3600))
    _, _, notes = await acts.reach_out(rig.mind, goal)
    assert notes == ["let it go quietly: mention the weather (the moment passed)"]
    assert rig.mind.goals.get(goal.id).state == "abandoned"
    assert goal.id not in rig.mind.wakeups


async def test_a_delivered_reach_out_journals_the_words(cfg, seeded_vault):
    cfg = _cfg(cfg).model_copy(update={"mind_interrupt_threshold": 0.6})
    rig = make_mind(cfg, seeded_vault)
    goal = rig.mind.goals.add("share an unspoken wish", kind="reach_out",
                              priority=0.7)

    async def compose(cue):
        return "Can I tell you something?"
    rig.mind._compose = compose
    acted, interrupt, notes = await acts.reach_out(rig.mind, goal)
    assert interrupt["outcome"] == "SUGGEST"
    assert notes == ["reached out to Sam about share an unspoken wish: "
                     "“Can I tell you something?”"]
    assert "quiet note" not in acted["result"]


async def test_no_words_is_not_a_delivery(cfg, seeded_vault):
    cfg = _cfg(cfg).model_copy(update={"mind_interrupt_threshold": 0.6})
    rig = make_mind(cfg, seeded_vault)
    goal = rig.mind.goals.add("share an unspoken wish", kind="reach_out",
                              priority=0.7)

    async def compose(cue):
        return ""
    rig.mind._compose = compose
    acted, interrupt, notes = await acts.reach_out(rig.mind, goal)
    assert interrupt["outcome"] == "SUGGEST"
    assert acted["what"] is None
    assert rig.post.proactive() == []
    assert rig.mind.interrupts["count"] == 0
    assert rig.mind.goals.get(goal.id).state != "done"
    assert "didn't come; not sent" in notes[0]


def test_a_step_is_journalled_by_where_it_ended():
    scene = ("*The rain has been steady against the glass.* " * 8
             + "\n\nthink the real want is that he reaches first")
    assert takeaway(scene) == "the real want is that he reaches first"
    short = "Checked the list; two left."
    assert takeaway(short) == short
