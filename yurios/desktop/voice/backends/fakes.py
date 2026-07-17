"""Deterministic fakes behind every voice seam (SPEC §13.3).

The point of the seams (like Build #1's provider seams) is that the loop runs
with no model installed — `pytest` is green on a laptop with no GPU, no torch,
no network. These fakes are scripted and instant, so the tests assert *loop
behaviour* (barge-in cancels the brain, sentences stream in order, a filler
fires before the first token) without any real inference.
"""
from __future__ import annotations

import asyncio

import numpy as np

from ..protocols import AudioChunk
from ..sentences import cut_sentences


class FakeSTT:
    """Returns a pre-scripted transcript regardless of the frames fed."""

    def __init__(self, transcript: str = "hey, i'm back"):
        self.transcript = transcript
        self.frames = 0

    def reset(self) -> None:
        self.frames = 0

    def feed(self, frame: np.ndarray, sample_rate: int) -> None:
        self.frames += 1

    def final(self) -> str:
        return self.transcript


class FakeTTS:
    """One AudioChunk per sentence; audio length ∝ text length, so tests can tell
    chunks apart. No sleep — instant, deterministic."""

    sample_rate = 24000

    def stream(self, text: str, register: str | None = None):
        sentences, tail = cut_sentences(text)
        if tail.strip():
            sentences.append(tail.strip())
        if not sentences:
            sentences = [text]
        for i, s in enumerate(sentences):
            n = max(1, len(s) * 10)
            yield AudioChunk(index=i, text=s,
                             audio=np.full(n, 0.01, dtype=np.float32),
                             sample_rate=self.sample_rate)


class FakeVAD:
    """Speech iff the frame's RMS clears the threshold. Lets a test drive
    barge-in by feeding a 'loud' frame."""

    def __init__(self, threshold: float = 0.1):
        self.threshold = threshold

    def is_speech(self, frame: np.ndarray, sample_rate: int) -> bool:
        if frame.size == 0:
            return False
        return float(np.sqrt(np.mean(frame.astype(np.float32) ** 2))) >= self.threshold

    def reset(self) -> None:
        pass


class FakeBrain:
    """Scripted reply stream + a persistence spy (SPEC §13.3).

    Yields tokens with a `await asyncio.sleep(0)` between them so a barge-in can
    land mid-stream, and records how many tokens it actually emitted — that count
    is how `test_turn_bargein` proves cancellation reached generation (it stops
    early). `persist` sets a flag so the test can assert a barged-in turn writes
    nothing."""

    def __init__(self, reply: str = "[happy] Hey, you made it back. [tender] I missed you today."):
        self.reply = reply
        self.tokens_emitted = 0
        self.persisted: tuple[str, str, str] | None = None
        self.persist_calls: list[tuple[str, str, str]] = []   # every turn that persisted
        self._gate: asyncio.Event | None = None

    def gate_after(self, n_tokens: int) -> asyncio.Event:
        """Return an Event that fires once `n_tokens` have been yielded — lets a
        test synchronize its barge-in to a known point in the stream."""
        self._gate_n = n_tokens
        self._gate = asyncio.Event()
        return self._gate

    async def stream_reply(self, session_id: str, text: str):
        self.tokens_emitted = 0
        # tokenize into word-ish tokens so a mid-stream cancel is observable
        for tok in _wordish(self.reply):
            self.tokens_emitted += 1
            if self._gate is not None and self.tokens_emitted >= getattr(self, "_gate_n", 0):
                self._gate.set()
            yield tok
            await asyncio.sleep(0)      # a real await point — cancellation lands here

    async def stream_greeting(self, session_id: str):
        for tok in _wordish("[happy] Oh, there you are."):
            yield tok
            await asyncio.sleep(0)

    def resolve_session(self, session_id: str | None) -> str:
        return session_id or "0" * 32

    async def persist(self, session_id: str, user_text: str, reply: str) -> None:
        self.persisted = (session_id, user_text, reply)
        self.persist_calls.append((session_id, user_text, reply))


def _wordish(text: str) -> list[str]:
    """Split into small tokens preserving spaces/brackets, like a real tokenizer."""
    out, cur = [], ""
    for ch in text:
        cur += ch
        if ch in " ]":
            out.append(cur)
            cur = ""
    if cur:
        out.append(cur)
    return out
