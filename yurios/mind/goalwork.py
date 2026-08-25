"""Working a goal — the one place the mind reaches for a hand (SPEC §22, §26).

`goal_work` is the tick's most expensive act and its only tool-using one: a
`tool_step` is reachable from nowhere else, because a hand she reaches for is a
step of an open goal or it does not happen. Around it sit the pieces that make
one step readable to her — the desk file that carries the work across ticks, the
memories and context the step is given, the instruction that says what this
particular moment is for, and the two small rules for when a step is finished
and when a kept promise becomes something to say.

Split out of `loop.py` because this is the part read when she does the *work*
badly, as opposed to when she decides badly. The class constants it needs —
`GOAL_DESK`, `DONE_MARK` — stay on `MindLoop`, which is where a reader looks for
them; they are reached here through `loop`.

`loop` is unannotated for the reason `world/runtime.py` gives: naming its type
means importing `MindLoop`, and `tests/test_layering.py` reads a
`TYPE_CHECKING` import off the parse tree like any other.
"""
from __future__ import annotations

import logging

from yurios.app.core.assemble import age_tag
from yurios.kernel import correlate

from . import acts
from .goals import Goal
from .hands import START_DONT_AWAIT, klass, parse_intent, stamp_contract
from .util import iso_of

log = logging.getLogger("mind.goalwork")



def desk_path(loop, goal: Goal) -> str:
    return loop.GOAL_DESK.format(id=goal.id)


def desk_read(loop, goal: Goal, *, limit: int = 3000) -> str:
    """What she has already worked out about this goal, newest at the end."""
    if loop.workspace is None:
        return ""
    try:
        text = loop.workspace.read(desk_path(loop, goal), default="") or ""
    except Exception:  # noqa: BLE001 — a desk file is never worth a dead tick
        log.warning("goal desk read failed", exc_info=True)
        return ""
    return text[-limit:]


def desk_write(loop, goal: Goal, line: str) -> None:
    """Append one step's conclusion to the goal's desk file.

    Append rather than replace: the value of the file is the trail. The
    workspace already caps file and tree size and jails the path
    (mind/workspace.py), so an unbounded trail is a caught error rather than
    a full disk — and the per-tick single-step rule bounds the rate.
    """
    if loop.workspace is None or not line.strip():
        return
    stamp = iso_of(loop.clock.now())
    try:
        loop.workspace.append(desk_path(loop, goal),
                              f"\n## {stamp}\n\n{line.strip()}\n")
    except Exception:  # noqa: BLE001
        log.warning("goal desk write failed", exc_info=True)
        return
    # …and the same journal line a desk tool would have produced, so the
    # inner-life page shows the write whichever hand made it (§34.2).
    loop._desk_notes.append(f"wrote up where I got to: "
                            f"{desk_path(loop, goal)}")
    loop.vault.mark_dirty()


def memories(loop, goal: Goal, facts: str) -> list:
    """The episodic half of §22.4 — what she can remember about this goal.

    `facts.md` above is the *semantic* residue: what DREAM decided is still
    true in a month, with the evenings it came from burned off. Working
    alone she had only that, so she knew you had a sister and not that you
    had mentioned her on Tuesday sounding tired. This asks the same index
    the conversation asks (`store.recall`), probed from the goal rather
    than from a message, the way the greeting probes from state.

    Anything already sitting in the facts block is dropped: the same
    sentence under two headings reads as two pieces of evidence.
    """
    probe = " ".join(p for p in (goal.text,
                                 str(goal.meta.get("about") or "").strip())
                     if p)
    try:
        mems = loop.store.recall(probe, loop.cfg.retrieval_k)
    except Exception:  # noqa: BLE001 — a cold index is not a reason to
        log.debug("goal work: no recall", exc_info=True)   # skip the step
        return []
    seen = facts.lower()
    return [m for m in mems if m.text.strip().lower() not in seen]


