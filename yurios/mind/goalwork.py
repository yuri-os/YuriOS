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

import json
import logging

from yurios.app.core.assemble import age_tag
from yurios.kernel import correlate

from . import acts
from .goals import Goal, night_owned, trim
from . import handwork
from .handwork import Reach
from .hands import FILE_GOAL, NEEDS_CAMERA, READ_ONLY, TELL, Offer, klass
from .policy import next_open
from .prompts import goal_history
from .util import closing, iso_of, ts_of_iso
from .workspace import DESK_DONE

log = logging.getLogger("mind.goalwork")


def record_decision(loop, goal: Goal, acted: dict, interrupt: dict) -> None:
    """Keep six completed attempts across restarts and long stretches of REST."""
    history = goal.meta.get("decisions", [])
    history = [line for line in history if isinstance(line, str)] \
        if isinstance(history, list) else []
    result = str(acted.get("result", ""))
    if interrupt:
        result += (f"; Gate 2 {interrupt['outcome']} "
                   f"({interrupt.get('reason', '')}, "
                   f"{interrupt['score']}/{interrupt['threshold']})")
    line = f"{iso_of(loop.clock.now())}: {result}"[:400]
    loop.goals.update(goal.id, meta={"decisions": [*history[-5:], line]})



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


def desk_write(loop, goal: Goal, line: str, *, step: int | None = None) -> None:
    """Append one step's conclusion to the goal's desk file.

    Append rather than replace: the value of the file is the trail. The
    workspace already caps file and tree size and jails the path
    (mind/workspace.py), so an unbounded trail is a caught error rather than
    a full disk — and the per-tick single-step rule bounds the rate.
    """
    if loop.workspace is None or not line.strip():
        return
    stamp = iso_of(loop.clock.now()) + (f" — step {step}" if step else "")
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


def handed_document(loop, goal: Goal, *, limit: int = 6000) -> str:
    """For a goal filed from a handed document (§34.6): the document.

    The goal names the path, but a step with no hands could not open it — and
    one with hands would spend its first call doing so every time. Shown here,
    bounded, and labelled as the source it is."""
    path = str(goal.meta.get("handed") or "")
    if not path or loop.workspace is None:
        return ""
    try:
        text = loop.workspace.read(path, default="") or ""
    except Exception:  # noqa: BLE001
        log.debug("goal work: handed document unreadable", exc_info=True)
        return ""
    if not text.strip():
        return ""
    head = text[:limit].rstrip()
    more = (f"\n\n(…{len(text) - len(head)} more characters — `read_note` "
            "reads on.)" if len(text) > len(head) else "")
    return f"THE DOCUMENT THEY HANDED YOU — {path}\n\n{head}{more}"


async def memories(loop, goal: Goal, facts: str) -> list:
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
        mems = await loop.store.arecall(probe, loop.cfg.retrieval_k + 3)
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


#: What the goal was filed with, in the order a step wants it. The night's
#: stock-take writes all four (§22.1b), a reviewed promise the first and last;
#: each is a label here so the step reads a plan and not a meta dump.
PLAN_FIELDS = (
    ("rationale", "why it matters"),
    ("evidence", "what it rests on"),
    ("first_action", "where you planned to start"),
    ("success", "how you will know it is done"),
)


def plan_of(goal: Goal) -> str:
    """The plan she made when she took this on, handed back to the step.

    Strategy asked her for a first bounded action, the evidence behind it and
    an observable finish, and filed all of it on the goal — then every working
    step was given the goal's title and nothing else, so she reconsidered the
    objective from scratch each time instead of doing the thing she had already
    decided to do first. A field that only repeats the goal's text is dropped.
    """
    title = " ".join(goal.text.split()).lower()
    lines = []
    for key, label in PLAN_FIELDS:
        value = " ".join(str(goal.meta.get(key) or "").split())
        if value and value.lower() != title:
            lines.append(f"- {label}: {value}")
    return "\n".join(lines)


