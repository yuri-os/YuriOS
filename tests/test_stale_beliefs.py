"""The surfaces that told her a question was unanswered after it was answered.

She asked which framing he wanted, he answered a minute later, and for the next
hour every place she could have learned it said otherwise: the rolling summary
had not folded the tail of the session, recall buried the reply as a duplicate
of the question, and goal work never sees a turn filed after the goal was. Then
her own note saying so came back next tick as evidence for itself.

One file because they are one failure with three doors into it.
"""
from __future__ import annotations

import json

from yurios.mind.util import iso_of

from .conftest import ScriptedUtility, make_mind


async def work(rig, ticks=1):
    traces = []
    for _ in range(ticks):
        traces.append(await rig.mind.tick())
        rig.clock.advance(rig.mind.cfg.mind_consider_cooldown_s + 60)
    return traces


def _said(rig, role: str, text: str, mid: str) -> None:
    """One committed line in the room, as `post_message` files it."""
    rig.mind.brain.state.sessions.log.add(
        {"id": mid, "role": role, "text": text,
         "ts": iso_of(rig.clock.now())}, window=True)


def _goal_prompts(utility) -> str:
    return json.dumps([c for c in utility.calls
                       if "advancing one of your own goals"
                       in json.dumps(c).lower()])


# --- goal work can be told the answer came ------------------------------------

async def test_a_goal_waiting_on_an_answer_is_shown_the_answer(
        cfg, seeded_vault):
    """`about` freezes when the goal is filed. This goal's own text names a
    turn that had not happened yet, so nothing frozen could ever satisfy it."""
    utility = ScriptedUtility(*["think still thinking about it."] * 4)
    rig = make_mind(cfg, seeded_vault, utility=utility)
    rig.mind.goals.add(
        "send the picture once they answer the framing question",
        kind="task", priority=0.95, provenance="promise:her-own-words",
        meta={"about": "I like the almost-nude ones"})
    rig.clock.advance(120)
    _said(rig, "user", "I want something only I can see", "m1")

    await work(rig)

    asked = _goal_prompts(utility)
    assert "only I can see" in asked, \
        "the goal was worked without the answer it was waiting for"
    assert "SAID SINCE" in asked


async def test_what_was_said_before_the_goal_is_not_replayed_as_news(
        cfg, seeded_vault):
    """Only what came *after*. The exchange that made the goal is already in
    the prompt under WHERE THIS CAME FROM."""
    utility = ScriptedUtility(*["think still thinking about it."] * 4)
    rig = make_mind(cfg, seeded_vault, utility=utility)
    _said(rig, "user", "a line from long before any of this", "m0")
    rig.clock.advance(120)
    rig.mind.goals.add("do the thing they asked about", kind="task",
                       priority=0.95, provenance="promise:her-own-words",
                       meta={"about": "please do the thing"})

    await work(rig)

    assert "long before any of this" not in _goal_prompts(utility)


# --- recall stops spending its slots restating the question -------------------

async def test_recall_does_not_hand_the_goal_back_its_own_words(
        cfg, seeded_vault):
    """Two of six slots went to a journal line reading "I took that on: <goal
    text>" and to the very exchange `about` was copied from — both already
    printed above, and holding the second is what let MMR bury the reply."""
    utility = ScriptedUtility(*["think still thinking about it."] * 4)
    rig = make_mind(cfg, seeded_vault, utility=utility)
    goal_text = "send the picture once they answer the framing question"
    store = rig.mind.store
    for cid, text in (
            ("echo", f"I took that on: {goal_text}"),
            ("news", "he said the kettle is on and he is walking back now")):
        store.index.upsert(
            id=cid, kind="turn", text=text, source_path="t", source_span="",
            created_at="2026-07-06T09:00:00+00:00", salience=1.0,
            embedding=store.embedder.embed([text])[0])
    rig.mind.goals.add(goal_text, kind="task", priority=0.95,
                       provenance="promise:her-own-words")

    await work(rig)

    asked = _goal_prompts(utility)
    assert f"I took that on: {goal_text}" not in asked, \
        "recall spent a slot restating the goal printed above it"


