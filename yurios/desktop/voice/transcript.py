"""Transcript sanity filter (SPEC §3.2, §4.2) — reject non-speech that leaked past the VAD.

Even with debounced turn-taking (`speech_gate.SpeechGate`), impulsive noise — a
mechanical keyboard under the mic while she talks, a cough, a door — occasionally
clears the gate, and faster-whisper, asked to transcribe it, does not return an
empty string: it *hallucinates*, most often as bare punctuation (". . . .",
"...") . Persisted, those become junk turns in her memory — the literal
"you: . . . . . ." lines this filter was written against.

`is_meaningful_transcript` is the cheap last line of defence at the text boundary:
a transcript with no alphanumeric content is noise, not a turn, and is dropped
before it ever reaches the brain or the Vault. It is deliberately conservative —
it rejects only what *cannot* be speech — so it never eats a real, if terse,
utterance ("ok", "mm", "8", "가").
"""
from __future__ import annotations

import re

# Any letter or digit in any language (Unicode word char, minus underscore).
# Matches "ok", "8", "가"; does not match ".", "…", "-", "?", whitespace.
_ALNUM = re.compile(r"[^\W_]", re.UNICODE)


def is_meaningful_transcript(text: str | None) -> bool:
    """True if `text` holds real spoken content worth taking as a turn.

    False for None / empty / whitespace and for punctuation-only hallucinations
    (". . . .", "...", "- -", "??"). One alphanumeric character is enough to pass.
    """
    if not text:
        return False
    return _ALNUM.search(text) is not None