async def context(loop, goal: Goal) -> str:
    """Everything the *conversational* prompt would have given her, minus
    the conversation (SPEC §7.1, §34.3, §19.2).

    She was measurably dumber alone than she is talking to you: chat gets
    the desk digest, the skills catalog, the situation and the shelf, and
    goal work got the goal's one-line text and nothing else. That is
    backwards for a project whose whole thesis is the inner life, and it is
    why every private step read like a fortune cookie.
    """
    parts: list[str] = [f"THE GOAL\n\n{goal.text}"]
    history = goal_history(goal)
    if history:
        parts.append(history)
    meta = [f"kind: {goal.kind}", f"state: {goal.state}",
            f"step {goal.steps + 1} of {loop.cfg.mind_goal_max_steps}",
            f"why you have it: {goal.provenance}"]
    if goal.due:
        meta.append(f"due: {goal.due}")
    # Its own desk file, by name (§22.3). Unnamed, the newest goals/… path in
    # the desk digest looked like it: live, 4 Oct, a skill goal's first step
    # read the finished diary goal's file as its own and appended to it twice.
    meta.append(f"its desk file: {desk_path(loop, goal)} — what each step "
                "works out is written there for you when the step ends, so you "
                "never need to log your progress yourself; every other goals/… "
                "file on your desk belongs to a different goal")
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
    plan = plan_of(goal)
    if plan:
        parts.append("YOUR PLAN FOR THIS\n\n" + plan)
    handed = handed_document(loop, goal)
    if handed:
        parts.append(handed)
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
        parts.append(f"WHAT YOU HAVE ALREADY WORKED OUT ON THIS ({desk_path(loop, goal)})"
                     "\n\n" + desk.strip())
    try:
        parts.append("THE SITUATION RIGHT NOW\n\n" + loop.world.situation())
    except Exception:  # noqa: BLE001
        log.debug("goal work: no situation", exc_info=True)
    if loop.workspace is not None:
        digest = loop.workspace.digest(limit=12, labels=loop.goals.desk_labels(
            loop.GOAL_DESK, this=goal.id))
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
    recalled = await memories(loop, goal, facts)
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


def settle_strays(loop) -> list[str]:
    """Either end of a decided message that has lost the other (SPEC §18.2b).

    `acts.settle_telling` is how the two normally part: on delivery, or when
    the message is let go of by hand. Anything else that closes one end — the
    goal let go of or retired by `reconsider` while its message waits, a line
    edited out of `goals.md` — used to strand the other: a message about a goal
    she no longer has, still going out at 09:00, or a goal in `waiting`, with
    no wakeup, on a message that no longer exists. Run every tick in SENSE,
    before ACT can send anything.
    """
    notes: list[str] = []
    user = loop.cfg.user_name
    goals = loop.goals.all()
    by_id = {g.id: g for g in goals}
    for g in goals:
        if not acts.is_open(g):
            continue
        if g.provenance.startswith("told:"):
            if acts.telling_for(loop, g) is not None:
                continue
            # Nothing left for the words to finish, so they don't go — but a
            # photo riding with them was made *for them*, and is handed on to
            # an ordinary errand rather than dropped with the words (§18.2a).
            notes += offer_the_picture(loop, g)
            loop.goals.set_state(g.id, "abandoned")
            loop.wakeups.pop(g.id, None)
            said = str(g.meta.get("say") or g.text)
            notes.append(f"didn't send what I was going to tell {user} — the "
                         f"goal it was for is closed: “{trim(said, 300)}”")
        elif g.state == "waiting":
            told = str((g.meta.get("telling") or {}).get("goal") or "")
            if told and not acts.is_open(by_id.get(told)):
                notes += acts.resume_after_telling(loop, g, delivered=False)
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


