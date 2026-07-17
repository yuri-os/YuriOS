"""Build #2 — the desktop-companion app (SPEC §2, §14).

Composes the Build #1 brain (`desktop.brain.BrainAdapter`) with the voice
backends (behind the §3 seams) and serves the desktop sanctuary + the /ws/voice
loop. Run:

    python -m desktop                      # reads HOST/PORT from .env (§11)

Backends are chosen from config and built once; each websocket connection gets
its own TurnController so a barge-in cancels only that connection's turn. The
filler bank is primed at startup (off the hot path) so masking a gap is instant.
"""
from __future__ import annotations

import logging
import threading

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from .brain import BrainAdapter
from .config import Config
from .voice.fillers import FillerBank
from .voice.protocols import STT, TTS, VAD

log = logging.getLogger("desktop.main")
WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"


def _graceful(kind: str, want: str, build_real, build_fake, extra: str):
    """Build the real backend; if its deps aren't installed, warn loudly and fall
    back to the fake so `python -m desktop` always boots (§3, the seam degrades
    like the avatar does). Installing the extra upgrades her on the next run."""
    if want == "fake":
        return build_fake(), "fake"
    try:
        return build_real(), want
    except Exception as e:               # missing dep, missing espeak-ng, model load, …
        # degrade rather than refuse to boot — the seam falls back like the avatar
        # does. Loud enough that "why is she silent?" is answered in the log.
        log.warning("%s backend %r unavailable — using the fake (%s). "
                    "Install it: pip install -e '.[%s]'. Reason: %s",
                    kind, want, "she'll be silent" if kind == "TTS" else "degraded",
                    extra, e)
        return build_fake(), "fake"


def build_tts(cfg: Config):
    from .voice.backends.fakes import FakeTTS
    def real():
        if cfg.tts_backend == "kokoro":
            from .voice.backends.tts_kokoro import KokoroTTS
            return KokoroTTS(cfg.tts_register)
        if cfg.tts_backend == "gpt_sovits":
            from .voice.backends.tts_sovits import SoVITSClient
            return SoVITSClient(cfg.sovits_base_url,
                                ref_audio=cfg.sovits_ref_audio or None,
                                ref_text=cfg.sovits_prompt_text or None,
                                prompt_lang=cfg.sovits_prompt_lang,
                                text_lang=cfg.sovits_text_lang)
        from .voice.backends.tts_qwen import QwenTTS          # default (§3.3)
        return QwenTTS(model=cfg.qwen_model, mode=cfg.qwen_mode,
                       ref_audio=cfg.qwen_ref_audio or None,
                       ref_text=cfg.qwen_ref_text or None,
                       instruct=cfg.qwen_instruct, language=cfg.qwen_language,
                       device=cfg.qwen_device, dtype=cfg.qwen_dtype,
                       attn=cfg.qwen_attn, sample_rate=cfg.tts_sample_rate)
    return _graceful("TTS", cfg.tts_backend, real, FakeTTS, "tts")


def build_stt(cfg: Config):
    from .voice.backends.fakes import FakeSTT
    def real():
        from .voice.backends.stt_whisper import WhisperSTT
        return WhisperSTT(cfg.stt_model, cfg.stt_compute)
    return _graceful("STT", cfg.stt_backend, real, FakeSTT, "stt")


def build_vad(cfg: Config):
    from .voice.backends.fakes import FakeVAD
    def real():
        from .voice.backends.vad_silero import SileroVAD
        return SileroVAD(cfg.vad_threshold)
    return _graceful("VAD", cfg.vad_backend, real, FakeVAD, "vad")


class Runtime:
    """Everything a connection needs, built once (mirrors Build #1's AppState)."""

    def __init__(self, cfg: Config, *, brain=None, chat_model=None,
                 utility_model=None, embedder=None):
        self.cfg = cfg
        # `brain` is injectable so the /ws/voice test can run the route against a
        # FakeBrain (no Vault, no SQLite) — same reason Build #1 injects providers.
        self.brain = brain or BrainAdapter.build(           # Build #1, reused (§2)
            cfg, chat_model=chat_model, utility_model=utility_model,
            embedder=embedder)
        # The voice stack warms on a background thread: Kokoro alone takes ~20 s
        # cold, and building it here kept the whole server — and her body — off
        # screen until the voice was up. Served immediately instead, the page
        # shows her in seconds; each /ws/voice connection awaits `voice_ready`
        # before its first turn, so an early turn waits for the real voice
        # rather than degrading to a fake. /api/health says "loading" meanwhile.
        self.tts = self.stt = self.vad = None
        self.tts_name = self.stt_name = self.vad_name = "loading"
        self.filler_bank: FillerBank | None = None
        # Sessions we've already greeted this run. She speaks first on arrival
        # (§7), but a *reconnect* is not a new arrival — and during the ~20 s
        # voice-warm wait several connections can park in `voice_ready.wait` and
        # release together, so without this every one of them would greet at once.
        self.greeted: set[str] = set()
        self.voice_ready = threading.Event()
        threading.Thread(target=self._warm_voice, daemon=True,
                         name="voice-warmup").start()

    def _warm_voice(self) -> None:
        try:
            # each returns (instance, actual_name) — the name reflects any
            # fallback, so /api/health tells the truth about what's really wired.
            self.tts, self.tts_name = build_tts(self.cfg)
            self.stt, self.stt_name = build_stt(self.cfg)
            self.vad, self.vad_name = build_vad(self.cfg)
            if self.cfg.mask_latency:
                filler_bank = FillerBank(tts=self.tts)
                try:
                    filler_bank.prime()                # pre-render, off the hot path (§5)
                    self.filler_bank = filler_bank
                except Exception:
                    log.exception("filler prime failed; masking disabled this run")
        finally:
            self.voice_ready.set()                     # never leave a connection hanging


def create_app(cfg: Config | None = None, *, brain=None, chat_model=None,
               utility_model=None, embedder=None) -> FastAPI:
    cfg = cfg or Config()
    app = FastAPI(title="desktop-companion", docs_url=None, redoc_url=None)
    app.state.rt = Runtime(cfg, brain=brain, chat_model=chat_model,
                           utility_model=utility_model, embedder=embedder)

    from .routes import voice_ws, health, avatar, settings
    app.include_router(health.router)
    app.include_router(avatar.router)
    app.include_router(settings.router)
    app.include_router(voice_ws.router)
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app


def app() -> FastAPI:
    """uvicorn factory: `uvicorn desktop.main:app --factory`."""
    return create_app()
