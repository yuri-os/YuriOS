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

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np

from ..protocols import AudioChunk
from ..sentences import cut_sentences

log = logging.getLogger(__name__)

_INSTALL_HINT = (
    "Kokoro not installed. `pip install -e '.[tts]'` and install espeak-ng "
    "(apt-get install espeak-ng / brew install espeak-ng). Kokoro is CPU-only, so "
    "on Linux get the CPU torch build first and save ~4 GB of unused CUDA: "
    "`pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu`. "
    "Or run against the fake voice: TTS_BACKEND=fake.")

_ESPEAK_HINT = (
    "Kokoro is installed but espeak-ng can't load its phoneme data here, and it exits "
    "the PROCESS rather than raising — so this refuses up front instead of taking the "
    "server down with it. Install espeak-ng (apt-get install espeak-ng / brew install "
    "espeak-ng), or point ESPEAK_DATA_PATH at a directory containing `phontab`. Or run "
    "against the fake voice: TTS_BACKEND=fake.")

# What misaki does at import, and where the failure lands: constructing the wrapper
# calls espeak_Initialize, which is what dies. Run in a CHILD so its exit(1) costs a
# subprocess instead of the server.
_PROBE = ("import misaki.espeak;"
          "from phonemizer.backend.espeak.wrapper import EspeakWrapper;"
          "EspeakWrapper()")


def _espeak_ok(data_dir: Path | None = None) -> bool:
    """Does espeak-ng initialise — with ESPEAK_DATA_PATH=`data_dir`, if given?

    Asked by running it, not by inspecting paths: espeak-ng resolves its data
    directory through several fallbacks (the caller's path, ESPEAK_DATA_PATH, $HOME,
    then an absolute path baked in at build time), and the `espeakng-loader` wheel's
    copy loses that race — the baked-in path is a CI checkout under /home/runner. The
    C library's answer to a data dir it can't read is one printed line and exit(1),
    with no exception for `_graceful` to catch: the server just stops mid-boot."""
    env = dict(os.environ)
    if data_dir is not None:
        env["ESPEAK_DATA_PATH"] = str(data_dir)
    try:
        done = subprocess.run([sys.executable, "-c", _PROBE], env=env,
                              capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):        # pragma: no cover
        return False
    return done.returncode == 0


def _system_espeak_data() -> Path | None:
    """The data directory of the *installed* espeak-ng, which is the package the
    README and install.sh already ask for. `espeak-ng --version` prints it:

        eSpeak NG text-to-speech: 1.51  Data at: /usr/lib/x86_64-linux-gnu/espeak-ng-data
    """
    exe = shutil.which("espeak-ng") or shutil.which("espeak")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.SubprocessError):        # pragma: no cover
        return None
    if "Data at:" not in out:                            # pragma: no cover
        return None
    found = Path(out.split("Data at:", 1)[1].strip())
    return found if (found / "phontab").is_file() else None


def ensure_espeak() -> None:
    """Leave the environment in a state where kokoro can phonemise, or refuse.

    Costs one short child process when everything is already fine, which is the
    normal case and is nothing beside loading the voice model itself."""
    if _espeak_ok():
        return
    system = _system_espeak_data()
    if system is not None and _espeak_ok(system):
        # Not setdefault: we only get here because whatever was set didn't work.
        os.environ["ESPEAK_DATA_PATH"] = str(system)
        log.warning("kokoro: espeak-ng couldn't find its phoneme data (the "
                    "espeakng-loader wheel's copy doesn't resolve) — using the system "
                    "install at %s", system)
        return
    raise RuntimeError(_ESPEAK_HINT)


# A small register→voice map (the ../kokoro impl has the full 54-voice table).
REGISTERS = {"default": "af_heart", "late_night": "af_nicole", "expressive": "af_bella"}


class KokoroTTS:
    sample_rate = 24000

    def __init__(self, register: str = "default"):
        try:
            from kokoro import KPipeline
        except ImportError as e:  # pragma: no cover - environment dependent
            raise RuntimeError(_INSTALL_HINT) from e
        ensure_espeak()
        # device="cpu", not the default None. KPipeline's default is
        # `'cuda' if torch.cuda.is_available() else 'cpu'`, so on any box with a
        # CUDA torch build installed for the body, the 82M voice quietly moves
        # onto the GPU the LLM is already living on — and the whole point of
        # kokoro here is that it does not (ch. 24: "every gigabyte the voice
        # eats is a gigabyte the LLM can't"). It doesn't even fail loudly: a
        # resident LM Studio leaves ~45 MB free, kokoro's first 12 MB
        # allocation OOMs, `build_seam` catches it and boots the fake, and she
        # is silent for the rest of the session with nothing on screen to say
        # why. Whether that happens depends on load order, so it looks
        # intermittent: boot before the model is loaded and the voice works.
        self._pipeline = KPipeline(lang_code="a", repo_id="hexgrad/Kokoro-82M",
                                   device="cpu")
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