def takeaway(note: str) -> str:
    """The line of a step worth journalling: where she ended up, not how she began.

    The journal is what recall, the diary and the night read back, and it used
    to get the first 160 characters of the step — which, for a step that opened
    in scene ("*The rain has been steady against the glass…*"), was the one
    part that said nothing. Her last `think` line is her own conclusion when
    she wrote one; otherwise the note's closing sentences.
    """
    text = (note or "").strip()
    thought = ""
    for line in text.splitlines():
        if line.strip().lower().startswith("think "):
            thought = line.strip()[6:].strip()
    return closing(thought or text, 240)


def step_offer(offer) -> Offer:
    """What a goal step may reach for: her hands, if any, and always `tell_them`.

    Always, because a goal whose point is that the user hears something must
    have a way to get there that is not a hand (§18.2b). Told only that
    nothing here reached anyone, the step that should have said something wrote
    a scene of saying it on her desk, then believed its own desk.
    """
    if not offer:
        return Offer(tools=(TELL,))
    return Offer(tools=(*offer.tools, TELL), held=offer.held,
                 held_why=offer.held_why)


def work_system(loop, goal: Goal, offer, last: bool) -> str:
    """The instruction half of a working step."""
    user = loop.cfg.user_name
    lines = [
        # The persona blocks arrive above this, fused on by `_utility`
        # (§22.4). So this opens by saying what the moment *is* rather than
        # who she is — the two used to be the same sentence, and with no
        # card behind it "you" pointed at nobody and every character wrote
        # the same note.
        "This is you, alone, between conversations — quietly advancing one "
        "of your own goals. Nobody is waiting on this. What you write here "
        "goes on your own desk, for you to pick up next time. Think it "
        "through as yourself, not as an assistant reporting on a task.",
        "",
        # The way out that a goal about *them* needs (§18.2b). Said plainly and
        # early, because the failure it ends was not refusal: it was a step
        # that wanted to speak, was told it couldn't, and performed it instead.
        f"When the point is for {user} to hear something from you, `{TELL}` "
        "is how you say it: your words reach them as a message from you, "
        "sent at the first moment that isn't quiet hours. Writing on your "
        "desk that you told them is not telling them — only this is.",
        "",
    ]
    offer = step_offer(offer)
    lines.append(loop.hands.catalog(tuple(offer.tools)))
    if offer.waiting():
        lines += ["", offer.waiting()]
    lines += [
        "",
        # "in your note" was ambiguous the moment she had hands: she read it
        # as the note file she was writing and put the words inside
        # `append_note`, where nothing reads them, and a finished goal parked
        # for twelve hours instead of closing. Name the line instead.
        f'When the goal is genuinely finished, write "{loop.DONE_MARK}" '
        "on your `think` line — and only then. Words inside a tool call "
        f"are not read. If what finishes it is {user} hearing something, "
        f"write it on the `think` line above `{TELL}`: the goal closes "
        "itself once the message has reached them.",
    ]
    if last:
        lines.append(
            "This is the last step you get on this goal for now, so make it "
            "the one that leaves the clearest trail for next time.")
    return "\n".join(line for line in lines if line is not None)


#: What a reach-out's preparing step writes on its `think` line once what the
#: message should carry is in hand (§18.2c). A phrase she has to choose, for
#: the reason `DONE_MARK` is one.
READY_MARK = "ready to send"

#: …and what it writes when the moment has not come: a reach-out with a real
#: date, about something that has not happened yet (§18.2c). Her call, not the
#: score's — the score cannot read "ask how the interview went".
NOT_YET_MARK = "not yet"

#: How far ahead of its date a reach-out she set aside comes back to her: the
#: same window in which Gate 2 counts a date as time-sensitive.
NOT_YET_LEAD_H = 6.0


