"""`yurios doctor` — what your .env asks for vs what's installed.

The base install includes sentence-transformers for local memory. Voice, camera,
and desktop backends remain opt-in extras behind lazy imports (see pyproject). The
cost of that is a gap between what `.env` *selects* and what's actually importable
— and the server, by design, degrades into fakes rather than refusing to boot (§3,
desktop/main._graceful). Quiet fakes are the right runtime behaviour and the wrong
debugging experience: "why is she silent?" deserves an answer before you go looking
in the log.

So this reads the same Config the server reads, tries the same imports the seams
try, and prints one table plus the exact commands to close the gap. Nothing here
imports a heavy dep unless it's already installed — running the doctor is free.

    yurios doctor                    # → table, exit 0 if every seam is real
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from yurios import attribution

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
    # The local camera is one knob over two architectures: SELFIE_BACKEND=
    # diffusers renders a Krea 2 checkpoint through the krea2 backend, which
    # needs a different extra. Read it off the file, the same way build_forge
    # does — cheap (a safetensors header), and no torch import.
    selfie_backend = getattr(cfg, "selfie_backend", "off")
    selfie_shown = selfie_backend
    if selfie_backend in ("diffusers", "krea2"):
        from yurios.forge.backends.sniff import sniff_local_checkpoint_architecture
        arch = sniff_local_checkpoint_architecture(getattr(cfg, "selfie_local_model", ""))
        if selfie_backend == "diffusers" and arch == "krea2":
            selfie_backend = "krea2"
            selfie_shown = "diffusers → krea2 (from the checkpoint)"
    if selfie_backend == "krea2":
        selfie_module, selfie_extra = "comfy_kitchen", "forge-krea2"
        selfie_note = ("in-process Krea 2 (INT4); also needs CUDA torch, a "
                       "checkpoint at SELFIE_LOCAL_MODEL, and HF access to "
                       "the gated krea/Krea-2-Raw for its text encoder/VAE")
    elif selfie_backend == "diffusers":
        selfie_module, selfie_extra = "diffusers", "forge-local"
        selfie_note = ("in-process SDXL; also needs CUDA torch and a "
                       "checkpoint at SELFIE_LOCAL_MODEL")
    else:
        selfie_module, selfie_extra = "", "forge-local"
        selfie_note = "hosted, keyless, or off — nothing to install"
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
               "", "sentence-transformers is a base dependency"),
        Check("hands (tools)", "TOOLS_BACKEND", getattr(cfg, "tools_backend", "mcp"),
              "mcp" if getattr(cfg, "tools_backend", "") == "mcp" else "",
              "", "mcp is a core dep — always installed"),
        Check("camera (selfies)", "SELFIE_BACKEND", selfie_shown,
              selfie_module, selfie_extra, selfie_note),
    ]
    if getattr(cfg, "window_gui", None) is not None:
        # The desktop window is opt-in at RUN time (--window), not config, so it's
        # reported as advisory: missing Qt is only a problem if you pass --window.
        checks.append(Check("desktop window", "--window", "pywebview", "webview",
                            "desktop", "only needed for `--window`", advisory=True))
    return checks


def _site_packages(venv: Path) -> list[Path]:
    return [*venv.glob("lib/python*/site-packages"), venv / "Lib" / "site-packages"]


def venv_gap(missing: list[Check]) -> str:
    """The "missing" seams that aren't missing at all — they're in the venv you
    didn't activate.

    `install.sh` builds `.venv` beside the source and puts every extra in there,
    but an editable install is importable from any interpreter that can see the
    checkout: run `python -m yurios.world` from a conda base and the package
    starts fine while faster-whisper, silero-vad and friends stay behind in the
    venv. The seams then report MISSING and the honest-looking fix — pip install
    the extras — installs a second copy into the wrong environment. So when the
    project venv holds what this interpreter lacks, say that instead. Empty
    string when there's nothing to say, which is every normal run."""
    venv = Path(__file__).resolve().parents[1] / ".venv"
    if not (venv / "pyvenv.cfg").is_file():
        return ""                           # installed, not a source checkout
    try:
        if Path(sys.prefix).resolve() == venv.resolve():
            return ""                       # already running in it
    except OSError:
        return ""
    roots = _site_packages(venv)
    there = [c for c in missing if c.module and any(
        (root / c.module).is_dir() or (root / f"{c.module}.py").is_file()
        for root in roots)]
    if not there:
        return ""
    activate = (venv / "Scripts" / "activate" if os.name == "nt"
                else venv / "bin" / "activate")
    return (f"{', '.join(c.seam for c in there)} are installed in {venv}, but this "
            f"is {sys.executable} — activate the project venv rather than "
            f"installing again: source {activate}")


