"""The things one tick can decide to do (SPEC §15, §17, §18, §21, §22).

DECIDE commits to exactly one of these or to resting, and `MindLoop._act` is
the switchboard that picks. Each returns the same triple — what was acted, the
patch for the tick trace, and the journal lines — because a tick is one entry in
a diary and the shape of the entry is the same whichever act wrote it.

They are gathered here, away from the tick's own machinery, because deciding and
doing are read for different reasons: `loop.py` when she chose the wrong thing,
this file when the thing she chose went wrong. Goal work is the exception and
lives in `goalwork.py` — it is the only act that may reach for a hand, and it is
longer than the rest of them together.

`loop` is unannotated for the reason `world/runtime.py` gives: naming its type
means importing `MindLoop`, and `tests/test_layering.py` reads a
`TYPE_CHECKING` import off the parse tree like any other.
"""
from __future__ import annotations

import logging

from yurios.kernel import correlate


from .goals import (Goal, trim, PROMISE_REVIEW_RESPONSE_FORMAT, PromiseCandidate,
                    parse_promise_review, promise_decision_grounded,
                    promise_kind, promise_review_messages)
from .policy import DREAM, score_interrupt
from .signals import Signal, failure_of
from .util import day_of, iso_of, ts_of_iso

log = logging.getLogger("mind.acts")

ANNOUNCE_CUE = (
    "((The timer for “{label}” just finished. Tell {user} it's done — one "
    "short, warm spoken line, nothing else.))")

SELF_TALK_CUES = (
    "((It's been quiet for a while. Murmur one short line to yourself about "
    "the rain on the window — a private thought said softly aloud, not "
    "expecting an answer.))",
    "((A quiet stretch. One soft spoken line to yourself about this room — "
    "the lamp, the plant, the window seat. Half to yourself.))",
    "((The room is quiet. Let one small remembered thing about {user} "
    "surface, and say one gentle line to yourself about it.))",
)

REACH_OUT_CUE = (
    "((You decided, on your own, to reach out first about this: {goal}. Say "
    "the one short, warm, specific spoken message you'd open with — no "
    "preamble, no explaining that you decided to speak.))")


def promise_review_available(loop) -> bool:
    return bool(loop.cfg.utility_enabled and loop.brain.state.utility is not None)


def eligible_promise_reviews(loop) -> int:
    now = loop.clock.now()
    return sum(1 for review in loop.promise_reviews
               if float(review.get("retry_at", 0) or 0) <= now)


def file_promise_candidate(loop, candidate: PromiseCandidate, *,
                            user_text: str, reply: str,
                            text: str | None = None,
                            kind: str | None = None,
                            rationale: str = "",
                            success: str = "",
                            sources: list[dict] | None = None) -> str:
    objective = trim(text or candidate.text, 240)
    goal_kind = kind or promise_kind(objective, candidate.provenance)
    before = {goal.id for goal in loop.goals.open_goals()}
    # `object`, not `str`: `candidate_sources` below is a list of rows. The
    # goal store writes this out as the goal's meta field either way; typing it
    # str was a thing nobody could see while this lived in an unchecked module.
    meta: dict[str, object] = {
        "about": trim(user_text),
        "source": trim(candidate.source, 300),
        "reply": trim(reply, 600),
    }
    if rationale:
        meta["rationale"] = trim(rationale, 400)
    if success:
        meta["success"] = trim(success, 400)
    if sources:
        meta["candidate_sources"] = sources
    goal = loop.goals.add(
        objective, kind=goal_kind,
        priority=0.6 if goal_kind == "reach_out" else 0.7,
        due=(iso_of(loop.clock.now() + 24 * 3600)
             if goal_kind == "reach_out" else None),
        commitment="single-minded", provenance=candidate.provenance,
        meta=meta)
    if goal.id in before:
        return ""
    return (f"I promised: {goal.text}" if goal_kind == "reach_out"
            else f"I took that on: {goal.text}")