def needs_preparing(loop, goal: Goal) -> bool:
    """Whether a reach-out still gets a step with her hands before Gate 2 (§18.2c).

    A `reach_out` used to go straight to the gate, and the gate can only
    compose a sentence — so "send them a selfie" could be ruled on all day and
    never take one. Not for a message that already carries what it is about:
    her own decided words (`told:`), a follow-up reporting on finished work, a
    picture already in hand. And not past the step horizon, where it goes to
    the gate with whatever it has rather than preparing forever.
    """
    if goal.kind != "reach_out" or goal.meta.get("prepared"):
        return False
    if goal.meta.get("decided") or goal.product.get("image_url"):
        return False
    if goal.provenance.startswith(("followup:", "told:")):
        return False
    return goal.steps < max(1, int(loop.cfg.mind_goal_max_steps))


def prepare_system(loop, goal: Goal, offer: Offer, last: bool) -> str:
    """The instruction half of a reach-out's preparing step (§18.2c)."""
    user = loop.cfg.user_name
    lines = [
        "This is you, alone, between conversations. You have decided to reach "
        f"out to {user} first about the goal below, and this is the moment "
        "before: get ready what the message should carry.",
        "",
    ]
    if offer:
        lines += [
            "If it should carry a picture, take it now with your hands — it "
            "goes with your message, and describing a picture you never took is "
            "not sending one. If it needs something looked up or read first, do "
            "that. Do not write the message itself here: it is written when it "
            "is sent.",
            "",
            loop.hands.catalog(tuple(offer.tools)),
        ]
        if offer.waiting():
            lines += ["", offer.waiting()]
    else:
        lines.append("Your hands are off right now, so this is only a moment to "
                     "think it over. Do not write the message itself here: it is "
                     "written when it is sent.")
    if goal.dated and ts_of_iso(str(goal.due)) > loop.clock.now():
        lines += [
            "",
            f"It is dated {goal.due}, and it is now "
            f"{iso_of(loop.clock.now())}. Early is fine when early is right. "
            "But if it is about something that has not happened yet — asking "
            f'how a thing went before it has — write "{NOT_YET_MARK}" on your '
            "`think` line, and it comes back to you nearer the time.",
        ]
    lines += [
        "",
        f'When it has what it needs — or needs nothing — write "{READY_MARK}" '
        "on your `think` line. If what it needs is held back, say what is "
        "missing and leave it: you will come back to it.",
    ]
    if last:
        lines.append(
            "This is the last step you get on this before it goes as it is, "
            "so make it count.")
    return "\n".join(lines)


