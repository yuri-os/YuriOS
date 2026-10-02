"""Free time — what she does when nothing on her list is ready (SPEC §22.7).

Every intention the loop had came from somewhere else: a signal, a promise, a
goal the night filed, a leftover. So a mind whose list had run dry sat in REST
for good — found live, 29 Sep: her one goal closed at 02:14 and every tick for
the next seventeen hours was `DORMANT → REST`, with nothing sensed, nothing
appraised, and nothing that ever could be until somebody spoke to her. An
always-on mind with nothing on its list is not resting, it is stopped.

This is the way out. When nothing crosses gate 1 and no goal is merely cooling
between steps, a cheap impulse offers her an hour's free time: she is shown her
recent days — what was said, her journal, what she finished or let go of, her
desk, her skills, the shelf — and decides, in her own voice, what is worth
doing next. The answer that moves anything is a goal (`create_goal`), filed as
hers and worked on its own turns with all her hands; principle 9 holds, because
a hand she reaches for is still a step of an open goal. Before she decides she
may open her own notes — reading is how she reviews, and it changes nothing.
An answer that is only a thought is kept too, on her desk and in her journal.

`loop` is unannotated for the reason `world/runtime.py` gives: naming its type
means importing `MindLoop`, and `tests/test_layering.py` reads a
`TYPE_CHECKING` import off the parse tree like any other.
"""
from __future__ import annotations

import json
import logging

from yurios.kernel import correlate

from . import acts, handwork
from .goals import GOAL_TEXT_MAX, trim
from .goalwork import takeaway
from .hands import FILE_GOAL, Hands, Offer
from .journal import parse_day_entries
from .policy import DORMANT, IDLE, Appraisal, appraise_goal
from .util import day_of, iso_of

log = logging.getLogger("mind.muse")

#: The hands free time may reach for before it decides: reading her own desk and
#: skills. Anything that changes the world — a note written, a search, a photo —
#: is a goal's step, so it is filed as one and done on its own turn.
REVIEW = ("list_notes", "read_note", "count_note_lines", "read_skill")

#: Calls one sitting may chain. Enough to open a few notes and decide.
MAX_CALLS = 6

#: Past this share of the day's budget she stops starting free time, and what is
#: left is kept for the goals it filed (goal work stops at 1.0, §17.3).
PRESSURE_CEILING = 0.75

#: Where each sitting's conclusion is kept, one file a day, and read back by the
#: next sitting so she does not have the same idea every hour.
DESK = "free-time/{day}.md"


def cooling(loop, goal, now: float) -> bool:
    """Is this goal work in progress, only waiting out its consider cooldown?

    Such a goal is what she is busy with: free time between its steps would
    compete with it for the same hours and budget. A goal that could not clear
    gate 1 even when rested is not in progress — it is stuck, and is exactly
    the case free time exists for.
    """
    last = loop.considered.get(goal.id)
    if not last or (now - last) >= loop.cfg.mind_consider_cooldown_s:
        return False
    return appraise_goal(goal, loop.clock).score >= loop.cfg.mind_act_threshold


def appraise(loop, appraisals: list[Appraisal], *, busy: bool,
             now: float) -> Appraisal | None:
    """The free-time impulse, or None. Model-free, as APPRAISE must be (§15.1).

    Offered only when nothing else crossed gate 1, and scored exactly at it, so
    it can never outrank anything that did.
    """
    cooldown = float(getattr(loop.cfg, "mind_muse_cooldown_s", 0) or 0)
    if cooldown <= 0 or not loop.cfg.utility_enabled:
        return None
    if busy or loop.activity.state not in (IDLE, DORMANT) or loop._engaged_now():
        return None
    if now - loop.last_mused < cooldown:
        return None
    if loop.budget.pressure() >= PRESSURE_CEILING:
        return None
    threshold = loop.cfg.mind_act_threshold
    if any(a.score >= threshold for a in appraisals):
        return None
    return Appraisal("muse", "impulse", threshold,
                     "nothing on my list is ready — free time")


def _desk(loop, day: str) -> str:
    if loop.workspace is None:
        return ""
    try:
        return loop.workspace.read(DESK.format(day=day), default="") or ""
    except Exception:  # noqa: BLE001 — a desk file is never worth a dead tick
        log.debug("free time: desk read failed", exc_info=True)
        return ""


