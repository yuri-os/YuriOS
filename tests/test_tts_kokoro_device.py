"""The default voice stays off the GPU (SPEC §3.3, ch. 24).

Kokoro is the default *because* it is 82M params on a CPU: every gigabyte the
voice eats is a gigabyte the LLM can't have, and on this hardware the LLM is
already holding ~14 of the 16 there are. But `KPipeline`'s own default is
`device='cuda' if torch.cuda.is_available() else 'cpu'` — so on any box with a
CUDA torch installed for the body (which is most of them), the voice quietly
moves onto the card anyway.

It doesn't fail loudly when it does. A resident LM Studio leaves ~45 MB free,
kokoro's first allocation OOMs, `build_seam` catches it and boots the fake, and
she is silent for the rest of the session with only a WARNING in the log to say
so. Load order decides it, which makes it look intermittent: boot before the
model is loaded and her voice works fine.
"""
from __future__ import annotations

import sys
import types

import pytest

from yurios.desktop.voice.backends import tts_kokoro


@pytest.fixture
def kokoro(monkeypatch):
    """A stand-in `kokoro` module that records how KPipeline was constructed.

    The real one downloads 300 MB and imports torch; what is under test is one
    keyword argument, so the seam is the import."""
    seen = {}

    class KPipeline:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setitem(sys.modules, "kokoro",
                        types.SimpleNamespace(KPipeline=KPipeline))
    monkeypatch.setattr(tts_kokoro, "ensure_espeak", lambda: None)
    return seen


def test_the_voice_is_pinned_to_the_cpu(kokoro):
    """Explicitly, not by leaving it to autodetect — the autodetect is the bug."""
    tts_kokoro.KokoroTTS()
    assert kokoro["device"] == "cpu"


def test_a_register_still_picks_the_voice(kokoro):
    """The pin is the only thing that changed about construction."""
    assert tts_kokoro.KokoroTTS("late_night")._voice == "af_nicole"
    assert tts_kokoro.KokoroTTS("af_sky")._voice == "af_sky"   # a raw id passes through
    assert kokoro["repo_id"] == "hexgrad/Kokoro-82M"
