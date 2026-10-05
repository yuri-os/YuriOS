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


def _handed_on(rig):
    """The two holders one photo has once it is handed on: the goal that made
    it, still open, and the follow-up filed to send it (§18.2a)."""
    from yurios.mind import goalwork
    from yurios.mind.util import iso_of

    parent = rig.mind.goals.add("take the raincheck selfie", kind="task")
    rig.mind.goals.update(parent.id, state="active", meta={"product": dict(SHOT)})
    goalwork.offer_the_picture(rig.mind, rig.mind.goals.get(parent.id))
    heir = next(g for g in rig.mind.goals.all()
                if g.provenance == f"followup:{parent.id}")
    rig.mind.goals.update(heir.id, priority=1.0,
                          due=iso_of(rig.clock.now() + 60))
    return rig.mind.goals.get(parent.id), rig.mind.goals.get(heir.id)


async def test_a_follow_up_that_sends_it_tells_the_goal_that_made_it(
        cfg, seeded_vault):
    """Live, 5 Oct: the follow-up sent the picture at 09:01; the parent went on
    telling chat it was "already taken; Grant hasn't seen it yet", and at 09:04
    she showed it a second time."""
    from yurios.mind import acts
    from yurios.world.tooltags import ToolCall

    rig, posted = _rig(cfg, seeded_vault)
    parent, heir = _handed_on(rig)
    rig.speak.connected = True

    _, interrupt, _ = await acts.reach_out(rig.mind, heir)
    assert interrupt["outcome"] in ("SPEAK", "SUGGEST"), interrupt

    assert rig.mind.goals.get(parent.id).held_picture == ""
    line = next(x for x in rig.mind.brain._open_goals()[0] if parent.id in x)
    assert "already taken" not in line
    result = await rig.mind.brain._execute(
        ToolCall("show_held_picture", {"goal_id": parent.id}))
    assert result.startswith("error") and posted == []


async def test_showing_it_in_chat_closes_the_follow_up_filed_to_send_it(
        cfg, seeded_vault):
    """The other order: shown mid-conversation, the follow-up must not send
    it again through Gate 2."""
    from yurios.world.tooltags import ToolCall

    rig, posted = _rig(cfg, seeded_vault)
    parent, heir = _handed_on(rig)

    await rig.mind.brain._execute(
        ToolCall("show_held_picture", {"goal_id": parent.id}))

    assert len(posted) == 1
    after = rig.mind.goals.get(heir.id)
    assert after.state == "done" and after.held_picture == ""


# --- PICTURES YOU HAVEN'T SENT: by the picture, not the goal (§18.2a) --------
#
# Live, 6 Oct: the night's stock-take took the raincheck selfie (rated 9), the
# selfie job rendered a near-copy (rated 10), and neither reached a goal — so
# no goal line said they existed and no hand could send them.

def _shelve(cfg, name, **row):
    cfg.selfie_dir.mkdir(parents=True, exist_ok=True)
    (cfg.selfie_dir / name).write_bytes(b"\x89PNG")
    line = {"image": name, "created_at": "2026-10-06T02:10:53", "kind": "selfie",
            "template": {"look": "floor below the window, hands reaching up"},
            **row}
    with (cfg.selfie_dir / "generations.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")
    return f"/selfies/{name}"


def _chatting(cfg, seeded_vault):
    """`_rig`, with `post` writing the conversation the way `post_message`
    does — the list reads the conversation, so a post that wrote nothing could
    never be seen to empty it."""
    rig, posted = _rig(cfg, seeded_vault)
    chatlog = rig.mind.brain.state.sessions.log

    def post(role, text, **kw):
        posted.append((role, text, kw))
        chatlog.add({"id": f"m{len(posted)}", "role": role, "text": text, **kw})
        return {}

    rig.mind.brain.post = post
    return rig, posted


async def test_the_prompt_lists_every_picture_not_yet_sent(cfg, seeded_vault):
    rig, _ = _chatting(cfg, seeded_vault)
    night = _shelve(cfg, "1791220254-cb7ec091.png", selfie_id="cb7ec091", by="mind")
    _shelve(cfg, "1791220260-0wn3r000.png", selfie_id="0wn3r000", by="owner")
    held = _holding(rig, {**SHOT, "image_url": night, "selfie_id": "cb7ec091"})

    block = await rig.mind.brain._pictures_block()
    assert block.startswith("## PICTURES YOU HAVEN'T SENT")
    assert "`show_held_picture` with its `picture_id`" in block
    assert ("- [cb7ec091] selfie, 2026-10-06 02:10 — floor below the window, "
            f"hands reaching up (goal [{held.id}] is holding it too)") in block
    assert "0wn3r000" not in block, "the owner's own render is not hers to send"

    soul, prompt = await rig.mind.brain._assemble(
        "s", "hi", window=[], lore=[])
    assert "[cb7ec091]" in prompt.messages[0]["content"]


async def test_no_hand_no_list(cfg, seeded_vault):
    rig, _ = _chatting(cfg, seeded_vault)
    _shelve(cfg, "1791220254-cb7ec091.png", selfie_id="cb7ec091")
    rig.mind.brain.set_hands_policy(lambda tool: tool != "show_held_picture")
    assert await rig.mind.brain._pictures_block() == ""


async def test_showing_one_by_picture_id_posts_it_and_closes_its_errand(
        cfg, seeded_vault):
    from yurios.world.tooltags import ToolCall

    rig, posted = _chatting(cfg, seeded_vault)
    night = _shelve(cfg, "1791220254-cb7ec091.png", selfie_id="cb7ec091", by="mind")
    errand = rig.mind.goals.add("send them the picture I took", kind="reach_out",
                                provenance="followup:cb7ec091",
                                meta={"product": {"image_url": night,
                                                  "selfie_id": "cb7ec091",
                                                  "deliver": "vault"}})

    result = json.loads(await rig.mind.brain._execute(
        ToolCall("show_held_picture", {"picture_id": "cb7ec091"})))

    assert result["status"] == "shown" and result["picture_id"] == "cb7ec091"
    assert posted == [("assistant", "", {"image_url": night,
                                         "selfie_id": "cb7ec091"})]
    assert rig.mind.goals.get(errand.id).state == "done", \
        "Gate 2 would have sent it a second time"
    assert await rig.mind.brain._pictures_block() == "", "and it is off the list"

    again = await rig.mind.brain._execute(
        ToolCall("show_held_picture", {"picture_id": "cb7ec091"}))
    assert again.startswith("error") and len(posted) == 1


def test_how_are_your_goals_going_is_a_status_question():
    assert is_goal_status_request("how are your goals going?")
    assert is_goal_status_request("How are your goals coming along")
    assert not is_goal_status_request("I'm going out for a bit")
