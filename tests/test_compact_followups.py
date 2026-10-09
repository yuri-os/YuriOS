"""Later rounds of one step send a short prompt (SPEC §22.4).

The first call of a chain carries the card and the whole step. Every call after
it keeps the step's instructions — what her answer is read against — and drops
only the card and the bulk of the opening message: the situation, the desk, the
facts. It keeps what the step is about, every earlier hand cut short, her
previous answer and the newest result. `MIND_COMPACT_FOLLOWUPS=false` resends
the transcript, card included.
"""
from __future__ import annotations

import asyncio
import json

from yurios.mind import goalwork
from yurios.mind.dreamjobs.builtins import STRATEGY_OUTPUT, StrategyJob
from yurios.mind import handwork
from yurios.mind.handed import document
from yurios.mind.hands import Offer
from yurios.mind.handwork import EARLIER_CHARS, LoopHands, compact_followup
from yurios.mind.loop import MindLoop

from .conftest import ScriptedUtility
from .test_mind_hands import rig_with_hands, work
from .test_mind_soul import _write_card


class _Loop:
    def __init__(self, soul: str = "full", name: str = "Yuri") -> None:
        class Cfg:
            mind_soul_in_prompts = soul
            companion_name = name
        self.cfg = Cfg()


class _Script:
    """A utility model that answers in order and records what it was sent."""

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    async def complete(self, messages, **params):
        self.calls.append([dict(m) for m in messages])
        return self.replies.pop(0) if self.replies else "think enough"


def _step(goal: str = "boil the kettle") -> list[dict]:
    return [
        {"role": "system",
         "content": "This is you, alone — quietly advancing one of your own goals. "
                    "Write FINISHED-MARK when it is done."},
        {"role": "user",
         "content": f"THE GOAL\n\n{goal}\n\nTHE SITUATION RIGHT NOW\n\nSENTINEL-DESK-FACTS"},
    ]


# --- the shape of a follow-up ---------------------------------------------------

def test_a_followup_keeps_the_instructions_and_every_hand_cut_short():
    out = compact_followup(_Loop(), [
        {"role": "system", "content": "CARD-TEXT\n\nWrite FINISHED-MARK when done."},
        {"role": "user", "content": "THE GOAL\n\nboil it\n\nTHE SITUATION RIGHT NOW\n\n"
                                    + "x" * 2000 + "SENTINEL"},
        {"role": "assistant", "content": 'use read_note {"path": "a.md"}'},
        {"role": "user", "content": "((read_note returned: page one. You have 1 hand "
                                    "left for this step — use another if you need "
                                    "one, or give your answer.))"},
        {"role": "assistant", "content": 'use read_note {"path": "c.md"}'},
        {"role": "user", "content": "((read_note returned: page three " + "p" * 900 + "))"},
        {"role": "assistant", "content": 'use read_note {"path": "b.md"}'},
        {"role": "user", "content": "((read_note returned: page two "
                                    + "q" * 900 + ". hands are spent))"},
    ], anchor="THE GOAL\n\nboil it", preamble="CARD-TEXT")
    system, body = out[0]["content"], out[1]["content"]
    assert system.startswith("You are Yuri. Continue as yourself.")
    # The instructions are what the answer is read against: they go back whole.
    assert "Write FINISHED-MARK when done." in system
    assert "CARD-TEXT" not in system
    assert body.startswith("THE GOAL\n\nboil it")
    assert "SENTINEL" not in body
    # The earlier hand is still there, so she knows she read it…
    assert "a.md" in body and "page one" in body
    # …but cut short, while the newest result goes back whole.
    earlier = body.split("WHAT YOU HAVE ALREADY DONE IN THIS STEP", 1)[1] \
                  .split("WHAT YOU JUST SAID", 1)[0]
    assert "p" * (EARLIER_CHARS + 1) not in earlier and "page three" in earlier
    assert "hand left" not in earlier       # the old count is no longer true
    assert "q" * 900 + ". hands are spent" in body
    assert body.split("WHAT YOU JUST SAID\n\n", 1)[1].startswith(
        'use read_note {"path": "b.md"}')


