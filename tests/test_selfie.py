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

        def close(self, owner=None):
            self.closed = True

        def open(self, owner=None):
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
    the contract refuses nothing); a contract with nothing to go on at all
    stays everyday."""
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


async def test_the_default_wardrobe_shuts_up_when_she_dressed_herself(cfg, clock,
                                                                     forge):
    """Same rule as the situation, one slot over: what she said, nobody argues
    with. A `look` naming an outfit used to get the everyday tier stapled on
    after it, and the renderer believes the last thing it is told — so she
    described a silk dress and was rendered in a sweater. An outfit she or the
    caller *asked* for still wins; a shot with nothing to go on still gets the
    everyday register."""
    rec = Recorder()
    dress = "in a soft dark silk dress on the window seat"
    lab = SelfieLab(forge, clock=clock, post=rec.post, speak=rec.speak)
    lab.start({"id": "dressed", "look": dress, "status": "started"})
    lab.start({"id": "asked", "look": dress, "wardrobe": "cozy",
               "status": "started"})
    lab.start({"id": "mute", "status": "started"})
    await settle(lab)

    metas = {}
    for post in rec.posts:
        png = cfg.selfie_dir / post["image_url"].removeprefix("/selfies/")
        key = post["image_url"].split("-")[-1].removesuffix(".png")
        metas[key] = json.loads(png.with_suffix(".json").read_text())

    sweater = "soft oversized sweater"
    assert dress in metas["dressed"]["prompt"]
    assert sweater not in metas["dressed"]["prompt"]
    assert "wardrobe" not in metas["dressed"]["template"]
    # an explicit ask is still an ask, described or not
    assert metas["asked"]["template"]["wardrobe"] == "cozy"
    assert sweater not in metas["asked"]["prompt"]
    # and the unprompted shot, which has no words of hers to respect, is
    # dressed by the library exactly as before
    assert metas["mute"]["template"]["wardrobe"] == "everyday"
    assert sweater in metas["mute"]["prompt"]


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


async def test_a_photo_you_asked_for_is_not_marked_she_spoke_first(
        cfg, guard, timers, controller, clock, forge):
    """The whole point of the marking (§15.5): "she spoke first" means she
    started this. A selfie lands minutes after the turn that reached for the
    camera, with no turn around it — so the lab used to read every photo as
    unprompted, and the chat put the tag on every single one, including the
    ones you asked for by name."""
    from yurios.world.tools.fakes import FakeToolRunner

    class SpyLab:
        def __init__(self):
            self.started: list[dict] = []

        def start(self, contract):
            self.started.append(contract)

    guard._rates["take_selfie"] = 2
    guard._buckets["take_selfie"] = {"tokens": 2.0, "at": clock.now()}
    lab = SpyLab()
    chat = ScriptedChat([
        ['ok~ [[take_selfie {"scene": "window"}]]'],
        ["coming right up."],
    ])
    brain = make_toolbrain(cfg, guard, timers, controller, chat,
                           runner=FakeToolRunner(), selfies=lab)
    with brain.turn_context(channel="web", client_id="browser-1",
                            session_id="s-1"):
        await collect(brain._stream_with_tools(
            [{"role": "user", "content": "send me a selfie"}], []))

    (contract,) = lab.started
    assert contract["_proactive"] is False     # decided while the turn was up

    # …and the lab, running long after that turn closed, commits it as a reply
    rec = Recorder()
    real = SelfieLab(forge, clock=clock, post=rec.post, speak=rec.speak)
    real.start({**contract, "id": "asked-for"})
    await settle(real)
    (post,) = rec.posts
    assert post["image_url"] and post["proactive"] is False


async def test_a_photo_nobody_asked_for_still_says_she_spoke_first(
        cfg, clock, forge):
    """The other half: a dream's picture (mind/dreamjobs.py) carries no
    `_proactive` at all, because nobody was talking — and that is exactly the
    line the tag was invented for."""
    rec = Recorder()
    lab = SelfieLab(forge, clock=clock, post=rec.post, speak=rec.speak)
    lab.start({"id": "dream-2026-08-08", "status": "started", "_dream": True})
    await settle(lab)
    (post,) = rec.posts
    assert post["proactive"] is True


async def test_long_selfie_result_is_realised_before_model_truncation(
        cfg, guard, timers, controller, clock):
    """The continuation may get a bounded result, but host realization must
    parse the complete JSON contract or a detailed `look` never starts."""
    from yurios.world.tools.fakes import FakeToolRunner

    class SpyLab:
        def __init__(self):
            self.started: list[dict] = []

        def start(self, contract):
            self.started.append(contract)

    look = "Amethyst skin in soft afternoon rain light. " * 20
    result = {"id": "long-look", "look": look, "status": "started",
              "note": "the photo will appear in the chat shortly"}
    guard._rates["take_selfie"] = 2
    guard._buckets["take_selfie"] = {"tokens": 2.0, "at": clock.now()}
    lab = SpyLab()
    chat = ScriptedChat([
        ['Here. [[take_selfie {"look": "soft rain"}]]'],
        ["It is on its way."],
    ])
    brain = make_toolbrain(
        cfg, guard, timers, controller, chat,
        runner=FakeToolRunner(results={"take_selfie": result}), selfies=lab)

    await collect(brain._stream_with_tools([], []))

    (contract,) = lab.started
    assert contract["look"] == look
    assert "…" in chat.calls[1][-1]["content"]


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


# --- the other camera: show_picture (§7.6) ---------------------------------
# `take_selfie` can only answer "here is a picture of me". These pin the half
# that lets her show you anything else — her words are the whole prompt, and
# her likeness is out of the frame.

async def test_a_picture_is_of_the_thing_and_not_of_her(cfg, clock, forge):
    """The one rule that makes this a different camera rather than a differently
    worded selfie: her identity never enters the prompt."""
    rec = Recorder()
    lab = SelfieLab(forge, clock=clock, post=rec.post, speak=rec.speak)
    subject = "the street below, wet and empty, one streetlight still on"
    lab.start({"id": "pic1", "kind": "picture", "subject": subject,
               "status": "started"})
    await settle(lab)

    (post,) = rec.posts
    png = cfg.selfie_dir / post["image_url"].removeprefix("/selfies/")
    meta = json.loads(png.with_suffix(".json").read_text())
    assert subject in meta["prompt"]
    assert forge.character.identity not in meta["prompt"]
    # …and the ledger records her words as the picture, the way a `look` is
    assert meta["template"]["look"] == subject


async def test_a_picture_does_not_borrow_the_situation(cfg, clock, forge):
    """She wrote the whole subject, so there is no gap to fill — and appending
    "it is night, rain on the window" to her sunlit meadow is worse than adding
    nothing at all (the same rule a placed selfie follows)."""
    rec = Recorder()
    lab = SelfieLab(forge, clock=clock, post=rec.post, speak=rec.speak,
                    situation=lambda: "It is night, heavy rain on the glass.")
    lab.start({"id": "pic2", "kind": "picture",
               "subject": "a sunlit meadow at noon", "status": "started"})
    await settle(lab)

    png = cfg.selfie_dir / rec.posts[0]["image_url"].removeprefix("/selfies/")
    meta = json.loads(png.with_suffix(".json").read_text())
    assert "heavy rain" not in meta["prompt"]


async def test_her_avoid_still_steers_a_picture(cfg, clock, forge):
    rec = Recorder()
    lab = SelfieLab(forge, clock=clock, post=rec.post, speak=rec.speak)
    lab.start({"id": "pic3", "kind": "picture", "subject": "my desk at night",
               "avoid": "people", "status": "started"})
    await settle(lab)

    png = cfg.selfie_dir / rec.posts[0]["image_url"].removeprefix("/selfies/")
    meta = json.loads(png.with_suffix(".json").read_text())
    assert "people" in meta["negative"]


async def test_she_calls_a_picture_a_picture(cfg, clock, forge):
    """The announce cue is the only place she names what she just made — "the
    selfie you just took" about a photo of the rain is a small lie."""
    rec = Recorder()
    lab = SelfieLab(forge, clock=clock, post=rec.post, speak=rec.speak)
    lab.start({"id": "pic4", "kind": "picture", "subject": "the rain",
               "status": "started"})
    await settle(lab)
    assert "picture you just took" in rec.cues[0]

    rec2 = Recorder()
    lab2 = SelfieLab(forge, clock=clock, post=rec2.post, speak=rec2.speak)
    lab2.start({"id": "self4", "status": "started"})   # no kind: still a selfie
    await settle(lab2)
    assert "selfie you just took" in rec2.cues[0]