# --- the greeting opens on what was actually said -----------------------------

async def test_a_greeting_opens_on_the_last_words_not_only_the_summary(
        cfg, seeded_vault, clock, controller):
    """`summary.md` folds every `summary_every_n` turns, so a session's last
    turns are never folded at all — and the summary asserts in confident prose.
    Hers said he "hasn't gotten the answer" four minutes after he gave it, and
    she opened by asking for it again."""
    from tests.test_bootstrap_greeting import make_brain
    from .conftest import CannedChat, collect

    (seeded_vault / "memory" / "episodic" / "2026-07-01.md").write_text(
        "# Journal — 2026-07-01\n\n### 09:00  you: hello  ⇄  her: hello\n")
    (seeded_vault / "memory" / "summary.md").write_text(
        "# Conversation summary\n\nShe asked which framing he wanted and "
        "hasn't gotten the answer.\n")
    chat = CannedChat("[tender] Mine-only, then.")
    brain = make_brain(cfg, seeded_vault, chat, clock, controller)
    brain.state.sessions.log.add(
        {"id": "m1", "role": "user", "text": "I want something only I can see",
         "ts": "2026-07-01T20:36:46"}, window=True)

    await collect(brain.stream_greeting("s1"))

    asked = json.dumps(chat.calls)
    assert "only I can see" in asked, \
        "she greeted from a summary that predates the answer, with no transcript"


async def test_a_tool_notice_in_the_column_never_reaches_the_greeting(
        cfg, seeded_vault, clock, controller):
    """A notice that she used a hand is a row in the column (§7.3) with its own
    role. Handed to the model as `role: "tool"` with no call behind it, the
    provider refuses the request — and the greeting is the first thing you hear."""
    from tests.test_bootstrap_greeting import make_brain
    from .conftest import CannedChat, collect

    (seeded_vault / "memory" / "episodic" / "2026-07-01.md").write_text(
        "# Journal — 2026-07-01\n\n### 09:00  you: hello  ⇄  her: hello\n")
    chat = CannedChat("[neutral] Hey.")
    brain = make_brain(cfg, seeded_vault, chat, clock, controller)
    brain.state.sessions.log.add(
        {"id": "m1", "role": "user", "text": "check my notes",
         "ts": "2026-07-01T20:36:40"}, window=True)
    brain.state.sessions.log.add(
        {"id": "t1", "role": "tool", "tool": "read_note",
         "text": "read_note · notes/a.md", "ts": "2026-07-01T20:36:46"})

    await collect(brain.stream_greeting("s1"))

    roles = {m["role"] for call in chat.calls for m in call}
    assert roles <= {"system", "user", "assistant"}
    assert "read_note · notes/a.md" not in json.dumps(chat.calls)
    assert "check my notes" in json.dumps(chat.calls)


async def test_a_wordless_entry_never_becomes_a_blank_turn(
        cfg, seeded_vault, clock, controller):
    """A selfie is a chat entry with no words in it. A blank turn in the window
    teaches her to send one."""
    from tests.test_bootstrap_greeting import make_brain
    from .conftest import CannedChat, collect

    (seeded_vault / "memory" / "episodic" / "2026-07-01.md").write_text(
        "# Journal — 2026-07-01\n\n### 09:00  you: hello  ⇄  her: hello\n")
    chat = CannedChat("[neutral] Hey.")
    brain = make_brain(cfg, seeded_vault, chat, clock, controller)
    brain.state.sessions.log.add(
        {"id": "pic", "role": "assistant", "text": "",
         "image_url": "/selfies/x.png", "ts": "2026-07-01T20:00:00"},
        window=True)

    await collect(brain.stream_greeting("s1"))

    window = chat.calls[0]
    assert all((m.get("content") or "").strip() for m in window), \
        f"a blank turn reached the prompt: {window}"
