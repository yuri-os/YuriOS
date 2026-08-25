"""How a Runtime is assembled (world/runtime.py) — the parts, on their own.

These four builders used to be the middle of a 273-line constructor, which
meant the only way to ask "does turning the camera off actually take the hand
away?" was to build a whole Runtime — an embedding model, a Vault, a boot
panel. So nothing asked. The rules they carry are small and load-bearing, and
this is where they are now checked directly.
"""
from __future__ import annotations

import types

import pytest

pytest.importorskip("fastapi")
from yurios.world import runtime                                   # noqa: E402
from yurios.world.config import Config                             # noqa: E402


def conf(**kw) -> Config:
    return Config(_env_file=None, **kw)


# ---- the allowlist behind her Guard (SPEC §7.3) ----

def test_absence_is_the_refusal_not_a_rate_of_zero():
    """A hand she may not use is not in the dict at all — the same rule the tool
    server follows with `list_tools`. A zero would be a hand that fails when she
    reaches for it, which is the thing §7.3 is written to avoid."""
    rates = runtime.tool_rates(conf(selfie_backend="off", search_backend="off",
                                    workspace_enabled=False, skills_enabled=False,
                                    mind_enabled=False))
    assert set(rates) == {"set_timer", "play_music"}
    assert all(v > 0 for v in rates.values())


def test_the_camera_brings_two_hands_and_takes_them_back():
    on = runtime.tool_rates(conf(selfie_backend="mock"))
    off = runtime.tool_rates(conf(selfie_backend="off"))
    assert {"take_selfie", "show_picture"} <= set(on)
    assert not {"take_selfie", "show_picture"} & set(off)


def test_the_web_hands_arrive_and_leave_together():
    """§7.7: searching with no way to read what you found is half a capability."""
    on = runtime.tool_rates(conf(search_backend="fake"))
    off = runtime.tool_rates(conf(search_backend="off"))
    web = {"web_search", "read_page", "research"}
    assert web <= set(on)
    assert not web & set(off)


def test_the_desk_and_the_skills_are_separately_switched():
    desk_only = runtime.tool_rates(conf(workspace_enabled=True, skills_enabled=False))
    skills_only = runtime.tool_rates(conf(workspace_enabled=False, skills_enabled=True))
    from yurios.mind.workspace import DESK_TOOLS, SKILL_TOOLS
    assert set(DESK_TOOLS) <= set(desk_only)
    assert not set(SKILL_TOOLS) & set(desk_only)
    assert set(SKILL_TOOLS) <= set(skills_only)
    assert not set(DESK_TOOLS) & set(skills_only)


def test_the_self_edit_door_needs_a_mind_to_be_a_door():
    """§23: the queue `propose_edit` writes into is only read where the loop
    runs, so without one the hand is absent rather than writing into a drawer
    nobody opens. And it is rationed hardest of all — this is the hand that
    reaches at who she is."""
    with_mind = runtime.tool_rates(conf(mind_enabled=True))
    without = runtime.tool_rates(conf(mind_enabled=False))
    assert "propose_edit" in with_mind
    assert "propose_edit" not in without
    assert with_mind["propose_edit"] < min(
        v for k, v in with_mind.items() if k != "propose_edit")


# ---- the camera and the reading desk ----

def _half_built(cfg, clock):
    """The parts of a Runtime the two workers' builders are allowed to touch.

    Deliberately a stub and not a Runtime: if a builder grows a reach into
    something else, it fails here rather than quietly coupling the two.
    """
    from yurios.kernel.hub import EventHub
    from yurios.world.vram import shared_gate
    hub = EventHub()

    async def _never():
        return None

    return types.SimpleNamespace(
        cfg=cfg, clock=clock, hub=hub, mind=None, park_gate=shared_gate(),
        post_message=lambda *a, **k: None,
        speak_ambient=lambda cue: None,
        wait_turns_idle=_never,
        visual_situation=lambda: "",
        post_signal=lambda *a, **k: None)


def test_no_camera_is_no_lab_and_says_so(cfg, clock):
    lab, status = runtime.build_camera(
        _half_built(cfg.model_copy(update={"selfie_backend": "off"}), clock))
    assert lab is None and status == "off"


def test_a_camera_is_built_and_reports_the_backend_that_landed(cfg, clock):
    lab, status = runtime.build_camera(
        _half_built(cfg.model_copy(update={"selfie_backend": "mock"}), clock))
    assert lab is not None
    assert status and status != "off"


def test_no_search_is_no_desk_and_says_so(cfg, clock):
    desk, status = runtime.build_reading(
        _half_built(cfg.model_copy(update={"search_backend": "off"}), clock))
    assert desk is None and status == "off"


def test_the_desk_asks_for_the_knowledge_store_rather_than_holding_one(cfg, clock):
    """It belongs to the MindLoop, which is built later and not at all when she
    is mindless — so the desk gets a getter it calls when it needs one, and
    building it against a runtime with no mind must not raise."""
    rt = _half_built(cfg.model_copy(update={"search_backend": "fake"}), clock)
    desk, status = runtime.build_reading(rt)
    assert desk is not None and status == "fake"
    assert desk.knowledge() is None                    # no mind yet, and that is fine
    rt.mind = types.SimpleNamespace(knowledge="the store")
    assert desk.knowledge() == "the store"             # …and it follows the mind