def _collapse(extras: list[str]) -> list[str]:
    """Print the umbrella extra when the pieces add up to it. Someone missing all
    three voice seams should be told `.[voice]`, not `.[stt,tts,vad]` — same install,
    but it matches how pyproject and the README name it. Longest cover first."""
    out = list(extras)
    for umbrella, parts in (("voice", {"stt", "tts", "vad"}),):
        if parts <= set(out):
            out = [umbrella] + [e for e in out if e not in parts]
            break
    return out


def torch_pair_mismatch() -> str:
    """The one broken install every check above still calls "ok", as a printable line.

    kokoro and silero-vad load torchaudio's C++ extension, and it has to come from the
    same build channel as torch. Take CPU torch first (the whole reason her voice fits
    in 1.6 GB) and then let the extras pull torchaudio from PyPI, and you get a
    CUDA-built extension on a CUDA-less torch: it dies with "libcudart.so.13: cannot
    open shared object file", both seams degrade to their fakes, and she is silent —
    while `find_spec` finds both modules exactly where they should be.

    Spotted from metadata alone, so it costs nothing: PyPI's wheels carry no local
    version tag, the pytorch.org CPU ones are `+cpu`. Only mismatched *that* way is a
    bug — macOS and Windows wheels are CPU-only with no tag at all, so a plain pair
    there is correct and must not warn."""
    try:
        from importlib.metadata import version
        torch_v, audio_v = version("torch"), version("torchaudio")
    except Exception:                       # torchaudio absent (no voice) — nothing to say
        return ""
    if not torch_v.endswith("+cpu") or audio_v.endswith("+cpu"):
        return ""
    return (f"\ntorch {torch_v} is the CPU build, but torchaudio {audio_v} came from "
            f"PyPI\n(CUDA). kokoro and silero-vad load torchaudio's extension, so they "
            f"will fail\nat runtime and fall back to the fakes. Take the pair from one "
            f"index:\n\n"
            "  pip install torch torchaudio --index-url "
            "https://download.pytorch.org/whl/cpu\n")


def mcp_api_mismatch() -> str:
    """The other broken install the table calls "ok" — her hands, on an SDK that moved.

    `mcp` is a core dep, so the tools row above only ever asks "is it importable", and
    on mcp 2.0 it is: what 2.0 removed is the module the server is *built* on
    (`mcp.server.fastmcp` → `mcp.server.mcpserver`, `FastMCP` → `MCPServer`), with no
    alias. So the package imports, the row says ok, and the failure lands three layers
    away — in the child process the client spawns, as "she has no hands this run".
    pyproject caps it now; this is for the venv that resolved 2.x before the cap, where
    the fix is a downgrade and nothing in the table would ever say so.

    Probes the module rather than the version, so the day 2.x is supported (or 3.0
    brings it back) this goes quiet on its own instead of lying the other way."""
    if _have("mcp.server.fastmcp"):
        return ""
    try:
        from importlib.metadata import version
        found = version("mcp")
    except Exception:           # no mcp at all — the tools row already says MISSING
        return ""
    return (f"\nmcp {found} is installed, but her hands are built on the 1.x SDK: 2.0 "
            f"renamed\n`mcp.server.fastmcp` and kept no alias, so the tool server dies "
            f"on its import\nline and she runs hand-less. Take the capped version:\n\n"
            "  pip install -e .\n")


# Mirrors providers/openrouter._route: a bare model id means OpenRouter, and only
# ollama/… and lm_studio/… are somebody's own machine. Reimplemented rather than
# imported because that module imports litellm, and running the doctor has to stay
# free (tests/test_doctor.py pins this against the real router).
_LOCAL_PREFIXES = ("ollama/", "lm_studio/", "gguf/")
_OTHER_HOSTS = ("openai/", "anthropic/")


def _hosted_on_openrouter(model: str) -> bool:
    # NONE is the first-run offline state, not LiteLLM's bare-model shorthand.
    return bool(model and model.upper() != "NONE"
                and not model.startswith(_LOCAL_PREFIXES + _OTHER_HOSTS))