def context(loop, goal: Goal) -> str:
    """Everything the *conversational* prompt would have given her, minus
    the conversation (SPEC §7.1, §34.3, §19.2).

    She was measurably dumber alone than she is talking to you: chat gets
    the desk digest, the skills catalog, the situation and the shelf, and
    goal work got the goal's one-line text and nothing else. That is
    backwards for a project whose whole thesis is the inner life, and it is
    why every private step read like a fortune cookie.
    """
    parts: list[str] = [f"THE GOAL\n\n{goal.text}"]
    meta = [f"kind: {goal.kind}", f"state: {goal.state}",
            f"step {goal.steps + 1} of {loop.cfg.mind_goal_max_steps}",
            f"why you have it: {goal.provenance}"]
    if goal.due:
        meta.append(f"due: {goal.due}")
    parts.append("ABOUT IT\n\n" + "\n".join(f"- {m}" for m in meta))
    # The exchange that made it, when it was made by one. A promise is
    # scanned as the *predicate* after "I'll", so "which of the two kettles
    # boils faster" survives only as "find out which one is faster for you"
    # — and a working step handed that alone invents a subject for it with
    # complete confidence. It only became visible when tasks started being
    # worked; as `reach_out` goals these never got a step at all.
    about = str(goal.meta.get("about") or "").strip()
    if about:
        parts.append(f"WHERE THIS CAME FROM\n\nThey said: “{about}”")
    desk = desk_read(loop, goal)
    if desk.strip():
        parts.append("WHAT YOU HAVE ALREADY WORKED OUT ON THIS\n\n" + desk.strip())
    try:
        parts.append("THE SITUATION RIGHT NOW\n\n" + loop.world.situation())
    except Exception:  # noqa: BLE001
        log.debug("goal work: no situation", exc_info=True)
    if loop.workspace is not None:
        digest = loop.workspace.digest(limit=12)
        if digest:
            parts.append("YOUR DESK (paths only — `read_note` opens one)"
                         "\n\n" + digest)
    if loop.skills is not None:
        catalog = loop.skills.catalog(limit=12)
        if catalog:
            parts.append("SKILLS YOU HAVE WRITTEN DOWN\n\n" + catalog)
    facts = loop.vault.read("memory/semantic/facts.md")[-1200:].strip()
    if facts:
        parts.append("WHAT YOU KNOW ABOUT THEM\n\n" + facts)
    recalled = memories(loop, goal, facts)
    if recalled:
        parts.append("THINGS THAT MAY BE RELEVANT\n\n" + "\n".join(
            f"- ({age_tag(m)}) {m.text}" for m in recalled))
    other = [g.text for g in loop.goals.open_goals() if g.id != goal.id][-8:]
    if other:
        parts.append("YOUR OTHER OPEN GOALS (do not work these now)\n\n"
                     + "\n".join(f"- {t}" for t in other))
    return "\n\n".join(parts)


def offer_to_tell(loop, goal: Goal) -> list[str]:
    """A promise she has now kept becomes something to say (§18.2, §22.1).

    Splitting a promise into work and news is what makes her do the work at
    all — but the news half has to be filed by something, or the split just
    loses it, and "I'll look into that" becomes a thing she quietly does and
    never mentions. That is a worse companion than the one who talked about
    everything and did none of it.

    Only her own promises. Work you planted is on the desk where you left
    it, maintenance is nobody's business but hers, and a `followup` never
    gets a follow-up of its own — reaching out is already the act.

    `open-minded` on purpose: news has a shelf life. If Gate 2 never finds a
    moment inside a day, letting it go is better company than opening with
    something she finished the day before yesterday.
    """
    if goal.kind != "task" or not goal.provenance.startswith("promise:"):
        return []
    loop.goals.add(
        f"tell them what came of “{goal.text}” — it's in "
        f"{loop.GOAL_DESK.format(id=goal.id)}",
        kind="reach_out", priority=0.6,
        due=iso_of(loop.clock.now() + 24 * 3600),
        commitment="open-minded", provenance=f"followup:{goal.id}")
    return [f"…and they should hear what came of it: {goal.text}"]


def finished(loop, note: str) -> bool:
    return loop.DONE_MARK in (note or "").lower()


