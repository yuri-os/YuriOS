"""A picture a goal is holding can be shown in the conversation (SPEC §18.2a, §22.6).

Live, 3 Oct: two selfies the mind took for two reach-out goals sat behind Gate 2
at 0.57 and 0.59 against a 0.6 threshold. The goal list in the chat prompt said
only "(active)", and asked how her goals were going she said she had not taken
either shot. These pin both halves: the line says the picture exists, and the
conversation has a hand that shows it.
"""
from __future__ import annotations

import json

from yurios.app.core.assemble import is_goal_status_request

from .conftest import make_mind

SHOT = {"image_url": "/selfies/1791023350-03ab0cad.png", "selfie_id": "03ab0cad",
        "detail": "window seat, rain behind", "deliver": "vault"}


def _rig(cfg, seeded_vault):
    from yurios.world.tools.fakes import SPECS, FakeToolRunner

    rig = make_mind(cfg, seeded_vault)
    posted: list[tuple[str, str, dict]] = []
    rig.mind.brain.post = lambda role, text, **kw: posted.append((role, text, kw)) or {}
    rig.mind.brain.set_tools(FakeToolRunner(), list(SPECS))
    rig.mind.brain.guard.allow("show_held_picture", 6)
    return rig, posted


def _holding(rig, product=SHOT):
    goal = rig.mind.goals.add("send Grant the window-seat selfie", kind="reach_out",
                              provenance="user:chat")
    rig.mind.goals.update(goal.id, state="active", meta={"product": dict(product)})
    return rig.mind.goals.get(goal.id)


def test_the_goal_line_says_the_picture_is_already_taken(cfg, seeded_vault):
    rig, _ = _rig(cfg, seeded_vault)
    held = _holding(rig)
    plain = rig.mind.goals.add("read the paddleboard thing", kind="task")

    lines, _ = rig.mind.brain._open_goals()
    line = next(x for x in lines if held.id in x)
    assert "already taken" in line and "hasn't seen it" in line
    assert "`show_held_picture` with this goal_id" in line
    assert not any("already taken" in x for x in lines if plain.id in x)


def test_the_line_names_the_hand_only_when_she_has_it(cfg, seeded_vault):
    rig, _ = _rig(cfg, seeded_vault)
    held = _holding(rig)
    rig.mind.brain.set_hands_policy(lambda tool: tool != "show_held_picture")

    line = next(x for x in rig.mind.brain._open_goals()[0] if held.id in x)
    assert "already taken" in line
    assert "show_held_picture" not in line


def test_a_picture_already_in_the_chat_is_not_marked(cfg, seeded_vault):
    rig, _ = _rig(cfg, seeded_vault)
    held = _holding(rig, {**SHOT, "deliver": "chat"})

    line = next(x for x in rig.mind.brain._open_goals()[0] if held.id in x)
    assert "already taken" not in line


async def test_showing_it_posts_the_picture_and_keeps_the_goal(cfg, seeded_vault):
    from yurios.mind import goalwork
    from yurios.world.tooltags import ToolCall

    rig, posted = _rig(cfg, seeded_vault)
    held = _holding(rig)

    result = json.loads(await rig.mind.brain._execute(
        ToolCall("show_held_picture", {"goal_id": held.id})))

    assert result["status"] == "shown" and result["goal_id"] == held.id
    assert posted == [("assistant", "", {"image_url": SHOT["image_url"],
                                         "selfie_id": "03ab0cad"})]
    goal = rig.mind.goals.get(held.id)
    assert goal.state == "done"
    assert goal.product["deliver"] == "chat" and goal.held_picture == ""
    assert goal.meta["completed_by"] == "show_held_picture"
    # …so no later exit of this goal hands the same picture on again.
    assert goalwork.offer_the_picture(rig.mind, goal) == []


async def test_nothing_to_show_is_an_error_she_reads(cfg, seeded_vault):
    from yurios.world.tooltags import ToolCall

    rig, posted = _rig(cfg, seeded_vault)
    empty = rig.mind.goals.add("tell Grant about the rain", kind="reach_out")
    shown = _holding(rig, {**SHOT, "deliver": "chat"})

    for goal_id in (empty.id, shown.id, "g-nope"):
        result = await rig.mind.brain._execute(
            ToolCall("show_held_picture", {"goal_id": goal_id}))
        assert result.startswith("error")
    assert posted == []
    assert rig.mind.goals.get(empty.id).state == "pending"


def test_how_are_your_goals_going_is_a_status_question():
    assert is_goal_status_request("how are your goals going?")
    assert is_goal_status_request("How are your goals coming along")
    assert not is_goal_status_request("I'm going out for a bit")
