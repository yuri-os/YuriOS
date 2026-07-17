"""Qwen3-TTS (SPEC §3.3) — the default voice: the "designed" clip, cloned.

Qwen3-TTS (QwenLM/Qwen3-TTS, Apache-2.0) is the convergence voice model: a fixed
preset voice (cf. Kokoro), 3-second zero-shot cloning (cf. GPT-SoVITS), AND
designing a voice from a natural-language description. This build's voice is the
"designed" one — warm, gentle, late-night — but *pinned*: it is authored once with
VoiceDesign (that render is the bundled `assets/designed.wav`), then **cloned**
from that single clip for every utterance.

Why clone a designed clip instead of re-designing each turn? VoiceDesign samples a
fresh voice on every call, so designing per sentence makes her timbre drift — the
filler "mm—" comes out one voice, the reply another. Cloning one frozen reference
gives ONE identical voice for the fillers and every sentence (SPEC §3.3: one
versioned voice asset, never swapped mid-conversation, → ch. 24). The clip ships
with the build, so it stays standalone and reproducible.

Runs in-process (no server), via the `qwen-tts` pip package directly. Higher
quality but slower than Kokoro (RTF > 1), so it leans on the §5 filler masking.
Two things keep the per-sentence render as fast as this model gets: the clone
prompt (the reference encoding) is computed ONCE at init, not per sentence, and
generation stays streaming (one short sentence at a time, for low time-to-first-
audio) rather than batched. Want the GPU free or lower latency still? Try
attn_implementation=flash_attention_2 (QWEN_ATTN), or TTS_BACKEND=kokoro. Set
QWEN_MODE=design to re-design from words each turn instead (drifts, no clip).
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from ..protocols import AudioChunk
from ..sentences import cut_sentences

log = logging.getLogger("desktop.voice.qwen")

# The bundled reference clip + its exact transcript (what designed.wav says). The
# clone conditions on both, so ref_text must match the audio.
ASSETS = Path(__file__).resolve().parent.parent / "assets"
DEFAULT_REF_AUDIO = ASSETS / "designed.wav"
DEFAULT_REF_TEXT = "Hey. You made it back. I kept the light on."

_INSTALL_HINT = (
    "qwen-tts not installed. `pip install qwen-tts soundfile` (first synthesis "
    "downloads the weights from Hugging Face; needs a CUDA GPU). Or run a "
    "lighter/faster voice: TTS_BACKEND=kokoro, or TTS_BACKEND=fake.")


class QwenTTS:
    """In-process Qwen3-TTS, pinned to one voice (clone of the designed clip)."""

    def __init__(self, *, model: str, mode: str = "clone",
                 ref_audio: str | Path | None = None, ref_text: str | None = None,
                 instruct: str = "", language: str = "English",
                 device: str = "cuda:0", dtype: str = "bfloat16",
                 attn: str = "sdpa", sample_rate: int = 24000):
        self.mode = mode
        self.ref_audio = str(ref_audio or DEFAULT_REF_AUDIO)
        self.ref_text = ref_text or DEFAULT_REF_TEXT
        self.instruct = instruct
        self.language = language
        self.sample_rate = sample_rate
        if mode == "clone" and not Path(self.ref_audio).exists():
            raise RuntimeError(f"clone reference not found: {self.ref_audio}")
        try:
            import torch  # noqa: F401
            from qwen_tts import Qwen3TTSModel
        except ImportError as e:  # pragma: no cover - environment dependent
            raise RuntimeError(_INSTALL_HINT) from e
        _dtype = {"bfloat16": torch.bfloat16, "float16": torch.float16,
                  "float32": torch.float32}.get(dtype, torch.bfloat16)
        self._model = Qwen3TTSModel.from_pretrained(
            model, device_map=device, dtype=_dtype, attn_implementation=attn)

        # Pin the clone: encode the reference clip ONCE (speech-tokenizer encode +
        # speaker-embedding extract + resample), not once per sentence. This is the
        # cloning voice's biggest per-render cost, and the reference never changes,
        # so we precompute the prompt here and hand it to every generate() call.
        # Output is identical — the prompt is deterministic from the reference —
        # it just skips re-deriving it each time. If it fails we fall back to the
        # per-call path (ref_audio/ref_text in _render).
        self._clone_prompt = None
        if self.mode == "clone":
            try:
                self._clone_prompt = self._model.create_voice_clone_prompt(
                    ref_audio=self.ref_audio, ref_text=self.ref_text)
            except Exception:
                log.warning("could not precompute the clone prompt; falling back "
                            "to per-render reference encoding (slower)", exc_info=True)

    def stream(self, text: str, register: str | None = None):
        sentences, tail = cut_sentences(text)
        if tail.strip():
            sentences.append(tail.strip())
        for i, sentence in enumerate(sentences or [text]):
            audio = self._render(sentence)
            yield AudioChunk(index=i, text=sentence, audio=audio,
                             sample_rate=self.sample_rate)

    def _render(self, sentence: str) -> np.ndarray:
        if self.mode == "design":
            # voice authored from words — re-samples the voice each call (drifts)
            wavs, sr = self._model.generate_voice_design(
                text=sentence, language=self.language, instruct=self.instruct)
        elif self._clone_prompt is not None:
            # clone the one pinned clip → identical timbre every utterance, reusing
            # the precomputed prompt so no reference re-encoding on the hot path
            wavs, sr = self._model.generate_voice_clone(
                text=sentence, language=self.language,
                voice_clone_prompt=self._clone_prompt)
        else:
            # fallback: prompt precompute failed at init → encode ref each call
            wavs, sr = self._model.generate_voice_clone(
                text=sentence, language=self.language,
                ref_audio=self.ref_audio, ref_text=self.ref_text)
        self.sample_rate = int(sr)
        audio = wavs[0]
        if hasattr(audio, "detach"):
            audio = audio.detach().cpu().numpy()
        audio = np.asarray(audio, dtype=np.float32)
        return audio.reshape(-1) if audio.ndim > 1 else audio
