"""One rule for every call she makes (SPEC §26.1, §7.3, §7.4).

A hand is usable when the house switch is on, her own switch is on, and the
allowlist admits it — in a reply, a greeting, a reach-out, a goal step or a
night's job alike. A reply and a step may chain calls. Every call is a small
notice in the chat.
"""
from __future__ import annotations

import json

import pytest

from yurios.kernel import correlate
from yurios.mind import handwork
from yurios.mind.hands import EVERY_HAND, HANDS, Hands, configured_names, permits
from yurios.world.tools.fakes import FakeToolRunner

from .conftest import ScriptedChat, ScriptedUtility, collect, make_mind, make_toolbrain

pytest.importorskip("fastapi")
from yurios.desktop.voice.backends.fakes import FakeBrain  # noqa: E402
from yurios.world.main import create_app, tool_notice_text  # noqa: E402

#: The fixture guard's allowlist is set_timer, play_music and list_notes.
LOOK = '[[list_notes {"folder": "notes"}]]'


# ---- the allowlist -----------------------------------------------------------

def test_star_is_every_hand_this_machine_can_offer(cfg):
    everything = cfg.model_copy(update={"mind_tool_allowlist": EVERY_HAND,
                                        "search_backend": "off"})
    assert configured_names(everything) is None
    names = Hands(cfg=everything, clock=None).allowlist  # type: ignore[arg-type]
    assert "write_note" in names and "play_music" in names
    assert "web_search" not in names, "a hand with its backend off is not offered"
    # …and a tool from somebody else's MCP server, which has no row in HANDS
    assert permits(everything, "their_tool")
    assert not permits(everything, "web_search")


def test_empty_is_none_and_a_list_is_exactly_that_list(cfg):
    """Empty is what unticking every box saves, so it cannot mean everything."""
    none = cfg.model_copy(update={"mind_tool_allowlist": ""})
    assert configured_names(none) == ()
    assert not permits(none, "write_note")
    some = cfg.model_copy(update={"mind_tool_allowlist": "read_note, set_timer"})
    assert permits(some, "read_note") and permits(some, "set_timer")
    assert not permits(some, "write_note") and not permits(some, "their_tool")


def test_the_chat_only_tools_are_hands_now():
    for name in ("play_music", "create_goal", "propose_edit", "delete_skill"):
        assert name in HANDS, f"{name} would be unreachable under one rule"


# ---- a reply: the directive, and the refusal ---------------------------------

async def test_a_reply_is_offered_only_what_the_rule_permits(
        cfg, guard, timers, controller):
    chat = ScriptedChat([[f"Let me look. {LOOK}"], ["Nothing there."]])
    runner = FakeToolRunner()
    tb = make_toolbrain(cfg, guard, timers, controller, chat, runner=runner)
    assert "- list_notes —" in tb.directive()
    tb.set_hands_policy(lambda tool: tool != "list_notes")

    assert "- list_notes —" not in tb.directive()
    assert "- set_timer —" in tb.directive()

    raw: list[str] = []
    outcomes: list[dict] = []
    await collect(tb._stream_with_tools(
        [{"role": "user", "content": "what's in a?"}], raw, outcomes))
    assert runner.calls == [], "a hand she may not use is never dispatched"
    assert outcomes[0]["verdict"].startswith("denied: not one of her hands")


async def test_switching_her_hands_off_takes_them_out_of_every_reply(
        cfg, guard, timers, controller):
    tb = make_toolbrain(cfg, guard, timers, controller, ScriptedChat([]),
                        runner=FakeToolRunner())
    on = {"value": True}
    tb.set_hands_policy(lambda tool: on["value"])
    assert tb.directive()
    on["value"] = False                               # the switchboard, live
    assert tb.directive() == ""
    assert not tb.goal_creation_available


async def test_a_reply_may_chain_calls_up_to_the_cap(cfg, guard, timers,
                                                     controller):
    """Look, then act: two calls in one reply, each result back first."""
    chat = ScriptedChat([
        [f"Let me look. {LOOK}"],
        ['Setting it. [[set_timer {"minutes": 5, "label": "tea"}]]'],
        ["Done."],
    ])
    runner = FakeToolRunner()
    tb = make_toolbrain(cfg, guard, timers, controller, chat, runner=runner)
    spoken = "".join(await collect(tb._stream_with_tools(
        [{"role": "user", "content": "fix a"}], [], [])))
    assert [c[0] for c in runner.calls] == ["list_notes", "set_timer"]
    assert spoken.endswith("Done.")