def _journal(loop, now: float, limit: int = 24) -> str:
    """Her own recent journal lines, yesterday's and today's, newest last."""
    rows: list[str] = []
    for day in (day_of(now - 86400), day_of(now)):
        text = loop.vault.read(f"memory/episodic/{day}.md")
        rows += [f"{day[5:]} {e['time']} {trim(e['text'], 220)}"
                 for e in parse_day_entries(text) if e["hers"]]
    return "\n".join(rows[-limit:])


def _said(loop, limit: int = 16) -> str:
    """The last few lines actually said between you, newest last."""
    sessions = getattr(getattr(loop.brain, "state", None), "sessions", None)
    chatlog = getattr(sessions, "log", None)
    if chatlog is None:
        return ""
    try:
        rows = chatlog.said(limit)
    except Exception:  # noqa: BLE001 — no transcript is not a failed sitting
        log.debug("free time: no transcript", exc_info=True)
        return ""
    them = getattr(loop.store, "user_name", None) or loop.cfg.user_name
    her = getattr(loop.store, "char_name", None) or "you"
    out = []
    for r in rows:
        text = " ".join(str(r.get("text") or "").split())
        if text:
            who = them if r.get("role") == "user" else her
            out.append(f"{str(r.get('ts') or '')[:16]} {who}: {trim(text, 300)}")
    return "\n".join(out)


def context(loop, now: float) -> str:
    """What she looks back over. Bounded throughout: a glance, not the archive."""
    parts: list[str] = []
    try:
        parts.append("THE SITUATION RIGHT NOW\n\n" + loop.world.situation())
    except Exception:  # noqa: BLE001
        log.debug("free time: no situation", exc_info=True)
    said = _said(loop)
    if said:
        parts.append("WHAT WAS SAID LATELY (newest last)\n\n" + said)
    journal = _journal(loop, now)
    if journal:
        parts.append("YOUR JOURNAL, YESTERDAY AND TODAY (newest last)\n\n" + journal)
    closed = [g for g in loop.goals.all() if g.state in ("done", "abandoned")][-8:]
    if closed:
        parts.append("GOALS YOU FINISHED OR LET GO OF LATELY (newest last)\n\n"
                     + "\n".join(f"- [{g.state}] {g.text}" for g in closed))
    open_goals = loop.goals.open_goals()
    if open_goals:
        parts.append("STILL ON YOUR LIST (waiting or resting — not for now)\n\n"
                     + "\n".join(f"- [{g.state}] {g.text}" for g in open_goals[-8:]))
    else:
        parts.append("STILL ON YOUR LIST\n\nNothing. Your list is empty.")
    if loop.workspace is not None:
        digest = loop.workspace.digest(limit=20)
        if digest:
            parts.append("YOUR DESK (paths only — `read_note` opens one)\n\n" + digest)
    if loop.skills is not None:
        catalog = loop.skills.catalog(limit=12)
        if catalog:
            parts.append("SKILLS YOU HAVE WRITTEN DOWN\n\n" + catalog)
    try:
        shelf = loop.knowledge.shelf()
    except Exception:  # noqa: BLE001
        shelf = []
    if shelf:
        parts.append("ON YOUR SHELF\n\n" + "\n".join(f"- {d}" for d in shelf[-12:]))
    facts = loop.vault.read("memory/semantic/facts.md")[-1200:].strip()
    if facts:
        parts.append("WHAT YOU KNOW ABOUT THEM\n\n" + facts)
    earlier = (_desk(loop, day_of(now)) or _desk(loop, day_of(now - 86400)))
    if earlier.strip():
        parts.append("WHAT YOU THOUGHT IN YOUR LAST FREE TIME\n\n"
                     + earlier.strip()[-1500:]
                     + "\n\nDon't settle on the same thing again unless it "
                       "still matters more than anything else.")
    return "\n\n".join(parts)


