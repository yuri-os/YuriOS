"""Several hands in one step of her own work (SPEC §26, as amended).

A goal step, or a night's job written in her own voice, is one utility call —
and it used to be allowed **one** intent: a thought, or a single tool call, and
then the step was over. Which meant a goal that needed "read the note, then fix
the passage" took two ticks an hour apart, and the second one had to work out
from the desk what the first had been for.

Now a step is a short conversation with her own hands. She answers with a `use`
line, the hand runs, the result goes back to her as the next message, and she is
asked again — until she answers with prose (a thought, the job's output) or the
step's calls run out — `MIND_GOAL_MAX_HANDS` for a goal step, a job's own
`max_hands` for a night, and never past `TOOL_MAX_CALLS_PER_TURN`, the number a
reply to you gets (`capped`). Each call still passes every precondition `Hands.check` has, one at a
time: the daily cap, the cooldowns, the expensive class waiting for an empty
room. What changed is how many a step may make, not what any one of them needs.

`dispatch` is the one way the mind runs a hand, and the only caller of
`Hands.execute` — so the audit line, the host-side realisation and the chat
notice are the same whichever job reached.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from yurios.kernel import correlate

from . import acts
from .goals import goal_shape_refused
from .hands import (FILE_GOAL, START_DONT_AWAIT, TELL, Hands, Intent, Offer, bounded,
                    parse_intent, stamp_contract)

log = logging.getLogger("mind.handwork")

#: How many times a step may write a refused goal again without it costing one
#: of the step's calls (SPEC §22.1d). Free, because a goal that was only too
#: long is the one refusal she fixes by rewriting the same call — and free time
#: once ended on exactly that, with the goal she had decided on unfiled because
#: the refusal was her sixth call. Bounded, because a model that cannot shorten
#: a line must not be asked again forever; past this a refusal costs a call.
GOAL_REWORDS = 3

#: Appended to a prompt that did not ask for hands — a DREAM job in her own
#: voice. Additive on purpose: the job's own instructions still say what its
#: answer looks like, and this only says how to reach for something first.
HANDS_BEFORE_ANSWER = """## YOUR HANDS

Before you answer, you may use your hands. Your limit for this answer is {cap}, so plan them: reach for what you need to see, not for everything you could. Each result tells you how many you have left. To use one, reply with ONLY a line like this and nothing else:

  use <hand> {{"arg": "value"}}

You will get its result back, and then you can use another or give your answer. When you are ready, give your answer exactly as asked above — with no `use` line in it.