def work_system(loop, goal: Goal, offer, last: bool) -> str:
    """The instruction half of a working step."""
    lines = [
        # The persona blocks arrive above this, fused on by `_utility`
        # (§22.4). So this opens by saying what the moment *is* rather than
        # who she is — the two used to be the same sentence, and with no
        # card behind it "you" pointed at nobody and every character wrote
        # the same note.
        "This is you, alone, between conversations — quietly advancing one "
        "of your own goals. Nobody is waiting on this and nothing you write "
        "here is sent to anyone: it goes on your own desk, for you to pick "
        "up next time. Think it through as yourself, not as an assistant "
        "reporting on a task.",
        "",
    ]
    if offer:
        lines.append(loop.hands.catalog(tuple(offer.tools)))
    else:
        lines.append(
            "Write a short working note (<=80 words) of what you concluded "
            "or want to try next. Just the note.")
    lines += [
        "",
        # "in your note" was ambiguous the moment she had hands: she read it
        # as the note file she was writing and put the words inside
        # `append_note`, where nothing reads them, and a finished goal parked
        # for twelve hours instead of closing. Name the line instead.
        f'When the goal is genuinely finished, write "{loop.DONE_MARK}" '
        + ("on your `think` line — and only then. Words inside a tool call "
           "are not read." if offer else "in your note — and only then."),
    ]
    if last:
        lines.append(
            "This is the last step you get on this goal for now, so make it "
            "the one that leaves the clearest trail for next time.")
    return "\n".join(line for line in lines if line is not None)


async def tool_step(loop, goal: Goal, intent,
                     offer) -> tuple[dict, str]:
    """One mind-initiated tool call: check → dispatch → realise → journal.

    Every precondition is checked again here, not because DECIDE's check was
    wrong but because the switch can be revoked between the two — which is
    exactly what a kill switch has to survive. A denial is audited and
    becomes a working note; it is never an exception, and it never costs the
    goal its step.
    """
    args = dict(intent.args)
    ok, why = loop.hands.check(
        intent.tool, args, state=loop.activity.state,
        pressure=loop.budget.pressure(),
        user_present=bool(loop.world.snapshot().get("user_present")))
    if not ok:
        loop.hands.deny(intent.tool, args, why)
        note = f"wanted to {intent.tool} for “{goal.text}” but didn't: {why}"
        desk_write(loop, goal, note)
        # A refused reach is still a reach, and the trace should say so:
        # "she thought about it" and "she tried to look it up and the cap
        # was spent" are different ticks, and only one of them is a reason
        # to go and change a knob.
        return ({"tool": intent.tool, "verdict": "denied", "why": why,
                 "class": klass(intent.tool), "dispatched": {}}, note)

    # Principle 7: every autonomous call names the goal that wanted it, so
    # `goals.md` stays the complete, readable list of what her hands might do.
    loop.hands.spend(intent.tool, args)
    with correlate.scope(kind=correlate.MIND_TOOL):
        result = await loop.hands.execute(
            intent.tool, args, timeout_s=loop.cfg.tool_timeout_s)
        # Host-side realisation (§7.5) — the timer actually scheduled, the
        # render actually started. The stamp is what makes the product land
        # in the Vault instead of in the chat (§18, principle 8).
        realise = getattr(loop.brain, "realise", None)
        if callable(realise):
            try:
                realise(intent.tool, result,
                        extra=stamp_contract({}, goal_id=goal.id))
            except Exception:  # noqa: BLE001 — realisation is not the call
                log.exception("mind tool realisation failed")

    dispatched: dict = {}
    if intent.tool in START_DONT_AWAIT and '"started"' in result:
        dispatched = {"tool": intent.tool, "at": iso_of(loop.clock.now())}
    short = result[:160].replace("\n", " ")
    note = f"reached for {intent.tool} on “{goal.text}” → {short}"
    # Her reason first, the result under it. A desk that records only what a
    # hand returned reads, three ticks later, as a list of things that
    # happened to her rather than steps she took — and she re-does them.
    why = (intent.text or "").strip()
    desk_write(loop, goal, f"{why}\n\n{note}" if why else note)
    return ({"tool": intent.tool, "verdict": "ok",
             "class": klass(intent.tool), "dispatched": dispatched}, note)


