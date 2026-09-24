"""Several hands in one step of her own work (SPEC §26, as amended).

A goal step, or a night's job written in her own voice, is one utility call —
and it used to be allowed **one** intent: a thought, or a single tool call, and
then the step was over. Which meant a goal that needed "read the note, then fix
the passage" took two ticks an hour apart, and the second one had to work out
from the desk what the first had been for.

Now a step is a short conversation with her own hands. She answers with a `use`
line, the hand runs, the result goes back to her as the next message, and she is
asked again — until she answers with prose (a thought, the job's output) or the
step's calls run out (`TOOL_MAX_CALLS_PER_TURN`, the same number a reply to you
gets). Each call still passes every precondition `Hands.check` has, one at a
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

from .hands import (START_DONT_AWAIT, Hands, Intent, Offer, parse_intent,
                    stamp_contract)

log = logging.getLogger("mind.handwork")

#: How much of one result goes back to her. The tool already bounds its payload
#: (SPEC §34.2); this bounds a step that chains several of them.
RESULT_CHARS = 4000

#: Appended to a prompt that did not ask for hands — a DREAM job in her own
#: voice. Additive on purpose: the job's own instructions still say what its
#: answer looks like, and this only says how to reach for something first.
HANDS_BEFORE_ANSWER = """## YOUR HANDS

Before you answer, you may use your hands — as many times as you need, up to {cap}. To use one, reply with ONLY a line like this and nothing else:

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


@dataclass
class Worked:
    """A whole step: the answer she ended on, and every call on the way."""
    answer: Intent
    reaches: list[Reach] = field(default_factory=list)

    @property
    def dispatched(self) -> Reach | None:
        return next((r for r in self.reaches if r.dispatched), None)


async def dispatch(loop, tool: str, args: dict, *, goal_id: str = "") -> Reach:
    """check → spend → execute → realise, for one call. Never raises.

    Every precondition is checked again here, not because the offer was wrong
    but because the switch can be revoked between the two — which is exactly
    what a kill switch has to survive. A denial is audited and becomes a result
    she can read; it is never an exception, and it never ends the step.
    """
    args = dict(args)
    ok, why = loop.hands.check(
        tool, args, state=loop.activity.state,
        pressure=loop.budget.pressure(),
        user_present=bool(loop.world.snapshot().get("user_present")))
    if not ok:
        loop.hands.deny(tool, args, why)
        return Reach(tool, args, "denied", f"denied ({why})", refused=why)
    # Principle 7: every autonomous call names the goal that wanted it, when a
    # goal wanted it — so `goals.md` stays the readable list of what her hands
    # might do. A night job's call names none, and lands in the Vault the same.
    loop.hands.spend(tool, args)
    with correlate.scope(kind=correlate.MIND_TOOL):
        verdict, result = await loop.hands.execute(
            tool, args, timeout_s=loop.cfg.tool_timeout_s)
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


def _returned(reach: Reach, *, spent: bool) -> str:
    body = reach.result
    if len(body) > RESULT_CHARS:
        body = body[:RESULT_CHARS] + " …(cut)"
    tail = (" Your hands are spent for this step — answer now, without a "
            "`use` line." if spent else
            " Use another hand if you need one, or give your answer.")
    return f"(({reach.tool} returned: {body}.{tail}))"


async def work(loop, messages: list[dict], *, offer: Offer,
               ask: Callable[[list[dict]], Awaitable[str]],
               goal_id: str = "", stop_on_dispatch: bool = False,
               on_reach: Callable[[Reach], None] | None = None,
               cap: int | None = None) -> Worked:
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
    while True:
        reply = await ask(messages)
        intent = parse_intent(reply, allowed=offer.tools)
        if intent.kind != "use" or len(done.reaches) >= limit:
            # Past the cap a `use` line is dropped, not run — her reasoning
            # beside it is still the step's answer.
            done.answer = intent if intent.kind != "use" \
                else Intent("think", text=intent.text)
            return done
        reach = await dispatch(loop, intent.tool, intent.args, goal_id=goal_id)
        reach.why = (intent.text or "").strip()
        done.reaches.append(reach)
        if on_reach is not None:
            on_reach(reach)
        if reach.dispatched and stop_on_dispatch:
            done.answer = Intent("think", text=reach.why)
            return done
        messages += [{"role": "assistant", "content": reply},
                     {"role": "user",
                      "content": _returned(reach,
                                           spent=len(done.reaches) >= limit)}]


class LoopHands:
    """The mind's hands, as a DREAM job reaches them (dreamjobs/context.py).

    Bound to the loop rather than to `loop.hands`, because the runner is built
    before the hands are, and a rebuilt runner must see the live switch.
    """

    def __init__(self, loop) -> None:
        self.loop = loop

    def offer(self) -> Offer:
        loop = self.loop
        return loop.hands.offer(
            state=loop.activity.state, pressure=loop.budget.pressure(),
            user_present=bool(loop.world.snapshot().get("user_present")))

    async def run(self, messages: list[dict],
                  ask: Callable[[list[dict]], Awaitable[str]]) -> str:
        """One job's call, with her hands offered before she answers.

        With none offered it is exactly the call it was. The answer comes back
        as she wrote it, minus any `use` line and any native call markup. The
        hands block is added to `messages` in place, so the job's transcript
        records the prompt that was actually sent.
        """
        offer = self.offer()
        if not offer:
            return await ask(messages)
        cap = int(getattr(self.loop.cfg, "tool_max_calls_per_turn", 1))
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
