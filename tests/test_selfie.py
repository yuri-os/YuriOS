"""Her camera (SPEC §7.6) — the SelfieLab's start-don't-await realisation, the
forge builder's degrade rule, the guard's price on the shutter, and the tool
loop wiring. Entirely offline: the mock backend renders placeholder cards."""
from __future__ import annotations

import asyncio
import json

import pytest

from yurios.world.selfies import SelfieLab, build_forge
from yurios.world.tools.guard import Guard

from .conftest import ScriptedChat, collect, make_toolbrain


class Recorder:
    """post_message + speak_ambient doubles the lab talks to."""

    def __init__(self, busy: bool = False):
        self.posts: list[dict] = []
        self.cues: list[str] = []
        self.busy = busy

    def post(self, role, text, *, image_url=None, proactive=False):
        entry = {"role": role, "text": text,
                 "image_url": image_url, "proactive": proactive}
        self.posts.append(entry)
        return entry

    async def speak(self, cue: str) -> bool:
        if self.busy:
            return False                       # a turn in flight (§8.4)
        self.cues.append(cue)
        return True


async def settle(lab: SelfieLab):
    await asyncio.gather(*lab._tasks, return_exceptions=True)


@pytest.fixture
def forge(cfg):
    forge, status = build_forge(cfg)
    assert status == "mock"                    # the cfg fixture pins mock (§13)
    return forge


async def test_the_shot_lands_in_the_chat_and_on_disk(cfg, clock, forge):
    rec = Recorder()
    lab = SelfieLab(forge, clock=clock, post=rec.post, speak=rec.speak)
    lab.start({"id": "abc123", "scene": "window", "mood": "happy",
               "status": "started"})
    await settle(lab)

    (post,) = rec.posts
    assert post["role"] == "assistant" and post["proactive"] is True
    assert post["image_url"].startswith("/selfies/") \
       and post["image_url"].endswith("-abc123.png")
    png = cfg.selfie_dir / post["image_url"].removeprefix("/selfies/")
    assert png.is_file() and png.read_bytes()[:4] == b"\x89PNG"
    # the provenance sidecar travels with the file (→ ch. 26)
    meta = json.loads(png.with_suffix(".json").read_text())
    assert meta["template"]["scene"] == "window"
    assert meta["template"]["mood"] == "happy"
    # …and she offers one line about it, since she was free (§8.3)
    assert rec.cues and "chat" in rec.cues[0]


async def test_wardrobe_rides_the_contract_and_defaults_to_everyday(cfg, clock, forge):
    """The asked-for wardrobe reaches the forge (a named tier, or free-form —
    the contract refuses nothing); a contract without one stays everyday."""
    rec = Recorder()
    lab = SelfieLab(forge, clock=clock, post=rec.post, speak=rec.speak)
    lab.start({"id": "w1", "scene": "bed", "mood": "tender",
               "wardrobe": "dressy", "status": "started"})
    lab.start({"id": "w2", "scene": "window", "status": "started"})
    await settle(lab)

    tiers = {}
    for post in rec.posts:
        png = cfg.selfie_dir / post["image_url"].removeprefix("/selfies/")
        meta = json.loads(png.with_suffix(".json").read_text())
        tiers[post["image_url"].split("-")[-1]] = meta["template"]["wardrobe"]
    assert tiers == {"w1.png": "dressy", "w2.png": "everyday"}


async def test_announce_is_dropped_when_she_is_busy_but_the_photo_stays(cfg, clock, forge):
    rec = Recorder(busy=True)
    lab = SelfieLab(forge, clock=clock, post=rec.post, speak=rec.speak)
    lab.start({"id": "b2", "scene": None, "mood": None, "status": "started"})
    await settle(lab)
    assert rec.posts and rec.posts[0]["image_url"]      # the photo landed anyway
    assert rec.cues == []                               # she never talks over you


async def test_a_failed_render_is_a_quiet_message_never_a_crash(cfg, clock):
    class BrokenForge:
        out_dir = cfg.selfie_dir

        def selfie(self, **kw):
            raise RuntimeError("api down")

    rec = Recorder()
    lab = SelfieLab(BrokenForge(), clock=clock, post=rec.post, speak=rec.speak)
    lab.start({"id": "x", "status": "started"})
    await settle(lab)
    (post,) = rec.posts
    assert post["image_url"] is None and "didn't come out" in post["text"]
    assert rec.cues == []


