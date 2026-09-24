"""Working a goal — the one place the mind reaches for a hand (SPEC §22, §26).

`goal_work` is the tick's most expensive act and the one that works a goal
with her hands (mind/handwork.py runs them; a night's job in her own voice is
the other door). Around it sit the pieces that make
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
from .goals import Goal, night_owned, trim
from . import handwork
from .handwork import Reach
from .hands import klass, parse_intent
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
    loop.hub.publish("workspace", {"action": "append", "path": desk_path(loop, goal)})


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
    parts = [p for p in (goal.text,
                         str(goal.meta.get("about") or "").strip()) if p]
    probe = " ".join(parts)
    try:
        # Ask for more than will be shown: the filter below throws some away,
        # and a probe that costs itself two slots should not also shorten it.
        mems = loop.store.recall(probe, loop.cfg.retrieval_k + 3)
    except Exception:  # noqa: BLE001 — a cold index is not a reason to
        log.debug("goal work: no recall", exc_info=True)   # skip the step
        return []
    seen = facts.lower()
    # The probe is built from text that is itself indexed, so recall's best
    # matches were echoes of the question: a journal line reading "I took that
    # on: <goal text>", and the very exchange `about` was copied from. Two of
    # six slots spent restating the prompt above them — and worse, holding that
    # exchange in the set let MMR suppress the *reply* to it as a near-
    # duplicate (0.89 cosine), which is how the answer she was waiting for lost
    # to a line fifteen days old. Anything quoting the probe is dropped.
    echoes = [p.lower() for p in parts if len(p) >= 25]
    out = []
    for m in mems:
        text = m.text.strip().lower()
        if text in seen or any(e in text for e in echoes):
            continue
        out.append(m)
    return out[:loop.cfg.retrieval_k]


def said_since(loop, goal: Goal, limit: int = 6) -> str:
    """What has actually been said in the room since this goal was taken on.

    `context()` is deliberately the conversational prompt minus the
    conversation, and for most goals that is right. But `about` freezes at the
    moment the goal is filed, so a goal whose own text names a *later* turn —
    "…once they answer the framing question" — could never see the answer. She
    waited an hour for a reply that had already arrived, wrote "still waiting
    on his answer" onto the desk, and that came back next tick under WHAT YOU
    HAVE ALREADY WORKED OUT as evidence for itself.

    Bounded and newest-last: a handful of lines, trimmed, so this stays a
    glance at the room and does not quietly become the raw window.
    """
    sessions = getattr(getattr(loop.brain, "state", None), "sessions", None)
    chatlog = getattr(sessions, "log", None)
    if chatlog is None:
        return ""
    try:
        rows = chatlog.said(40)             # speech only: a tool notice is not a line
    except Exception:  # noqa: BLE001 — no transcript is not a failed step
        log.debug("goal work: no transcript", exc_info=True)
        return ""
    them = getattr(loop.store, "user_name", None) or "they"
    her = getattr(loop.store, "char_name", None) or "you"
    since = str(goal.created or "")
    out = []
    for r in rows:
        ts = str(r.get("ts") or "")
        if since and ts <= since:
            continue
        text = " ".join(str(r.get("text") or "").split())
        if not text:
            continue
        who = them if r.get("role") == "user" else her
        out.append(f"{ts[11:16]} {who}: {trim(text, 240)}")
    return "\n".join(out[-limit:])


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
    # …and whatever has been said since. `about` is frozen at filing time, so
    # without this a goal that waits on an answer can never be told it came.
    since = said_since(loop, goal)
    if since:
        parts.append(
            "WHAT HAS BEEN SAID SINCE YOU TOOK THIS ON (newest last)\n\n"
            + since
            + "\n\nIf this answers what the goal was waiting for, it is "
              "answered \u2014 act on it rather than waiting to be told again.")
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
    if night_owned(goal.text):
        parts.append(
            "THIS IS THE NIGHT'S JOB\n\n"
            "Consolidation writes durable facts (the block below), not a "
            "desk folder called kept-memory. A standing research job writes "
            "the morning brief to reports/ on the desk. `list_notes` only "
            "sees workspace/. If last night already produced the product, "
            "this goal is finished — park it and leave the rest for DREAM.")
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


def offer_the_picture(loop, goal: Goal) -> list[str]:
    """A finished goal holding a photo hands it to a goal that can send it.

    The landing rule (SPEC §18.2a) is right that the lab must not post its own
    product — but a rendered picture then lived only in the gallery, and the
    goal that made it had no way to pass it on. The news half said "it's in
    goals/g-….md", so "show me" was answered with a file path, three separate
    times, while the photo sat on the shelf. Now the follow-up carries the
    picture itself and Gate 2 still decides whether it goes.

    Filed on every way out of a goal, and parking is one of them: finished,
    let go at the horizon, swept by `reconsider`, or out of steps and waiting
    on the world. A promise she gave up on is still a promise whose photo
    exists and whom nobody has shown it to — and so is one she is still
    holding, which is the case that actually happened. `single-minded`, unlike
    ordinary news, because news has a shelf life and is better let go than
    opened with stale — a photo she promised *is* the promise, and letting this
    one quietly expire unsent is the failure, not good manners.
    """
    shot = str(goal.product.get("image_url") or "")
    if not shot:
        return []
    if goal.product.get("deliver") == "chat":
        # The producer already put this one in the conversation. Its product
        # still belongs on the goal as a record of what landed, but filing a
        # reach-out would send the same picture a second time.
        return []
    if goal.meta.get("offered"):
        # Handed on already, and once is the whole point. Every way out of a
        # goal files this now, and one goal can take more than one of them in
        # its life — parked at the horizon, woken, worked, finished. But
        # `already_carrying` only sees goals that are still *open*, so once the
        # follow-up has been delivered and closed there is nothing left to
        # dedupe against, and the next exit would file a second errand to send
        # a photograph they have already been sent.
        return []
    # Her own words about the shot rather than the goal's text: in the
    # checklist it says *which* picture, which "tell them what came of …"
    # never did. (Quoting the parent is safe now — `already_carrying` files a
    # follow-up on its provenance, not its words — but this still reads
    # better, and it was written when quoting silently deleted the goal.)
    detail = str(goal.product.get("detail") or "").strip()
    sid = str(goal.product.get("selfie_id") or "").strip()
    about = f" — {detail}" if detail else (f" ({sid})" if sid else "")
    heir = loop.goals.add(
        trim("send them the picture I took for them" + about),
        kind="reach_out", priority=0.7,
        due=iso_of(loop.clock.now() + 24 * 3600),
        commitment="single-minded", provenance=f"followup:{goal.id}",
        meta={"product": dict(goal.product)})
    # Which goal is carrying it, on the goal that made it — so the checklist
    # says where the photograph went, and so the guard above has something to
    # read that outlives the follow-up.
    loop.goals.update(goal.id, meta={"offered": heir.id})
    goal.meta["offered"] = heir.id      # …and on the copy the caller is holding
    return [f"…and it's for them, not the shelf: {shot}"]


def rescue_pictures(loop, dropped) -> list[str]:
    """Goals swept by `reconsider()` hand their photos on first (SPEC §18.2a).

    `reconsider` drops a stale open-minded goal wholesale — in `pending` or
    `waiting`, before it is ever given another working step — so a goal holding
    a rendered picture went out the same door as one holding nothing, and the
    picture went with it. Letting go of the intention is right; letting go of
    the object she has already made *for them* is the failure §18.2a exists to
    end. The follow-up outlives the goal.
    """
    notes: list[str] = []
    for goal in dropped:
        notes += offer_the_picture(loop, goal)
    return notes


def offer_to_tell(loop, goal: Goal) -> list[str]:
    """A promise she has now kept becomes something to say (§18.2, §22.1).

    Splitting a promise into work and news is what makes her do the work at
    all — but the news half has to be filed by something, or the split just
    loses it, and "I'll look into that" becomes a thing she quietly does and
    never mentions. That is a worse companion than the one who talked about
    everything and did none of it.

    Only her own promises get a sentence about the work. Work you planted
    is on the desk where you left it, maintenance is nobody's business but
    hers, and a `followup` never gets a follow-up of its own — reaching out
    is already the act.

    An undelivered picture is handed on whoever asked. The photograph is the
    news: a `user:chat` goal holds the same shot a promise does, and finishing
    while `Goal.product` still has it files the same `reach_out` the park and
    abandon exits already file. `offer_the_picture` is idempotent on
    `meta.offered`, so a second exit of the same goal files nothing, a picture
    already delivered to chat is not sent twice, and a picture never becomes
    a path to the goal file.

    `open-minded` on purpose: news has a shelf life. If Gate 2 never finds a
    moment inside a day, letting it go is better company than opening with
    something she finished the day before yesterday.
    """
    if goal.product.get("image_url"):
        return offer_the_picture(loop, goal)
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
        if offer.waiting():
            lines += ["", offer.waiting()]
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


def journal_reach(loop, goal: Goal, reach: Reach) -> str:
    """One call a step made, onto the goal's desk. Returns the journal note.

    Her reason first, the result under it. A desk that records only what a
    hand returned reads, three ticks later, as a list of things that happened
    to her rather than steps she took — and she re-does them.
    """
    if reach.verdict == "denied" and reach.refused:
        # A refused reach is still a reach, and the desk should say so: "she
        # thought about it" and "she tried to look it up and the cap was spent"
        # are different steps, and only one of them is a reason to change a knob.
        note = f"wanted to {reach.tool} for “{goal.text}” but didn't: {reach.refused}"
        desk_write(loop, goal, note)
        return note
    # list_notes is a catalog: the listing IS the step. Clipping it to 160
    # characters of pretty JSON is how she spent days retrying the same
    # folder — the next tick only saw one file. The tool already bounds the
    # payload (SPEC §34.2).
    keep = len(reach.result) if reach.tool == "list_notes" else 160
    short = reach.result[:keep].replace("\n", " ")
    note = f"reached for {reach.tool} on “{goal.text}” → {short}"
    desk_write(loop, goal, f"{reach.why}\n\n{note}" if reach.why else note)
    return note


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

    One step is one intention — this goal, this tick — worked through as far
    as her hands take it: while hands are offered she may chain calls, each
    result coming back before the next (mind/handwork.py), up to
    `TOOL_MAX_CALLS_PER_TURN`, and the step ends on her thought. Work that
    finishes off-tick ends it early, and the goal waits for it.

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

    async def ask(messages: list[dict]) -> str:
        return await loop._utility(messages, soul=True)

    messages = [{"role": "system", "content": work_system(loop, goal, offer, last)},
                {"role": "user", "content": context(loop, goal)}]
    with correlate.scope(kind=correlate.GOAL_WORK):
        if offer:
            # A step may chain hands now (mind/handwork.py) — read the note,
            # then fix the passage — each one journalled as it lands.
            worked = await handwork.work(
                loop, messages, offer=offer, ask=ask, goal_id=goal.id,
                stop_on_dispatch=True,
                on_reach=lambda reach: notes.append(journal_reach(loop, goal, reach)))
        else:
            worked = handwork.Worked(parse_intent(await ask(messages), allowed=()))

    intent = worked.answer
    note = (intent.text or "").strip()
    if note or not worked.reaches:
        note = note or f"(sat with it; nothing new yet on: {goal.text})"
        desk_write(loop, goal, note)
        notes.append(f"worked on: {goal.text} — {note[:160]}")

    meta: dict = {"steps": step, "last_step": iso_of(loop.clock.now())}
    started = worked.dispatched
    if started is not None:
        # Start-don't-await: the answer comes back as `task_completion`, and
        # until it does there is nothing to think about (§7.6, §16).
        meta["dispatched"] = {"tool": started.tool, "at": iso_of(loop.clock.now())}
        state = "waiting"
        loop.wakeups[goal.id] = (loop.clock.now()
                                 + float(loop.cfg.mind_dispatch_timeout_s))
        notes.append("…and I'm waiting on it before I go further")
    elif finished(loop, intent.text) or any(finished(loop, r.why)
                                            for r in worked.reaches):
        # …or beside a call: found live, she wrote "goal complete" above the
        # `append_note` that finished it and said nothing more after.
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
            # Letting the goal go must not let the photo go with it.
            notes += offer_the_picture(loop, goal)
        else:
            state = "waiting"
            loop.wakeups[goal.id] = loop.clock.now() + 12 * 3600
            notes.append(f"parked: {goal.text} — I've taken it as far as I "
                         "can on my own for now")
            # …and neither must parking. This is the exit her own promise
            # actually took (`g-6233dc189e71`: single-minded, three steps,
            # `waiting`) — out of steps, waiting on an answer, holding a
            # finished photograph that only a `reach_out` can send. The other
            # two exits were covered and this one is the common one: a
            # single-minded goal never reaches the abandon branch above, and
            # `reconsider` only sweeps open-minded ones, so the picture sat
            # parked for twelve hours at a time and then parked again.
            notes += offer_the_picture(loop, goal)
    else:
        state = "active"

    loop.goals.update(goal.id, state=state, meta=meta)
    reaches = worked.reaches
    did = (", ".join(f"{r.tool} ({r.verdict})" for r in reaches) if reaches
           else "thought about it")
    last_reach = reaches[-1] if reaches else None
    # The trace says what `calls.jsonl` says (§26.2): a step whose call failed
    # is still a step, but it is not an `ok` one. `tool`/`verdict` name the
    # last call, as they did when a step could only make one; `tools` is all.
    return ({"what": "tool_step" if reaches else "goal_work",
             "result": f"step {step}: {did}", "goal": goal.id,
             "state": state,
             **({"tool": last_reach.tool, "verdict": last_reach.verdict,
                 "class": klass(last_reach.tool),
                 "tools": [{"tool": r.tool, "verdict": r.verdict,
                            "class": klass(r.tool)} for r in reaches]}
                if last_reach else {})},
            {}, notes)
