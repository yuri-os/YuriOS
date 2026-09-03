"""The [[tool {json}]] marker parser (SPEC §7.4) — streaming-safe, junk-proof."""
from __future__ import annotations

import json

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


def test_a_doubled_expression_tag_is_not_a_tool():
    """Found live: `[[tender]]` was parsed as a call and denied 'not a tool
    she has', which also stripped the face tag from speech."""
    text, calls = push_all(ToolTagParser(),
                           ["[[tender]] I missed you. [[happy]] There."])
    assert calls == []
    assert text == "[tender] I missed you. [happy] There."


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
    _, calls = push_all(ToolTagParser(), ["[[list_notes]]"])
    assert calls[0].tool == "list_notes" and calls[0].args == {}


def test_bad_json_dropped_silently():
    text, calls = push_all(ToolTagParser(), ['ok [[set_timer {minutes: ten}]] fine'])
    assert calls == [] and text == "ok  fine"


def test_bad_tool_name_dropped():
    _, calls = push_all(ToolTagParser(), ['[[set timer! {"minutes": 1}]]'])
    assert calls == []


def test_args_not_an_object_dropped():
    _, calls = push_all(ToolTagParser(), ['[[set_timer [1, 2]]]'])
    assert calls == []


def test_detailed_freeform_selfie_look_is_not_treated_as_oversized():
    look = "Amethyst skin in soft afternoon rain light. " * 20
    marker = '[[take_selfie {"look": ' + json.dumps(look) + '}]]'

    text, calls = push_all(ToolTagParser(), [marker])

    assert text == ""
    assert len(marker) > 400
    assert [(call.tool, call.args) for call in calls] == [
        ("take_selfie", {"look": look}),
    ]


def test_oversized_marker_dropped_never_spoken():
    huge = "[[set_timer " + "x" * (MAX_MARKER_LEN + 50) + "]] after"
    text, calls = push_all(ToolTagParser(), [huge])
    assert calls == []
    assert "x" not in text and text.endswith(" after")


def test_unclosed_marker_dropped_at_end_of_stream():
    """Truncated mid-JSON is still half a call, and half a call still drops."""
    p = ToolTagParser()
    text, calls = push_all(p, ['So [[set_timer {"minutes": 5'])
    assert calls == [] and text == "So "
    assert p.salvaged == [] and p.dropped == 1


def test_lone_open_bracket_at_end_is_flushed_as_text():
    p = ToolTagParser()
    s, _ = p.push("hmm [")
    assert s == "hmm "
    assert p.finish() == "["


# -- what a 12B model actually emits (see the module docstring) ----------------

def test_the_closer_may_have_a_space_in_it():
    """The live shape, verbatim from the model on every desk tool it was given:
    `}] ]`. `endswith("]]")` never fires on it, so the marker used to stay open
    and swallow the rest of the stream — her next sentences and her next marker
    with it."""
    p = ToolTagParser()
    text, calls = push_all(
        p, ['[tender] Let me see... ',
            '[[read_note {"path": "research/probe.md"}] ]'])
    assert text == "[tender] Let me see... "
    assert len(calls) == 1 and p.dropped == 0
    assert calls[0].tool == "read_note"
    assert calls[0].args == {"path": "research/probe.md"}


def test_a_split_closer_does_not_swallow_what_comes_after_it():
    """The consequence that made one stray space cost the whole turn: everything
    after the unclosed marker became marker body instead of speech."""
    p = ToolTagParser()
    text, calls = push_all(p, ['[[list_notes {"folder": ""}] ]',
                               " [shy] Oh, just the one for now."])
    assert calls[0].tool == "list_notes"
    assert text == " [shy] Oh, just the one for now."


def test_a_leftover_bracket_is_never_spoken():
    """Live, she writes `}] ]]` often enough that the third bracket reached the
    transcript as `. ][happy] Right now…` — and TTS then said it."""
    p = ToolTagParser()
    text, calls = push_all(
        p, ['before I tell you. [[list_notes {"folder": ""}] ]]', " more"])
    assert calls[0].tool == "list_notes"
    assert "]" not in text and text == "before I tell you.  more"


def test_a_bracket_that_is_not_adjacent_is_still_prose():
    """Only brackets touching the closer are eaten; a `]` in her sentence stays."""
    text, _ = push_all(ToolTagParser(), ["a [[list_notes]] ] b"])
    assert text == "a  ] b"


def test_a_newline_between_the_brackets_closes_too():
    _, calls = push_all(ToolTagParser(), ['[[set_timer {"minutes": 10}]\n]'])
    assert calls[0].args == {"minutes": 10}


def test_marker_a_bracket_short_is_salvaged():
    """The observed failure: object closed, `]]` written as `]`, stream ends.
    The call is whole — only the marker isn't — so it comes back in `salvaged`."""
    p = ToolTagParser()
    text, calls = push_all(p, ['One sec. [[set_timer {"minutes": 10}]'])
    assert text == "One sec. "
    assert calls == []                       # push had already returned for good
    assert len(p.salvaged) == 1 and p.dropped == 0
    assert p.salvaged[0].tool == "set_timer"
    assert p.salvaged[0].args == {"minutes": 10}
    assert p.calls == p.salvaged             # `calls` stays the whole record


def test_raw_newlines_inside_a_string_are_repaired():
    """A note is prose, and prose has line breaks. She escapes some of them."""
    body = '# Title\nline one\nline two'
    _, calls = push_all(ToolTagParser(),
                        ['[[write_note {"path": "a.md", "text": "%s"}]]' % body])
    assert calls[0].args == {"path": "a.md", "text": body}


def test_unescaped_quotes_inside_a_string_are_repaired():
    """`the way your "tired" changes` — verbatim from the transcript that started
    this. No leniency flag on `json.loads` recovers an inner bare quote."""
    _, calls = push_all(ToolTagParser(),
                        ['[[write_note {"path": "a.md", "text": "your "tired" '
                         'changes"}]]'])
    assert calls[0].args["text"] == 'your "tired" changes'


def test_repair_keeps_every_non_string_value_typed():
    """`json` still owns the scalars, lists and nested objects — the repair pass
    only takes the prose, so a repaired marker's numbers are numbers."""
    _, calls = push_all(ToolTagParser(), [
        '[[take_selfie {"look": "me, smiling\nby the window", "n": 2, '
        '"hd": true, "avoid": null, "tags": ["a", "b"], "size": {"w": 512}}]]'])
    assert calls[0].args == {"look": "me, smiling\nby the window", "n": 2,
                             "hd": True, "avoid": None, "tags": ["a", "b"],
                             "size": {"w": 512}}


def test_repair_refuses_anything_that_is_not_a_flat_object():
    """Loose about content, strict about shape: junk still drops."""
    for junk in ['[[set_timer {minutes: ten}]]',
                 '[[set_timer [1, 2]]]',
                 '[[set_timer {"minutes"}]]',
                 '[[set_timer {"a": 1,,}]]']:
        p = ToolTagParser()
        _, calls = push_all(p, [junk])
        assert calls == [] and p.dropped == 1, junk


def test_salvage_only_accepts_a_call_that_parses():
    """The salvage is self-validating — it cannot invent a call out of a
    fragment, so an unclosed marker holding junk is dropped, and counted."""
    p = ToolTagParser()
    _, calls = push_all(p, ['[[write_note {"path": "a.md", "text": '])
    assert calls == [] and p.salvaged == [] and p.dropped == 1
