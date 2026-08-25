"""What the runtime needs a brain to be, written down (SPEC §2, §15).

Two things hold the name "brain" here. `BrainAdapter` (`desktop/brain.py`) can
hold a conversation: sessions, a streamed reply, the post-turn commit. `ToolBrain`
subclasses it and adds hands and the seams the mind hands its stores through. A
room needs the first; the tick loop needs the second. And in the tests a third
thing turns up — `FakeBrain`, which is deliberately only the first, so a route
test can run a turn with no model, no Vault and no mind anywhere near it.

Until this module, the difference between the three was asked ten times, one
attribute at a time: `hasattr(brain, "set_world")`, `hasattr(brain, "state")`,
`hasattr(brain, "set_tools")` — across `world/main.py` and `mind/loop.py`, each
site re-deciding what a brain is. That is a bad way to hold a contract for a
specific reason: nothing anywhere said what the whole of it was, so renaming a
method broke no import and failed no check. The `hasattr` simply became False,
the branch quietly took its other arm, and the feature it guarded turned itself
off. A fake that drifted from the real class drifted silently, in the direction
of doing less.

So: name the two shapes, ask once, and let a test check that the classes really
are what they claim. `isinstance` against a runtime-checkable Protocol tests for
the members and not their signatures, which is why `tests/test_brain_protocol.py`
compares those too — between them they catch a rename, a dropped method and a
changed parameter list.
"""
from __future__ import annotations

from typing import Any, AsyncIterator, Optional, Protocol, runtime_checkable


@runtime_checkable
class ConversationalBrain(Protocol):
    """Enough to hold a conversation — what a room, a channel and a voice socket
    need, and the whole of what the test fakes implement."""

    def resolve_session(self, session_id: str | None) -> str:
        """A live session id: the client's if it is valid and known, else a new one."""
        ...

    def stream_reply(self, session_id: str, text: str,
                     image: str | None = None) -> AsyncIterator[str]:
        """One turn, streamed. `image` is a picture sent with the line, or None."""
        ...

    def stream_greeting(self, session_id: str) -> AsyncIterator[str]:
        """The continuity opener. Not a turn, so it is never persisted."""
        ...

    async def persist(self, session_id: str, user_text: str, reply: str) -> None:
        """Commit the turn: corpus line, journal, index, USER.md, one git commit."""
        ...

    def abandon(self, session_id: str) -> None:
        """The other close-out — this turn never happened (barge-in, or an error).

        A failed turn leaves no trace, which is why this is on the contract rather
        than an implementation detail: every brain has to be able to roll one back."""
        ...

    def cold_open(self) -> str | None:
        """The authored first message while she has not met anyone yet, or None."""
        ...


@runtime_checkable
class AutonomousBrain(ConversationalBrain, Protocol):
    """…and what an always-on mind needs on top.

    `state` is the Build #1 `AppState` the loop reads its embedder, its store and
    its models out of. The `set_*` methods are the late-bound seams: the mind
    builds its stores, then hands each one to the brain so that the *conversational*
    prompt carries them too — which is the whole point of §19.2, §20.2 and §22.
    The talking self and the intending self stop being two people.

    Late-bound rather than constructor arguments because the brain exists before
    the mind does: a room works with `MIND_ENABLED=false`, and it must, so the
    stores cannot be required to build one.
    """

    state: Any                       # app.core.state.AppState

    def set_prompt_log(self, prompt_log: Any) -> None:
        """The sink that records what she was actually asked (SPEC §24.2)."""
        ...

    def set_tools(self, runner: Optional[Any], specs: list[Any]) -> None:
        """The discovered hands (SPEC §7.2). None or empty means no hands here."""
        ...

    def set_world(self, world: Any) -> None:
        """The mind's `WorldModelStore` — the §19.2 seam swap."""
        ...

    def set_knowledge(self, store: Any) -> None:
        """The shelf, into the prompt's §20.2 knowledge slot."""
        ...

    def set_workspace(self, workspace: Any, skills: Any, on_write: Any = None) -> None:
        """Her desk and her skills (SPEC §34.3)."""
        ...

    def set_goals(self, store: Any) -> None:
        """Her standing list, into the conversational prompt (SPEC §22)."""
        ...

    def set_selfedit(self, selfedit: Any) -> None:
        """The §23 self-edit door, so `propose_edit` has somewhere to land."""
        ...