{catalog}"""


@dataclass
class Reach:
    """One call a step made, and what came of it."""
    tool: str
    args: dict
    verdict: str                 # ok | denied | error
    result: str
    #: It started work that finishes off-tick (§7.6) — a render, a research run.
    dispatched: bool = False
    #: Why she reached, in her own words, when she said.
    why: str = ""
    #: Why the preconditions refused it, when they did.
    refused: str = ""
    #: The message goal a `tell_them` filed (§18.2b), or "".
    told: str = ""
    #: A `create_goal` refused for its shape alone (§22.1d): she can write it
    #: again, and up to `GOAL_REWORDS` times that costs the step nothing.
    reword: bool = False


@dataclass
class Worked:
    """A whole step: the answer she ended on, and every call on the way."""
    answer: Intent
    reaches: list[Reach] = field(default_factory=list)

    @property
    def dispatched(self) -> Reach | None:
        return next((r for r in self.reaches if r.dispatched), None)


async def dispatch(loop, tool: str, args: dict, *, goal_id: str = "",
                   file_goal: Callable[[dict], tuple[str, str]] | None = None
                   ) -> Reach:
    """check → spend → execute → realise, for one call. Never raises.

    Every precondition is checked again here, not because the offer was wrong
    but because the switch can be revoked between the two — which is exactly
    what a kill switch has to survive. A denial is audited and becomes a result
    she can read; it is never an exception, and it never ends the step.
    """
    args = dict(args)
    if tool == TELL:
        # Not a hand: no tool server, no switch, no call cap. What it files is
        # Gate 2's to deliver, under Gate 2's hard limits (§18.2b).
        verdict, result, told = acts.queue_telling(loop, args, goal_id=goal_id)
        return Reach(tool, args, verdict, result, told=told,
                     refused=result if verdict == "denied" else "")
    if tool == FILE_GOAL and file_goal is not None:
        # A sitting's one decision — free time (§22.7), a handed document
        # (§34.6): filed by the mind, not the tool server, so it works whether
        # or not her hands are on. The caller says under what rules.
        verdict, result = file_goal(args)
        shape = goal_shape_refused(args) if verdict == "denied" else ""
        return Reach(tool, args, verdict, result,
                     refused=result if verdict == "denied" else "",
                     reword=bool(shape) and result == f"denied ({shape})")
    ok, why = loop.hands.check(
        tool, args, state=loop.activity.state,
        pressure=loop.budget.pressure(),
        user_present=bool(loop.world.snapshot().get("user_present")))
    if ok and tool == FILE_GOAL:
        why = acts.filing_refused(loop, args, goal_id=goal_id)
        ok = not why
    if not ok:
        loop.hands.deny(tool, args, why)
        return Reach(tool, args, "denied", f"denied ({why})", refused=why,
                     reword=tool == FILE_GOAL and why == goal_shape_refused(args))
    # Principle 7: every autonomous call names the goal that wanted it, when a
    # goal wanted it — so `goals.md` stays the readable list of what her hands
    # might do. A night job's call names none, and lands in the Vault the same.
    loop.hands.spend(tool, args)
    with correlate.scope(kind=correlate.MIND_TOOL):
        # The one hand filed on this side of the wire (§7.5, SPEC §22.1c): what she
        # reads back, and what the audit records, is the goal as filed.
        filing = ((lambda contract: acts.file_from_step(
            loop, contract, goal_id=goal_id)) if tool == FILE_GOAL else None)
        verdict, result = await loop.hands.execute(
            tool, args, timeout_s=loop.cfg.tool_timeout_s, realise=filing)
        # Host-side realisation (§7.5) — the timer actually scheduled, the
        # render actually started. The stamp is what makes the product land in
        # the Vault instead of in the chat (§18, principle 8).
        realise = getattr(loop.brain, "realise", None)
        if verdict == "ok" and callable(realise):
            try:
                realise(tool, result, extra=stamp_contract({}, goal_id=goal_id))
            except Exception:  # noqa: BLE001 — realisation is not the call
                log.exception("mind tool realisation failed")
    dispatched = (verdict == "ok" and tool in START_DONT_AWAIT
                  and '"started"' in result)
    return Reach(tool, args, verdict, result, dispatched=dispatched)


def _returned(reach: Reach, *, left: int) -> str:
    """A result, and how many hands the step has left after it.

    The count is the point: told only her total up front, a chain spent its
    whole allowance reading and had none left for the thing it was reading
    toward. With the count beside each result she can plan the rest.
    """
    body = bounded(reach.tool, reach.result)
    tail = (" Your hands are spent for this step — answer now, without a "
            "`use` line." if left <= 0 else
            f" You have {left} {'hand' if left == 1 else 'hands'} left for "
            "this step — use another if you need one, or give your answer.")
    return f"(({reach.tool} returned: {body}.{tail}))"


async def work(loop, messages: list[dict], *, offer: Offer,
               ask: Callable[[list[dict]], Awaitable[str]],
               goal_id: str = "", stop_on_dispatch: bool = False,
               on_reach: Callable[[Reach], None] | None = None,
               cap: int | None = None,
               file_goal: Callable[[dict], tuple[str, str]] | None = None
               ) -> Worked:
    """Ask; while she answers with a `use` line, run it and ask again.

    `ask` is the model call — the caller's, so a goal step keeps its soul and a
    night job keeps its transcript. `stop_on_dispatch` ends the step on work
    that finishes off-tick: a goal then waits for its `task_completion` rather
    than reasoning about a photo that does not exist yet.
    """
    limit = max(0, int(cap if cap is not None
                       else getattr(loop.cfg, "tool_max_calls_per_turn", 1)))
    messages = list(messages)
    done = Worked(Intent("think"))
    rewords = 0     # refused goals she wrote again for free (§22.1d)
    while True:
        reply = await ask(messages)
        intent = parse_intent(reply, allowed=offer.tools)
        if intent.kind != "use" or len(done.reaches) - rewords >= limit:
            # Past the cap a `use` line is dropped, not run — her reasoning
            # beside it is still the step's answer.
            done.answer = intent if intent.kind != "use" \
                else Intent("think", text=intent.text, unrun=(intent.tool,))
            return done
        if intent.fumbled:
            # Not dispatched: nothing reached a server, so nothing is booked —
            # not the ledger, not the day's count. It costs her one of the
            # step's calls, which is what bounds a model that cannot stop.
            why = (f"{intent.fumbled} — write the call again as `use "
                   f"{intent.tool} {{…}}` with one complete JSON object, "
                   "\\n for a line break inside text")
            loop.hands.deny(intent.tool, intent.args, why)
            reach = Reach(intent.tool, intent.args, "denied", f"denied ({why})",
                          refused=why)
        else:
            reach = await dispatch(loop, intent.tool, intent.args,
                                   goal_id=goal_id, file_goal=file_goal)
        reach.why = (intent.text or "").strip()
        done.reaches.append(reach)
        if reach.reword and rewords < GOAL_REWORDS:
            rewords += 1
        if on_reach is not None:
            on_reach(reach)
        if (reach.dispatched or reach.told) and stop_on_dispatch:
            # A message filed ends the step as a dispatch does: the goal now
            # waits on it being delivered, and anything she wrote after it would
            # be written about a message nobody has read yet.
            done.answer = Intent("think", text=reach.why)
            return done
        # Her turn as far as the call that ran (SPEC §26.2): a second call, or
        # a "result" she wrote under the first, never goes back to her as
        # something she said and is still owed an answer to.
        messages += [{"role": "assistant", "content": intent.said or reply},
                     {"role": "user",
                      "content": _returned(
                          reach, left=limit - (len(done.reaches) - rewords))}]


def capped(loop, wanted: int | None) -> int:
    """How many hands a step of hers may chain: `wanted`, never past the house.

    `None` is the house's `TOOL_MAX_CALLS_PER_TURN` itself. The clamp is the
    §26.1 two-switch rule one layer down — a job file or a mind setting may ask
    for fewer than a reply gets, never more — and it is applied here, where the
    call is made, so the number she is told is the one that holds.
    """
    house = int(getattr(loop.cfg, "tool_max_calls_per_turn", 1))
    return house if wanted is None else max(0, min(int(wanted), house))


def goal_cap(loop) -> int:
    """A goal step's hands (§26.2): `MIND_GOAL_MAX_HANDS`, clamped."""
    return capped(loop, getattr(loop.cfg, "mind_goal_max_hands", None))


