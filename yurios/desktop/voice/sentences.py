"""Incremental sentence splitting for streaming TTS (SPEC §4.1, → ch. 24).

Build #1's TTS lesson: synthesize sentence-by-sentence so the first words play
while later ones are still rendering. But here the text arrives a token at a
time, so we can't split a finished string — we cut complete sentences off the
*front* of a growing buffer and keep the remainder. No model dependency, so it
is unit-testable on its own (mirrors ../kokoro/kokoro_voice/stream.py, which
splits an already-complete reply)."""
from __future__ import annotations

import re

_BOUNDARY = re.compile(r"(?<=[.!?…])\s+|\n+")


def cut_sentences(buffer: str) -> tuple[list[str], str]:
    """Return (complete sentences, remainder still being written).

    A sentence is complete once a terminator is followed by whitespace — until
    then it stays in the remainder, because the next token might extend it
    (`3.` could be the start of `3.14`)."""
    out: list[str] = []
    last = 0
    for m in _BOUNDARY.finditer(buffer):
        piece = buffer[last:m.start()].strip()
        if piece:
            out.append(piece)
        last = m.end()
    return out, buffer[last:]
