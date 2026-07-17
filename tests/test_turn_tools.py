"""The tool loop inside a real TurnController (SPEC §7.4, §9, §13):
first audio precedes tool execution, and barge-in mid-continuation kills
everything and persists nothing."""
from __future__ import annotations

import asyncio

from yurios.desktop.voice.backends.fakes import FakeTTS
from yurios.desktop.voice.turn import TurnController
from yurios.world.tools.fakes import FakeToolRunner

from .conftest import ScriptedChat, make_toolbrain


class LoopBrain:
    """The minimum ReplyBrain: ToolBrain's tool loop under a TurnController.
    (The full path is test_integration.py; this isolates the loop.)"""

    def __init__(self, tb):
        self.tb = tb
        self.persist_calls: list[tuple[str, str]] = []

    async def stream_reply(self, session_id: str, text: str):
        async for tok in self.tb._stream_with_tools(
                [{"role": "user", "content": text}], []):
            yield tok

    async def persist(self, session_id: str, user_text: str, reply: str) -> None:
        self.persist_calls.append((user_text, reply))


class BlockingRunner(FakeToolRunner):
    """A runner whose call parks until the test releases it — the probe for
    'first audio never waits on a tool' (SPEC §7.4)."""

    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = False

    async def call(self, tool, args):
        self.entered.set()
        await self.release.wait()
        result = await super().call(tool, args)
        self.finished = True
        return result


async def test_first_audio_precedes_tool_execution(cfg, guard, timers, controller):
    chat = ScriptedChat([
        ["Sure. ", "Hold on. ", '[[set_timer {"minutes": 10}]]'],
        ["Ten minutes — counting."],
    ])
    runner = BlockingRunner()
    brain = LoopBrain(make_toolbrain(cfg, guard, timers, controller, chat,
                                     runner=runner))
    tc = TurnController(brain=brain, tts=FakeTTS(), filler_bank=None,
                        mask_latency=False)

    kinds, texts = [], []
    async for ev in tc.run_turn("s1", "set a timer"):
        kinds.append(ev.kind)
        if ev.kind == "audio":
            texts.append(ev.text)
            if not runner.release.is_set():
                # her lead-in is already at the speaker while the tool is still
                # blocked: the §1 first-audio budget cannot be blown by a slow
                # tool. (Wait for the producer to reach the call, then release.)
                await asyncio.wait_for(runner.entered.wait(), 2)
                assert not runner.finished
                runner.release.set()
    assert kinds[-1] == "done"
    assert runner.finished
    assert any("counting" in (t or "") for t in texts)   # the continuation spoke


async def test_bargein_mid_continuation_cancels_and_persists_nothing(
        cfg, guard, timers, controller):
    chat = ScriptedChat([
        ["One sec. ", '[[get_weather {}]]'],
        ["It's ", "raining ", "and ", "seventeen ", "degrees ", "and ", "the ",
         "wind ", "is ", "picking ", "up ", "outside…"],
    ])
    runner = FakeToolRunner()
    brain = LoopBrain(make_toolbrain(cfg, guard, timers, controller, chat,
                                     runner=runner))
    tc = TurnController(brain=brain, tts=FakeTTS(), filler_bank=None,
                        mask_latency=False)

    events = []

    async def puller():
        async for ev in tc.run_turn("s1", "weather?"):
            events.append(ev)

    task = asyncio.create_task(puller())
    # the tool ran and the continuation is streaming — NOW the user talks over her
    await asyncio.wait_for(chat.pass_started[1].wait(), timeout=2.0)
    tc.cancel()
    await asyncio.wait_for(task, timeout=2.0)

    kinds = [e.kind for e in events]
    assert "cancelled" in kinds and "done" not in kinds
    assert runner.calls == [("get_weather", {})]   # mid-CONTINUATION, tool did run
    assert brain.persist_calls == []               # a turn that didn't happen (B2 §4.4)
