"""Her own thinking gives the model back when you start talking (SPEC §17.4).

Found live, 18 Nov. A free-time sitting on her local model (LM Studio) was one
call into a three-call chain when a message arrived. The hand she reached for
was refused ("mid-conversation") but the chain kept going: two more calls on
the same model, each holding the process's one inference slot while the
reply waited behind it. The reply landed 23 seconds after the message.

These tests pin the way out:
- the call in flight is cancelled;
- the step stops at the next call it would make;
- nothing counts the yield as a failure;
- the work comes back once the conversation is over;
- none of this happens when her model is hosted, or the setting says keep going.
"""
from __future__ import annotations

import asyncio

import pytest

from yurios.app.providers.admission import inference_admission
from yurios.mind import handed
from yurios.mind.loop import WAITS_FOR_CHAT
from yurios.mind.policy import DORMANT, ENGAGED, IDLE

from .conftest import ScriptedUtility, make_mind

LOCAL = "lm_studio/local"
HOSTED = "openrouter/deepseek/deepseek-v4-flash"
THOUGHT = "think the lighthouse keepers again — I want to know more"


class HeldUtility(ScriptedUtility):
    """A utility model whose free-time and goal calls hang until released.

    A held call sits inside the process-wide inference gate, the way a call to
    a local server does, so a test can check the gate is given up as well.
    """

    def __init__(self, *lines: str, muse: tuple[str, ...] = ()):
        super().__init__(*lines, muse=muse)
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = 0
        self.held = 0

    async def complete(self, messages, **params):
        system = (messages[0].get("content", "") if messages else "").lower()
        if ("this is your free time" in system
                or "advancing one of your own goals" in system
                or "reviewing possible commitments" in system
                or "handed you a document" in system):
            self.held += 1
            async with inference_admission():
                self.started.set()
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    self.cancelled += 1
                    raise
        return await super().complete(messages, **params)


def rig_for(cfg, vault, *, model=LOCAL, utility, **extra):
    cfg = cfg.model_copy(update={"utility_model": model, **extra})
    return make_mind(cfg, vault, utility=utility)


def quiet(rig, *, state=IDLE):
    """Nobody has said anything for a while, so free time is on offer."""
    rig.mind.activity.state = state
    rig.mind.activity.last_user_msg = rig.clock.now() - 6 * 3600
    rig.mind._last_turn_end = rig.clock.now() - 6 * 3600


async def start_tick(rig, utility):
    tick = asyncio.create_task(rig.mind.tick())
    await asyncio.wait_for(utility.started.wait(), 5)
    return tick


async def test_your_message_takes_back_her_free_time_call(cfg, seeded_vault):
    utility = HeldUtility(muse=(THOUGHT,))
    rig = rig_for(cfg, seeded_vault, utility=utility)
    quiet(rig)
    mused_before = rig.mind.last_mused
    tick = await start_tick(rig, utility)

    rig.mind.turn_started()
    trace = await asyncio.wait_for(tick, 5)

    assert utility.cancelled == 1, "the request itself is dropped"
    assert trace["decided"]["intention"] == "muse"
    assert trace["acted"] == {"what": "muse", "result": f"yielded: {WAITS_FOR_CHAT}"}
    assert trace["activity_state"] == ENGAGED
    # not a sitting she had: the two-hour cooldown has not started
    assert rig.mind.last_mused == mused_before
    days = (seeded_vault / "memory" / "episodic").glob("*.md")
    assert not any("free time" in day.read_text() for day in days)


async def test_her_reply_gets_the_inference_slot_at_once(cfg, seeded_vault):
    """The slot the cancelled call held goes to the reply, not after it."""
    utility = HeldUtility(muse=(THOUGHT,))
    rig = rig_for(cfg, seeded_vault, utility=utility)
    quiet(rig)
    tick = await start_tick(rig, utility)

    async def reply():
        async with inference_admission():
            return "her reply"

    answering = asyncio.create_task(reply())
    await asyncio.sleep(0)
    assert not answering.done(), "the slot is hers until she gives it up"
    rig.mind.turn_started()
    assert await asyncio.wait_for(answering, 5) == "her reply"
    await tick


