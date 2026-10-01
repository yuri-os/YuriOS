"""A document you hand her — read, and hers to decide about (SPEC §34.6).

The shelf (§20) is for documents she *looks things up in*: chunked, indexed,
searched on every turn whether or not she knows they are there. This is the
other thing you do with a document, which is give it to somebody. It lands in
her desk's inbox (`workspace/inbox/`), she is told it arrived, and then she sits
down with it the way free time sits down with her days (§22.7): reads it, looks
at her desk and her list, and decides what it means for what she is doing — a
goal to work it into a report, a follow-up for you, or nothing but a thought.

Nothing here works the document itself. Anything that changes something — a
note rewritten, a report extended — is a goal's step, filed from here
(`create_goal`) and done on its own turns with all her hands; the goal carries
the path, so each step is shown the document again (`goalwork.context`). The
sitting may only open her notes, and only when her hands are on: without them,
she still has the head of the document in front of her, and filing a goal is
not a hand.

When she sits down with it is `MIND_INBOX_WAKE`. On, the arrival wakes her
from any state, 4am included, and spends model calls then. Off, SENSE notices
it at once — a journal line, no model — and the sitting waits until she is
next active, which is to say until you are back. Either way it waits for a
conversational turn in progress to end, and for a day's budget that is not
already spent.

`loop` is unannotated for the reason `world/runtime.py` gives.
"""
from __future__ import annotations

import json
import logging

from yurios.kernel import correlate

from . import acts, handwork
from .goals import trim
from .goalwork import takeaway
from .hands import FILE_GOAL, Hands, Offer
from .muse import REVIEW
from .policy import ENGAGED, IDLE, Appraisal
from .signals import Signal
from .util import iso_of

log = logging.getLogger("mind.handed")

#: The desk folder a handed document lands in.
INBOX = "inbox"

#: Above everything a goal earns on priority alone, below a timer or a promise
#: under review: you handed it over, so it is next — but it is not urgent.
SCORE = 0.7

#: Calls one sitting may chain — enough to read further into a long document
#: and glance at the notes it might belong in.
MAX_CALLS = 6

#: How much of the document the sitting is shown. Enough to know what it is
#: and usually enough to decide; past it, `read_note` reads on.
HEAD_CHARS = 6000

#: The most handed documents remembered. Waiting ones are never dropped for
#: room — only the oldest she has already read — and what falls off this list
#: is still on the desk and in the journal.
MAX_KEPT = 20

#: A record's states. `waiting` is the queue; the other two are history, kept
#: so the inner-life panel can say what came of each one (§24.3).
WAITING, READ, GONE = "waiting", "read", "gone"


def waiting(loop) -> list[dict]:
    """The documents she has yet to sit down with, oldest first."""
    return [h for h in loop.handed if h.get("state", WAITING) == WAITING]


def _keep(loop) -> None:
    done = [h for h in loop.handed if h.get("state", WAITING) != WAITING]
    drop = {id(h) for h in done[:max(0, len(loop.handed) - MAX_KEPT)]}
    loop.handed = [h for h in loop.handed if id(h) not in drop]


def received(loop, sig: Signal) -> str:
    """SENSE: note the arrival and queue the sitting. Model-free.

    The same file handed again replaces its record rather than adding a second
    — it is one document, and the newest copy is the one to read, so a copy she
    has already read goes back to waiting.
    """
    path = str(sig.payload.get("path") or "").strip()
    if not path:
        return ""
    name = str(sig.payload.get("name") or path)
    loop.handed = [h for h in loop.handed if h.get("path") != path]
    loop.handed.append({"path": path, "name": name, "at": sig.ts,
                        "state": WAITING})
    _keep(loop)
    later = "" if loop.cfg.mind_inbox_wake else \
        " — I'll sit down with it when you're back"
    return f"you handed me {name}; it's on my desk at {path}{later}"


def appraise(loop) -> Appraisal | None:
    """The sitting, as an impulse — or None while it has to wait (§15.1)."""
    queue = waiting(loop)
    if not queue or not loop.cfg.utility_enabled:
        return None
    if loop._engaged_now():
        return None                       # never mid-turn: the room comes first
    if (not loop.cfg.mind_inbox_wake
            and loop.activity.state not in (ENGAGED, IDLE)):
        return None                       # noticed already; decided when back
    if loop.budget.pressure() >= 1.0:
        return None
    first = queue[0]
    more = len(queue) - 1
    return Appraisal("handed", "impulse", SCORE,
                     f"you handed me {first['name']}"
                     + (f" (and {more} more)" if more else ""))


def system(loop, tools: tuple[str, ...], path: str) -> str:
    """The instruction half. In her voice, like every mind prompt (§22.4)."""
    user = loop.cfg.user_name
    lines = [
        f"{user} handed you a document. It is on your desk at {path}, and the "
        "start of it is below. Read it as yourself, then decide what it means "
        "for what you are doing — not what an assistant would do with a file.",
        "",
        "Maybe it belongs in something you are already writing: a report, a "
        "note, the desk file of a goal you are working. Maybe it changes a "
        f"goal, or answers something {user} asked. Maybe there is something "
        f"you want to say to {user} about it. Or maybe it is worth no more than "
        "a thought, and that is a fine answer too.",
        "",
        f"Anything that means doing work — writing it into a report, pulling "
        f"facts from it, telling {user} something — goes on your list with "
        f"`{FILE_GOAL}`: one concrete goal, written as what you'll do, naming "
        f"{path} so the work knows what to read. It gets its own turns with "
        f"all your hands. If the point is for {user} to hear something from "
        "you, make its kind \"reach_out\". One goal at most.",
        "",
        "Answer with one line, in one of these forms:",
        "",
        "  think <what you made of it, and what you decided>",
        Hands.rows(tools),
        "",
        "…where the part after the tool name is one line of JSON, on a line of "
        "its own. Each result comes back to you before you go on — up to "
        f"{MAX_CALLS} in this sitting. Put a `think` line above a hand saying "
        "why, and end on a `think` line.",
    ]
    return "\n".join(lines).strip()