def test_without_an_anchor_a_followup_keeps_the_head_not_a_heading():
    """Free time opens on THE SITUATION RIGHT NOW — its first paragraph is a
    heading, and an anchor of that is an anchor of nothing."""
    out = compact_followup(_Loop(), [
        {"role": "system", "content": "This is your free time."},
        {"role": "user", "content": "THE SITUATION RIGHT NOW\n\nraining, Tuesday evening"},
        {"role": "assistant", "content": "use list_notes {}"},
        {"role": "user", "content": "((list_notes returned: a.md))"},
    ])
    assert "raining, Tuesday evening" in out[1]["content"]


def test_a_followup_with_the_card_off_does_not_invent_a_name():
    out = compact_followup(_Loop(soul="off"), [
        {"role": "system", "content": "instruction"},
        {"role": "user", "content": "THE GOAL\n\nboil the kettle"},
        {"role": "assistant", "content": "think half"},
        {"role": "user", "content": "((read_note returned: a page.))"},
    ])
    assert out[0]["content"].startswith("Continue this step")
    assert "You are" not in out[0]["content"]
    assert "instruction" in out[0]["content"]


def test_a_followup_with_no_name_does_not_say_you_are_yourself():
    out = compact_followup(_Loop(name=""), [
        {"role": "system", "content": "instruction"},
        {"role": "user", "content": "THE GOAL\n\nboil the kettle"},
        {"role": "assistant", "content": "think half"},
        {"role": "user", "content": "((read_note returned: a page.))"},
    ])
    assert out[0]["content"].startswith("Continue as yourself.")


# --- end to end -----------------------------------------------------------------

def _chain(cfg, vault, **extra):
    utility = ScriptedUtility(
        'use read_note {"path": "notes/a.md"}',
        "think the note is enough")
    rig = rig_with_hands(cfg, vault, allow="read_note", utility=utility,
                         companion_name="Yuri", **extra)
    return rig, utility


async def _run(rig, messages, **kw):
    async def ask(outbound):
        return await rig.mind._utility(outbound, soul=True)

    await LoopHands(rig.mind).run(messages, ask, cap=2, **kw)


async def test_the_second_call_drops_the_card_and_the_situation(tmp_path, cfg):
    vault = _write_card(tmp_path / "yuri", "yuri")
    rig, utility = _chain(cfg, vault)
    await _run(rig, _step())
    assert len(utility.calls) == 2
    first, second = utility.calls
    assert "VOICE LAW" in first[0]["content"]
    assert "SENTINEL-DESK-FACTS" in first[1]["content"]
    assert "VOICE LAW" not in second[0]["content"]
    assert second[0]["content"].startswith("You are Yuri. Continue as yourself.")
    assert "Write FINISHED-MARK when it is done." in second[0]["content"]
    body = second[1]["content"]
    assert "boil the kettle" in body
    assert "read_note returned" in body
    assert "notes/a.md" in body


async def test_off_resends_the_whole_step_and_the_card(tmp_path, cfg):
    vault = _write_card(tmp_path / "yuri", "yuri")
    rig, utility = _chain(cfg, vault, mind_compact_followups=False)
    await _run(rig, _step())
    assert len(utility.calls) == 2
    second = utility.calls[1]
    assert "VOICE LAW" in second[0]["content"]
    assert "advancing one of your own goals" in second[0]["content"]
    assert "Continue this step" not in second[0]["content"]
    assert any("SENTINEL-DESK-FACTS" in m["content"] for m in second)
    assert "read_note returned" in second[-1]["content"]
    assert len(second) == 4


async def test_compact_with_the_card_off_sends_no_name(tmp_path, cfg):
    vault = _write_card(tmp_path / "yuri", "yuri")
    rig, utility = _chain(cfg, vault, mind_soul_in_prompts="off")
    await _run(rig, _step())
    assert len(utility.calls) == 2
    first, second = utility.calls
    assert "VOICE LAW" not in first[0]["content"]
    assert second[0]["content"].startswith("Continue this step")
    assert "You are" not in second[0]["content"]
    assert "VOICE LAW" not in second[0]["content"]


