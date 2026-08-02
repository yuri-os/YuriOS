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

    def post(self, role, text, *, image_url=None, proactive=False, **metadata):
        entry = {"role": role, "text": text,
                 "image_url": image_url, "proactive": proactive}
        entry.update(metadata)
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


async def test_selfie_status_is_correlated_and_cancellable(cfg, clock, forge):
    rec = Recorder()
    events = []
    lab = SelfieLab(
        forge, clock=clock, post=rec.post, speak=rec.speak,
        notify=lambda type_, payload: events.append({"type": type_, **payload}))
    contract = {"id": "stop-me", "status": "started",
                "_client_id": "browser-1", "_channel": "web"}
    lab.start(contract)
    assert lab.active_ids("browser-1") == ["stop-me"]
    assert events == [{"type": "selfie_status", "id": "stop-me",
                       "state": "started", "client_id": "browser-1"}]

    assert await lab.cancel(["stop-me"]) == ["stop-me"]
    await settle(lab)
    assert events[-1] == {"type": "selfie_status", "id": "stop-me",
                          "state": "cancelled", "client_id": "browser-1"}
    assert rec.posts == []


async def test_cancelling_waits_for_a_running_render_thread(cfg, clock):
    """Cancellation suppresses the result, but a worker thread is not killable;
    the coroutine must stay alive until it exits so VRAM cannot be handed back
    to the LLM early."""
    import threading

    started = threading.Event()
    release = threading.Event()

    class SlowForge:
        out_dir = cfg.selfie_dir

        def selfie(self, **kw):
            started.set()
            release.wait(timeout=5)
            raise RuntimeError("discard this cancelled result")

    rec = Recorder()
    lab = SelfieLab(SlowForge(), clock=clock, post=rec.post, speak=rec.speak)
    lab.start({"id": "running", "status": "started"})
    assert await asyncio.to_thread(started.wait, 2)

    stopping = asyncio.create_task(lab.cancel(["running"]))
    await asyncio.sleep(0.02)
    assert not stopping.done()
    stopping_again = asyncio.create_task(lab.cancel(["running"]))
    await asyncio.sleep(0.02)
    assert not stopping_again.done()
    release.set()
    assert await stopping == ["running"]
    assert await stopping_again == ["running"]
    assert rec.posts == []


async def test_cancelling_while_waiting_for_quiet_reopens_the_gate(cfg, clock, forge):
    class Gate:
        closed = False

        def close(self):
            self.closed = True

        def open(self):
            self.closed = False

    class Parker:
        gate = Gate()

        def applicable(self):
            return True

        def needs_park(self):
            return True

    never_quiet = asyncio.Event()
    lab = SelfieLab(forge, clock=clock, post=Recorder().post,
                    speak=Recorder().speak, parker=Parker(),
                    quiet=never_quiet.wait)
    lab.start({"id": "waiting", "status": "started",
               "_client_id": "browser-1"})
    for _ in range(50):
        if lab.parker.gate.closed:
            break
        await asyncio.sleep(0)
    assert lab.parker.gate.closed
    assert await lab.cancel([], client_id="browser-1") == ["waiting"]
    assert not lab.parker.gate.closed


async def test_selfie_cancel_cannot_cross_request_owners(cfg, clock, forge):
    rec = Recorder()
    lab = SelfieLab(forge, clock=clock, post=rec.post, speak=rec.speak)
    lab.start({"id": "owned", "status": "started", "_client_id": "owner"})
    assert await lab.cancel(["owned"], client_id="other") == []
    assert lab.active_ids("owner") == ["owned"]
    await lab.cancel([], client_id="owner")


async def test_selfie_jobs_are_serialized(cfg, clock, forge):
    import threading

    first_started = threading.Event()
    release_first = threading.Event()
    starts = []

    class SerialForge:
        out_dir = forge.out_dir

        def selfie(self, **kw):
            starts.append(kw.get("scene"))
            if len(starts) == 1:
                first_started.set()
                release_first.wait(timeout=5)
            return forge.selfie(**kw)

        def _write_provenance(self, *args, **kwargs):
            return forge._write_provenance(*args, **kwargs)

    rec = Recorder()
    lab = SelfieLab(SerialForge(), clock=clock, post=rec.post, speak=rec.speak)
    lab.start({"id": "one", "scene": "window", "status": "started"})
    lab.start({"id": "two", "scene": "bed", "status": "started"})
    assert await asyncio.to_thread(first_started.wait, 2)
    await asyncio.sleep(0.02)
    assert starts == ["window"]
    release_first.set()
    await settle(lab)
    assert starts == ["window", "bed"]
    assert len(rec.posts) == 2


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


async def test_her_own_words_reach_the_render_and_the_ledger(cfg, clock, forge):
    """`look` is the field she describes a whole picture in. It has to survive
    the whole way — contract, lab, forge, provenance — or she is back to five
    dropdowns."""
    rec = Recorder()
    words = "curled on the window seat, sleeves over my hands, grinning sideways"
    lab = SelfieLab(forge, clock=clock, post=rec.post, speak=rec.speak)
    lab.start({"id": "lk", "look": words, "framing": "close",
               "avoid": "no hats", "status": "started"})
    await settle(lab)

    png = cfg.selfie_dir / rec.posts[-1]["image_url"].removeprefix("/selfies/")
    meta = json.loads(png.with_suffix(".json").read_text())
    assert meta["template"]["look"] == words
    assert words in meta["prompt"]                 # hers, in the actual prompt
    assert meta["template"]["framing"] == "close"
    assert "no hats" in meta["negative"]           # her own "not like that"
    # and her words are what she is cued to speak about, not two slot names
    assert words in rec.cues[0]


