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


def test_the_camera_inherits_the_idle_unload_timeout(cfg, clock):
    """`build_camera` is the production constructor: a lab built by hand
    defaults to never timing out, so tests of the warm pipeline don't arm a
    one-hour asyncio task. The house knob has to actually reach the lab."""
    lab, _ = runtime.build_camera(
        _half_built(cfg.model_copy(update={"selfie_backend": "mock",
                                           "selfie_unload_after_s": 900.0}),
                    clock))
    assert lab is not None
    assert lab.unload_after_s == 900.0


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


def test_building_the_brain_does_not_wait_for_the_embedder(monkeypatch):
    """The sentence-transformers load is the slow part of a cold boot, and it
    used to sit in front of her hands and mind. Construction now returns
    while the boot panel is still on `loading` (SPEC §2.4)."""
    import threading
    import time

    from yurios.world.boot import BootBoard

    release = threading.Event()

    class Slow:
        dim = 384

        def __init__(self):
            self.ready = False

        def ensure_ready(self):
            assert release.wait(timeout=5)
            self.ready = True

    monkeypatch.setattr("yurios.app.main._default_embedder",
                        lambda cfg, wait=False: Slow())
    monkeypatch.setattr(runtime, "pin_lmstudio", lambda *a, **k: None)
    monkeypatch.setattr(runtime.ToolBrain, "build",
                        classmethod(lambda cls, *a, **k: "brain"))

    rt = types.SimpleNamespace(
        cfg=conf(embed_model="BAAI/bge-small-en-v1.5", embed_dim=384,
                 character_id="adia"),
        boot=BootBoard(),
        guard=None, timers=None, controller=None, selfies=None, research=None,
        model_configured=True,
    )
    assert runtime.build_brain(rt, chat_model=None, utility_model=None,
                               embedder=None) == "brain"
    embed = next(s for s in rt.boot.snapshot()["services"] if s["key"] == "embed")
    assert embed["state"] == "loading"
    release.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        embed = next(s for s in rt.boot.snapshot()["services"] if s["key"] == "embed")
        if embed["state"] == "ready":
            break
        time.sleep(0.02)
    assert embed["state"] == "ready"
    assert "384d" in embed["detail"]


def test_the_embedder_load_starts_after_the_brain_is_built(monkeypatch):
    """Found live: five characters, a sixty-second boot reported as two and a
    half minutes. Both are module imports — `sentence_transformers` on the
    embedder's thread, litellm behind `build_chat_model` on this one — and
    CPython locks per module, so a thread is no escape from one. Kicked off
    first, the load was something the build then sat inside `import litellm`
    waiting for. Ordering them is the fix, so the order is the test (§2.4)."""
    from yurios.world.boot import BootBoard

    events = []

    class Embedder:
        dim = 384
        ready = False

        def ensure_ready(self):
            events.append("load begins")

    def build(cls, *a, **k):
        events.append("brain built")
        return "brain"

    monkeypatch.setattr("yurios.app.main._default_embedder",
                        lambda cfg, wait=False: Embedder())
    monkeypatch.setattr(runtime, "pin_lmstudio", lambda *a, **k: None)
    monkeypatch.setattr(runtime.ToolBrain, "build", classmethod(build))
    # the announcing thread would race the assertion; run it where we can see it
    monkeypatch.setattr(runtime.threading, "Thread",
                        lambda target, **kw: types.SimpleNamespace(start=target))

    rt = types.SimpleNamespace(
        cfg=conf(embed_model="BAAI/bge-small-en-v1.5", embed_dim=384,
                 character_id="adia"),
        boot=BootBoard(),
        guard=None, timers=None, controller=None, selfies=None, research=None,
        model_configured=True,
    )
    assert runtime.build_brain(rt, chat_model=None, utility_model=None,
                               embedder=None) == "brain"
    assert events == ["brain built", "load begins"]