async def prepare(loop, goal: Goal, offer) -> tuple[dict, dict, list[str]]:
    """One step with her hands before a reach-out goes to Gate 2 (§18.2c).

    The same machinery a task's step uses — the context, the chained hands,
    the desk, start-don't-await — less two hands: `tell_them`, because this
    goal *is* the message and Gate 2 still rules on it, and `create_goal`,
    because preparing a message is not taking on new work. A render she
    dispatches comes back onto this goal as its product (`land_dispatched`),
    and Gate 2 then delivers it with the picture. Ready, or out of steps, it
    goes to the gate on this same tick: preparing is this intention's first
    half, not a separate one.

    A dated reach-out gets this step with her hands off too, as a moment to
    think: a score that clears the threshold a day early cannot tell "remind
    him before Thursday" from "ask how Tuesday's interview went", and she can.
    """
    offer = offer or Offer()
    hands = Offer(tools=tuple(t for t in offer.tools if t != FILE_GOAL),
                  held=offer.held, held_why=offer.held_why)
    if not hands and not goal.dated:
        # Nothing to fetch with and no moment to judge: the gate, as before.
        return await acts.reach_out(loop, goal)
    notes: list[str] = []
    if goal.state == "pending":
        loop.goals.update(goal.id, state="active")
        goal.state = "active"
    step = goal.steps + 1
    last = step >= max(1, int(loop.cfg.mind_goal_max_steps))

    async def ask(messages: list[dict]) -> str:
        return await loop._utility(messages, soul=True)

    messages = [{"role": "system", "content": prepare_system(loop, goal, hands, last)},
                {"role": "user", "content": await context(loop, goal)}]
    with correlate.scope(kind=correlate.GOAL_WORK):
        worked = await handwork.work(
            loop, messages, offer=hands, ask=ask, goal_id=goal.id,
            stop_on_dispatch=True,
            on_reach=lambda reach: notes.append(journal_reach(loop, goal, reach)))

    intent = worked.answer
    note = (intent.text or "").strip()
    if note or not worked.reaches:
        note = note or f"(nothing to get ready yet for: {goal.text})"
        notes.append(f"got ready to reach {loop.cfg.user_name}: {goal.text} "
                     f"— {takeaway(note)}")
    desk_step(loop, goal, note, worked.reaches, step=step)
    reaches = worked.reaches
    did = (", ".join(f"{r.tool} ({r.verdict})" for r in reaches) if reaches
           else "thought about it")
    meta: dict = {"steps": step, "last_step": iso_of(loop.clock.now())}
    started = worked.dispatched
    if started is not None:
        # Start-don't-await (§7.6): the render comes back as `task_completion`
        # onto this goal, and with a picture in hand it needs no more steps.
        meta["dispatched"] = {"tool": started.tool, "at": iso_of(loop.clock.now())}
        loop.goals.update(goal.id, state="waiting", meta=meta)
        loop.wakeups[goal.id] = (loop.clock.now()
                                 + float(loop.cfg.mind_dispatch_timeout_s))
        notes.append("…and I'm waiting on it before I send anything")
        return ({"what": "tool_step", "result": f"preparing, step {step}: {did}",
                 "goal": goal.id, "state": "waiting",
                 "tool": started.tool, "verdict": started.verdict,
                 "class": klass(started.tool)}, {}, notes)
    said = " ".join([note, *(r.why for r in reaches)]).lower()
    ready = not intent.unrun and READY_MARK in said
    now = loop.clock.now()
    if (not ready and NOT_YET_MARK in said and goal.dated
            and ts_of_iso(str(goal.due)) > now):
        # Her judgement that the moment has not come. It does not spend one of
        # the goal's steps — waiting for the right day is not failing to get
        # ready — and it parks the goal until nearer its date rather than
        # asking her again every hour in between.
        meta.pop("steps")
        back = ts_of_iso(str(goal.due)) - NOT_YET_LEAD_H * 3600
        state = "waiting" if back > now else "active"
        loop.goals.update(goal.id, state=state, meta=meta)
        if state == "waiting":
            loop.wakeups[goal.id] = back
        notes.append(f"not yet: {goal.text} — I'll come back to it nearer the time")
        return ({"what": "goal_work", "result": f"not yet ({did})", "goal": goal.id,
                 "state": state}, {}, notes)
    if not ready and hands.held:
        # Not ready while a hand she has is held back — the camera while you
        # are in the room, a web hand behind the budget line. Spending her
        # steps here would send the message at the horizon without the very
        # thing it was about, which is the failure this step exists to end. A
        # budget hold lifts with the day, so it waits for the morning (both of
        # Gate 2's hard gates open then too) instead of asking every hour.
        meta.pop("steps")
        budget = (loop.budget.pressure()
                  >= float(getattr(loop.cfg, "mind_tool_pressure_ceiling", 0.5))
                  and any(t not in NEEDS_CAMERA for t in hands.held))
        state = "waiting" if budget else "active"
        loop.goals.update(goal.id, state=state, meta=meta)
        if budget:
            loop.wakeups[goal.id] = next_open(now)
        notes.append(f"can't get {goal.text} ready yet — "
                     f"{', '.join(hands.held)} {hands.held_why or 'held back'}")
        return ({"what": "tool_step" if reaches else "goal_work",
                 "result": f"preparing, held: {did}", "goal": goal.id,
                 "state": state}, {}, notes)
    if not (ready or last):
        loop.goals.update(goal.id, state="active", meta=meta)
        return ({"what": "tool_step" if reaches else "goal_work",
                 "result": f"preparing, step {step}: {did}", "goal": goal.id,
                 "state": "active"}, {}, notes)
    meta["prepared"] = iso_of(loop.clock.now())
    loop.goals.update(goal.id, state="active", meta=meta)
    acted, interrupt, sent = await acts.reach_out(loop, loop.goals.get(goal.id) or goal)
    acted = {**acted, "result": f"prepared ({did}); {acted.get('result', '')}"}
    return acted, interrupt, notes + sent


