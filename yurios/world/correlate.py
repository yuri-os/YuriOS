"""Origin — the join key that makes the logs one story (SPEC §24.2).

Every durable record she leaves is written by a different object: the tick trace
by `mind/trace.py`, the tool audit by `world/tools/guard.py`, the assembled
prompt by `mind/promptlog.py` and `app/corpus.py`. Read alone, each answers half
a question. "Why did she take that photo" needs the tick that decided it, the
prompt that phrased it and the audit line that ran it — and before this module
those three could only be lined up by comparing timestamps and hoping.

So one unit of work carries one `corr_id`, and every writer stamps it. The
carrier is a ContextVar rather than a parameter because the writers sit several
frames below the thing that knows the answer: `Guard.audit` is called from
`ToolBrain._execute`, which is called from the pass loop, which is called from
the route — and the selfie that turn starts is finished off-turn by a task that
inherits the scope it was created in. Threading an argument through all of that
would touch every signature in the reply path and still miss the deferred work.

Nothing here is required: a writer with no scope in view records `None`, which
reads as "host" and is exactly right for a call nobody asked for.
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Iterator, Optional

#: The kinds of work that reach a model or a tool. `kind` is what the debug page
#: filters on, so these names are a contract with the frontend.
CHAT_TURN = "chat_turn"     # a committed conversational turn (corpus/turns.jsonl)
TICK = "tick"               # anything inside one MindLoop tick
AMBIENT = "ambient"         # self-talk, a timer announcement
GREETING = "greeting"       # the arrival hello
COMPOSE = "compose"         # a reach-out being written (gate 2 already crossed)
UTILITY = "utility"         # the small model, unattributed
DREAM = "dream"             # DREAM-state consolidation
GOAL_WORK = "goal_work"     # deliberate work on an open goal
KNOWLEDGE = "knowledge"     # ingesting something you dropped on her shelf
HOST = "host"               # no scope was in view


@dataclass(frozen=True, slots=True)
class Origin:
    """What one unit of work is, and what it belongs to."""
    corr_id: str
    kind: str = HOST
    session_id: str | None = None
    turn_index: int | None = None
    tick_id: str | None = None
    channel: str | None = None
    client_id: str | None = None
    #: A turn she started rather than one she is answering. The greeting and the
    #: ambient injector ride the same route as a real reply (voice_ws `run`), so
    #: `kind` alone cannot tell them apart — this is how they say so.
    proactive: bool = False

    @property
    def answering(self) -> bool:
        """Is this work part of answering something the user said?

        Work started inside a turn and finished off it — a selfie, a research
        read — inherits the answer: a photo you asked for is a reply, however
        many minutes later it lands, and must not be marked "she spoke first"
        (§15.5).
        """
        return self.kind == CHAT_TURN and not self.proactive

    def stamp(self) -> dict:
        """The fields a log line carries. Written flat rather than nested so the
        debug page can filter a JSONL log on one key without unpacking."""
        return {"corr_id": self.corr_id, "origin": self.kind,
                "session_id": self.session_id, "turn_index": self.turn_index,
                "tick_id": self.tick_id}


#: Absent by default: most code paths have no origin and must not invent one.
_current: ContextVar[Optional[Origin]] = ContextVar("yurios_origin", default=None)

#: What a writer stamps when nothing is in scope. Constant, so a log line always
#: has the same shape and the frontend never has to test for missing keys.
_UNSCOPED = {"corr_id": None, "origin": HOST, "session_id": None,
             "turn_index": None, "tick_id": None}


def new_corr_id() -> str:
    return "c-" + uuid.uuid4().hex[:12]


def current() -> Origin | None:
    return _current.get()


def answering() -> bool:
    """Whether the scope in view is a turn answering the user. No scope at all
    is a call nobody asked for, which is exactly not that."""
    origin = _current.get()
    return origin is not None and origin.answering


def stamp() -> dict:
    """The correlation fields for whatever is in scope — the one call a log
    writer makes. Never raises, never absent, always the same five keys."""
    origin = _current.get()
    return origin.stamp() if origin is not None else dict(_UNSCOPED)


@contextmanager
def scope(**overrides) -> Iterator[Origin]:
    """Enter a unit of work.

    A fresh `corr_id` is minted only when none is in scope, so nesting refines
    rather than restarts: a DREAM act inside a tick becomes `kind="dream"` while
    keeping the tick's id, and both records still join to the tick that caused
    them. Pass `corr_id=` explicitly to force a new one.
    """
    parent = _current.get()
    if parent is None:
        overrides.setdefault("corr_id", new_corr_id())
        origin = Origin(**overrides)
    else:
        origin = replace(parent, **overrides)
    token = _current.set(origin)
    try:
        yield origin
    finally:
        _current.reset(token)