async def promise_review(loop, offer=None) -> tuple[dict, dict, list[str]]:
    now = loop.clock.now()
    index = next((index for index, review in enumerate(loop.promise_reviews)
                  if float(review.get("retry_at", 0) or 0) <= now), -1)
    if index < 0:
        return ({"what": "promise_review", "result": "review is backing off"}, {}, [])
    review = loop.promise_reviews.pop(index)
    candidates = review.get("candidates", [])
    messages = promise_review_messages(
        user_text=str(review.get("user_text", "")),
        reply=str(review.get("reply", "")), candidates=candidates,
        capabilities=list(offer.tools) if offer else [])
    try:
        with correlate.scope(kind=correlate.UTILITY):
            raw = await loop._utility(
                messages, soul=False, thinking=True, reasoning_effort="low",
                max_tokens=1200,
                response_format=PROMISE_REVIEW_RESPONSE_FORMAT)
        decision = parse_promise_review(raw, candidate_count=len(candidates))
    except Exception:  # noqa: BLE001 — a malformed review must not kill the tick
        log.warning("promise review failed; retaining it", exc_info=True)
        attempts = int(review.get("attempts", 0)) + 1
        review["attempts"] = attempts
        review["retry_at"] = now + min(3600.0, 30.0 * (2 ** (attempts - 1)))
        loop.promise_reviews.append(review)
        return ({"what": "promise_review",
                 "result": "invalid review retained for retry"}, {}, [])

    if decision is None:
        return ({"what": "promise_review", "result": "no unresolved promise"}, {}, [])
    if not promise_decision_grounded(
            decision, candidates, str(review.get("user_text", ""))):
        log.warning("promise review returned an ungrounded objective; discarded")
        return ({"what": "promise_review", "result": "ungrounded goal discarded"}, {}, [])
    selected = [candidates[index] for index in decision.candidates]
    primary = selected[0]
    candidate = PromiseCandidate(
        index=int(primary.get("index", 0)),
        text=str(primary.get("text", "")),
        provenance=str(primary.get("provenance", "promise:her-own-words")),
        source=str(primary.get("source", "")),
        start=int(primary.get("start", 0)),
        confidence=str(primary.get("confidence", "soft")))
    sources = [{"text": trim(str(item.get("source", "")), 200),
                "candidate": int(item.get("index", 0))}
               for item in selected]
    note = file_promise_candidate(
        loop, candidate, user_text=str(review.get("user_text", "")),
        reply=str(review.get("reply", "")), text=decision.text,
        kind=decision.kind, rationale=decision.rationale,
        success=decision.success, sources=sources)
    return ({"what": "promise_review",
             "result": "filed one canonical goal" if note else
                       "matched an existing goal"}, {}, [note] if note else [])


async def announce(loop) -> tuple[dict, dict, list[str]]:
    """A landed timer — a promise, so it queues until deliverable (the
    Build #4 rule, kept verbatim)."""
    t = loop._pending_announce[0]
    loop.controller.set_expression("surprised", 0.6, reset_ms=4000)
    cue = ANNOUNCE_CUE.format(label=t.get("label", "your timer"),
                              user=loop.cfg.user_name)
    with correlate.scope(kind=correlate.AMBIENT):
        spoken = await loop.speak(cue)
    if spoken:
        loop._pending_announce.pop(0)
        return ({"what": "speak", "result": "announced the timer"}, {},
                [f"told them the “{t.get('label')}” timer finished"])
    return ({"what": "speak", "result": "announce queued (nobody to tell)"},
            {}, [])


async def self_talk(loop) -> tuple[dict, dict, list[str]]:
    """The Ukagaka murmur, now decided rather than diced — ambient, never
    persisted, dropped if she can't be heard (SPEC §15.5)."""
    cue = loop.rng.choice(SELF_TALK_CUES).format(user=loop.cfg.user_name)
    with correlate.scope(kind=correlate.AMBIENT):
        delivered = await loop.speak(cue)
    loop._next_self_talk = loop.clock.now() + loop._uniform(
        loop.cfg.idle_talk_min_s, loop.cfg.idle_talk_max_s)
    return ({"what": "speak",
             "result": "murmured to herself" if delivered else
                       "let the murmur go (busy or alone)"}, {}, [])


async def ingest(loop) -> tuple[dict, dict, list[str]]:
    with correlate.scope(kind=correlate.KNOWLEDGE):
        results = await loop.knowledge.scan()
    notes = [f"read and shelved {r.doc} ({r.chunks} passages"
             + (", too long to keep word-for-word, so notes)" if r.digested
                else ")")
             for r in results]
    return ({"what": "knowledge.ingest",
             "result": f"ingested {len(results)} doc(s)"}, {}, notes)