# ---- a line she says unprompted ------------------------------------------------

async def test_a_line_she_says_unprompted_has_her_hands(cfg, guard, timers,
                                                        controller):
    """The 24 Sep message: a reach-out cue pointed at a file, the compose call
    had no hands, and her model wrote its own call markup as the message."""
    chat = ScriptedChat([[f"One second. {LOOK}"], ["It came out lovely."]])
    runner = FakeToolRunner()
    tb = make_toolbrain(cfg, guard, timers, controller, chat, runner=runner)
    messages = [{"role": "system", "content": "you are her"},
                {"role": "user", "content": "((tell them what came of it))"}]

    said = "".join(await collect(tb._stream_unprompted(messages)))

    assert runner.calls == [("list_notes", {"folder": "notes"})]
    assert "[[" not in said and said.endswith("It came out lovely.")
    assert "## TOOLS" in chat.calls[0][0]["content"]
    assert "## TOOLS" in messages[0]["content"], "the prompt log sees what was sent"


# ---- the chat notice -------------------------------------------------------------

def test_every_call_is_a_notice_in_the_chat_and_nowhere_else(cfg):
    rt = create_app(cfg, brain=FakeBrain()).state.rt
    seen: list[dict] = []
    rt.hub.publish = lambda kind, payload, **kw: seen.append({"type": kind, **payload})

    with correlate.scope(kind=correlate.CHAT_TURN):
        rt.guard.audit("read_note", {"path": "notes/a.md"}, "ok", 3.0, "{}")
    with correlate.scope(kind=correlate.MIND_TOOL):
        rt.guard.audit("web_search", {"query": "tile adhesive"},
                       "denied: rate limit", 0.0, "")
    rt.guard.audit("(unparsed marker)", {}, "dropped: malformed marker", 0.0, "")

    rows = [m for m in rt.transcript if m["role"] == "tool"]
    assert [r["text"] for r in rows] == ["read_note · notes/a.md",
                                         "web_search · tile adhesive"]
    reply, own = rows
    assert "background" not in reply and own["background"] is True
    assert own["verdict"] == "denied" and own["why"] == "rate limit"
    assert [e["type"] for e in seen] == ["message", "message"]
    # …on the page and in the archive, never in the inbox or a prompt's window
    assert rt.inbox.unread()["count"] == 0
    assert any(r.get("role") == "tool" for r in rt.chatlog.tail(10))
    assert not any(r.get("w") for r in rt.chatlog.tail(10) if r.get("role") == "tool")


def test_her_switch_is_part_of_the_rule(cfg):
    rt = create_app(cfg, brain=FakeBrain()).state.rt
    assert rt.hands_permit("write_note")
    rt.set_hands_enabled(False)
    assert not rt.hands_permit("write_note")


def test_a_notice_names_what_the_call_touched():
    assert tool_notice_text("set_timer", {"minutes": 5}) == "set_timer · 5 min"
    assert tool_notice_text("list_notes", {}) == "list_notes"
    long = tool_notice_text("web_search", {"query": "x" * 200})
    assert len(long) < 80 and long.endswith("…")


# ---- her own work: a step chains ---------------------------------------------------

def _hands(rig, **extra):
    from yurios.mind.hands import build_guard
    rig.mind.cfg = rig.mind.cfg.model_copy(update={
        "mind_tools_enabled": True, "mind_tool_allowlist": "*", **extra})
    rig.mind.hands.cfg = rig.mind.cfg
    rig.mind.hands.guard = build_guard(rig.mind.cfg, rig.clock)


