"""The tool loop end-to-end over a scripted stream (SPEC §7.4, §13) — guard
consulted, result in the continuation, cap enforced, errors survivable."""
from __future__ import annotations

import json

from yurios.world.tools.client import (
    DESC_MAX_CHARS, ToolSpec, build_directive, coerce_args, one_line)
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
        ["a [[list_notes {}]]"],
        ["b [[set_timer {\"minutes\": 1}]]"],
        ["c [[set_timer {\"minutes\": 2}]] done"],   # past the cap: dropped
    ])
    runner = FakeToolRunner()
    tb = make_toolbrain(cfg, guard, timers, controller, chat, runner=runner)
    spoken = "".join(await collect(tb._stream_with_tools([], [])))

    assert [c[0] for c in runner.calls] == ["list_notes", "set_timer"]
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
        ["checking… [[list_notes {}]]"],
        ["Hm, I can't reach my desk right now."],
    ])
    runner = FakeToolRunner(errors={"list_notes": "disk gone"})
    tb = make_toolbrain(cfg, guard, timers, controller, chat, runner=runner)
    spoken = "".join(await collect(tb._stream_with_tools([], [])))

    assert spoken.endswith("I can't reach my desk right now.")
    assert "error (disk gone)" in chat.calls[1][-1]["content"]


async def test_no_runner_marker_stripped_single_pass(cfg, guard, timers, controller):
    chat = ScriptedChat([["Sure. " + TIMER_MARKER + " done"]])
    tb = make_toolbrain(cfg, guard, timers, controller, chat)   # no hands (§7.2)
    spoken = "".join(await collect(tb._stream_with_tools([], [])))
    assert spoken == "Sure.  done"
    assert len(chat.calls) == 1                        # no continuation pass


async def test_the_continuation_does_not_say_the_lead_in_twice(cfg, guard, timers,
                                                               controller):
    """Every live tool turn came back with its lead-in doubled: the cue says
    "continue from where you left off" and a 12B model starts again. The echo is
    matched against the previous pass and dropped (§7.4)."""
    chat = ScriptedChat([
        ["[tender] Let me see... ", '[[list_notes {}]]'],
        ["[tender] Let me see... ", "[happy] It's just the one about you."],
    ])
    tb = make_toolbrain(cfg, guard, timers, controller, chat,
                        runner=FakeToolRunner())
    spoken = "".join(await collect(tb._stream_with_tools([], [])))
    assert spoken == "[tender] Let me see... [happy] It's just the one about you."


async def test_a_lead_in_said_three_times_is_still_said_once(cfg, guard, timers,
                                                             controller):
    """One echo was the common case; live, the continuation reprised the lead-in
    twice over before getting to the result. Each completed echo is committed and
    matching restarts from the top, bounded so a pass cannot be swallowed whole."""
    lead = "[tender] Let me see... "
    chat = ScriptedChat([
        [lead, '[[read_note {"path": "a.md"}]]'],
        [lead, lead, "[happy] It says you take your tea strong."],
    ])
    tb = make_toolbrain(cfg, guard, timers, controller, chat,
                        runner=FakeToolRunner())
    spoken = "".join(await collect(tb._stream_with_tools([], [])))
    assert spoken.count("Let me see") == 1
    assert spoken == lead + "[happy] It says you take your tea strong."


async def test_a_repeat_under_a_different_emotion_tag_is_still_a_repeat(cfg, guard,
                                                                        timers,
                                                                        controller):
    """Live, the second pass re-tagged the same sentence — `[tender]` where the
    first said `[neutral]` — and a characterwise match died on the very first
    character. Tags are skipped on both sides; only the words have to agree. The
    tag that opens the *new* sentence is still kept, or her face never changes."""
    chat = ScriptedChat([
        ["[neutral] I understand. ", '[[delete_skill {"name": "calming"}]]'],
        ["[tender] I understand. ", "[happy] It's gone."],
    ])
    tb = make_toolbrain(cfg, guard, timers, controller, chat,
                        runner=FakeToolRunner())
    spoken = "".join(await collect(tb._stream_with_tools([], [])))
    assert spoken == "[neutral] I understand. [happy] It's gone."


