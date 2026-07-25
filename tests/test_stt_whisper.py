"""faster-whisper STT backend tests."""
from __future__ import annotations

import sys
from types import SimpleNamespace

from yurios.desktop.voice.backends.stt_whisper import WhisperSTT


def test_whisper_uses_cpu_to_avoid_wsl_cuda_runtime(monkeypatch):
    created = {}

    class FakeWhisperModel:
        def __init__(self, model, **kwargs):
            created.update(model=model, **kwargs)

    monkeypatch.setitem(
        sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=FakeWhisperModel)
    )

    WhisperSTT("base.en", "int8")

    assert created == {"model": "base.en", "device": "cpu", "compute_type": "int8"}