async def test_a_goal_step_can_still_finish_after_a_hand(tmp_path, cfg):
    """The round that ends a step is a follow-up whenever a hand ran. It has to
    be told how to say the goal is finished, or no goal that used a hand
    closes on the step that used it."""
    vault = _write_card(tmp_path / "yuri", "yuri")
    rig = rig_with_hands(
        cfg, vault, 'think look first\nuse read_note {"path": "notes/a.md"}',
        f"think it's written — {MindLoop.DONE_MARK}",
        allow="read_note", companion_name="Yuri")
    goal = rig.mind.goals.add("tidy the shed notes", kind="task", priority=0.95)
    await work(rig)
    utility = rig.mind.brain.state.utility
    second = utility.calls[-1]
    assert rig.mind.DONE_MARK in second[0]["content"]
    assert "VOICE LAW" not in second[0]["content"]
    body = second[1]["content"]
    assert body.startswith(goalwork.anchor(rig.mind, rig.mind.goals.get(goal.id)))
    assert "THE SITUATION RIGHT NOW" not in body
    assert rig.mind.goals.get(goal.id).state == "done"


async def test_a_night_job_keeps_its_output_format_and_its_input(tmp_path, cfg):
    """Live shape of the stock-take: four hands, and an answer read as JSON. A
    follow-up that dropped the job's prompt got prose back, and the night filed
    nothing."""
    vault = _write_card(tmp_path / "yuri", "yuri")
    answer = json.dumps({"reflection": "the shed can wait", "next": None})
    utility = _Script('use read_note {"path": "goals/shed.md"}', answer)
    rig = rig_with_hands(cfg, vault, allow="read_note", utility=utility,
                         companion_name="Yuri")
    rig.mind.goals.add("tidy the shed notes", kind="task", priority=0.9)
    ctx = rig.mind.dreams._context(job="strategy", soul="full", max_hands=2)
    report = await StrategyJob().work(ctx, "2026-10-08")
    assert len(utility.calls) == 2
    first, second = utility.calls
    assert "VOICE LAW" in first[0]["content"]
    assert "VOICE LAW" not in second[0]["content"]
    assert STRATEGY_OUTPUT.splitlines()[0] in second[0]["content"]
    # The job's whole input is what its answer is written from.
    assert second[1]["content"].startswith(first[1]["content"])
    assert "tidy the shed notes" in second[1]["content"]
    assert "could not read" not in report.result


async def test_a_handed_document_stays_in_front_of_her(tmp_path, cfg):
    vault = _write_card(tmp_path / "yuri", "yuri")
    rig, utility = _chain(cfg, vault)
    text = "Dear Yuri,\n\nthe garden plan is attached. SENTINEL-DOC"
    messages = [
        {"role": "system", "content": "This is you, alone — quietly advancing "
                                      "one of your own goals: a document."},
        {"role": "user", "content": document("inbox/plan.md", text)
                                    + "\n\nTHE SITUATION RIGHT NOW\n\nSENTINEL-SITUATION"},
    ]

    async def ask(outbound):
        return await rig.mind._utility(outbound, soul=True)

    await handwork.work(rig.mind, messages, offer=Offer(tools=("read_note",)),
                        ask=ask, cap=2, anchor=document("inbox/plan.md", text))
    assert len(utility.calls) == 2
    second = utility.calls[-1][1]["content"]
    assert "SENTINEL-DOC" in second
    assert "SENTINEL-SITUATION" not in second


async def test_another_call_made_during_a_followup_keeps_its_card(tmp_path, cfg):
    """The follow-up leaves the card off its own call only. A loop attribute
    did it for every call made while the model was thinking."""
    vault = _write_card(tmp_path / "yuri", "yuri")
    rig, utility = _chain(cfg, vault)
    release, finished = asyncio.Event(), asyncio.Event()
    elsewhere: list[list[dict]] = []

    async def other():
        await release.wait()
        await rig.mind._utility([{"role": "system", "content": "OTHER-CALL"},
                                 {"role": "user", "content": "hi"}], soul=True)
        elsewhere.append(utility.calls[-1])
        finished.set()

    task = asyncio.create_task(other())

    async def ask(outbound):
        if any(m["role"] == "user" and "read_note returned" in m["content"]
               for m in outbound):
            # The other call runs start to finish while this one is pending.
            release.set()
            await finished.wait()
        return await rig.mind._utility(outbound, soul=True)

    await LoopHands(rig.mind).run(_step(), ask, cap=2)
    await task
    assert "VOICE LAW" in elsewhere[0][0]["content"]
