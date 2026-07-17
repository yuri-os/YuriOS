"""Latency masking — instant acknowledgments (SPEC §5, → ch. 24 "Masking latency").

The hardest stage to shrink is time-to-first-audio: STT endpointing + the LLM's
first tokens + the TTS's first render all stack before she makes a sound. The fix
isn't always a faster model — it's covering the gap with speech that doesn't
depend on the answer. The instant the user's turn ends, *before* the LLM has a
token, she plays a short content-free reaction ("mm—", "hm, okay—", a breath).
Real conversation is full of these; it reads as attentiveness, not lag.

Two rules from ch. 24 make it honest, and both are enforced by the caller
(TurnController):
  - The clips are **pre-rendered once** and cached, so firing one is tens of
    milliseconds, not a TTS round-trip.
  - Filler is real audio, so it is **interruptible audio**: the same barge-in
    path that kills a reply kills a filler. A filler you can't interrupt is worse
    than the silence it replaced.

Tune the bank to the persona (SPEC §5.1): a warm companion's "mm, let me
think…" lands very differently from a deadpan one's clipped "okay." These are a
persona asset, versioned like any voice clip.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

import numpy as np

from .protocols import TTS, AudioChunk

# Warm-companion bank (SPEC §5.1). Short, content-free, no commitment to an answer.
DEFAULT_PHRASES = ("Mm.", "Hm, okay.", "Let me think.", "Mm, one sec.", "Oh—")


@dataclass
class FillerBank:
    """Pre-renders a small bank of acknowledgments and hands one back at random.

    Built once at startup (`prime`), then `pick()` is O(1) and allocation-free —
    it returns already-synthesized audio, so it fires in the tens-of-ms the
    masking trick needs (SPEC §5)."""

    tts: TTS
    phrases: tuple[str, ...] = DEFAULT_PHRASES
    register: str | None = None
    _clips: list[AudioChunk] = field(default_factory=list)
    _last: int = -1

    def prime(self) -> None:
        """Synthesize every phrase once. Call at startup, off the hot path."""
        self._clips = []
        for i, phrase in enumerate(self.phrases):
            audio = _concat(self.tts.stream(phrase, self.register))
            self._clips.append(
                AudioChunk(index=i, text=phrase, audio=audio,
                           sample_rate=self.tts.sample_rate))

    def pick(self) -> AudioChunk | None:
        """A random clip, never the same one twice in a row (so it doesn't loop)."""
        if not self._clips:
            return None
        i = random.randrange(len(self._clips))
        if len(self._clips) > 1 and i == self._last:
            i = (i + 1) % len(self._clips)
        self._last = i
        return self._clips[i]


def _concat(chunks) -> np.ndarray:
    parts = [c.audio for c in chunks]
    if not parts:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(parts)