async def dream(loop) -> tuple[dict, dict, list[str]]:
    """One DREAM tick: the whole pipeline, one shared budget (§21.2).

    The night is chunked exactly as consolidation alone used to be — this
    tick spends what it may and yields, the ladder stays in DREAM while any
    job still has a backlog, and the next tick picks up where this one
    stopped. What changed is only how many kinds of work that covers — and
    that research carries its own budget, so a night of reading neither
    starves the roster nor is starved by it (§21.2).
    """
    if not loop.cfg.dream_enabled or not loop.cfg.utility_enabled:
        return ({"what": "dream", "result": "DREAM disabled"}, {}, [])
    with correlate.scope(kind=correlate.DREAM):
        report = await loop.dreams.run(
            token_budget=loop.cfg.mind_dream_tick_tokens,
            research_budget=loop.cfg.mind_dream_research_tokens)
    return ({"what": "dream", "result": report.summary,
             "jobs": [j.as_dict() for j in report.jobs]}, {}, report.notes)


def deliver_report(loop, *, title: str, path: str, summary: str,
                    job: str) -> None:
    """File one night's report where they will find it (§18.2a).

    Not Gate 2. Gate 2 asks whether *she* should interrupt, and spends one
    of a handful of daily interrupts when the answer is yes; this is a
    standing instruction its owner wrote into a job file, which is the
    difference between her deciding to reach for you and you having asked
    for a thing to be ready in the morning. So it costs no interrupt and
    argues with no threshold — it only puts a named artifact in the inbox
    and lets them find it when they next look.

    `unheard=True` for the same reason `_act_reach_out`'s SUGGEST does it: a
    briefing is by definition waiting for the next time they look, so it
    belongs in the inbox whether or not a page is open at four in the
    morning (`world/inbox.py`).
    """
    try:
        loop.post_message("assistant", summary, proactive=True,
                          unheard=True, report_path=path,
                          report_title=title, report_job=job)
    except Exception:  # noqa: BLE001 — a night's work is not lost to delivery
        log.exception("DREAM: couldn't deliver %s", path)


async def reach_out(loop, goal: Goal) -> tuple[dict, dict, list[str]]:
    today = day_of(loop.clock.now())       # her day rolls at local midnight
    if loop.interrupts.get("date") != today:
        loop.interrupts = {"date": today, "count": 0}
    world = loop.world.snapshot()
    last_out = world.get("last_contact_out")
    decision = score_interrupt(
        clock=loop.clock,
        relevance=goal.priority,
        time_sensitivity=1.0 if goal.is_due(loop.clock, 6) else 0.2,
        last_contact_out=ts_of_iso(last_out) if last_out else None,
        interrupts_today=loop.interrupts["count"],
        max_interrupts_per_day=loop.cfg.mind_max_interrupts_per_day,
        threshold=loop.cfg.mind_interrupt_threshold)
    interrupt = {"score": decision.score, "threshold": decision.threshold,
                 "outcome": decision.outcome, "factors": decision.factors,
                 "goal": goal.text}

    if decision.outcome == "SILENT":
        # THE DEFAULT: do it silently and journal it
        if goal.is_stale(loop.clock) and goal.commitment != "blind":
            loop.goals.set_state(goal.id, "abandoned")
            note = f"let it go quietly: {goal.text} (the moment passed)"
        else:
            note = f"thought about {goal.text}; chose not to interrupt"
        return ({"what": None, "result": "stayed quiet"}, interrupt, [note])

    if decision.outcome == "SUGGEST":
        # a soft line in the chat — waiting when they next look, never spoken
        text = await loop._compose(REACH_OUT_CUE.format(goal=goal.text))
        if text:
            # `unheard`: a SUGGEST is *by definition* a line waiting for the
            # next time they look, so it belongs in the inbox whether or not
            # a page happens to be open right now (world/inbox.py).
            loop.post_message("assistant", text, proactive=True, unheard=True)
        loop.world.note_contact_out()
        loop.interrupts["count"] += 1
        loop.goals.set_state(goal.id, "done")
        return ({"what": "chat", "result": f"left a quiet note: {goal.text}"},
                interrupt, [f"left them a note about {goal.text}"])

    # SPEAK: aloud through the ambient seam if a page is open (the full turn
    # pipeline — voice, face, barge-in); as a chat line if the room is empty
    cue = REACH_OUT_CUE.format(goal=goal.text)
    with correlate.scope(kind=correlate.COMPOSE):
        spoken = await loop.speak(cue)
    if not spoken:
        # `speak` said no: there is no page to say it through, so this is the
        # case the inbox exists for — she spent an interrupt on an empty room.
        text = await loop._compose(cue)
        if text:
            loop.post_message("assistant", text, proactive=True, unheard=True)
    loop.world.note_contact_out()
    loop.interrupts["count"] += 1
    loop.goals.set_state(goal.id, "done")
    return ({"what": "speak", "result": f"reached out: {goal.text}"},
            interrupt, [f"reached out first about {goal.text}"])