async def goal_work(loop, goal: Goal,
                         offer=None) -> tuple[dict, dict, list[str]]:
    """Advance one task/maintenance goal by exactly one step (SPEC §22).

    The lifecycle is the point. A goal used to be created `pending`, worked
    once, and marked `done` — which meant "she does things while you're
    gone" was one paragraph of a local model and a tick. Now:

        pending → active     on the first step
        active  → waiting    when it is blocked on the user, or on work it
                             dispatched and will not await (§7.6)
        active  → done       when the step says the work is finished
        active  → waiting/abandoned  when the step budget runs out, by
                             whichever the commitment strategy says

    One step is one utility call that may emit **one** intent: a thought, or
    — when her hands are offered — one tool call. Never both, never two;
    that is "one intention per tick" applied one level down.

    Nothing here ever speaks. The product of a step lands on her desk and in
    her journal; reaching the user is Gate 2's decision, made about a
    `reach_out` goal, on some later tick.
    """
    notes: list[str] = []
    # A maintenance goal that only *stands for* a leftover does not get a
    # paragraph written about it — it gets the leftover done. The impulses
    # are still the cheap path (§21, §20.1); this is what makes the standing
    # record of them something more than a line in a checklist.
    auto = goal.meta.get("auto")
    if auto in ("shelf", "dream"):
        return await acts.maintenance(loop, goal, auto)

    if goal.state == "pending":
        loop.goals.update(goal.id, state="active")
        goal.state = "active"
    step = goal.steps + 1
    last = step >= max(1, int(loop.cfg.mind_goal_max_steps))

    with correlate.scope(kind=correlate.GOAL_WORK):
        reply = await loop._utility([
            {"role": "system", "content": work_system(loop, goal, offer, last)},
            {"role": "user", "content": context(loop, goal)}],
            soul=True)

    intent = parse_intent(reply, allowed=tuple(offer.tools) if offer else ())
    used: dict = {}
    if intent.kind == "use":
        used, note = await tool_step(loop, goal, intent, offer)
        notes.append(note)
    else:
        note = (intent.text or "").strip() or \
            f"(sat with it; nothing new yet on: {goal.text})"
        desk_write(loop, goal, note)
        notes.append(f"worked on: {goal.text} — {note[:160]}")

    meta: dict = {"steps": step, "last_step": iso_of(loop.clock.now())}
    if used.get("dispatched"):
        # Start-don't-await: the answer comes back as `task_completion`, and
        # until it does there is nothing to think about (§7.6, §16).
        meta["dispatched"] = used["dispatched"]
        state = "waiting"
        loop.wakeups[goal.id] = (loop.clock.now()
                                 + float(loop.cfg.mind_dispatch_timeout_s))
        notes.append("…and I'm waiting on it before I go further")
    elif finished(loop, intent.text):
        state = "done"
        notes.append(f"finished: {goal.text}")
        notes += offer_to_tell(loop, goal)
    elif last:
        # The horizon (§22): three steps and it either waits for something to
        # change or the commitment strategy lets it go. Without this a goal
        # loops forever, which is the failure a lifecycle exists to prevent.
        if goal.commitment == "open-minded" and goal.is_stale(loop.clock):
            state = "abandoned"
            notes.append(f"let go of: {goal.text} (I gave it what I had)")
        else:
            state = "waiting"
            loop.wakeups[goal.id] = loop.clock.now() + 12 * 3600
            notes.append(f"parked: {goal.text} — I've taken it as far as I "
                         "can on my own for now")
    else:
        state = "active"

    loop.goals.update(goal.id, state=state, meta=meta)
    did = (f"{used['tool']} ({used['verdict']})" if used
           else "thought about it")
    return ({"what": "tool_step" if used else "goal_work",
             "result": f"step {step}: {did}", "goal": goal.id,
             "state": state,
             **({"tool": used["tool"], "verdict": used["verdict"]}
                if used else {})},
            {}, notes)
