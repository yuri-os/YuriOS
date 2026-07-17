"""The tool loop end-to-end over a scripted stream (SPEC §7.4, §13) — guard
consulted, result in the continuation, cap enforced, errors survivable."""
from __future__ import annotations

import json

from yurios.world.tools.fakes import FakeToolRunner

from .conftest import ScriptedChat, collect, make_toolbrain

TIMER_MARKER = '[[set_timer {"minutes": 10, "label": "tea"}]]'


async def test_one_call_result_reaches_the_continuation(cfg, guard, timers,
                                                        controller):
    chat = ScriptedChat([
        ["Sure — ", "one sec. ", TIMER_MARKER],
        ["Ten minutes, ", "counting."],
    ])
    runner = FakeToolRunner()
    tb = make_toolbrain(cfg, guard, timers, controller, chat, runner=runner)

    raw: list[str] = []
    spoken = "".join(await collect(
        tb._stream_with_tools([{"role": "user", "content": "set a tea timer"}], raw)))

    # the marker was never spoken; both passes' speech joined the same turn
    assert spoken == "Sure — one sec. Ten minutes, counting."
    # the guard allowed it and the runner was called with the parsed args
    assert runner.calls == [("set_timer", {"minutes": 10, "label": "tea"})]
    # the continuation pass saw her partial reply AND the tool result
    cont = chat.calls[1]
    assert cont[-2] == {"role": "assistant", "content": "Sure — one sec. "}
    assert cont[-1]["role"] == "user"
    assert "set_timer returned" in cont[-1]["content"]
    assert '"seconds": 600' in cont[-1]["content"]
    # host-side realisation (§7.5): the timer is on the board, not just in words
    assert [t.label for t in timers.pending()] == ["tea"]
    # the raw record keeps the marker + result for the corpus (§7.4)
    joined = "".join(raw)
    assert TIMER_MARKER in joined and "set_timer → " in joined


async def test_per_turn_cap_second_call_runs_third_denied(cfg, guard, timers,
                                                          controller):
    cfg = cfg.model_copy(update={"tool_max_calls_per_turn": 2})
    chat = ScriptedChat([
        ["a [[get_weather {}]]"],
        ["b [[set_timer {\"minutes\": 1}]]"],
        ["c [[set_timer {\"minutes\": 2}]] done"],   # past the cap: dropped
    ])
    runner = FakeToolRunner()
    tb = make_toolbrain(cfg, guard, timers, controller, chat, runner=runner)
    spoken = "".join(await collect(tb._stream_with_tools([], [])))

    assert [c[0] for c in runner.calls] == ["get_weather", "set_timer"]
    assert spoken == "a b c  done"                    # third marker stripped, not run
    # the dropped marker left an audit line, so the log tells the whole story
    lines = [json.loads(l) for l in
             (cfg.tool_log_dir / "calls.jsonl").read_text().splitlines()]
    assert any(l["verdict"] == "denied: per-turn cap" for l in lines)
    # and the cap-spent notice reached the second continuation
    assert "budget for this turn is now spent" in chat.calls[2][-1]["content"]


async def test_denied_by_guard_becomes_a_speakable_result(cfg, guard, timers,
                                                          controller):
    chat = ScriptedChat([
        ['hm [[rm_rf {"path": "/"}]]'],
        ["I can't do that."],
    ])
    runner = FakeToolRunner()
    tb = make_toolbrain(cfg, guard, timers, controller, chat, runner=runner)
    spoken = "".join(await collect(tb._stream_with_tools([], [])))

    assert runner.calls == []                          # never reached the runner
    assert spoken == "hm I can't do that."
    assert "denied (not a tool she has)" in chat.calls[1][-1]["content"]


async def test_tool_error_still_completes_the_turn(cfg, guard, timers, controller):
    chat = ScriptedChat([
        ["checking… [[get_weather {}]]"],
        ["Hm, I can't see the sky right now."],
    ])
    runner = FakeToolRunner(errors={"get_weather": "network down"})
    tb = make_toolbrain(cfg, guard, timers, controller, chat, runner=runner)
    spoken = "".join(await collect(tb._stream_with_tools([], [])))

    assert spoken.endswith("I can't see the sky right now.")
    assert "error (network down)" in chat.calls[1][-1]["content"]


async def test_no_runner_marker_stripped_single_pass(cfg, guard, timers, controller):
    chat = ScriptedChat([["Sure. " + TIMER_MARKER + " done"]])
    tb = make_toolbrain(cfg, guard, timers, controller, chat)   # no hands (§7.2)
    spoken = "".join(await collect(tb._stream_with_tools([], [])))
    assert spoken == "Sure.  done"
    assert len(chat.calls) == 1                        # no continuation pass


async def test_play_music_realised_on_the_controller(cfg, guard, timers, controller):
    chat = ScriptedChat([
        ['mm. [[play_music {"action": "play", "track": "night_piano"}]]'],
        ["there."],
    ])
    tb = make_toolbrain(cfg, guard, timers, controller, chat,
                        runner=FakeToolRunner())
    await collect(tb._stream_with_tools([], []))
    music = [c for c in controller.commands if c["type"] == "music"]
    assert music == [{"type": "music", "action": "play",
                      "track": "night_piano", "volume": 0.4}]


async def test_result_truncated_before_the_continuation(cfg, guard, timers,
                                                        controller):
    chat = ScriptedChat([["x [[get_weather {}]]"], ["ok"]])
    runner = FakeToolRunner(results={"get_weather": "y" * 5000})
    tb = make_toolbrain(cfg, guard, timers, controller, chat, runner=runner)
    await collect(tb._stream_with_tools([], []))
    assert len(chat.calls[1][-1]["content"]) < 800     # 600-char cap + the cue text