def context(loop, path: str, text: str) -> str:
    """The document, and enough of her own situation to place it."""
    head = text[:HEAD_CHARS].rstrip()
    rest = len(text) - len(head)
    lines = text.count("\n") + 1
    parts = [f"THE DOCUMENT — {path}, {lines} lines\n\n{head}"]
    if rest > 0:
        parts[0] += (f"\n\n(…{rest} more characters. `read_note` with "
                     f'{{"path": "{path}", "start_line": …}} reads on, if your '
                     "hands are on.)")
    try:
        parts.append("THE SITUATION RIGHT NOW\n\n" + loop.world.situation())
    except Exception:  # noqa: BLE001
        log.debug("handed: no situation", exc_info=True)
    open_goals = loop.goals.open_goals()
    parts.append("ON YOUR LIST\n\n" + ("\n".join(
        f"- [{g.state}] {g.text}" for g in open_goals[-8:]) or "Nothing."))
    if loop.workspace is not None:
        digest = loop.workspace.digest(limit=20)
        if digest:
            parts.append("YOUR DESK (paths only — `read_note` opens one)\n\n"
                         + digest)
    facts = loop.vault.read("memory/semantic/facts.md")[-1200:].strip()
    if facts:
        parts.append("WHAT YOU KNOW ABOUT THEM\n\n" + facts)
    return "\n\n".join(parts)


async def consider(loop, offer: Offer | None) -> tuple[dict, dict, list[str]]:
    """One sitting with the oldest handed document: read, decide, leave a trail.

    Marked read before the call, not after: a sitting that fails must not
    become a retry every tick. The document stays on the desk regardless, and
    the record keeps what came of it for the inner-life panel.
    """
    item = waiting(loop)[0]
    path, name = item["path"], item["name"]
    item.update(state=READ, read_at=iso_of(loop.clock.now()))
    text: str | None = None
    try:
        if loop.workspace is not None:
            text = loop.workspace.read(path)
    except FileNotFoundError:
        text = None                        # cleared off the desk since
    except Exception:  # noqa: BLE001 — outside the desk, unreadable
        log.warning("handed: couldn't read %s", path, exc_info=True)
        text = None
    if text is None:
        item["state"] = GONE
        return ({"what": "handed", "result": f"{path} was gone"}, {},
                [f"{name} was gone from my desk before I got to it"])

    loop._muse_filed = ""
    tools = tuple(t for t in (offer.tools if offer else ()) if t in REVIEW)
    tools += (FILE_GOAL,)

    async def ask(messages: list[dict]) -> str:
        return await loop._utility(messages, soul=True)

    messages = [{"role": "system", "content": system(loop, tools, path)},
                {"role": "user", "content": context(loop, path, text)}]
    with correlate.scope(kind=correlate.HANDED):
        worked = await handwork.work(
            loop, messages, offer=Offer(tools=tools), ask=ask, cap=MAX_CALLS,
            file_goal=lambda args: acts.file_from_handed(loop, args, path=path))

    notes: list[str] = []
    filed = None
    for reach in worked.reaches:
        if reach.tool == FILE_GOAL and reach.verdict == "ok":
            try:
                data = json.loads(reach.result)
            except ValueError:
                data = {}
            if data.get("status") == "created":
                filed = data
                # Her reason is the goal's plan, as free time's is (§22.4).
                if reach.why:
                    loop.goals.update(str(data["id"]),
                                      meta={"rationale": trim(reach.why, 600)})
                notes.append(f"read {name}: decided to “{data.get('text')}”")
            else:
                notes.append(f"read {name}: it fits something I'm already "
                             f"doing — “{data.get('text')}”")
        elif reach.verdict != "ok":
            notes.append(f"read {name}: wanted to {reach.tool} but didn't: "
                         f"{reach.refused or reach.result}")
    thought = (worked.answer.text or "").strip()
    if thought:
        notes.append(f"read {name}: {takeaway(thought)}")
    elif not notes:
        notes.append(f"read {name}, and nothing came of it")
    result = (f"read {path}: filed {filed.get('id')}" if filed
              else f"read {path}: thought it over" if thought
              else f"read {path}: nothing came of it")
    item["outcome"] = (f"decided to “{filed.get('text')}”" if filed
                       else takeaway(thought) if thought else "nothing came of it")
    if filed:
        item["goal"] = filed.get("id")
    acted: dict = {"what": "handed", "result": result, "doc": path}
    if worked.reaches:
        acted["tools"] = [{"tool": r.tool, "verdict": r.verdict}
                          for r in worked.reaches]
    if filed:
        acted["goal"] = filed.get("id")
    return acted, {}, notes