async def test_a_continuation_that_merely_starts_the_same_way_is_kept_whole(
        cfg, guard, timers, controller):
    """The echo skip holds rather than swallows, so a genuinely new sentence that
    opens with the same words is released intact — never left mid-clause."""
    chat = ScriptedChat([
        ["I'll check. ", '[[list_notes {}]]'],
        ["I'll check the other one too."],
    ])
    tb = make_toolbrain(cfg, guard, timers, controller, chat,
                        runner=FakeToolRunner())
    spoken = "".join(await collect(tb._stream_with_tools([], [])))
    assert spoken == "I'll check. I'll check the other one too."


async def test_a_pass_that_only_repeats_itself_adds_nothing(cfg, guard, timers,
                                                            controller):
    chat = ScriptedChat([
        ["Mm, one moment. ", '[[list_notes {}]]'],
        ["Mm, one moment."],
    ])
    tb = make_toolbrain(cfg, guard, timers, controller, chat,
                        runner=FakeToolRunner())
    spoken = "".join(await collect(tb._stream_with_tools([], [])))
    assert spoken == "Mm, one moment. "


async def test_a_marker_a_bracket_short_still_runs(cfg, guard, timers, controller):
    """The observed failure (see tooltags' docstring): she closed the object,
    wrote one `]`, and stopped. The call is whole, so the turn runs it — and gets
    a continuation pass, exactly as if the marker had closed properly."""
    chat = ScriptedChat([
        ["Sure — ", 'one sec. [[set_timer {"minutes": 10, "label": "tea"}]'],
        ["Ten minutes."],
    ])
    runner = FakeToolRunner()
    tb = make_toolbrain(cfg, guard, timers, controller, chat, runner=runner)

    spoken = "".join(await collect(tb._stream_with_tools([], [])))

    assert runner.calls == [("set_timer", {"minutes": 10, "label": "tea"})]
    assert spoken == "Sure — one sec. Ten minutes."
    assert [t.label for t in timers.pending()] == ["tea"]


async def test_a_broken_marker_is_told_to_her_not_swallowed(cfg, guard, timers,
                                                            controller):
    """A dropped marker used to be silent, so the next turn she read her own
    broken call back out of the transcript and reported the note existed. Now the
    turn says it didn't land — to her, in the verbatim record, and in the audit
    log — and she gets one more go at writing it."""
    chat = ScriptedChat([
        ["I'll note that. ", '[[set_timer {minutes: ten}]]'],   # unrepairable
        ['Sorry — [[set_timer {"minutes": 10}]]'],              # her second try
        ["There."],
    ])
    runner = FakeToolRunner()
    tb = make_toolbrain(cfg, guard, timers, controller, chat, runner=runner)

    raw: list[str] = []
    spoken = "".join(await collect(tb._stream_with_tools([], raw)))

    assert runner.calls == [("set_timer", {"minutes": 10})]     # the retry landed
    assert spoken == "I'll note that. Sorry — There."
    # she was told, in the pass that follows the drop
    retry = chat.calls[1][-1]["content"]
    assert "didn't parse" in retry and "nothing ran" in retry
    # …and the record she reads back next turn says so too
    assert "did not parse" in "".join(raw)
    # …and the drop is visible from outside, which is where it was invisible
    lines = [json.loads(l) for l in
             (cfg.tool_log_dir / "calls.jsonl").read_text().splitlines()]
    assert any(l["verdict"] == "dropped: malformed marker" for l in lines)


async def test_only_one_retry_per_turn(cfg, guard, timers, controller):
    """Two broken markers in a row end the turn in words rather than looping."""
    chat = ScriptedChat([
        ["a [[set_timer {minutes: ten}]]"],
        ["b [[set_timer {still: broken}]]"],
        ["c should never stream"],
    ])
    runner = FakeToolRunner()
    tb = make_toolbrain(cfg, guard, timers, controller, chat, runner=runner)

    spoken = "".join(await collect(tb._stream_with_tools([], [])))

    assert runner.calls == []
    assert spoken == "a b "
    assert len(chat.calls) == 2                    # no third pass


async def test_a_broken_marker_with_no_hands_is_not_retried(cfg, guard, timers,
                                                            controller):
    """Nothing to reach for, nothing to correct — one pass, as before (§7.2)."""
    chat = ScriptedChat([["Sure. [[set_timer {minutes: ten}]] done"]])
    tb = make_toolbrain(cfg, guard, timers, controller, chat)
    spoken = "".join(await collect(tb._stream_with_tools([], [])))
    assert spoken == "Sure.  done"
    assert len(chat.calls) == 1


