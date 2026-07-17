"""Kokoro TTS (SPEC §3.3) — the default voice.

The book's pick for a *fixed* companion voice (→ ch. 24 TTS short list): 82M
params, faster-than-real-time on CPU, Apache-2.0, and — the reason it's the
default here — it leaves the whole GPU for the local LLM (ch. 24: "every gigabyte
the voice eats is a gigabyte the LLM can't"). Streams sentence-by-sentence so
time-to-first-audio is short.

Self-contained (uses the `kokoro` pip package directly). The sibling
`../../kokoro` reference impl is the fuller version — named registers + a
latency/quality eval harness — and is the one to read for the voice-as-versioned-
asset lesson. Swapping to the canon voice is one config line: TTS_BACKEND=gpt_sovits.
"""
from __future__ import annotations

import numpy as np

from ..protocols import AudioChunk
from ..sentences import cut_sentences

_INSTALL_HINT = (
    "Kokoro not installed. `pip install kokoro soundfile` and install espeak-ng "
    "(apt-get install espeak-ng / brew install espeak-ng). Or run against the "
    "fake voice: TTS_BACKEND=fake.")

# A small register→voice map (the ../kokoro impl has the full 54-voice table).
REGISTERS = {"default": "af_heart", "late_night": "af_nicole", "expressive": "af_bella"}


class KokoroTTS:
    sample_rate = 24000

    def __init__(self, register: str = "default"):
        try:
            from kokoro import KPipeline
        except ImportError as e:  # pragma: no cover - environment dependent
            raise RuntimeError(_INSTALL_HINT) from e
        self._pipeline = KPipeline(lang_code="a", repo_id="hexgrad/Kokoro-82M")
        self._voice = REGISTERS.get(register, register)

    def stream(self, text: str, register: str | None = None):
        voice = REGISTERS.get(register, register) if register else self._voice
        sentences, tail = cut_sentences(text)
        if tail.strip():
            sentences.append(tail.strip())
        for i, sentence in enumerate(sentences or [text]):
            audio = self._render(sentence, voice)
            yield AudioChunk(index=i, text=sentence, audio=audio,
                             sample_rate=self.sample_rate)

    def _render(self, sentence: str, voice: str) -> np.ndarray:
        parts = []
        for result in self._pipeline(sentence, voice=voice, speed=1.0):
            audio = result[-1] if isinstance(result, tuple) else result.audio
            if hasattr(audio, "detach"):
                audio = audio.detach().cpu().numpy()
            parts.append(np.asarray(audio, dtype=np.float32).reshape(-1))
        return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)
