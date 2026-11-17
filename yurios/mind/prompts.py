"""What the mind sends a model, and how it sounds like her (SPEC §22.4, §17.3).

Everything the mind sent a model used to be characterless. `utility` below is
the seam the knowledge store, every DREAM job and the goal-work step are handed,
and it passed the caller's messages through untouched — so her diary's whole
character content was the string "You are {char}", and two Vaults with
completely different cards produced byte-identical prompts for their private
thinking. Fixing that is `soul_text`, and the reason these three live together:
the persona blocks, the cache that makes them affordable at ten calls a night,
and the one call that spends the budget.

Split out of `loop.py` because a tick's *decisions* and a tick's *prompts* are
different work — one is read when the ladder misbehaves, the other when she
stops sounding like herself. `loop` keeps thin methods over these; see the note
there on why.

`loop` is deliberately unannotated, for the reason `world/runtime.py` gives:
naming its type means importing `MindLoop`, and `tests/test_layering.py` reads a
`TYPE_CHECKING` import off the parse tree like any other.
"""
from __future__ import annotations

import asyncio
import logging
from contextvars import ContextVar

from yurios.app.core.assemble import soul_preamble
from yurios.kernel import correlate
from yurios.world.vram import PATIENT_WAIT_S

log = logging.getLogger("mind.prompts")

#: Set while a later round of a chained step is asked (SPEC §22.4,
#: mind/handwork.py): that round already opens with a one-line reminder of
#: who she is, so `utility` leaves the card off it.
FOLLOWUP: ContextVar[bool] = ContextVar("mind_compact_followup", default=False)


class YieldedToChat(BaseException):
    """A model call of her own thinking, given up for a conversation (SPEC §17.4).

    The tick catches it around ACT, and it is the only place that catches it.
    It is a `BaseException`, the way cancellation is, so the `except Exception`
    on its way up does not read it as a failure. A failed promise review is
    retried on a backoff. A failed night job is retried tomorrow. A failed goal
    step goes into the goal's history. None of that happened here: she did not
    fail, you started talking. A caller that changed something before its call
    and must put it back catches this, puts it back and raises it again.
    """


def goal_history(goal) -> str:
    """A bounded record of actual attempts, including gate refusals (SPEC §22.4)."""
    history = goal.meta.get("decisions", [])
    if not isinstance(history, list):
        return ""
    lines = [str(line)[:400] for line in history[-6:] if isinstance(line, str)]
    if not lines:
        return ""
    return ("RECENT DECISIONS ON THIS GOAL (newest last)\n\n"
            + "\n".join(f"- {line}" for line in lines)
            + "\n\nThese are outcomes, not evidence that the work was completed. "
              "Use them to advance the goal rather than repeating an unchanged "
              "attempt. A blocked reach-out has not been delivered; do not "
              "invent urgency or another delivery route to evade the gate.")


def with_soul(messages: list[dict], preamble: str) -> list[dict]:
    """Prepend the persona blocks to a prompt's system message (SPEC §22.4).

    Fused onto the existing system message rather than added as a second one:
    a chat template renders two system turns however it likes, and half the
    local models this runs on silently drop the second. The instruction the
    caller wrote stays *after* the persona, because it is the part that says
    what this particular call is for and the last thing read wins ties.
    """
    out = [dict(m) for m in messages]
    for m in out:
        if m.get("role") == "system":
            m["content"] = f"{preamble}\n\n{m.get('content', '')}".strip()
            return out
    return [{"role": "system", "content": preamble}, *out]


def soul_text(loop, *, full: bool = True) -> str:
    """The persona blocks her private prompts open with (SPEC §22.4, §7.1).

    Cached, because `SoulLoader.load()` re-reads the whole soul directory
    on every call by design (§5) — correct for a turn, wasteful for a night
    that makes ten of them. Keyed on the newest mtime in `soul/`, so an
    edit she made through the self-edit gate is picked up without a
    restart and without a signal, the way `KnowledgeStore.search` watches
    its index.

    Never raises. A missing or mangled soul costs the block and not the
    call, which is §20.2's rule for the shelf applied to the self.
    """
    mode = str(getattr(loop.cfg, "mind_soul_in_prompts", "full") or "full").lower()
    if mode == "off":
        return ""
    full = full and mode != "brief"
    try:
        soul_dir = loop.cfg.vault_dir / "soul"
        stamp = max((p.stat().st_mtime for p in
                     [*soul_dir.glob("*.md"), soul_dir / "soul.yaml"]
                     if p.is_file()),
                     default=0.0)
    except OSError:
        stamp = 0.0
    key = (stamp, full)
    cached_key, cached_text, cached_at = loop._soul_cache
    ttl = float(getattr(loop.cfg, "mind_soul_cache_s", 300.0) or 0.0)
    if cached_key == key and (loop.clock.now() - cached_at) < ttl:
        return cached_text
    text = ""
    try:
        loader = getattr(loop.brain.state, "soul_loader", None)
        if loader is not None:
            text = soul_preamble(
                loader.load(),
                user_md=loop.vault.read("soul/USER.md"),
                user_name=loop.cfg.user_name, full=full)
    except Exception:  # noqa: BLE001 — an absent self is not a dead tick
        log.warning("soul preamble unavailable", exc_info=True)
        text = ""
    loop._soul_cache = (key, text, loop.clock.now())
    return text