async def test_a_goal_step_reads_then_writes_then_finishes(cfg, seeded_vault):
    runner = FakeToolRunner(results={"read_note": json.dumps(
        {"path": "notes/tiles.md", "text": "grout: grey"})})
    utility = ScriptedUtility(
        'think see what I wrote\nuse read_note {"path": "notes/tiles.md"}',
        'use append_note {"path": "notes/tiles.md", "text": "adhesive: C2"}',
        "think both halves are down — goal complete")
    rig = make_mind(cfg, seeded_vault, utility=utility, tools=runner)
    _hands(rig)
    goal = rig.mind.goals.add("finish the tile notes", kind="task", priority=0.95)

    trace = await rig.mind.tick()

    assert [c[0] for c in runner.calls] == ["read_note", "append_note"]
    assert trace["acted"]["result"].endswith("read_note (ok), append_note (ok)")
    assert [t["tool"] for t in trace["acted"]["tools"]] == ["read_note", "append_note"]
    assert rig.mind.goals.get(goal.id).state == "done"
    # the second call was asked with the first one's result in hand
    step = [c for c in utility.calls if "advancing one of your own goals"
            in c[0]["content"].lower()]
    assert "grout: grey" in step[1][-1]["content"]


async def test_a_step_stops_at_the_cap(cfg, seeded_vault):
    line = 'use write_note {{"path": "n{}.md", "text": "x"}}'
    utility = ScriptedUtility(*[line.format(i) for i in range(5)])
    runner = FakeToolRunner()
    rig = make_mind(cfg, seeded_vault, utility=utility, tools=runner)
    _hands(rig, tool_max_calls_per_turn=2)
    rig.mind.goals.add("write five notes", kind="task", priority=0.95)

    await rig.mind.tick()
    assert len(runner.calls) == 2, "past the cap a `use` line is dropped, not run"


# ---- a night's job in her own voice -----------------------------------------------

async def test_a_job_in_her_voice_may_use_her_hands_and_extraction_may_not(
        cfg, seeded_vault):
    runner = FakeToolRunner()
    rig = make_mind(cfg, seeded_vault, tools=runner)
    _hands(rig)
    replies = iter(['use read_note {"path": "diary/2026-07-05.md"}',
                    "Dear diary: the rain again."])

    async def utility(messages, **params):
        return next(replies)

    ctx = rig.mind.dreams._context()
    ctx.utility = utility
    ctx.soul = "full"
    assert await ctx.ask("write your diary", "today") == "Dear diary: the rain again."
    assert runner.calls == [("read_note", {"path": "diary/2026-07-05.md"})]
    # the debug page's transcript is the prompt that was sent, not a summary of it
    assert "## YOUR HANDS" in ctx.exchanges[-1].system

    runner.calls.clear()
    ctx.soul = "off"                                    # consolidation is extraction
    replies = iter(['use read_note {"path": "x.md"}'])
    await ctx.ask("list the facts", "today")
    assert runner.calls == []

    ctx.soul, ctx.dry_run = "full", True                # a rehearsal touches nothing
    replies = iter(['use read_note {"path": "x.md"}'])
    await ctx.ask("write your diary", "today")
    assert runner.calls == []


def test_the_hands_before_answer_block_is_additive():
    text = handwork.HANDS_BEFORE_ANSWER.format(cap=3, catalog="  use x {}")
    assert "give your answer exactly as asked above" in text



def test_a_goal_step_hears_what_was_said_not_the_tool_notices(tmp_path):
    """`said_since` labels every row that isn't yours as hers. A notice read
    that way is her "saying" `list_notes · diary` — and forty of them push the
    real conversation out of the window it reads."""
    from types import SimpleNamespace

    from yurios.app.conversation import ConversationLog
    from yurios.mind.goalwork import said_since

    log = ConversationLog(tmp_path)
    log.add({"id": "u1", "role": "user", "text": "how did the tiles go?",
             "ts": "2026-09-24T10:00:00"})
    for i in range(45):
        log.add({"id": f"t{i}", "role": "tool", "tool": "list_notes",
                 "text": "list_notes · diary", "ts": "2026-09-24T10:01:00"})
    loop = SimpleNamespace(
        brain=SimpleNamespace(state=SimpleNamespace(sessions=SimpleNamespace(log=log))),
        store=SimpleNamespace(user_name="Grant", char_name="Yuri"))
    goal = SimpleNamespace(created="2026-09-24T09:00:00")

    heard = said_since(loop, goal)  # type: ignore[arg-type]
    assert "how did the tiles go?" in heard
    assert "list_notes" not in heard