async def test_a_picture_that_fails_says_so_as_a_picture(cfg, clock):
    class DyingForge:
        out_dir = cfg.selfie_dir
        backend = None

        def picture(self, subject, **kw):
            raise RuntimeError("CUDA out of memory")

    rec = Recorder()
    lab = SelfieLab(DyingForge(), clock=clock, post=rec.post, speak=rec.speak)
    lab.start({"id": "pic5", "kind": "picture", "subject": "the rain",
               "status": "started"})
    await settle(lab)
    assert "the picture didn't come out" in rec.posts[0]["text"]


async def test_the_tool_loop_starts_a_picture_too(cfg, guard, timers, controller,
                                                  clock):
    """[[show_picture …]] takes the same §7.5 realisation path as the selfie —
    one lab, one start-don't-await rule, two cameras."""
    from yurios.world.tools.fakes import FakeToolRunner

    class SpyLab:
        def __init__(self):
            self.started: list[dict] = []

        def start(self, contract):
            self.started.append(contract)

    guard._rates["show_picture"] = 2
    guard._buckets["show_picture"] = {"tokens": 2.0, "at": clock.now()}
    lab = SpyLab()
    chat = ScriptedChat([
        ['Here, look — ', '[[show_picture {"subject": "the rain on the glass"}]]'],
        ['…it does that every night.'],
    ])
    brain = make_toolbrain(cfg, guard, timers, controller, chat,
                           runner=FakeToolRunner(), selfies=lab)
    spoken = "".join(await collect(
        brain._stream_with_tools([{"role": "user", "content": "what's it like out?"}], [])))
    assert "every night" in spoken
    (contract,) = lab.started
    assert contract["kind"] == "picture"
    assert contract["subject"] == "the rain on the glass"