def soul_drives(loop) -> list[str]:
    """Current durable motives, loaded live like every other SOUL surface."""
    try:
        loader = getattr(loop.brain.state, "soul_loader", None)
        return list(loader.load().drives) if loader is not None else []
    except Exception:  # noqa: BLE001 — absent drives cost context, not a night
        log.warning("character drives unavailable", exc_info=True)
        return []


async def utility(loop, messages: list[dict], *, soul: bool = False,
                   **params) -> str:
    """Local-tier utility call, debited against the governor. The loop's
    only other model use is inside deliberate ACT speech (SPEC §17.3).

    One instrumentation point covers five callers: the knowledge store, the
    dream consolidator and the goal-work step are all handed `MindLoop._utility`
    (which is this) as their `utility` seam, so the prompt each of them sends is
    recorded here, labelled by whichever ACT opened the scope.

    `soul` opts a caller into the persona blocks (§22.4). It defaults off
    rather than on because the two callers that reach this method *without*
    going through a job flag — the knowledge store's chunk blurbs and the
    consolidator — are the two doing mechanical extraction, and a shelf
    blurb written in character is a shelf that lies about what it read.
    Everything that thinks as *her* passes `soul=True` explicitly.

    `params` are the provider's, not this method's, and they are handed
    over untouched — `thinking`, `reasoning_effort`, `max_tokens`,
    `timeout`. This used to take `soul` and nothing else, and a seam that
    accepts no parameters is a seam that *rejects* them: a DREAM research
    round asking for `thinking=False` did not get a thoughtless call, it
    got a `TypeError` and a job that failed every night. Nothing here reads
    them, and that is the point — the knob a job turns is the provider's
    knob, and this method's job is to not be in the way of it.
    """
    utility = loop.brain.state.utility
    if utility is None:
        return ""
    yields = loop.yields_to_chat()
    if yields and loop.chat_has_model():
        raise YieldedToChat                 # don't start what would be given up
    # A follow-up round already carries the one-line reminder (§22.4). Fusing
    # the card again here is the resend `MIND_COMPACT_FOLLOWUPS` exists to stop.
    if soul and not FOLLOWUP.get():
        preamble = soul_text(loop)
        if preamble:
            messages = with_soul(messages, preamble)
    # A selfie may be holding her brain's VRAM right now (§7.6). Wait at
    # the door rather than JIT-loading the chat model back onto a card the
    # render hasn't finished with — the OOM that killed half of one night's
    # dreamt selfies, because the DREAM selfie job starts a render and then
    # immediately asks the model about the next day. Patiently: nothing
    # here is a person waiting for an answer. Then `hold`, so a park that
    # starts now waits for this call instead of evicting under it — and in
    # that order, or the park's quiet wait would be waiting for a call
    # that is waiting for the park.
    await loop.park_gate.wait(timeout_s=PATIENT_WAIT_S)
    async with loop.park_gate.hold():
        call = utility.complete(messages, **params)
        text = await (_yielding(loop, call) if yields else call)
    loop.budget.debit("".join(m.get("content", "") for m in messages), text)
    origin = correlate.current()
    loop.prompt_log.record(
        kind=(origin.kind if origin and origin.kind != correlate.TICK
              else correlate.UTILITY),
        messages=messages, completion=text, model=loop.cfg.utility_model,
        tier="utility")
    return text


async def _yielding(loop, call) -> str:
    """Run one model call that a user turn may take back (SPEC §17.4).

    It runs as a task of its own, so `MindLoop.turn_started` can cancel it.
    Cancelling the task cancels the request: LiteLLM's HTTP client drops the
    connection, a local server stops generating, and the place this call held
    in the process-wide inference queue goes to her reply.

    A cancel can also come from outside, when the tick itself is cancelled at
    shutdown. Only the cancel that `turn_started` asked for, while the tick
    was not being cancelled, becomes `YieldedToChat`. Any other cancel goes
    on as a cancel.
    """
    task = asyncio.ensure_future(call)
    loop._model_call, loop._model_yielded = task, False
    try:
        return await task
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if loop._model_yielded and not (current and current.cancelling()):
            raise YieldedToChat from None
        raise
    finally:
        loop._model_call = None