async def test_detailed_selfie_look_reaches_runner(cfg, guard, timers, controller,
                                                   clock):
    look = "Amethyst skin in soft afternoon rain light. " * 20
    marker = '[[take_selfie {"look": ' + json.dumps(look) + '}]]'
    chat = ScriptedChat([["Here. " + marker], ["It is on its way."]])
    runner = FakeToolRunner()
    guard._rates["take_selfie"] = 2
    guard._buckets["take_selfie"] = {"tokens": 2.0, "at": clock.now()}
    tb = make_toolbrain(cfg, guard, timers, controller, chat, runner=runner)

    spoken = "".join(await collect(tb._stream_with_tools([], [])))

    assert spoken == "Here. It is on its way."
    assert runner.calls == [("take_selfie", {"look": look})]


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
    chat = ScriptedChat([["x [[list_notes {}]]"], ["ok"]])
    runner = FakeToolRunner(results={"list_notes": "y" * 5000})
    tb = make_toolbrain(cfg, guard, timers, controller, chat, runner=runner)
    await collect(tb._stream_with_tools([], []))
    assert len(chat.calls[1][-1]["content"]) < 800     # 600-char cap + the cue text


# ---- the web hands (SPEC §7.7) ---------------------------------------------

class ShelfSpy:
    """The Researcher seam the brain realises against."""

    def __init__(self):
        self.shelved: list[dict] = []
        self.runs: list[dict] = []

    def shelve(self, page):
        self.shelved.append(page)

    def start(self, contract):
        self.runs.append(contract)


def _web(guard, clock, *tools):
    for t in tools:
        guard.allow(t, 6)


async def test_read_page_shelves_the_whole_page_while_she_speaks_to_a_gist(
        cfg, guard, timers, controller, clock):
    """The contract read_page is shaped around: `_execute` keeps the untruncated
    result for host realisation and truncates only the copy the model sees, so
    the shelf gets the page and the turn gets 600 characters of it."""
    from yurios.world.tools.fakes import FAKE_PAGE
    _web(guard, clock, "read_page")
    chat = ScriptedChat([['ok [[read_page {"url": "https://a.example/x"}]]'],
                         ["it says a few things."]])
    shelf = ShelfSpy()
    tb = make_toolbrain(cfg, guard, timers, controller, chat,
                        runner=FakeToolRunner(), research=shelf)

    await collect(tb._stream_with_tools([], []))

    # the host got all of it…
    assert len(shelf.shelved) == 1
    assert shelf.shelved[0]["text"] == FAKE_PAGE
    assert len(FAKE_PAGE) > 600
    # …while the continuation the model reads was bounded
    cont = chat.calls[1][-1]["content"]
    assert len(cont) < 800 and "read_page returned" in cont


async def test_read_page_without_a_researcher_is_simply_not_shelved(
        cfg, guard, timers, controller, clock):
    """No mind, no shelf, no crash — she still read it out loud."""
    _web(guard, clock, "read_page")
    chat = ScriptedChat([['ok [[read_page {"url": "https://a.example/x"}]]'],
                         ["done"]])
    tb = make_toolbrain(cfg, guard, timers, controller, chat,
                        runner=FakeToolRunner())          # research=None
    spoken = "".join(await collect(tb._stream_with_tools([], [])))
    assert spoken == "ok done"


async def test_research_starts_off_turn_and_the_turn_finishes_immediately(
        cfg, guard, timers, controller, clock):
    _web(guard, clock, "research")
    chat = ScriptedChat([['looking into it — [[research {"topic": "tea"}]]'],
                         ["I'll tell you what I find."]])
    shelf = ShelfSpy()
    tb = make_toolbrain(cfg, guard, timers, controller, chat,
                        runner=FakeToolRunner(), research=shelf)

    spoken = "".join(await collect(tb._stream_with_tools([], [])))

    assert spoken == "looking into it — I'll tell you what I find."
    assert len(shelf.runs) == 1
    assert shelf.runs[0]["topic"] == "tea"
    assert shelf.runs[0]["status"] == "started"
    assert "_channel" in shelf.runs[0]        # its way home, for a late message
    assert shelf.shelved == []                # a research run is not a read_page


