"""Voice seams (SPEC §3) — the only vendor-facing surfaces in the voice layer.

Exactly like Build #1's `app.providers.base`, nothing else in the voice layer
imports an STT/TTS/VAD SDK directly. Swapping faster-whisper for Parakeet, or
Kokoro for GPT-SoVITS, is adding one file behind these Protocols and changing
config (→ ch. 24's short lists; SPEC §3.2–§3.4). The test suite passes fakes
(`backends.fakes`), so the whole loop runs offline with no model installed.

Audio convention, fixed once here so every stage agrees (SPEC §3.1):
    float32 mono, values in [-1, 1], sample rate carried alongside the array.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Iterator, Protocol

import numpy as np


@dataclass
class AudioChunk:
    """One synthesized span of speech, streamed out as it renders (SPEC §3.3).

    `text` is the clean spoken text (emotion tags already stripped, → emotion.py),
    kept so the frontend can caption and so the corpus sees what was actually said.
    """
    index: int
    text: str
    audio: np.ndarray          # float32 mono
    sample_rate: int


class STT(Protocol):
    """Ears (SPEC §3.2). Streaming: feed frames during speech, finalize on endpoint.

    The contract is deliberately incremental — `feed` during speech so only the
    last chunk is on the clock at endpoint time (the §4.2 latency trap)."""

    def reset(self) -> None:
        """Start a fresh utterance."""
        ...

    def feed(self, frame: np.ndarray, sample_rate: int) -> None:
        """Accept one mic frame mid-utterance (may update a partial transcript)."""
        ...

    def final(self) -> str:
        """Return the finished transcript for the utterance just ended."""
        ...


class TTS(Protocol):
    """Voice (SPEC §3.3). Streams sentence-by-sentence so audio starts before the
    whole reply is synthesized — time-to-first-audio is the number that matters
    (→ ch. 24 streaming)."""

    sample_rate: int

    def stream(self, text: str, register: str | None = None) -> Iterator[AudioChunk]:
        """Yield AudioChunks as each sentence finishes rendering."""
        ...


class VAD(Protocol):
    """Turn-taking (SPEC §3.4). Frame-level speech detection, local and cheap, so
    the mic never round-trips a server just to know she should listen or stop
    (→ ch. 24 barge-in). Silero is the default."""

    def is_speech(self, frame: np.ndarray, sample_rate: int) -> bool:
        """True if this frame contains speech (probability ≥ threshold)."""
        ...

    def reset(self) -> None:
        """Clear any internal state between utterances."""
        ...


class ReplyBrain(Protocol):
    """The brain seam, from the loop's point of view (SPEC §3.5).

    Build #1's assemble+recall+SOUL+model, wrapped so the TurnController depends
    on *behaviour*, not on `app.*` internals — which is what lets the barge-in
    test drive the loop with a fake brain. One method: given the user's
    transcript, stream reply tokens; cancellable by dropping the iterator."""

    def stream_reply(self, session_id: str, text: str) -> AsyncIterator[str]:
        ...

    async def persist(self, session_id: str, user_text: str, reply: str) -> None:
        """Run Build #1's post-turn pipeline (journal, index, USER.md, commit,
        corpus) off the hot path. Never called on a barged-in (incomplete) turn."""
        ...