class SpyParker:
    """Records park/restore around the render (world/vram.LLMParker's shape)."""

    def __init__(self):
        self.events: list[str] = []

    def parked(self):
        from contextlib import contextmanager

        @contextmanager
        def _ctx():
            self.events.append("park")
            try:
                yield
            finally:
                self.events.append("restore")
        return _ctx()


async def test_a_render_borrows_the_llms_vram_and_returns_it(cfg, clock, forge):
    rec = Recorder()
    parker = SpyParker()
    lab = SelfieLab(forge, clock=clock, post=rec.post, speak=rec.speak,
                    parker=parker)
    lab.start({"id": "p1", "scene": "window", "status": "started"})
    await settle(lab)
    assert parker.events == ["park", "restore"]
    assert rec.posts and rec.posts[0]["image_url"]      # the photo still landed


async def test_a_failed_render_still_restores_the_llm(cfg, clock):
    class BrokenForge:
        out_dir = cfg.selfie_dir

        def selfie(self, **kw):
            raise RuntimeError("CUDA out of memory")

    rec = Recorder()
    parker = SpyParker()
    lab = SelfieLab(BrokenForge(), clock=clock, post=rec.post, speak=rec.speak,
                    parker=parker)
    lab.start({"id": "p2", "status": "started"})
    await settle(lab)
    assert parker.events == ["park", "restore"]         # finally, always
    assert "didn't come out" in rec.posts[0]["text"]


async def test_a_borrowed_render_releases_the_pipeline_before_the_restore(cfg, clock):
    """The cached pipeline and the re-pinning chat model are the two things that
    don't fit the card at once — a render on borrowed VRAM must free its pipe
    first, or the restore 500s with the card still full."""
    from contextlib import contextmanager

    class PipeForge:
        out_dir = cfg.selfie_dir

        class backend:                       # the diffusers backend's shape
            torn: list[str] = []

            @staticmethod
            def _teardown():
                PipeForge.backend.torn.append("teardown")

        def selfie(self, **kw):
            from yurios.forge.types import ImageResult
            return ImageResult.new(b"\x89PNG fake", "mock", model="m", seed=1)

    class BorrowParker:
        def __init__(self):
            self.events: list[str] = []

        def parked(self):
            @contextmanager
            def _ctx():
                self.events.append("park")
                try:
                    yield True               # this render borrowed the VRAM
                finally:
                    self.events.append("restore")
            return _ctx()

    rec = Recorder()
    parker = BorrowParker()
    lab = SelfieLab(PipeForge(), clock=clock, post=rec.post, speak=rec.speak,
                    parker=parker)
    lab.start({"id": "p3", "status": "started"})
    await settle(lab)
    assert PipeForge.backend.torn == ["teardown"]   # freed BEFORE the restore
    assert parker.events == ["park", "restore"]


async def test_a_parked_render_waits_for_a_quiet_moment(cfg, clock, forge):
    """start-don't-await means the render spawns while the turn that asked is
    still streaming — and that stream reads from the very LM Studio model the
    parker would evict. An eviction mid-turn kills the stream and her reply
    vanishes from the chat (draft_cancel). So a render that will park waits
    for the world to go quiet first; one that fits alongside her brain (or a
    backend with no parker) starts at once."""
    events: list[str] = []
    gate = asyncio.Event()                   # unset = a turn is in flight

    class NeedyParker:
        def applicable(self):
            return True

        def needs_park(self):
            return True

        def parked(self):
            from contextlib import contextmanager

            @contextmanager
            def _ctx():
                events.append("park")
                try:
                    yield True
                finally:
                    events.append("restore")
            return _ctx()

    async def quiet():
        await gate.wait()
        events.append("quiet")

    rec = Recorder()
    lab = SelfieLab(forge, clock=clock, post=rec.post, speak=rec.speak,
                    parker=NeedyParker(), quiet=quiet)
    lab.start({"id": "q1", "status": "started"})
    for _ in range(100):                     # give the job every chance to run
        await asyncio.sleep(0)
        if "park" in events:
            break
    assert "park" not in events              # no eviction while she's talking
    gate.set()                               # the turn ended
    await settle(lab)
    assert events == ["quiet", "park", "restore"]
    assert rec.posts and rec.posts[0]["image_url"]      # the photo still landed