async def test_search_then_read_is_exactly_the_two_call_budget(
        cfg, guard, timers, controller, clock):
    """The default TOOL_MAX_CALLS_PER_TURN is 2, and find-it-then-read-it is
    two — the shape the web hands were sized for."""
    _web(guard, clock, "web_search", "read_page")
    chat = ScriptedChat([
        ['let me look. [[web_search {"query": "tea"}]]'],
        ['found one — [[read_page {"url": "https://example.invalid/overview"}]]'],
        ["it says tea is warm."],
    ])
    runner = FakeToolRunner()
    tb = make_toolbrain(cfg, guard, timers, controller, chat, runner=runner,
                        research=ShelfSpy())
    await collect(tb._stream_with_tools([], []))
    assert [t for t, _a in runner.calls] == ["web_search", "read_page"]


# ---------------------------------------------------------- the tools directive
# What she is *told* she has. A tool she can call but was never told the purpose
# of is a tool she doesn't use, which is indistinguishable from not having it.

def _spec(name: str, description: str) -> ToolSpec:
    return ToolSpec(name=name, description=description,
                    schema={"properties": {"topic": {}}, "required": ["topic"]})


def test_a_wrapped_description_reaches_her_whole():
    """The regression: `description.split("\\n")[0]` kept the first *physical*
    line of a wrapped docstring — 12% of `research`, and with it the sentence
    that says when to reach for it instead of `web_search`."""
    wrapped = ("Go and find out about something properly.\n"
               "Use this instead of `web_search` when the answer is going to\n"
               "take more than one page.")
    line = one_line(wrapped)
    assert "\n" not in line
    assert "instead of `web_search`" in line
    assert line.endswith("more than one page.")


def test_a_strangers_description_is_capped_at_a_word():
    """The cap is a bound on a mounted third-party server (§7.2), not on ours."""
    line = one_line("word " * 800)
    assert len(line) <= DESC_MAX_CHARS + 2      # + the ellipsis
    assert line.endswith("…") and not line.endswith("wor …")


def test_the_directive_carries_no_copyable_placeholder():
    """`[[tool_name {"arg": value}]]` sat in the grammar line as a metavariable
    and was emitted verbatim — ten `denied: not a tool she has` calls to a tool
    named `tool_name`. The concrete example teaches the same grammar."""
    text = build_directive([_spec("research", "Go and find out.")],
                           user_name="Sam", max_calls=2)
    assert "tool_name" not in text
    assert '[[set_timer {"minutes": 10, "label": "tea"}]]' in text   # still shown
    assert "- research(topic) — Go and find out." in text


# ------------------------------------------------------- fitting args to schema
# She writes JSON mid-sentence on a 12B model. An optional argument she fumbles
# must not cost the call the required arguments she got right.

RESEARCH_SCHEMA = {
    "properties": {"topic": {"type": "string"}, "depth": {"type": "integer"}},
    "required": ["topic"],
}


def test_prose_in_an_optional_number_is_dropped_not_fatal():
    """The live failure: `depth` was never documented, so she used it as a
    second free-text field and lost the whole research run to a pydantic error."""
    fixed = coerce_args(
        {"topic": "AI roleplay escalation", "depth": "current state and key stages"},
        RESEARCH_SCHEMA)
    assert fixed == {"topic": "AI roleplay escalation"}   # the tool's default applies


def test_a_quoted_number_is_coerced():
    assert coerce_args({"topic": "t", "depth": "4"}, RESEARCH_SCHEMA)["depth"] == 4
    assert coerce_args({"topic": "t", "depth": 4.0}, RESEARCH_SCHEMA)["depth"] == 4


def test_a_bad_required_arg_is_left_for_the_tool_to_reject():
    """Dropping it would turn a precise "must be an integer" into "field
    required" — the tool's own error is the more useful one to answer with."""
    schema = {"properties": {"minutes": {"type": "integer"}}, "required": ["minutes"]}
    assert coerce_args({"minutes": "a while"}, schema) == {"minutes": "a while"}


def test_a_bool_is_not_quietly_a_number():
    """True is an int in Python and never is in a model's intent."""
    assert coerce_args({"topic": "t", "depth": True}, RESEARCH_SCHEMA) == {"topic": "t"}


def test_undeclared_and_untyped_args_are_left_alone():
    """`anyOf`, or a key the server didn't declare: it knows its schema; we don't."""
    schema = {"properties": {"a": {"anyOf": [{"type": "integer"}, {"type": "string"}]}}}
    args = {"a": "either", "b": {"nested": 1}}
    assert coerce_args(args, schema) == args
    assert coerce_args(args, {}) == args           # no schema, no opinion