def wake_goal(loop, goal_id: str) -> str:
    """A `wakeup` the loop scheduled has landed.

    Two things it can mean, and the goal's own state says which: a parked
    goal is due another look (clear the consider cooldown and let APPRAISE
    see it), or a goal has been sitting in `waiting` on work that never
    posted its `task_completion` (unstrand it, and say so — a goal invisible
    to every gate she has is worse than one that failed).
    """
    goal = loop.goals.get(goal_id) if goal_id else None
    if goal is None or goal.state not in ("pending", "active", "waiting"):
        return ""
    loop.considered.pop(goal.id, None)
    if goal.state != "waiting":
        return ""
    dispatched = goal.dispatched
    loop.goals.update(goal.id, state="active", meta={"dispatched": {}})
    if dispatched:
        return (f"the {dispatched.get('tool', 'work')} I started for "
                f"“{goal.text}” never came back; picking it up myself")
    return f"came back to: {goal.text}"


def land_dispatched(loop, sig: Signal) -> str:
    """`task_completion` → the goal that dispatched it returns to `active`.

    This is the whole reason the signal type existed and was never posted:
    the loop was built for a return path and the return path was a stub.
    Bookkeeping, done in SENSE, so a busy tick cannot leave a finished run's
    goal stranded in `waiting` behind some louder intention.
    """
    goal_id = str(sig.payload.get("goal_id") or "")
    goal = loop.goals.get(goal_id) if goal_id else None
    if goal is None or goal.state != "waiting":
        return ""
    loop.goals.update(goal.id, state="active", meta={"dispatched": {}})
    loop.considered.pop(goal.id, None)     # workable again on this very tick
    loop.wakeups.pop(goal.id, None)        # the safety net is not needed now
    what = sig.payload.get("kind") or "work"
    # The goal comes back to `active` either way — that is what posting the
    # failure down this path buys — but the note must not promise a product
    # that isn't there. "It's in the chat" sends her (and you) looking.
    if (err := failure_of(sig)):
        return (f"the {what} I started for “{goal.text}” failed "
                f"({err}) — nothing landed")
    where = ("it's in the vault, not in the chat"
             if sig.payload.get("deliver") == "vault" else "it's in the chat")
    return (f"the {what} I started for “{goal.text}” came back — {where}")


async def maintenance(loop, goal: Goal,
                           auto: str) -> tuple[dict, dict, list[str]]:
    """A `maintenance:*` goal that stands for a standing leftover (§22).

    It does not get a paragraph written about it — it gets the leftover
    done, through the same act the cheap impulse path uses, and it closes
    itself the moment there is nothing left. That is what keeps the goals
    page from filling with to-dos nobody can finish by reading them.
    """
    if auto == "shelf":
        acted, interrupt, notes = await ingest(loop)
        left = bool(loop.knowledge.pending_docs())
    elif loop.activity.state != DREAM:
        # Defence, not a path she is expected to take: the dream goal
        # carries no due time precisely so it never outranks the window
        # that owns this decision (§21). If somebody raises its priority by
        # hand, it still waits for the night rather than starting one.
        loop.goals.update(goal.id, meta={"last_step": iso_of(loop.clock.now())})
        return ({"what": None, "result": "the backlog waits for tonight"},
                {}, [f"still to do tonight: {goal.text}"])
    else:
        acted, interrupt, notes = await dream(loop)
        left = bool(loop.dreams.backlog())
    state = "active" if left else "done"
    if state == "done":
        notes.append(f"cleared: {goal.text}")
    loop.goals.update(goal.id, state=state,
                      meta={"steps": goal.steps + 1,
                            "last_step": iso_of(loop.clock.now())})
    return ({**acted, "goal": goal.id, "state": state}, interrupt, notes)
