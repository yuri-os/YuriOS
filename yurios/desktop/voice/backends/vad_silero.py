"""Silero VAD (SPEC §3.4) — turn-taking at the edge.

ch. 24's named default. Frame-level speech detection, local and cheap, so the mic
never round-trips a server just to know she should listen or stop talking — the
event that drives both endpointing (when has the user finished?) and barge-in
(is the user talking over her?).

Silero wants 16 kHz, 512-sample frames (~32 ms). `is_speech` returns the model's
speech probability against a threshold. Note the real VAD lives *at the edge* —
the frontend runs its own VAD in the browser for barge-in latency (web/voice.js);
this server-side copy is for endpointing and for a headless/CLI driver.
"""
from __future__ import annotations

import numpy as np

_INSTALL_HINT = ("silero-vad not installed. `pip install silero-vad`, or run "
                 "against the fake VAD: VAD_BACKEND=fake.")


class SileroVAD:
    def __init__(self, threshold: float = 0.5):
        try:
            from silero_vad import load_silero_vad
            import torch  # noqa: F401
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(_INSTALL_HINT) from e
        self._model = load_silero_vad()
        self.threshold = threshold

    def is_speech(self, frame: np.ndarray, sample_rate: int) -> bool:
        import torch
        frame = np.asarray(frame, dtype=np.float32).reshape(-1)
        if frame.size == 0:
            return False
        prob = float(self._model(torch.from_numpy(frame), sample_rate).item())
        return prob >= self.threshold

    def reset(self) -> None:
        if hasattr(self._model, "reset_states"):
            self._model.reset_states()
