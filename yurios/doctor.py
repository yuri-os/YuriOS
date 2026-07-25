"""`python -m yurios.doctor` — what your .env asks for vs what's installed.

The install is deliberately thin: a bare `pip install -e .` is ~270 MB with no
torch, no CUDA and no models, and every heavy backend is an opt-in extra behind a
lazy import (see pyproject). The cost of that is a gap between what `.env`
*selects* and what's actually importable — and the server, by design, degrades
into the fakes rather than refusing to boot (§3, desktop/main._graceful). Quiet
fakes are the right runtime behaviour and the wrong debugging experience: "why is
she silent?" deserves an answer before you go looking in the log.

So this reads the same Config the server reads, tries the same imports the seams
try, and prints one table plus the exact commands to close the gap. Nothing here
imports a heavy dep unless it's already installed — running the doctor is free.

    python -m yurios.doctor          # → table, exit 0 if every seam is real
"""
from __future__ import annotations

import importlib.util
import sys

# Backends that need nothing installed: they either use the core deps (httpx to a
# server you run) or they ARE the no-dep path. Listed so the table can say "ok"
# instead of "unknown" for a perfectly good configuration.
_FREE = {"fake", "off", "mock", "lm_studio", "ollama", "open_meteo", "openrouter"}


def _have(module: str) -> bool:
    """Is `module` importable without importing it? find_spec doesn't execute the
    module, so probing for torch costs nothing when torch is present."""
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):   # a broken/namespace-shadowed install
        return False


class Check:
    """One seam: what config selected, the module that proves it's installed, and
    the extra that installs it."""

    def __init__(self, seam: str, knob: str, want: str, module: str,
                 extra: str, note: str = "", advisory: bool = False):
        self.seam, self.knob, self.want = seam, knob, want
        self.module, self.extra, self.note = module, extra, note
        # Advisory seams are selected at RUN time, not in .env (the --window Qt
        # stack), so missing ones are worth mentioning but are not a broken config
        # and must not fail the exit code — that's what CI would gate on.
        self.advisory = advisory

    @property
    def free(self) -> bool:
        return self.want in _FREE or not self.module

    @property
    def ok(self) -> bool:
        return self.free or _have(self.module)

    @property
    def state(self) -> str:
        if self.free:
            return "ok (no install needed)"
        if _have(self.module):
            return "ok"
        return "not installed" if self.advisory else "MISSING"


def collect(cfg) -> list[Check]:
    """Build the check list from a live Config. Only the seams whose backend is
    selectable are listed — a knob set to a fake/hosted value still shows, so the
    table doubles as "here's what she's actually wired to"."""
    tts_module = {"kokoro": "kokoro", "qwen3_tts": "qwen_tts",
                  "gpt_sovits": "soundfile"}.get(cfg.tts_backend, "")
    tts_extra = {"kokoro": "tts", "qwen3_tts": "tts-qwen",
                 "gpt_sovits": "tts-sovits"}.get(cfg.tts_backend, "tts")
    checks = [
        Check("ears (STT)", "STT_BACKEND", cfg.stt_backend,
              "faster_whisper" if cfg.stt_backend == "faster_whisper" else "",
              "stt", "CTranslate2, ~200 MB, no torch"),
        Check("voice (TTS)", "TTS_BACKEND", cfg.tts_backend, tts_module, tts_extra,
              "needs espeak-ng too" if cfg.tts_backend == "kokoro" else
              "wants a CUDA GPU" if cfg.tts_backend == "qwen3_tts" else
              "client only — you run the server"),
        Check("turn-taking (VAD)", "VAD_BACKEND", cfg.vad_backend,
              "silero_vad" if cfg.vad_backend == "silero" else "", "vad",
              "pulls torch"),
        Check("embeddings", "EMBED_BACKEND", cfg.embed_backend,
              "sentence_transformers" if cfg.embed_backend == "sentence_tf" else "",
              "local-embed", "pulls torch; lm_studio/ollama need no install"),
        Check("hands (tools)", "TOOLS_BACKEND", getattr(cfg, "tools_backend", "mcp"),
              "mcp" if getattr(cfg, "tools_backend", "") == "mcp" else "",
              "", "mcp is a core dep — always installed"),
    ]
    if getattr(cfg, "window_gui", None) is not None:
        # The desktop window is opt-in at RUN time (--window), not config, so it's
        # reported as advisory: missing Qt is only a problem if you pass --window.
        checks.append(Check("desktop window", "--window", "pywebview", "webview",
                            "desktop", "only needed for `--window`", advisory=True))
    return checks


def _optional_line(skipped: list[Check]) -> str:
    names = ", ".join(f"{c.seam} (`pip install -e '.[{c.extra}]'`)" for c in skipped)
    return f"Optional, not installed (fine to ignore): {names}"


def report(checks: list[Check], *, out=None) -> int:
    """Print the table + the fix. Returns the number of missing *required* seams
    (advisory ones are listed but never counted — see Check.advisory).

    `out` resolves at call time, not as a default argument: binding sys.stdout at
    import would ignore any later redirection (pytest's capsys, a caller's
    StringIO) and write to the original stream instead."""
    out = out if out is not None else sys.stdout
    seam_w = max(len(c.seam) for c in checks)
    knob_w = max(len(f"{c.knob}={c.want}") for c in checks)
    state_w = max(len(c.state) for c in checks)
    print("\nYuriOS dependency check — configured backend vs installed\n", file=out)
    for c in checks:
        mark = "  " if c.ok or c.advisory else "->"
        print(f"{mark} {c.seam:<{seam_w}}  {f'{c.knob}={c.want}':<{knob_w}}  "
              f"{c.state:<{state_w}}" + (f"  ({c.note})" if c.note else ""), file=out)

    missing = [c for c in checks if not c.ok and not c.advisory]
    skipped = [c for c in checks if not c.ok and c.advisory]
    if not missing:
        print("\nEverything your .env selects is installed. Nothing to do.", file=out)
        if skipped:
            print(_optional_line(skipped), file=out)
        print(file=out)
        return 0

    # Only the seams that actually matter get named, and each one names the
    # cheaper alternative — for most of these, editing .env beats downloading.
    print("\nMissing — she'll boot and run, but these seams fall back to the fakes"
          "\n(silent voice / no transcription), which is usually not what you want:\n",
          file=out)
    extras = sorted({c.extra for c in missing if c.extra})
    for c in missing:
        print(f"  - {c.seam}: {c.knob}={c.want} needs `{c.module}`", file=out)
    if extras:
        torchy = {"tts", "vad", "local-embed", "tts-qwen"} & set(extras)
        print(f"\nInstall them:\n\n  pip install -e \".[{','.join(extras)}]\"\n", file=out)
        if torchy and sys.platform.startswith("linux"):
            print("On Linux those pull the CUDA torch wheel (~4.5 GB of nvidia-* and\n"
                  "triton). If you're not running a GPU backend, get the CPU build\n"
                  "first and the extras reuse it (~500 MB instead):\n\n"
                  "  pip install torch --index-url https://download.pytorch.org/whl/cpu\n"
                  f"  pip install -e \".[{','.join(extras)}]\"\n", file=out)
        print("Or install nothing and set the fakes/hosted routes in .env — see the\n"
              "note beside each seam above.\n", file=out)
    if skipped:
        print(_optional_line(skipped) + "\n", file=out)
    return len(missing)


def main(argv: list[str] | None = None) -> int:
    from yurios.world.config import Config      # the same config the server reads
    missing = report(collect(Config()))
    return 1 if missing else 0


if __name__ == "__main__":                      # pragma: no cover — the CLI entry
    raise SystemExit(main())