async def test_a_step_stops_at_its_next_call_once_you_are_talking(
        cfg, seeded_vault):
    """The live shape. The first call came back, and then you spoke. She does
    not go back to the model for the next round of the chain."""
    rig = None

    class SpeaksDuring(ScriptedUtility):
        async def complete(self, messages, **params):
            system = (messages[0].get("content", "") if messages else "").lower()
            if "free time" in system and rig is not None:
                # your message lands as this round comes back
                rig.mind.activity.preempt_engaged()
            return await super().complete(messages, **params)

    utility = SpeaksDuring(muse=(
        'think I want to read about lighthouses.\n'
        'use create_goal {"text": "read about the lighthouse keepers", '
        '"kind": "task"}',
        "think that's it"))
    rig = rig_for(cfg, seeded_vault, utility=utility)
    quiet(rig)
    trace = await rig.mind.tick()

    sittings = [c for c in utility.calls
                if "free time" in (c[0].get("content") or "").lower()]
    assert len(sittings) == 1, "the second round never reached the model"
    assert trace["acted"]["result"] == f"yielded: {WAITS_FOR_CHAT}"
    # what her hands did before the yield happened, and stays done
    assert any(g.text == "read about the lighthouse keepers"
               for g in rig.mind.goals.open_goals())


async def test_a_yielded_goal_step_is_not_a_step(cfg, seeded_vault):
    utility = HeldUtility("think one more source and I'm done")
    rig = rig_for(cfg, seeded_vault, utility=utility)
    quiet(rig)
    goal = rig.mind.goals.add("read about the lighthouse keepers", kind="task",
                              priority=0.9)
    tick = await start_tick(rig, utility)
    rig.mind.turn_started()
    trace = await asyncio.wait_for(tick, 5)

    assert trace["decided"]["intention"].startswith("goal:")
    assert trace["acted"]["result"].startswith("yielded:")
    after = rig.mind.goals.get(goal.id)
    assert after.steps == 0
    assert not after.meta.get("decisions"), "nothing tried, nothing to record"
    assert goal.id not in rig.mind.considered, "no hour-long cooldown either"


async def test_while_you_talk_her_own_thinking_waits(cfg, seeded_vault):
    """ENGAGED on a local model: the goal is in the trace with why it waits, and
    the model is not asked at all."""
    utility = ScriptedUtility("think worked on it")
    rig = rig_for(cfg, seeded_vault, utility=utility)
    goal = rig.mind.goals.add("read about the lighthouse keepers", kind="task",
                              priority=0.9)
    rig.mind.turn_started()
    rig.mind.turn_ended()
    trace = await rig.mind.tick()

    assert trace["activity_state"] == ENGAGED
    assert trace["decided"]["intention"] == "REST"
    waiting = next(a for a in trace["appraised"]
                   if a["what"].startswith("goal:"))
    assert waiting["score_to_act"] == 0.0
    assert waiting["why"].endswith(f"waits: {WAITS_FOR_CHAT}")
    assert not utility.calls
    assert rig.mind.goals.get(goal.id).steps == 0


async def test_once_the_conversation_is_over_the_work_comes_back(
        cfg, seeded_vault):
    utility = HeldUtility("think worked on it")
    rig = rig_for(cfg, seeded_vault, utility=utility)
    quiet(rig)
    goal = rig.mind.goals.add("read about the lighthouse keepers", kind="task",
                              priority=0.9)
    tick = await start_tick(rig, utility)
    rig.mind.turn_started()
    await asyncio.wait_for(tick, 5)
    rig.mind.turn_ended()

    # the conversation times out, as ENGAGED does
    rig.clock.advance(rig.mind.cfg.mind_engaged_timeout_s + 60)
    rig.mind.activity.update(dream_backlog=False, budget_pressure=0.0)
    assert rig.mind.activity.state in (IDLE, DORMANT)
    utility.release.set()
    trace = await rig.mind.tick()
    assert trace["decided"]["intention"].startswith("goal:")
    assert rig.mind.goals.get(goal.id).steps == 1


