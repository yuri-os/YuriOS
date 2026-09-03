"""Streaming tool-call markers (SPEC §7.4) — the sibling of B2's emotion parser.

The model's reply stream carries tool calls inline:

    Sure — give me a second. [[set_timer {"minutes": 10, "label": "tea"}]]

Double brackets, because single brackets are already the emotion-tag channel
(B2 §6) — and this parser runs *upstream* of that one (inside ToolBrain, before
the TurnController's EmotionParser ever sees the tokens), so a `[happy]` passes
through untouched while a `[[…]]` is extracted whole.

Same disciplines as `desktop/voice/emotion.py`, tolerant by contract:
  - streaming-safe: a marker can split across any token boundary (`[[set_ti`,
    `mer {"min`, `utes": 10}]]`);
  - stripped from speech: she never reads a tool call aloud;
  - junk-proof: unknown tools, unreadable JSON and oversized markers are dropped
    (logged, never spoken) — a 12B local model *will* emit a broken one (§7.4).

"Junk-proof" was doing too much work in that last line, though. The tools whose
argument is *prose* — `write_note`, `append_note`, the selfie `look` — ask the
model to be a JSON serializer for a paragraph it is composing as it speaks, and
a 12B model is not one. What it actually emits, observed in the wild:

    [[write_note {"path": "research/learning_you.md", "text": "# How I …
    1. **The Echoes of the Past**
    … the way your "tired" changes from one day to the next …"}]

Three separate defects in one marker: raw newlines inside the string, an
unescaped `"` inside it, and a closing `}]` a bracket short. Any one of them
made `json.loads` fail or the marker never close, and the whole call — a note
she had already written every word of — was dropped with a log line and no other
trace. She then read her own marker back out of the transcript next turn and
reported the document existed. A silent drop doesn't just lose the call; it
teaches her that saying a tool's name is the same as using it.

And the closer itself is not reliably two adjacent brackets. Driving the live
model through every desk tool, the shape it emits — every time, for every tool —
is a closer with a **space inside it**:

    [[read_note {"path": "research/probe.md"}] ]

`endswith("]]")` never fires on that, so the marker stayed open and swallowed the
rest of the stream: her next sentences, her next marker, everything up to the next
accidental `]]`. One misplaced space cost the whole turn, which is why the first
attempt failed on all nine tools and only the re-emit pass ever landed.

So the tolerance goes one step further than "drop it cleanly":
  - `_CLOSE` accepts any two brackets separated by whitespace as the closer, and
    `_trim` forgives a stray one left over inside the body;
  - `_repair` re-reads a malformed object leniently, letting `json` handle every
    scalar and taking prose strings literally to their real terminator;
  - `finish` salvages a marker that never closed but is otherwise complete.
The last three are self-validating — they only ever produce a call that parses —
so junk still ends up dropped, and `dropped` now counts it so the caller can say so.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger("world.tooltags")

# Free-form tool arguments such as a detailed selfie `look` can legitimately be
# long. Keep a bounded buffer, but leave enough room for the advertised schema.
MAX_MARKER_LEN = 4096

#: Expression tags from `desktop/voice/emotion.py` PALETTE. The model is told
#: to write `[tender]`; live it sometimes writes `[[tender]]`, which this
#: parser used to treat as a tool and the Guard then denied ("not a tool she
#: has"). Keep in sync with PALETTE — a name here is a face, never a hand.
EXPRESSION_NAMES = frozenset({
    "neutral", "happy", "sad", "surprised",
    "shy", "thinking", "playful", "tender",
})


def _as_expression(body: str) -> str | None:
    """`[[tender]]` is a doubled emotion tag, not a tool (SPEC §7.4)."""
    body = _trim(body.strip()) if body else ""
    name, _, _rest = body.partition(" ")
    name = name.strip().lower()
    return name if name in EXPRESSION_NAMES else None


@dataclass
class ToolCall:
    """One parsed [[tool {json}]] marker."""
    tool: str
    args: dict


#: The closer, as the model actually writes it: two brackets, possibly with
#: whitespace between them (`]]`, `] ]`, `]\n]`). Anchored at the end because it
#: is tested against the buffer after every character.
_CLOSE = re.compile(r"\]\s*\]$")
#: A stray closing bracket the model left inside the body (`{…}]` → `{…}`), plus
#: any whitespace around it. Only ever strips the tail *after* the arguments.
_STRAY = re.compile(r"[\s\]]+$")


def _trim(body: str) -> str:
    return _STRAY.sub("", body)


#: `"key":` at the head of what's left — a model writes short identifier keys,
#: which is what makes the prose/structure boundary findable at all.
_KEY = re.compile(r'\s*"([A-Za-z_][A-Za-z0-9_]*)"\s*:\s*')
_SEP = re.compile(r"\s*,\s*")
#: The closing quote of a string value: the one followed by the next key, or by
#: the end of the object. Everything before it belongs to the prose, quotes and
#: newlines included — which is the whole point of not using `json` here.
_VALUE_END = re.compile(r'"(?=\s*,\s*"[A-Za-z_][A-Za-z0-9_]*"\s*:|\s*\Z)')

_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\",
            "/": "/", "b": "\b", "f": "\f"}


def _unescape(text: str) -> str:
    """JSON string escapes, applied by hand — the half of the encoding the model
    did get right. An unknown escape is kept verbatim rather than dropped."""
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            if nxt in _ESCAPES:
                out.append(_ESCAPES[nxt])
                i += 2
                continue
            if nxt == "u" and len(text) >= i + 6:
                try:
                    out.append(chr(int(text[i + 2:i + 6], 16)))
                    i += 6
                    continue
                except ValueError:
                    pass
        out.append(ch)
        i += 1
    return "".join(out)


def _repair(rest: str) -> dict | None:
    """A malformed argument object, read leniently — or None if it isn't one.

    Only the *string* values are read by hand; `json.raw_decode` still takes
    every number, bool, null, list and nested object, because those are the
    parts a model gets right and hand-rolling them would only add ways to be
    wrong. A string runs to `_VALUE_END`, so an unescaped quote or a literal
    newline in the middle of a paragraph costs nothing.

    Deliberately strict about *shape* while loose about content: anything that
    isn't a flat `{"key": value, …}` returns None and the marker drops as before.
    """
    text = rest.strip()
    if not (text.startswith("{") and text.endswith("}")):
        return None
    inner = text[1:-1]
    out: dict = {}
    p = 0
    while True:
        if not inner[p:].strip():          # trailing comma, or an empty object
            return out
        key = _KEY.match(inner, p)
        if key is None:
            return None
        p = key.end()
        if p < len(inner) and inner[p] == '"':
            end = _VALUE_END.search(inner, p + 1)
            if end is None:
                return None
            out[key.group(1)] = _unescape(inner[p + 1:end.start()])
            p = end.end()
        else:
            try:                            # every non-prose value, json's job
                value, p = json.JSONDecoder().raw_decode(inner, p)
            except ValueError:
                return None
            out[key.group(1)] = value
        sep = _SEP.match(inner, p)
        if sep is None:
            return out if not inner[p:].strip() else None
        p = sep.end()


@dataclass
class ToolTagParser:
    """Feed raw model tokens; get speakable text + closed tool calls out.

    Stateful and streaming — one instance per model pass. `push` returns
    (speakable_text, calls_closed_on_this_token)."""

    calls: list[ToolCall] = field(default_factory=list)
    #: Calls recovered by `finish` from a marker that never closed — the caller
    #: has already left its streaming loop by then, so they are handed over here
    #: rather than through `push`.
    salvaged: list[ToolCall] = field(default_factory=list)
    #: Markers that were dropped: junk, but junk she *meant* as a call. The turn
    #: loop reads this to tell her the call didn't land instead of letting her
    #: believe it did.
    dropped: int = 0
    _hold: str = ""          # a lone '[' waiting to learn if it opens a marker
    _buf: str = ""           # marker body (after '[[')
    _in_marker: bool = False
    _drop: bool = False      # oversized marker: discard to the closing ']]'
    #: Just closed a marker — eat any further ']' before resuming speech. The
    #: model writes three brackets often enough (`}] ]]`) that the leftover one
    #: reached the transcript as `. ][happy] Right now…`, which she then "said".
    _after: bool = False

    def push(self, token: str) -> tuple[str, list[ToolCall]]:
        out = ""
        new_calls: list[ToolCall] = []
        for ch in token:
            if self._after:                       # leftover brackets, never spoken
                if ch == "]":
                    continue
                self._after = False
            if self._drop:
                self._buf += ch
                if _CLOSE.search(self._buf):
                    self._drop, self._in_marker, self._buf = False, False, ""
                    self._after = True
                    self.dropped += 1
                continue
            if self._in_marker:
                self._buf += ch
                closer = _CLOSE.search(self._buf) if ch == "]" else None
                if closer is not None:
                    raw = self._buf[:closer.start()]
                    tag = _as_expression(raw)
                    if tag:
                        out += f"[{tag}]"
                    else:
                        call = self._close(raw)
                        if call is not None:
                            self.calls.append(call)
                            new_calls.append(call)
                        else:
                            self.dropped += 1
                    self._in_marker, self._buf = False, ""
                    self._after = True
                elif len(self._buf) > MAX_MARKER_LEN:
                    log.warning("oversized tool marker dropped (%d chars)", len(self._buf))
                    self._drop = True
                continue
            if self._hold:                       # previous char was a lone '['
                self._hold = ""
                if ch == "[":                    # '[[' → a marker opens
                    self._in_marker, self._buf = True, ""
                else:                            # ordinary '[' (an emotion tag) — pass through
                    out += "[" + ch
                continue
            if ch == "[":
                self._hold = "["
            else:
                out += ch
        return out, new_calls

    def finish(self) -> str:
        """End of stream: flush a held '[' as text; salvage or drop an unclosed
        marker — half a tool call must never be spoken (SPEC §7.4).

        A marker open at end-of-stream is usually not half a call. It is a whole
        call that ran out of brackets: the model closed the object, wrote `]`, and
        stopped. Try to close it ourselves — stripping the trailing brackets it
        did emit — and keep the result only if it parses. Recovered calls land in
        `salvaged`, because `push` has already returned for the last time.
        """
        tail = self._hold
        self._hold = ""
        if self._in_marker or self._drop:
            tag = None if self._drop else _as_expression(self._buf)
            call = None if (self._drop or tag) else self._close(self._buf)
            if tag:
                tail += f"[{tag}]"
            elif call is not None:
                log.info("salvaged an unclosed %s marker at end of stream", call.tool)
                self.calls.append(call)
                self.salvaged.append(call)
            else:
                log.warning("unclosed tool marker dropped at end of stream")
                self.dropped += 1
            self._in_marker = self._drop = False
            self._buf = ""
        return tail

    @staticmethod
    def _close(body: str) -> ToolCall | None:
        """Parse 'tool_name {json}' → ToolCall, or None if malformed.

        `_trim` first, so a bracket the model left over — `{…}]`, the tail of the
        `] ]` closer it likes to write — is forgiven here once, for every caller,
        instead of at each of the three places a body arrives from."""
        body = _trim(body.strip())
        if not body:
            return None
        name, _, rest = body.partition(" ")
        name = name.strip()
        if not name.replace("_", "").isalnum():
            log.warning("bad tool name in marker: %r", name)
            return None
        rest = rest.strip()
        if not rest:
            return ToolCall(name, {})
        try:
            args = json.loads(rest)
        except ValueError:
            args = _repair(rest)
            if args is None:
                log.warning("bad JSON in tool marker for %r: %r", name, rest[:80])
                return None
            log.info("repaired the argument JSON in a %r marker (%d chars)",
                     name, len(rest))
        if not isinstance(args, dict):
            log.warning("tool marker args not an object for %r", name)
            return None
        return ToolCall(name, args)
