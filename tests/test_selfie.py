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
    """The asked-for tier reaches the forge (templates/selfie.yaml: a tier, not
    a gate); a contract without one stays in the everyday default."""
    rec = Recorder()
    lab = SelfieLab(forge, clock=clock, post=rec.post, speak=rec.speak)
    lab.start({"id": "w1", "scene": "bed", "mood": "tender",
               "wardrobe": "intimate", "status": "started"})
    lab.start({"id": "w2", "scene": "window", "status": "started"})
    await settle(lab)

    tiers = {}
    for post in rec.posts:
        png = cfg.selfie_dir / post["image_url"].removeprefix("/selfies/")
        meta = json.loads(png.with_suffix(".json").read_text())
        tiers[post["image_url"].split("-")[-1]] = meta["template"]["wardrobe"]
    assert tiers == {"w1.png": "intimate", "w2.png": "everyday"}


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