async def test_a_promise_review_given_up_waits_without_a_backoff(
        cfg, seeded_vault):
    utility = HeldUtility()
    rig = rig_for(cfg, seeded_vault, utility=utility)
    quiet(rig)
    review = {"signal_id": "sig-1", "user_text": "can you look into it?",
              "reply": "I'll look into the ferry times for you.",
              "candidates": [{"text": "look into the ferry times", "index": 0,
                              "provenance": "promise:her-own-words",
                              "source": "I'll look into the ferry times",
                              "start": 0, "confidence": "soft"}],
              "tool_outcomes": [], "attempts": 0}
    rig.mind.promise_reviews.append(dict(review))
    tick = await start_tick(rig, utility)
    rig.mind.turn_started()
    trace = await asyncio.wait_for(tick, 5)

    assert trace["decided"]["intention"] == "promise_review"
    assert trace["acted"]["result"].startswith("yielded:")
    assert rig.mind.promise_reviews == [review], "kept as it was: no attempt, no retry_at"


async def test_a_document_she_put_down_for_you_is_still_waiting(
        cfg, seeded_vault):
    utility = HeldUtility()
    rig = rig_for(cfg, seeded_vault, utility=utility)
    quiet(rig, state=DORMANT)
    rig.mind.workspace.write("inbox/q3.md", "# Q3\n\nRevenue rose 12%.\n")
    rig.mind.bus.post("handed", {"path": "inbox/q3.md", "name": "q3.md"},
                      source="user")
    tick = await start_tick(rig, utility)
    rig.mind.turn_started()
    trace = await asyncio.wait_for(tick, 5)

    assert trace["decided"]["intention"] == "handed"
    assert trace["acted"]["result"].startswith("yielded:")
    assert [d["path"] for d in handed.waiting(rig.mind)] == ["inbox/q3.md"]
    assert not rig.mind.handed[0].get("read_at")

@pytest.mark.parametrize("model, during_chat", [(HOSTED, "auto"), (LOCAL, "on")])
async def test_a_hosted_model_or_the_setting_keeps_the_call(
        cfg, seeded_vault, model, during_chat):
    """Somebody else's machine can answer you both. `on` says carry on anyway."""
    utility = HeldUtility(muse=(THOUGHT,))
    rig = rig_for(cfg, seeded_vault, model=model, utility=utility,
                  mind_tools_during_chat=during_chat)
    quiet(rig)
    tick = await start_tick(rig, utility)
    rig.mind.turn_started()
    await asyncio.sleep(0)
    assert not tick.done()
    utility.release.set()
    trace = await asyncio.wait_for(tick, 5)
    assert utility.cancelled == 0
    assert trace["acted"]["what"] == "muse"
    assert not trace["acted"]["result"].startswith("yielded")


async def test_her_own_turn_takes_nothing_back(cfg, seeded_vault):
    """A reach-out she starts is a turn too (proactive), and it is not you."""
    utility = HeldUtility(muse=(THOUGHT,))
    rig = rig_for(cfg, seeded_vault, utility=utility)
    quiet(rig)
    tick = await start_tick(rig, utility)
    rig.mind.turn_started(proactive=True)
    await asyncio.sleep(0)
    assert not tick.done()
    utility.release.set()
    await asyncio.wait_for(tick, 5)
    assert utility.cancelled == 0


async def test_stopping_the_tick_is_still_a_stop(cfg, seeded_vault):
    """A cancel from outside, at shutdown, is not turned into a yield."""
    utility = HeldUtility(muse=(THOUGHT,))
    rig = rig_for(cfg, seeded_vault, utility=utility)
    quiet(rig)
    tick = await start_tick(rig, utility)
    tick.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tick
    assert utility.cancelled == 1
    assert rig.mind._model_call is None and rig.mind._tick_task is None


async def test_work_off_the_tick_is_not_taken_back(cfg, seeded_vault):
    """A call another task made, such as a reading or a research run already
    given out, has no way to take a yield back, so it is left alone."""
    utility = HeldUtility(muse=(THOUGHT,))
    rig = rig_for(cfg, seeded_vault, utility=utility)
    rig.mind.turn_started()                       # you are talking
    messages = [{"role": "system", "content": "This is your free time."},
                {"role": "user", "content": "go on"}]
    call = asyncio.create_task(rig.mind._utility(messages))
    await asyncio.wait_for(utility.started.wait(), 5)
    rig.mind.turn_started()
    utility.release.set()
    assert await asyncio.wait_for(call, 5) == THOUGHT
    assert utility.cancelled == 0
