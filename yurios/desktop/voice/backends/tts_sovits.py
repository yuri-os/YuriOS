"""GPT-SoVITS TTS (SPEC §3.3) — the canon-voice swap.

The waifu/VTuber standard for cloning a *specific character voice*, and — per this
project's own testing (→ ch. 24) — the practical real-time pick at ~700 ms to
first audio, a notch below Kokoro on naturalness but the one that gives Yuri the
voice the audience has in their head. Reach for it when the persona has a canon
voice; keep Kokoro when you want the GPU free.

This is a thin HTTP client to a running GPT-SoVITS `api_v2` server. GPT-SoVITS is
a *zero-shot cloner*: every `/tts` call must carry a reference clip + its exact
transcript — the api_v2 server holds no default, so a request without them 500s.
We POST a JSON body with `ref_audio_path` / `prompt_text` / `prompt_lang` (the
same shape the sibling `reference-implementations/gpt-sovits` client proved out).

`ref_audio_path` is a path on the SERVER's filesystem. Build #2 ships its own
reference — the bundled `assets/designed.wav`, the same clip the Qwen clone uses,
so identity is one pinned asset across backends (SPEC §3.3, → ch. 24) — and since
the server runs on this machine, its absolute path resolves for both. Point
SOVITS_REF_AUDIO at another clip (with SOVITS_PROMPT_TEXT = its transcript) to
swap in the canon voice. Start the server first (see that impl's README).
"""
from __future__ import annotations

import io
from pathlib import Path

import numpy as np

from ..protocols import AudioChunk
from ..sentences import cut_sentences

# The bundled reference clip + its exact transcript — the same pinned asset the
# Qwen clone conditions on (desktop/voice/backends/tts_qwen.py). One voice, one
# asset, no drift across backends.
ASSETS = Path(__file__).resolve().parent.parent / "assets"
DEFAULT_REF_AUDIO = ASSETS / "designed.wav"
DEFAULT_REF_TEXT = "Hey. You made it back. I kept the light on."


class SoVITSClient:
    """Streams sentence-by-sentence from a running GPT-SoVITS api_v2 server."""

    def __init__(self, base_url: str = "http://127.0.0.1:9880", *,
                 ref_audio: str | Path | None = None, ref_text: str | None = None,
                 prompt_lang: str = "en", text_lang: str = "en",
                 timeout_s: float = 180, sample_rate: int = 32000):
        self.base_url = base_url.rstrip("/")
        # absolute so the server resolves it regardless of its own working dir
        self.ref_audio = str(Path(ref_audio or DEFAULT_REF_AUDIO).resolve())
        self.ref_text = ref_text or DEFAULT_REF_TEXT
        self.prompt_lang = prompt_lang
        self.text_lang = text_lang
        self.timeout_s = timeout_s
        self.sample_rate = sample_rate            # v1/v2 output 32 kHz; _decode confirms
        if not Path(self.ref_audio).exists():
            raise RuntimeError(
                f"SoVITS reference clip not found: {self.ref_audio}. Set "
                f"SOVITS_REF_AUDIO to a wav readable by the server (+ its transcript "
                f"in SOVITS_PROMPT_TEXT).")
        try:
            import httpx  # noqa: F401
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("pip install httpx soundfile for the SoVITS client") from e

    def stream(self, text: str, register: str | None = None):
        import httpx
        sentences, tail = cut_sentences(text)
        if tail.strip():
            sentences.append(tail.strip())
        for i, sentence in enumerate(sentences or [text]):
            # api_v2 /tts (POST): the reference clip + transcript are REQUIRED —
            # a zero-shot cloner has no default voice, so omitting them 500s.
            payload = {
                "text": sentence,
                "text_lang": self.text_lang,
                "ref_audio_path": self.ref_audio,
                "prompt_text": self.ref_text,
                "prompt_lang": self.prompt_lang,
                "media_type": "wav",
                "streaming_mode": False,
            }
            r = httpx.post(f"{self.base_url}/tts", json=payload, timeout=self.timeout_s)
            if r.status_code != 200:
                # surface the server's own message — a bare raise_for_status hides it
                raise RuntimeError(
                    f"GPT-SoVITS /tts failed ({r.status_code}): {r.text[:200]}")
            audio = self._decode_wav(r.content)
            yield AudioChunk(index=i, text=sentence, audio=audio,
                             sample_rate=self.sample_rate)

    def _decode_wav(self, data: bytes) -> np.ndarray:
        import soundfile as sf
        audio, sr = sf.read(io.BytesIO(data), dtype="float32")
        self.sample_rate = sr
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        return audio.astype(np.float32)
