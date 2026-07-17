"""Typed configuration (SPEC §11) — extends Build #1's brain config.

The brain's `app.config.Config` already holds every brain knob (model, Vault,
prompt/memory budgets). This subclass adds the *voice* knobs and flips the
brain defaults that matter for the voice stack:

  - CHAT_MODEL defaults to a **local** model (`lm_studio/…`, matching the
    reference `.env` and app/config.py, SPEC §2.4) — Build #2's whole point is
    that her thinking is now yours too (SPEC §1, → ch. 32 sovereignty). The seam
    is Build #1's; only the default value changes. Any local route works
    (`ollama/…`, `lm_studio/…`); a hosted `openrouter/…` is a one-line swap.
  - VAULT_DIR / SOUL_SRC are Build #2's own (standalone). Her identity is the
    SOUL (same card as Build #1); to *continue* a Build #1 companion instead of
    starting fresh, point VAULT_DIR at that Vault — moving her is copying a folder
    (→ ch. 19, the DoD "same someone").
"""
from __future__ import annotations

from pathlib import Path

from yurios.app.config import Config as BrainConfig


class Config(BrainConfig):
    # --- brain defaults, re-pointed for a local-first desktop build ---
    # Local LM Studio by default (§2.4): one OpenAI-compatible server backs both
    # the mind and — with embed_backend below — its memory. `ollama/…` and a
    # hosted `openrouter/…` are one-line swaps (the id's prefix routes it).
    chat_model: str = "lm_studio/google/gemma-4-12b-qat"   # local by default (§2.4)
    utility_model: str = "lm_studio/google/gemma-4-12b-qat"
    # Real-time voice: disable the *reply's* <think> pass so a local reasoning model
    # answers immediately instead of thinking first (§2.6; see app/providers). The
    # utility model keeps thinking ON (inherited default) — it runs off the hot path
    # (post-turn fact extraction + summarisation), so it can afford to reason for
    # better quality (§2.5). A non-reasoning local model ignores both. Overridden in .env.
    chat_thinking: bool = False
    # Embeddings stay local (§2.4): the same LM Studio server as the chat model, so a
    # single process backs the mind and its memory. Switching to Ollama's nomic at the
    # same 768-d width auto-reindexes from the .md files (fingerprint check, §2.4).
    embed_backend: str = "lm_studio"           # keep the whole mind on-device
    embed_model: str = "text-embedding-nomic-embed-text-v1.5"
    embed_dim: int = 768
    vault_dir: Path = Path("./vault")          # own Vault; seed it once (scripts/seed_vault.py)
    soul_src: Path = Path("./soul-src")        # SOUL, for seeding

    host: str = "127.0.0.1"
    port: int = 8766                            # +1 off Build #1's 8765

    # --- voice: STT (ears) — SPEC §3.2 ---
    stt_backend: str = "faster_whisper"         # faster_whisper | fake
    stt_model: str = "base.en"                  # tiny/base = latency over accuracy (→ ch. 24)
    stt_compute: str = "int8"                   # CTranslate2 quantization

    # --- voice: TTS (voice) — SPEC §3.3 ---
    # Kokoro by default: CPU, faster-than-real-time, needs no GPU and leaves the
    # whole GPU for the LLM + avatar (→ ch. 24). A single fixed voice, but it works
    # out of the box on a modest machine. Swap to qwen3_tts (designed persona voice,
    # needs a CUDA GPU) or gpt_sovits (canon clone) — one-line TTS_BACKEND changes.
    tts_backend: str = "kokoro"                 # kokoro (default) | qwen3_tts | gpt_sovits | fake
    tts_register: str = "default"               # kokoro register (→ ../kokoro config)
    tts_sample_rate: int = 24000                # qwen/kokoro are 24 kHz; sovits adapter resamples

    # Qwen3-TTS "designed" voice (TTS_BACKEND=qwen3_tts — the GPU upgrade). The voice
    # is DESIGNED once (that render is the bundled assets/designed.wav) then CLONED
    # from it, so every utterance shares one identical timbre (design-per-turn drifts).
    # Clone mode uses the Base model; set qwen_mode="design" + the VoiceDesign model to
    # author from words each turn instead.
    qwen_mode: str = "clone"                    # clone (default, stable) | design (drifts)
    qwen_model: str = "Qwen/Qwen3-TTS-12Hz-1.7B-Base"   # design mode -> …-VoiceDesign
    qwen_ref_audio: str = ""                    # clone reference; "" = bundled designed.wav
    qwen_ref_text: str = ""                     # its transcript; "" = the bundled clip's
    qwen_instruct: str = ("A warm, gentle young woman in her twenties; soft and "
                          "affectionate, unhurried, with a slight late-night breathiness.")
    qwen_language: str = "English"
    qwen_device: str = "cuda:0"                 # in-process; needs a CUDA GPU
    qwen_dtype: str = "bfloat16"
    qwen_attn: str = "sdpa"                     # "flash_attention_2" for lower VRAM if installed

    # GPT-SoVITS (canon-voice swap). It's a zero-shot cloner, so every /tts call
    # needs a reference clip + its exact transcript — the server holds no default.
    # Defaults reuse Build #2's bundled designed.wav (the same asset Qwen clones),
    # so gpt_sovits works standalone; swap SOVITS_REF_AUDIO for the canon voice.
    # ref_audio is a path on the SERVER's filesystem (here: this machine).
    sovits_base_url: str = "http://127.0.0.1:9880"   # GPT-SoVITS api_v2 base url
    sovits_ref_audio: str = ""                       # "" → bundled designed.wav
    sovits_prompt_text: str = ""                     # "" → the designed clip's transcript
    sovits_prompt_lang: str = "en"                   # language of the reference clip
    sovits_text_lang: str = "en"                     # language to synthesise

    # --- voice: VAD (turn-taking) — SPEC §3.4 ---
    vad_backend: str = "silero"                 # silero | fake
    vad_threshold: float = 0.5                  # speech-probability gate
    vad_min_silence_ms: int = 250               # endpointing dead air (§4.2 budget)
    # Debounce (SpeechGate): how many *consecutive* speech frames confirm a turn.
    # A mechanical-keyboard click is a 1–2 frame transient; real speech sustains,
    # so requiring a run of frames rejects "I typed and she stopped" false triggers.
    # Interrupting her (barge-in) is held to a higher bar than starting a new turn.
    vad_onset_frames: int = 3                   # frames to confirm a new-turn onset
    vad_bargein_frames: int = 5                 # frames to confirm a barge-in (stricter)
    # Server-side: require the VAD to confirm real speech in an endpointed utterance
    # before taking it as a turn — defense-in-depth if a client's edge gate is naive.
    # Turn off if a quiet mic/over-strict VAD drops real speech (the transcript
    # filter still catches punctuation-only hallucinations either way).
    vad_confirm: bool = True

    # --- the real-time loop — SPEC §4 ---
    frame_ms: int = 32                          # audio frame size fed to VAD/STT
    mask_latency: bool = True                   # play a filler while the LLM spins up (§5)
    expression_default: str = "neutral"         # avatar's resting face (§6)
    avatar_model: str = "hiyori"                # which Live2D rig she wears (§6, desktop/avatar_models.py)
    max_reply_tokens: int = 1600                # a roomy cap, not a target: leaves room
                                                #   for a heartfelt turn (→ ch. 28). A
                                                #   no-think reply stops when done, rarely near it.

    # --- desktop-pet window (`python -m desktop --window`, desktop/window.py) ---
    # A frameless, transparent, always-on-top native window that hosts the same
    # page with its chrome hidden — she floats on the desktop instead of in a
    # browser tab. Reuses the WebGL renderer; needs the [desktop] extra (pywebview).
    window_width: int = 360                     # avatar window size (px)
    window_height: int = 640
    window_on_top: bool = True                  # keep her above other windows
    # Which browser engine hosts the window (pywebview "gui"). Default "" = auto:
    # prefer Qt (QtWebEngine = Chromium) when installed, else the platform engine.
    # A labeled side-by-side on the reference rig (X11 + NVIDIA) showed WebKitGTK
    # caps requestAnimationFrame at ~30fps — her idle sway visibly blurs/judders —
    # while Chromium holds 60 and is crisp. Force "gtk" or "qt" to pin an engine.
    window_gui: str = ""                        # "" (auto: qt if installed) | qt | gtk

    # where the latency traces go (personal debug data, gitignored like the corpus)
    trace_dir: Path = Path("./traces")
