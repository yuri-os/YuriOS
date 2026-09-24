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
  - `finish` salvages a marker that never closed but is otherwise complete;
  - `_close` splits the name at the first non-identifier, so a missing space
    (`read_note{...}`) still yields the object, and a parenthesized call
    (`read_note("path")`, `read_note({"path": "…"})`) is the same call.
The recoveries are self-validating — they only ever produce a call that parses —
so junk still ends up dropped, and `dropped` now counts it so the caller can say so.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
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


# ---- a model's own call markup ------------------------------------------------
#: DeepSeek's tool-call markup (DSML). It is trained on it, and no call here
#: declares tools the API way — every hand is a line of text she writes — so
#: when it wants a hand it sometimes writes this instead of the marker it was
#: shown: in a reply's continuation pass, in a goal step, in a compose call with
#: no hands at all. The provider passes it through as content. Unread, it was
#: spoken as the reply (24 Sep, twice: the whole of a reach-out, and the tail of
#: an answer that had just used `list_notes` correctly). The bars are fullwidth
#: (U+FF5C) and come back single or doubled; the tag names come back with a
#: space where `function_` was.
_BAR = r"[｜|]+"
_DSML = rf"<\s*/?\s*{_BAR}\s*DSML\s*{_BAR}"
#: A whole block. One cut off before its closer (a clipped desk line) ends at
#: the first blank line: the markup never has one, and running on to the end of
#: the text would take every entry after it too.
_DSML_BLOCK = re.compile(
    _DSML + r"\s*(?:function_)?\s*calls\s*>.*?(?:" + _DSML
    + r"\s*(?:function_)?\s*calls\s*>|(?=\n[ \t]*\n)|\Z)", re.S | re.I)
_DSML_TAG = re.compile(_DSML + r"[^\n>]*>?", re.I)
_DSML_INVOKE = re.compile(
    _DSML + r'\s*invoke\s+name\s*=\s*"([^"]+)"\s*>(.*?)(?:' + _DSML
    + r"\s*invoke\s*>|\Z)", re.S | re.I)
_DSML_PARAM = re.compile(
    _DSML + r'\s*parameter\s+name\s*=\s*"([^"]+)"'
    r'(?:\s+string\s*=\s*"(true|false)")?\s*>(.*?)' + _DSML
    + r"\s*parameter\s*>", re.S | re.I)
_DSML_ANY = re.compile(r"<\s*/?\s*" + _BAR + r"\s*DSML", re.I)
#: The stream's view: what a `<` has to grow into before it is held back from
#: speech, and the two closers that end a block.
_NATIVE_PREFIX = re.compile(r"<\s*/?\s*(?:" + _BAR + r"\s*(?:D|DS|DSM)?)?", re.I)
_NATIVE_OPEN = re.compile(r"<\s*/?\s*" + _BAR + r"\s*DSML", re.I)
_NATIVE_WRAPPED = re.compile(r"<\s*" + _BAR + r"\s*DSML\s*" + _BAR
                             + r"\s*(?:function_)?\s*calls", re.I)
_NATIVE_END_CALLS = re.compile(r"<\s*/\s*" + _BAR + r"\s*DSML\s*" + _BAR
                               + r"\s*(?:function_)?\s*calls\s*>$", re.I)
_NATIVE_END_INVOKE = re.compile(r"<\s*/\s*" + _BAR + r"\s*DSML\s*" + _BAR
                                + r"\s*invoke\s*>$", re.I)
#: A block this long is not a call being written; it is a loop.
MAX_NATIVE_LEN = 20_000


def _native_invokes(text: str) -> list[tuple[str, dict]]:
    """Every call in native markup, in order, as (tool, args).

    `string="true"` marks a raw string; anything else is a JSON value, kept as
    the raw text when it doesn't parse — the server refusing a bad argument is
    a sentence she can read, which beats guessing what she meant.
    """
    out: list[tuple[str, dict]] = []
    for found in _DSML_INVOKE.finditer(text or ""):
        args: dict = {}
        for name, is_string, value in _DSML_PARAM.findall(found.group(2)):
            if is_string.lower() != "false":
                args[name] = value
                continue
            try:
                args[name] = json.loads(value)
            except ValueError:
                args[name] = value
        out.append((found.group(1).strip(), args))
    return out