def _on(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def network_lines(cfg) -> list[str]:
    """What this configuration puts on the wire, and under whose name.

    The table above answers "will she work"; for a local-first companion the other
    half of the same question is what leaves the machine, and it is just as
    invisible until something surprises you. Environment is the right thing to read
    here, not the libraries: litellm and huggingface_hub take these at IMPORT time,
    so yurios/__init__.py has already applied them (or deferred to your .env) before
    the doctor could ask, and importing either one to double-check would cost more
    than the whole run."""
    hosted = [seam for seam, model in (("chat", cfg.chat_model),
                                       ("utility", cfg.utility_model))
              if _hosted_on_openrouter(model)]
    if getattr(cfg, "selfie_backend", "") == "openrouter":
        hosted.append("selfies")

    lines = ["\nWhat leaves the machine\n"]
    if hosted:
        # The composite user-agent of whichever path is actually hosted — the chat
        # seam posts through litellm, the camera through the standard library.
        client = (attribution.client_token("litellm") if len(hosted) > 1
                  or "selfies" not in hosted else attribution.URLLIB_CLIENT)
        lines.append(f"   OpenRouter        {', '.join(hosted)} — billed to "
                     f"{attribution.APP_TITLE} at {attribution.APP_URL}")
        lines.append(f"   sent as           {attribution.user_agent(client)}  "
                     f"(app page: openrouter.ai/apps?url={attribution.APP_URL})")
    else:
        lines.append("   OpenRouter        nothing hosted selected — every model "
                     "is on your own machine")
    cost_map = os.environ.get("LITELLM_LOCAL_MODEL_COST_MAP", "")
    lines.append("   litellm prices    " + (
        "the copy in the wheel — no fetch at start"
        if _on(cost_map) else
        "1.67 MB fetched from raw.githubusercontent.com at EVERY start "
        "(LITELLM_LOCAL_MODEL_COST_MAP)"))
    hf = os.environ.get("HF_HUB_DISABLE_TELEMETRY", "")
    lines.append("   Hugging Face      " + (
        "telemetry off — downloads name no torch build or AI harness"
        if _on(hf) else
        "model downloads report your torch build and AI harness "
        "(HF_HUB_DISABLE_TELEMETRY)"))
    return lines


def _optional_line(skipped: list[Check]) -> str:
    names = ", ".join(f"{c.seam} (`pip install -e '.[{c.extra}]'`)" for c in skipped)
    return f"Optional, not installed (fine to ignore): {names}"


def report(checks: list[Check], *, network: list[str] | None = None, out=None) -> int:
    """Print the table + the fix. Returns the number of missing *required* seams
    (advisory ones are listed but never counted — see Check.advisory; the torch pair
    and mcp API warnings are printed but not counted either: in both, everything the
    table asks about IS installed).

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

    # Printed either way: these are invisible in the table above by construction —
    # every module they need IS installed, just not in a combination that runs.
    broken = [w for w in (torch_pair_mismatch(), mcp_api_mismatch()) if w]
    for warning in broken:
        print(warning, file=out)

    # Informational, never counted: nothing here is broken, it's what she's wired
    # to talk to. `network=None` (a caller that only wants the seams) prints nothing.
    for line in network or []:
        print(line, file=out)

    missing = [c for c in checks if not c.ok and not c.advisory]
    skipped = [c for c in checks if not c.ok and c.advisory]
    if not missing:
        print("\nEverything your .env selects is installed."
              + (" Fix what's flagged above and she'll be whole."
                 if broken else " Nothing to do."), file=out)
        if skipped:
            print(_optional_line(skipped), file=out)
        print(file=out)
        return 0

    # Only the seams that actually matter get named, and each one names the
    # cheaper alternative — for most of these, editing .env beats downloading.
    print("\nMissing — she'll boot and run, but these seams fall back to the fakes"
          "\n(silent voice / no transcription), which is usually not what you want:\n",
          file=out)
    extras = _collapse(sorted({c.extra for c in missing if c.extra}))
    for c in missing:
        print(f"  - {c.seam}: {c.knob}={c.want} needs `{c.module}`", file=out)
    # …unless they're only missing from *this* interpreter, in which case
    # installing anything is the wrong move (see venv_gap).
    gap = venv_gap(missing)
    if gap:
        print(f"\n{gap}\n", file=out)
    if extras and not gap:
        torchy = {"tts", "vad", "tts-qwen", "voice", "all"} & set(extras)
        print(f"\nInstall them:\n\n  pip install -e \".[{','.join(extras)}]\"\n", file=out)
        if torchy and sys.platform.startswith("linux"):
            # Measured in an otherwise-empty venv: the default PyPI wheel is 4.5 GB on
            # disk (2.73 GB download, 23 CUDA packages) against 747 MB for whl/cpu.
            print("On Linux those pull torch, and PyPI's wheel bundles CUDA: 4.5 GB on\n"
                  "disk, vs 747 MB for the CPU build. Unless you're running a GPU\n"
                  "backend, fetch CPU torch first and the extras reuse it:\n\n"
                  "  pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu\n"
                  f"  pip install -e \".[{','.join(extras)}]\"\n", file=out)
        print("Or install nothing and set the fakes/hosted routes in .env — see the\n"
              "note beside each seam above.\n", file=out)
    if skipped:
        print(_optional_line(skipped) + "\n", file=out)
    return len(missing)


def main(argv: list[str] | None = None) -> int:
    from yurios.world.config import Config      # the same config the server reads
    cfg = Config()
    missing = report(collect(cfg), network=network_lines(cfg))
    return 1 if missing else 0


if __name__ == "__main__":                      # pragma: no cover — the CLI entry
    raise SystemExit(main())