async def test_an_unparked_render_does_not_wait(cfg, clock, forge):
    """The quiet gate is the parker's price, not the camera's: a render with
    free VRAM to spare starts immediately, turn or no turn."""
    rendered: list[str] = []

    class ComfortableParker:
        def applicable(self):
            return True

        def needs_park(self):
            return False                   # fits alongside her brain

        def parked(self):
            from contextlib import contextmanager

            @contextmanager
            def _ctx():
                rendered.append("render")
                yield False
            return _ctx()

    async def quiet():
        raise AssertionError("an unparked render must never wait")

    rec = Recorder()
    lab = SelfieLab(forge, clock=clock, post=rec.post, speak=rec.speak,
                    parker=ComfortableParker(), quiet=quiet)
    lab.start({"id": "q2", "status": "started"})
    await settle(lab)
    assert rendered == ["render"]
    assert rec.posts and rec.posts[0]["image_url"]


def test_no_key_degrades_openrouter_to_mock_loudly(cfg, tmp_path, monkeypatch, caplog):
    """The voice-fakes philosophy (B2 §3): she still works, the log names the fix."""
    monkeypatch.delenv("OPENROUTER_TOKEN", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "nohome")
    cfg = cfg.model_copy(update={"selfie_backend": "openrouter",
                                 "openrouter_api_key": ""})
    with caplog.at_level("WARNING"):
        forge, status = build_forge(cfg)
    assert status.startswith("mock") and "no key" in status
    assert any("OPENROUTER_API_KEY" in r.message for r in caplog.records)
    assert forge.backend.name == "mock"


def test_a_configured_key_keeps_the_real_camera(cfg):
    cfg = cfg.model_copy(update={"selfie_backend": "openrouter",
                                 "openrouter_api_key": "sk-or-test"})
    forge, status = build_forge(cfg)
    assert status == "openrouter" and forge.backend.model == cfg.selfie_model


def test_the_guard_prices_the_shutter(cfg, clock):
    """take_selfie is allowlisted only when the camera exists, and rate-limited
    like every hand (§7.3) — images are expensive."""
    guard = Guard(rates_per_min={"take_selfie": 2},
                  log_dir=cfg.tool_log_dir, clock=clock)
    assert guard.check("take_selfie") == (True, "")
    assert guard.check("take_selfie") == (True, "")
    ok, reason = guard.check("take_selfie")
    assert not ok and reason == "rate limit"
    clock.advance(60)
    assert guard.check("take_selfie")[0]


async def test_the_tool_loop_starts_the_lab(cfg, guard, timers, controller, clock):
    """[[take_selfie …]] in the stream → guard → runner → _realise → lab.start;
    the turn finishes long before any pixels exist (start-don't-await, §7.6)."""
    from yurios.world.tools.fakes import FakeToolRunner

    class SpyLab:
        def __init__(self):
            self.started: list[dict] = []

        def start(self, contract):
            self.started.append(contract)

    guard._rates["take_selfie"] = 2            # the fixture's allowlist + the camera
    guard._buckets["take_selfie"] = {"tokens": 2.0, "at": clock.now()}
    lab = SpyLab()
    chat = ScriptedChat([
        ['Hold on — one second. ', '[[take_selfie {"scene": "window"}]]'],
        ['There, taking it now~'],
    ])
    brain = make_toolbrain(cfg, guard, timers, controller, chat,
                           runner=FakeToolRunner(), selfies=lab)
    spoken = "".join(await collect(
        brain._stream_with_tools([{"role": "user", "content": "selfie?"}], [])))
    assert "taking it now" in spoken           # the turn completed
    (contract,) = lab.started                  # …and the lab got the contract
    assert contract["scene"] == "window" and contract["status"] == "started"