class LoopHands:
    """The mind's hands, as a DREAM job reaches them (dreamjobs/context.py).

    Bound to the loop rather than to `loop.hands`, because the runner is built
    before the hands are, and a rebuilt runner must see the live switch.
    """

    def __init__(self, loop) -> None:
        self.loop = loop

    def offer(self) -> Offer:
        """Her hands, less `create_goal`: a goal is filed from one of her
        goals (§22.1), and a night files its own through the stock-take.
        Left in, it was a hand every call of which would be refused."""
        loop = self.loop
        offer = loop.hands.offer(
            state=loop.activity.state, pressure=loop.budget.pressure(),
            user_present=bool(loop.world.snapshot().get("user_present")))
        if FILE_GOAL not in offer.tools:
            return offer
        tools = tuple(t for t in offer.tools if t != FILE_GOAL)
        return Offer(tools=tools, held=offer.held, held_why=offer.held_why,
                     reason="" if tools else "no hand but create_goal, which "
                                             "a night job does not file with")

    async def run(self, messages: list[dict],
                  ask: Callable[[list[dict]], Awaitable[str]], *,
                  cap: int | None = None) -> str:
        """One job's call, with her hands offered before she answers.

        With none offered it is exactly the call it was. The answer comes back
        as she wrote it, minus any `use` line and any native call markup. The
        hands block is added to `messages` in place, so the job's transcript
        records the prompt that was actually sent.
        """
        offer = self.offer()
        if not offer:
            return await ask(messages)
        # The job's own `max_hands`, never past the house's (§21.2) — the
        # number she is told below is then the one that will actually hold.
        cap = capped(self.loop, cap)
        if not cap:
            return await ask(messages)
        catalog = Hands.rows(offer.tools)
        if offer.waiting():
            catalog += "\n\n" + offer.waiting()
        messages[0]["content"] = (messages[0].get("content") or "") + "\n\n" + \
            HANDS_BEFORE_ANSWER.format(cap=cap, catalog=catalog)
        worked = await work(self.loop, messages, offer=offer, ask=ask, cap=cap)
        return worked.answer.text

    async def use(self, tool: str, args: dict) -> Reach:
        """One call, for a job that runs its own loop (research)."""
        return await dispatch(self.loop, tool, args)