def system(loop, tools: tuple[str, ...]) -> str:
    """The instruction half. In her voice, like every mind prompt (§22.4)."""
    user = loop.cfg.user_name
    lines = [
        "This is your free time. Nothing on your list is ready for you and "
        "nobody is waiting on you — so rather than sitting idle, look back "
        f"over your days: what you and {user} talked about, your journal, "
        "what you finished or let go of, what's on your desk and your shelf. "
        "Then decide what is worth doing next. It's open: follow up on "
        f"something {user} said or cared about, look into a topic that "
        "pulled at you, go back to a note or a skill and see what it's "
        "missing, work out what went well or badly in a conversation and "
        "what you'd do differently, or start something that is simply yours.",
        "",
        "Think it through as yourself, not as an assistant filling a slot.",
        "",
    ]
    if FILE_GOAL in tools:
        lines += [
            "When you know what you want to do, put it on your list with "
            f"`{FILE_GOAL}` — one concrete goal, written as what you'll do, "
            f"in one line of at most {GOAL_TEXT_MAX} characters. Why you want "
            "it goes in the `think` line above the call, not in the goal: "
            "that line is kept with the goal as its plan. "
            "It gets worked on its own turns, with all your hands (search, "
            "reading, notes, the camera). If the point is for "
            f"{user} to hear something from you, make its kind \"reach_out\". "
            "Most free time should end with something on your list; ending "
            "on a thought alone is fine now and then, not every time.",
            "",
        ]
    lines += [
        "Answer with one line, in one of these forms:",
        "",
        "  think <what you noticed, and what you decided>",
        Hands.rows(tools) if tools else "",
        "",
    ]
    if tools:
        lines += [
            "…where the part after the tool name is one line of JSON, on a line "
            "of its own. Each result comes back to you before you go on, so you "
            f"can look before you decide — up to {MAX_CALLS} in this sitting. "
            "Put a `think` line above a hand saying why, and end on a `think` "
            "line.",
        ]
    return "\n".join(line for line in lines if line is not None).strip()


async def muse(loop, offer: Offer | None) -> tuple[dict, dict, list[str]]:
    """One sitting of free time: look back, decide, leave a trail (§22.7)."""
    now = loop.clock.now()
    # Stamped before the call, not after: a sitting that fails must not be
    # retried on the very next tick — the cooldown is the rate, either way.
    loop.last_mused = now
    loop._muse_filed = ""
    tools = tuple(t for t in (offer.tools if offer else ()) if t in REVIEW)
    if getattr(loop.cfg, "mind_goal_filing_enabled", True):
        tools += (FILE_GOAL,)
    notes: list[str] = []

    async def ask(messages: list[dict]) -> str:
        return await loop._utility(messages, soul=True)

    messages = [{"role": "system", "content": system(loop, tools)},
                {"role": "user", "content": context(loop, now)}]
    with correlate.scope(kind=correlate.MUSE):
        worked = await handwork.work(
            loop, messages, offer=Offer(tools=tools), ask=ask, cap=MAX_CALLS,
            file_goal=lambda args: acts.file_from_muse(loop, args))

    filed = None
    for reach in worked.reaches:
        if reach.tool == FILE_GOAL and reach.verdict == "ok":
            try:
                data = json.loads(reach.result)
            except ValueError:
                data = {}
            if data.get("status") == "created":
                filed = data
                # Her reason becomes the goal's plan, so its first step starts
                # from what she decided here rather than from the title alone
                # (§22.4, "and the plan she filed it with").
                if reach.why:
                    loop.goals.update(str(data["id"]),
                                      meta={"rationale": trim(reach.why, 600)})
                notes.append(f"free time: decided to take on “{data.get('text')}”")
            else:
                notes.append(f"free time: thought of “{data.get('text')}”, "
                             "but I'm already carrying it")
        elif reach.verdict != "ok":
            notes.append(f"free time: wanted to {reach.tool} but didn't: "
                         f"{reach.refused or reach.result}")

    thought = (worked.answer.text or "").strip()
    if thought:
        notes.append(f"free time: {takeaway(thought)}")
    trail = "\n\n".join(p for p in (
        thought,
        f"→ on my list now: {filed.get('text')} ({filed.get('id')})" if filed else "",
    ) if p)
    if trail and loop.workspace is not None:
        try:
            loop.workspace.append(DESK.format(day=day_of(now)),
                                  f"\n## {iso_of(now)}\n\n{trail}\n")
            loop.hub.publish("workspace", {"action": "append",
                                           "path": DESK.format(day=day_of(now))})
        except Exception:  # noqa: BLE001
            log.warning("free time: desk write failed", exc_info=True)
    looked = [r.tool for r in worked.reaches if r.tool in REVIEW]
    result = (f"free time: filed {filed.get('id')}" if filed
              else "free time: thought it over" if thought
              else "free time: nothing came of it")
    if looked:
        result += f" (looked at {len(looked)} thing{'s' if len(looked) != 1 else ''})"
    acted: dict = {"what": "muse", "result": result}
    if worked.reaches:
        acted["tools"] = [{"tool": r.tool, "verdict": r.verdict}
                          for r in worked.reaches]
    if filed:
        acted["goal"] = filed.get("id")
    return acted, {}, notes