def journal_reach(loop, goal: Goal, reach: Reach) -> str:
    """One call a step made, as the journal line the inner-life page shows.

    The desk gets the whole step at its end instead (`desk_step`): one entry a
    call was seven entries in twenty seconds, and a desk read back from its
    last 3,000 characters then showed the next step only the previous step's
    calls — live, 4 Oct, she re-read and re-checked a skill she had finished an
    hour before, because the entry saying so had scrolled out.
    """
    if reach.told and reach.verdict == "ok":
        said = trim(str(reach.args.get("text") or ""), 300)
        return (f"decided to tell {loop.cfg.user_name}: “{said}” — queued, "
                "not sent yet; it goes when the gate allows")
    if reach.tool == FILE_GOAL and reach.verdict == "ok":
        return _filed(reach)
    if reach.verdict == "denied" and reach.refused:
        # A refused reach is still a reach, and the desk should say so: "she
        # thought about it" and "she tried to look it up and the cap was spent"
        # are different steps, and only one of them is a reason to change a knob.
        return f"wanted to {reach.tool} for “{goal.text}” but didn't: {reach.refused}"
    short = reach.result[:160].replace("\n", " ")
    return f"reached for {reach.tool} on “{goal.text}” → {short}"


def _filed(reach: Reach) -> str:
    """A `create_goal`, said in words: "reached for create_goal → {…}" is a
    tool log, and the step that reads it back should know whether there is now
    a goal or not."""
    try:
        filed = json.loads(reach.result)
    except ValueError:
        filed = {}
    what = trim(str(filed.get("text") or ""), 200)
    if filed.get("status") == "created":
        return (f"filed a goal of its own: “{what}” ({filed.get('id')}) — "
                "it gets worked on its own turn")
    return f"wanted to file a goal, but I'm already carrying it: “{what}”"


#: How a call that worked is listed on the desk: what she did, to what.
DESK_VERBS = {"read_note": "read", "read_skill": "read skill",
              "count_note_lines": "counted the lines of", "write_note": "wrote",
              "append_note": "appended to", "edit_note": "edited",
              "delete_note": "deleted", "write_skill": "wrote skill",
              "delete_skill": "deleted skill", "web_search": "searched for",
              "read_page": "read the page", "take_selfie": "started a selfie",
              "show_picture": "showed a picture", "research": "started research on"}


def desk_line(loop, reach: Reach) -> str:
    """One call, as a line of the step's desk entry (SPEC §22.3).

    What she did and to what — a path, not the first 160 characters of the JSON
    that came back, which was most of every old entry and none of its meaning.
    A change keeps the last sentence of why she made it; a read does not, the
    step's conclusion above the list being the reason it was worth reading.
    """
    args = reach.args or {}
    target = str(args.get("path") or args.get("name") or args.get("query")
                 or args.get("folder") or args.get("url") or "")
    if reach.told and reach.verdict == "ok":
        said = trim(str(args.get("text") or ""), 300)
        return (f"told {loop.cfg.user_name}: “{said}” — queued, not sent yet; "
                "it goes when the gate allows")
    if reach.tool == FILE_GOAL and reach.verdict == "ok":
        return _filed(reach)
    called = f"{reach.tool} {target}".strip()
    if reach.verdict == "denied" and reach.refused:
        return f"{called}: refused — {trim(reach.refused, 200)}"
    if reach.verdict != "ok":
        return f"{called}: {trim(reach.result, 200)}"
    if reach.tool == "list_notes":
        # The catalog is the step (SPEC §22.3): clipped to a sentence, she spent
        # days retrying the same folder because the next tick saw one file.
        return f"listed {target or 'my notes'}: {reach.result}"
    verb = DESK_VERBS.get(reach.tool, reach.tool)
    line = f"{verb} {target}".strip()
    why = closing(reach.why, 120) if reach.why else ""
    if why and reach.tool not in READ_ONLY:
        line += f" ({why})"
    return line