def test_the_two_cameras_have_separate_budgets(cfg, clock):
    """Spending her picture budget on the street below must not cost her the
    ability to send you her face a minute later — different urges, one GPU."""
    guard = Guard(rates_per_min={"take_selfie": 2, "show_picture": 2},
                  log_dir=cfg.tool_log_dir, clock=clock)
    assert guard.check("show_picture")[0]
    assert guard.check("show_picture")[0]
    assert guard.check("show_picture") == (False, "rate limit")
    assert guard.check("take_selfie")[0]


# ---- the warm pipeline between renders --------------------------------------
# The render that keeps the pipeline warm is never the render that fails, which
# is why this reads as "selfies fail randomly". See SelfieLab._render.

class WarmthParker:
    """A parker that never needs to park (the card looks roomy) but answers
    the question that actually matters: is there room for her brain too?"""

    def __init__(self, *, room_for_the_brain: bool):
        self.room = room_for_the_brain
        self.asked = 0

    def parked(self):
        from contextlib import contextmanager

        @contextmanager
        def _ctx():
            yield False                     # no loan taken for this render
        return _ctx()

    def can_keep_pipeline_warm(self) -> bool:
        self.asked += 1
        return self.room


class WarmForge:
    """A forge whose backend keeps weights on the card, and says when dropped."""

    def __init__(self, out_dir):
        self.out_dir = out_dir
        self.backend = self
        self.torn_down = 0

    def _teardown(self):
        self.torn_down += 1

    def selfie(self, **kw):
        from yurios.forge.service import ImageResult
        return ImageResult(data=b"\x89PNG\r\n\x1a\n", meta={"template": {}})

    def _write_provenance(self, path, meta):
        pass


async def test_a_roomy_card_keeps_the_pipeline_warm(cfg, clock):
    """The optimisation is real — 25 seconds a selfie — and costs nothing when
    her brain still fits beside it."""
    rec = Recorder()
    parker = WarmthParker(room_for_the_brain=True)
    lab = SelfieLab(WarmForge(cfg.selfie_dir), clock=clock, post=rec.post,
                    speak=rec.speak, parker=parker)
    lab.start({"id": "p1", "scene": "window", "status": "started"})
    await settle(lab)
    assert parker.asked == 1
    assert lab.forge.torn_down == 0


async def test_a_full_card_drops_the_pipeline_even_without_a_loan(cfg, clock):
    """The fix: a render that didn't park still hands the card back when
    keeping it would leave her brain nowhere to come home to. Without this the
    next render parks into a card the last render already filled."""
    rec = Recorder()
    parker = WarmthParker(room_for_the_brain=False)
    lab = SelfieLab(WarmForge(cfg.selfie_dir), clock=clock, post=rec.post,
                    speak=rec.speak, parker=parker)
    lab.start({"id": "p1", "scene": "window", "status": "started"})
    await settle(lab)
    assert lab.forge.torn_down >= 1


async def test_a_parker_that_cannot_measure_releases_rather_than_guess(cfg, clock):
    """Being wrong here strands the card until a restart, so an unanswerable
    question resolves to the safe side."""
    class Broken(WarmthParker):
        def can_keep_pipeline_warm(self):
            raise RuntimeError("no torch")

    lab = SelfieLab(WarmForge(cfg.selfie_dir), clock=clock,
                    post=Recorder().post, speak=Recorder().speak,
                    parker=Broken(room_for_the_brain=True))
    lab.start({"id": "p1", "scene": "window", "status": "started"})
    await settle(lab)
    assert lab.forge.torn_down >= 1


async def test_an_old_parker_without_the_question_keeps_the_old_behaviour(cfg, clock):
    """SpyParker and the hosted cameras don't answer it; they must not break."""
    lab = SelfieLab(WarmForge(cfg.selfie_dir), clock=clock,
                    post=Recorder().post, speak=Recorder().speak,
                    parker=SpyParker())
    lab.start({"id": "p1", "scene": "window", "status": "started"})
    await settle(lab)
    assert lab.forge.torn_down == 0
