"""faster-whisper STT (SPEC §3.2) — the ears.

CTranslate2 Whisper; real-time on a laptop CPU at tiny/base (→ ch. 24 STT short
list). Tuned for latency over accuracy: a companion needs "close enough, now,"
not a perfect transcript (ch. 32 §4.2 — "tune for latency, not accuracy").

faster-whisper doesn't stream partials natively, so this buffers the utterance's
frames and transcribes on endpoint — correct, and simple. For true streaming
partials (words landing as you speak), swap to Moonshine or Parakeet behind this
same STT seam; nothing above it changes (SPEC §3.2).
"""
from __future__ import annotations

import numpy as np

_INSTALL_HINT = ("faster-whisper not installed. `pip install faster-whisper`, or "
                 "run against the fake ears: STT_BACKEND=fake.")


class WhisperSTT:
    def __init__(self, model: str = "base.en", compute_type: str = "int8",
                 no_speech_threshold: float = 0.6):
        try:
            from faster_whisper import WhisperModel
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(_INSTALL_HINT) from e
        self._model = WhisperModel(model, device="auto", compute_type=compute_type)
        self._frames: list[np.ndarray] = []
        self._sr = 16000
        # Drop segments Whisper itself flags as probably-not-speech. Non-speech
        # (keyboard clatter, a cough) is where Whisper hallucinates text out of
        # nothing; its own `no_speech_prob` is the cheapest signal that a segment
        # is noise (ch. 32 §4.2, with SpeechGate + the transcript filter behind it).
        self._no_speech_threshold = no_speech_threshold

    def reset(self) -> None:
        self._frames = []

    def feed(self, frame: np.ndarray, sample_rate: int) -> None:
        self._sr = sample_rate
        self._frames.append(np.asarray(frame, dtype=np.float32).reshape(-1))

    def final(self) -> str:  # pragma: no cover — needs the real model; fakes cover the loop
        if not self._frames:
            return ""
        audio = np.concatenate(self._frames)
        # Whisper wants 16 kHz mono float32; the frontend already sends that.
        segments, _ = self._model.transcribe(audio, language="en", beam_size=1,
                                              vad_filter=False)
        kept = (seg.text for seg in segments
                if getattr(seg, "no_speech_prob", 0.0) < self._no_speech_threshold)
        return "".join(kept).strip()
