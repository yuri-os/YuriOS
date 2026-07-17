"""The [[tool {json}]] marker parser (SPEC §7.4) — streaming-safe, junk-proof."""
from __future__ import annotations

from yurios.world.tooltags import MAX_MARKER_LEN, ToolTagParser


def push_all(parser: ToolTagParser, tokens: list[str]):
    spoken, calls = [], []
    for t in tokens:
        s, c = parser.push(t)
        spoken.append(s)
        calls.extend(c)
    return "".join(spoken) + parser.finish(), calls


def test_plain_text_passes_through():
    text, calls = push_all(ToolTagParser(), ["Hello ", "there."])
    assert text == "Hello there." and calls == []


def test_emotion_tags_pass_through_untouched():
    """Single brackets are B2's emotion channel — this parser runs upstream of
    the EmotionParser and must hand `[happy]` through byte-identical."""
    text, calls = push_all(ToolTagParser(), ["[happy] ", "Hey ", "you."])
    assert text == "[happy] Hey you." and calls == []


def test_whole_marker_in_one_token():
    text, calls = push_all(ToolTagParser(),
                           ['One sec. [[set_timer {"minutes": 10, "label": "tea"}]]'])
    assert text == "One sec. "
    assert len(calls) == 1
    assert calls[0].tool == "set_timer"
    assert calls[0].args == {"minutes": 10, "label": "tea"}


def test_marker_split_across_any_token_boundary():
    tokens = ["Sure. [", "[set_ti", 'mer {"min', 'utes": 1', "0}]", "] done"]
    text, calls = push_all(ToolTagParser(), tokens)
    assert text == "Sure. " + " done"
    assert calls[0].tool == "set_timer" and calls[0].args == {"minutes": 10}


def test_marker_with_no_args():
    _, calls = push_all(ToolTagParser(), ["[[get_weather]]"])
    assert calls[0].tool == "get_weather" and calls[0].args == {}


def test_bad_json_dropped_silently():
    text, calls = push_all(ToolTagParser(), ['ok [[set_timer {minutes: ten}]] fine'])
    assert calls == [] and text == "ok  fine"


def test_bad_tool_name_dropped():
    _, calls = push_all(ToolTagParser(), ['[[set timer! {"minutes": 1}]]'])
    assert calls == []


def test_args_not_an_object_dropped():
    _, calls = push_all(ToolTagParser(), ['[[set_timer [1, 2]]]'])
    assert calls == []


def test_oversized_marker_dropped_never_spoken():
    huge = "[[set_timer " + "x" * (MAX_MARKER_LEN + 50) + "]] after"
    text, calls = push_all(ToolTagParser(), [huge])
    assert calls == []
    assert "x" not in text and text.endswith(" after")


def test_unclosed_marker_dropped_at_end_of_stream():
    text, calls = push_all(ToolTagParser(), ['So [[set_timer {"minutes": 5'])
    assert calls == [] and text == "So "


def test_lone_open_bracket_at_end_is_flushed_as_text():
    p = ToolTagParser()
    s, _ = p.push("hmm [")
    assert s == "hmm "
    assert p.finish() == "["