def native_call(text: str) -> tuple[str, dict] | None:
    """The first call written in a model's native markup, as (tool, args)."""
    calls = _native_invokes(text)
    return calls[0] if calls else None


def strip_native_calls(text: str) -> str:
    """`text` with any native tool-call markup taken out — never shown, never kept."""
    if not text or not _DSML_ANY.search(text):
        return text
    text = _DSML_TAG.sub("", _DSML_BLOCK.sub("", text))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def native_to_markers(text: str) -> str:
    """Native call markup rewritten as the `[[tool {json}]]` she was shown.

    For the verbatim record a turn keeps (§7.4): what she reads back next turn
    is her own history, and a history full of the other format teaches it.
    """
    if not text or not _DSML_ANY.search(text):
        return text

    def marker(block: re.Match) -> str:
        return "".join(f"[[{tool} {json.dumps(args, ensure_ascii=False)}]]"
                       for tool, args in _native_invokes(block.group(0)))

    return _DSML_TAG.sub("", _DSML_BLOCK.sub(marker, text))


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


#: A tool name is an identifier. Splitting on the first space used to drop the
#: live GLM shape `read_note{"path": "…"}` (no space) and the listing-copy
#: `read_note("path")` before `_repair` ever saw the arguments.
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _split_call(body: str) -> tuple[str, str] | None:
    """`name`, `name {json}`, `name{json}`, `name("v")` → (name, rest)."""
    body = _trim(body.strip())
    if not body:
        return None
    m = _NAME.match(body)
    if m is None:
        return None
    return m.group(0), body[m.end():].strip()


def _loads_obj(text: str) -> dict | None:
    """A JSON object, or the same object after `_repair`. Else None."""
    try:
        args = json.loads(text)
    except ValueError:
        args = _repair(text)
        if args is None:
            return None
    return args if isinstance(args, dict) else None


def _kwargs(inner: str) -> dict | None:
    """`path="x", minutes=10` → dict. None if this isn't keyword arguments."""
    text = inner.strip()
    if not text or not _NAME.match(text):
        return None
    out: dict = {}
    p = 0
    while True:
        while p < len(text) and text[p] in " \t\n\r,":
            p += 1
        if p >= len(text):
            return out or None
        key = _NAME.match(text, p)
        if key is None:
            return None
        p = key.end()
        while p < len(text) and text[p] in " \t":
            p += 1
        if p >= len(text) or text[p] not in "=:":
            return None
        p += 1
        while p < len(text) and text[p] in " \t":
            p += 1
        try:
            value, p = json.JSONDecoder().raw_decode(text, p)
        except ValueError:
            return None
        out[key.group(0)] = value


