"""The brain contract is real, and the fakes still match it.

`world/brain_protocol.py` replaced ten `hasattr` checks with two named shapes.
That is only worth something if something checks that the classes are those
shapes — otherwise the contract is a comment, and the failure it was written to
prevent (rename a method, the `hasattr` silently goes False, the feature turns
itself off) becomes: rename a method, the `isinstance` silently goes False, the
feature turns itself off. Same bug, longer walk.

So this file is the check. It matters more than usual here because
`world.main`, `world.brain`, `desktop.brain` and `mind.loop` are all on mypy's
baseline list, so the typechecker is not reading any of them: at present these
assertions are the only thing standing between a renamed seam and a mind that
quietly stops being handed its stores.

Signatures as well as names, because `isinstance` against a runtime-checkable
Protocol tests only that the attributes exist. A `set_goals` that grew a second
required argument would pass `isinstance` and fail at the call.
"""
from __future__ import annotations

import inspect

from yurios.desktop.voice.backends.fakes import FakeBrain
from yurios.world.brain_protocol import AutonomousBrain, ConversationalBrain


def _protocol_methods(proto) -> list[str]:
    return sorted(n for n in getattr(proto, "__protocol_attrs__", ())
                  if callable(getattr(proto, n, None)))


def _assert_signatures_match(proto, obj, *, skip=()) -> None:
    """Every method the protocol declares takes what the protocol says it takes."""
    for name in _protocol_methods(proto):
        if name in skip:
            continue
        want = inspect.signature(getattr(proto, name))
        got = inspect.signature(getattr(obj, name))
        want_params = [p for p in want.parameters if p != "self"]
        got_params = [p for p in got.parameters if p != "self"]
        assert got_params == want_params, (
            f"{type(obj).__name__}.{name}{got} does not match the contract "
            f"{proto.__name__}.{name}{want} — the protocol and the class have "
            f"drifted, and `isinstance` cannot see it")


def test_the_tool_brain_is_an_autonomous_brain(cfg, guard, timers, controller):
    """The real one. If this fails, the mind is about to stop being wired to the
    conversation — no import breaks and no other test necessarily notices."""
    from tests.conftest import CannedChat, make_toolbrain
    brain = make_toolbrain(cfg, guard, timers, controller, CannedChat())

    assert isinstance(brain, AutonomousBrain)
    assert isinstance(brain, ConversationalBrain)
    _assert_signatures_match(ConversationalBrain, brain)
    _assert_signatures_match(AutonomousBrain, brain)


def test_the_fake_brain_can_hold_a_conversation_and_nothing_more():
    """`FakeBrain` is deliberately the smaller shape, and the distinction is the
    whole reason the two protocols are separate: a route test injects it to run a
    turn with no model and no Vault, and `Runtime` has to notice and skip the
    mind rather than half-wire one."""
    fake = FakeBrain()

    assert isinstance(fake, ConversationalBrain)
    assert not isinstance(fake, AutonomousBrain)
    _assert_signatures_match(ConversationalBrain, fake)


def test_the_autonomous_contract_is_the_conversational_one_plus_the_seams():
    """A guard on the guard: if `AutonomousBrain` ever stopped extending
    `ConversationalBrain`, `Runtime.autonomous` would start being true for a
    brain that cannot hold a turn."""
    conversational = set(_protocol_methods(ConversationalBrain))
    autonomous = set(_protocol_methods(AutonomousBrain))

    assert conversational < autonomous
    assert autonomous - conversational == {
        "set_prompt_log", "set_tools", "set_world", "set_knowledge",
        "set_workspace", "set_goals", "set_selfedit"}
    assert "state" in AutonomousBrain.__protocol_attrs__
