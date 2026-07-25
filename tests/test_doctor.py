"""The dependency doctor (`python -m yurios.doctor`).

The install is thin on purpose — a bare `pip install -e .` carries no torch, no
CUDA and no models — and every heavy backend is a lazy import behind a seam that
degrades to its fake (SPEC §3). The doctor is what keeps that from being a silent
trap: it reads the same Config the server reads and says which selected backends
aren't installed, plus the extra that installs each one.

Two things can rot here, and both of them look fine until someone follows the
advice: the extra names printed to the user can drift out of `pyproject.toml`,
and the config→module mapping can drift from what the backends actually import.
These pin both against the real files rather than against a copy of the table.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

from yurios.doctor import Check, collect, report
from yurios.world.config import Config

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _extras() -> set[str]:
    with PYPROJECT.open("rb") as fh:
        return set(tomllib.load(fh)["project"]["optional-dependencies"])


# ---- the printed advice has to be runnable ---------------------------------

def test_every_extra_named_by_the_doctor_exists():
    """`pip install -e '.[tts-qwen]'` must be a real extra for every backend the
    doctor can be pointed at — a typo here is a dead-end error message."""
    declared = _extras()
    for backend in ("kokoro", "qwen3_tts", "gpt_sovits", "fake"):
        cfg = Config(_env_file=None, tts_backend=backend)
        for check in collect(cfg):
            if check.extra:
                assert check.extra in declared, (
                    f"{check.seam} points at extra [{check.extra}], which "
                    f"pyproject.toml doesn't declare")


def test_tts_extras_match_the_backend_hints():
    """desktop.main.TTS_EXTRAS is what the runtime warning prints; the doctor has
    to agree with it or the two disagree about how to fix the same problem."""
    from yurios.desktop.main import TTS_EXTRAS

    for backend, extra in TTS_EXTRAS.items():
        cfg = Config(_env_file=None, tts_backend=backend)
        voice = next(c for c in collect(cfg) if c.knob == "TTS_BACKEND")
        assert voice.extra == extra, f"{backend}: doctor says {voice.extra}"


def test_heavy_extras_are_not_in_the_base_dependencies():
    """The whole point: none of the model backends may be a core dependency."""
    with PYPROJECT.open("rb") as fh:
        project = tomllib.load(fh)["project"]
    core = " ".join(project["dependencies"]).lower()
    for heavy in ("torch", "faster-whisper", "kokoro", "qwen-tts", "silero",
                  "sentence-transformers", "pyqt", "pywebview"):
        assert heavy not in core, f"{heavy} crept into [project.dependencies]"


def test_python_requires_has_an_upper_bound():
    """litellm caps the Python it supports, so this project must too — without a
    ceiling, a too-new interpreter installs fine and then sends the resolver
    backtracking through years of litellm releases (the Windows failure)."""
    with PYPROJECT.open("rb") as fh:
        requires = tomllib.load(fh)["project"]["requires-python"]
    assert re.search(r"<\s*3\.\d+", requires), (
        f"requires-python={requires!r} has no upper bound")


def test_default_extras_exclude_the_gpu_voice():
    """[voice] and [all] are the recommended installs, so neither may drag in the
    CUDA-only designed voice that nothing defaults to."""
    extras = _extras()
    with PYPROJECT.open("rb") as fh:
        table = tomllib.load(fh)["project"]["optional-dependencies"]
    assert "tts-qwen" in extras
    for name in ("voice", "all"):
        joined = " ".join(table[name]).lower()
        assert "qwen" not in joined, f"[{name}] pulls qwen-tts"
        assert "pyqt" not in joined, f"[{name}] pulls the Qt desktop stack"


# ---- the check logic itself ------------------------------------------------

def test_no_install_backends_count_as_ok():
    """Selecting a fake or a server-backed route is a complete configuration, not
    a missing dependency — the doctor must not nag about it."""
    cfg = Config(_env_file=None, stt_backend="fake", tts_backend="fake",
                 vad_backend="fake", embed_backend="lm_studio")
    for check in collect(cfg):
        if check.advisory:
            continue
        assert check.ok, f"{check.seam} flagged despite needing no install"


def test_missing_module_is_reported_with_its_extra(capsys):
    check = Check("ears (STT)", "STT_BACKEND", "faster_whisper",
                  "a_module_that_is_not_installed", "stt")
    assert not check.ok
    assert check.state == "MISSING"
    assert report([check]) == 1
    out = capsys.readouterr().out
    assert 'pip install -e ".[stt]"' in out


def test_advisory_seams_never_fail_the_check(capsys):
    """The --window Qt stack is chosen at run time, not in .env, so a missing one
    is worth a note and must not make `--check` exit non-zero."""
    check = Check("desktop window", "--window", "pywebview",
                  "a_module_that_is_not_installed", "desktop", advisory=True)
    assert not check.ok
    assert report([check]) == 0
    assert "fine to ignore" in capsys.readouterr().out


def test_probing_does_not_import_the_module():
    """`_have` uses find_spec, so running the doctor on a machine with torch
    installed must not pay torch's import cost (or its side effects)."""
    import sys

    from yurios.doctor import _have

    sentinel = "yurios.mind.dream"          # a real module, not yet imported here
    sys.modules.pop(sentinel, None)
    assert _have(sentinel)
    assert sentinel not in sys.modules