def desk_step(loop, goal: Goal, words: str, reaches: list[Reach], *,
              step: int) -> None:
    """The whole step as one desk entry: her words, then the calls she made."""
    body = words.strip()
    if reaches:
        listed = "\n".join(f"- {desk_line(loop, r)}" for r in reaches)
        body = f"{body}\n\n{DESK_DONE}\n{listed}" if body else f"{DESK_DONE}\n{listed}"
    desk_write(loop, goal, body, step=step)


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
                {"role": "user", "content": await context(loop, goal)}]
    with correlate.scope(kind=correlate.GOAL_WORK):
        # A step may chain hands now (mind/handwork.py) — read the note,
        # then fix the passage — each one journalled as it lands. With no
        # hands it is still offered `tell_them`, so it always goes this way.
        worked = await handwork.work(
            loop, messages, offer=step_offer(offer), ask=ask, goal_id=goal.id,
            stop_on_dispatch=True,
            on_reach=lambda reach: notes.append(journal_reach(loop, goal, reach)))

    intent = worked.answer
    note = (intent.text or "").strip()
    if note or not worked.reaches:
        note = note or f"(sat with it; nothing new yet on: {goal.text})"
        notes.append(f"worked on: {goal.text} — {takeaway(note)}")

    meta: dict = {"steps": step, "last_step": iso_of(loop.clock.now())}
    # A finish she wrote beside calls that never ran is a finish she narrated
    # (§22.3). Live, 29 Sep: six calls glued into one paragraph, none
    # dispatched, and "goal complete — skill written, verified" closed a goal
    # on a skill that did not exist. It stays open, and the desk says why, so
    # the next step does the work instead of believing it.
    narrated = bool(intent.unrun) and finished(loop, intent.text)
    if narrated:
        hands = ", ".join(dict.fromkeys(intent.unrun))
        note = (f"{note}\n\n" if note else "") + (
            f"(not finished: the {hands} I wrote out never ran — nothing it "
            "would have done is done)")
        notes.append(f"not done yet: {goal.text} — the {hands} I wrote out "
                     "never ran")
    desk_step(loop, goal, note, worked.reaches, step=step)
    started = worked.dispatched
    told = next((r for r in worked.reaches if r.told and r.verdict == "ok"), None)
    if told is not None:
        # She said something (§18.2b). The goal waits on it arriving, and a
        # done-mark beside it closes the goal *then* — not now, when all that
        # has happened is that a message was queued behind the quiet hours.
        completes = (finished(loop, intent.text)
                     or any(finished(loop, r.why) for r in worked.reaches))
        meta["telling"] = {"goal": told.told, "completes": completes}
        state = "waiting"
    elif started is not None:
        # Start-don't-await: the answer comes back as `task_completion`, and
        # until it does there is nothing to think about (§7.6, §16).
        meta["dispatched"] = {"tool": started.tool, "at": iso_of(loop.clock.now())}
        state = "waiting"
        loop.wakeups[goal.id] = (loop.clock.now()
                                 + float(loop.cfg.mind_dispatch_timeout_s))
        notes.append("…and I'm waiting on it before I go further")
    elif not narrated and (finished(loop, intent.text)
                           or any(finished(loop, r.why) for r in worked.reaches)):
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
