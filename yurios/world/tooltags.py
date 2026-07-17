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
  - junk-proof: unknown tools, malformed JSON, oversized markers, and a marker
    still open at end-of-stream are dropped silently (logged, never spoken) —
    a 12B local model *will* eventually emit a broken one (SPEC §7.4).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

log = logging.getLogger("world.tooltags")

# A well-formed marker is a tool name + a small JSON object. Anything growing
# past this is the model rambling inside double brackets — drop it (SPEC §7.4).
MAX_MARKER_LEN = 400


@dataclass
class ToolCall:
    """One parsed [[tool {json}]] marker."""
    tool: str
    args: dict


@dataclass
class ToolTagParser:
    """Feed raw model tokens; get speakable text + closed tool calls out.

    Stateful and streaming — one instance per model pass. `push` returns
    (speakable_text, calls_closed_on_this_token)."""

    calls: list[ToolCall] = field(default_factory=list)
    _hold: str = ""          # a lone '[' waiting to learn if it opens a marker
    _buf: str = ""           # marker body (after '[[')
    _in_marker: bool = False
    _drop: bool = False      # oversized marker: discard to the closing ']]'

    def push(self, token: str) -> tuple[str, list[ToolCall]]:
        out = ""
        new_calls: list[ToolCall] = []
        for ch in token:
            if self._drop:
                self._buf += ch
                if self._buf.endswith("]]"):
                    self._drop, self._in_marker, self._buf = False, False, ""
                continue
            if self._in_marker:
                self._buf += ch
                if self._buf.endswith("]]"):
                    call = self._close(self._buf[:-2])
                    if call is not None:
                        self.calls.append(call)
                        new_calls.append(call)
                    self._in_marker, self._buf = False, ""
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
        """End of stream: flush a held '[' as text; drop an unclosed marker —
        half a tool call must never be spoken (SPEC §7.4)."""
        tail = self._hold
        self._hold = ""
        if self._in_marker or self._drop:
            log.warning("unclosed tool marker dropped at end of stream")
            self._in_marker = self._drop = False
            self._buf = ""
        return tail

    @staticmethod
    def _close(body: str) -> ToolCall | None:
        """Parse 'tool_name {json}' → ToolCall, or None if malformed."""
        body = body.strip()
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
            log.warning("bad JSON in tool marker for %r: %r", name, rest[:80])
            return None
        if not isinstance(args, dict):
            log.warning("tool marker args not an object for %r", name)
            return None
        return ToolCall(name, args)