async def test_the_situation_fills_only_a_real_gap(cfg, clock, forge):
    """The world fills in what she didn't say — and shuts up the moment she
    says where she is, because appending rain-on-the-glass to her sunlit beach
    is worse than adding nothing."""
    rec = Recorder()
    lab = SelfieLab(forge, clock=clock, post=rec.post, speak=rec.speak,
                    situation=lambda: "It is night, rain on the glass.")
    lab.start({"id": "gap", "mood": "happy", "status": "started"})
    lab.start({"id": "placed", "look": "on a sunlit beach", "status": "started"})
    await settle(lab)

    prompts = {}
    for post in rec.posts:
        png = cfg.selfie_dir / post["image_url"].removeprefix("/selfies/")
        meta = json.loads(png.with_suffix(".json").read_text())
        prompts[post["image_url"].split("-")[-1]] = meta["prompt"]
    assert "rain on the glass" in prompts["gap.png"]
    assert "rain on the glass" not in prompts["placed.png"]
    assert "sunlit beach" in prompts["placed.png"]


async def test_a_broken_situation_costs_a_photo_nothing(cfg, clock, forge):
    def boom() -> str:
        raise RuntimeError("the world model fell over")

    rec = Recorder()
    lab = SelfieLab(forge, clock=clock, post=rec.post, speak=rec.speak,
                    situation=boom)
    lab.start({"id": "s1", "mood": "happy", "status": "started"})
    await settle(lab)
    assert rec.posts and rec.posts[-1]["image_url"]     # the photo still lands


def test_the_camera_renders_her_and_not_the_shipped_character(cfg, tmp_path):
    """The bug this whole seam exists to close: one hardcoded yuri.yaml meant
    every character wore Yuri's face and the sidecar called the photo hers."""
    from yurios.characters.appearance import write_appearance
    path = write_appearance(tmp_path / "appearance.yaml", "Lumina",
                            "a petite young woman with silver-white hair")
    forge, _ = build_forge(cfg.model_copy(update={"selfie_character": str(path)}))
    assert forge.character.name == "Lumina"
    assert "silver-white hair" in forge.character.identity
    assert "cat ears" not in forge.character.identity
    assert "masterpiece" in forge.character.quality_preamble   # still on-register


def test_a_missing_appearance_file_renders_nobody_not_someone_else(cfg, tmp_path,
                                                                   caplog):
    cfg = cfg.model_copy(update={"selfie_character": str(tmp_path / "gone.yaml"),
                                 "companion_name": "Lumina"})
    with caplog.at_level("WARNING"):
        forge, _ = build_forge(cfg)
    assert "cat ears" not in forge.character.identity
    assert "unspecified" in forge.character.identity
    assert "neutral stand-in" in caplog.text


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

        def _write_provenance(self, path, meta):
            pass                             # the ledger isn't what's under test

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


async def test_a_render_that_dies_still_hands_its_pipeline_back(cfg, clock):
    """The OOM that started this: the render raised, the teardown sat on the
    success path, and ~8 GiB of SDXL stayed on a 16 GiB card for the life of
    the process — so the *next* render, and her brain, had nowhere to load."""
    from contextlib import contextmanager

    class DyingForge:
        out_dir = cfg.selfie_dir

        class backend:
            torn: list[str] = []

            @staticmethod
            def _teardown():
                DyingForge.backend.torn.append("teardown")

        def selfie(self, **kw):
            raise RuntimeError("CUDA out of memory")

    class BorrowParker:
        def __init__(self):
            self.events: list[str] = []

        def parked(self):
            @contextmanager
            def _ctx():
                self.events.append("park")
                try:
                    yield True
                finally:
                    self.events.append("restore")
            return _ctx()

    rec = Recorder()
    parker = BorrowParker()
    lab = SelfieLab(DyingForge(), clock=clock, post=rec.post, speak=rec.speak,
                    parker=parker)
    lab.start({"id": "oom1", "status": "started"})
    await settle(lab)

    # Twice, and both matter. The first runs inside the park, before the
    # restore re-pins her brain onto the card. The second runs after the
    # `except` block has ended — only there is the traceback (and the frames
    # still holding the pipeline) gone, so only there can the VRAM actually go.
    assert DyingForge.backend.torn == ["teardown", "teardown"]
    assert parker.events == ["park", "restore"]
    assert rec.posts and "didn't come out" in rec.posts[0]["text"]


async def test_an_unparked_render_that_dies_still_hands_its_pipeline_back(cfg, clock):
    """No park means no borrowed VRAM, but a dead pipeline is dead weight on
    the card either way — and an OOM is the likeliest reason to be here."""
    class DyingForge:
        out_dir = cfg.selfie_dir

        class backend:
            torn: list[str] = []

            @staticmethod
            def _teardown():
                DyingForge.backend.torn.append("teardown")

        def selfie(self, **kw):
            raise RuntimeError("CUDA out of memory")

    rec = Recorder()
    lab = SelfieLab(DyingForge(), clock=clock, post=rec.post, speak=rec.speak)
    lab.start({"id": "oom2", "status": "started"})
    await settle(lab)
    assert DyingForge.backend.torn == ["teardown"]


async def test_a_backend_with_nothing_resident_is_left_alone(cfg, clock, forge):
    """mock and the hosted cameras have no `_teardown` — the release must be a
    no-op there, not an AttributeError that eats the failure message."""
    lab = SelfieLab(forge, clock=clock, post=Recorder().post,
                    speak=Recorder().speak)
    lab._release()                                # no backend pipeline: nothing to do