def _from_rest(rest: str, keys: Sequence[str]) -> dict | None:
    """Arguments after the tool name — JSON, glued JSON, or a parenthesized call."""
    if not rest:
        return {}
    if rest.startswith("{"):
        return _loads_obj(rest)
    if rest.startswith("(") and rest.endswith(")"):
        inner = rest[1:-1].strip()
        if not inner:
            return {}
        if inner.startswith("{"):
            return _loads_obj(inner)
        kw = _kwargs(inner)
        if kw is not None:
            return kw
        try:
            values = json.loads(f"[{inner}]")
        except ValueError:
            return None
        if not isinstance(values, list) or not keys or len(values) > len(keys):
            return None
        return dict(zip(keys, values))
    return None


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
    #: Property names in schema order, keyed by tool. Positional
    #: `read_note("path")` zips onto these; unknown tools still drop.
    arg_names: Mapping[str, Sequence[str]] = field(default_factory=dict)
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
    #: A `<` that may be opening a model's own call markup, held until it
    #: either grows into one or plainly isn't (`<3`, `a < b`).
    _angle: str = ""
    #: A native markup block being read, or None (see `_DSML` above).
    _native: str | None = None
    #: How much of the last `push`'s speakable text came before the first call
    #: that closed in it, or None when none did. A caller that ends its pass on
    #: that call keeps only this much: the model has often started on what it
    #: expects next in the same chunk — a `((` copying the tool-result line,
    #: which reached her reply as words (SPEC §7.4).
    said_before: int | None = None

    def push(self, token: str) -> tuple[str, list[ToolCall]]:
        out = ""
        new_calls: list[ToolCall] = []
        cut: int | None = None
        for index, ch in enumerate(token):
            if self._native is not None:
                call = self._native_push(ch)
                if call is not None:
                    self.calls.append(call)
                    new_calls.append(call)
                    cut = len(out) if cut is None else cut
                continue
            if self._angle:
                self._angle += ch
                if _NATIVE_OPEN.match(self._angle):
                    self._native, self._angle = self._angle, ""
                elif not _NATIVE_PREFIX.fullmatch(self._angle):
                    # Not markup after all: say the `<`, and read the rest
                    # again — it may open a marker of its own.
                    held, self._angle = self._angle[1:], ""
                    more, calls = self.push(held + token[index + 1:])
                    if cut is None and calls:
                        cut = len(out) + 1 + (self.said_before or 0)
                    new_calls += calls
                    self.said_before = cut
                    return out + "<" + more, new_calls
                continue
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
                            cut = len(out) if cut is None else cut
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
            elif ch == "<":
                self._angle = "<"
            else:
                out += ch
        self.said_before = cut
        return out, new_calls

    def _native_push(self, ch: str) -> ToolCall | None:
        """One character of a native block; the call, once the block closes."""
        assert self._native is not None
        self._native += ch
        buf = self._native
        if re.match(r"<\s*/", buf):
            # A closing tag with no block open — the tail of one already read,
            # or a stray. Nothing to say and nothing to call.
            if ch == ">":
                self._native = None
            return None
        if len(buf) > MAX_NATIVE_LEN:
            log.warning("oversized native call markup dropped (%d chars)", len(buf))
            self._native = None
            self.dropped += 1
            return None
        if ch != ">":
            return None
        wrapped = _NATIVE_WRAPPED.match(buf) is not None
        if not (_NATIVE_END_CALLS.search(buf)
                or (not wrapped and _NATIVE_END_INVOKE.search(buf))):
            return None
        self._native = None
        return self._native_call(buf)

    def _native_call(self, block: str) -> ToolCall | None:
        found = native_call(block)
        if found is None:
            log.warning("native call markup with no call in it: %r", block[:80])
            self.dropped += 1
            return None
        log.info("read a %s call from native markup", found[0])
        return ToolCall(*found)

    def finish(self) -> str:
        """End of stream: flush a held '[' as text; salvage or drop an unclosed
        marker — half a tool call must never be spoken (SPEC §7.4).

        A marker open at end-of-stream is usually not half a call. It is a whole
        call that ran out of brackets: the model closed the object, wrote `]`, and
        stopped. Try to close it ourselves — stripping the trailing brackets it
        did emit — and keep the result only if it parses. Recovered calls land in
        `salvaged`, because `push` has already returned for the last time.
        """
        tail = self._hold + self._angle
        self._hold = self._angle = ""
        if self._native is not None:
            block, self._native = self._native, None
            if not re.match(r"<\s*/", block):
                call = self._native_call(block)
                if call is not None:
                    self.calls.append(call)
                    self.salvaged.append(call)
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

    def _close(self, body: str) -> ToolCall | None:
        """Parse a marker body → ToolCall, or None if malformed.

        `_trim` first, so a bracket the model left over — `{…}]`, the tail of the
        `] ]` closer it likes to write — is forgiven here once, for every caller,
        instead of at each of the three places a body arrives from. The name is
        the leading identifier, not `partition(" ")`: a glued `{` or a
        parenthesized call is still a call (SPEC §7.4)."""
        split = _split_call(body)
        if split is None:
            log.warning("bad tool name in marker: %r", body[:80])
            return None
        name, rest = split
        args = _from_rest(rest, tuple(self.arg_names.get(name) or ()))
        if args is None:
            log.warning("bad JSON in tool marker for %r: %r", name, rest[:80])
            return None
        return ToolCall(name, args)
