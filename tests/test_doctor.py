"""The dependency doctor (`python -m yurios.doctor`).

The base install carries the local sentence-transformer embedder; optional heavy
backends remain lazy imports behind seams that degrade to fakes (SPEC §3). The
doctor is what keeps those optional seams from becoming a silent trap: it reads
the same Config the server reads and says which selected backends aren't installed,
plus the extra that installs each one.

Two things can rot here, and both of them look fine until someone follows the
advice: the extra names printed to the user can drift out of `pyproject.toml`,
and the config→module mapping can drift from what the backends actually import.
These pin both against the real files rather than against a copy of the table.
"""
from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

from yurios.doctor import Check, _collapse, collect, report
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


def test_only_the_local_embedder_is_a_model_base_dependency():
    """Memory must work without a server; other model backends stay optional."""
    with PYPROJECT.open("rb") as fh:
        project = tomllib.load(fh)["project"]
    core = " ".join(project["dependencies"]).lower()
    assert "sentence-transformers" in core
    for heavy in ("torch", "faster-whisper", "kokoro", "qwen-tts", "silero",
                   "pyqt", "pywebview"):
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


def test_missing_voice_seams_collapse_to_the_voice_extra(capsys):
    """Three separate extras for one obvious install is worse advice than the name
    pyproject and the README both use."""
    assert _collapse(["stt", "tts", "vad"]) == ["voice"]
    assert _collapse(["stt", "tts", "vad", "desktop"]) == ["voice", "desktop"]
    assert _collapse(["stt"]) == ["stt"]                      # nothing to collapse
    assert _collapse(["tts", "vad"]) == ["tts", "vad"]        # not the full set

    cfg = Config(_env_file=None, embed_backend="lm_studio")   # only the voice missing
    checks = [c for c in collect(cfg) if c.knob != "--window"]
    if all(not c.ok for c in checks if c.extra in {"stt", "tts", "vad"}):
        report(checks)
        assert 'pip install -e ".[voice]"' in capsys.readouterr().out


def test_the_cpu_torch_recipe_takes_torchaudio_from_the_same_index(monkeypatch, capsys):
    """CPU torch + PyPI torchaudio is a silent-voice install, so every place that
    prints the recipe has to name both packages, and the doctor has to spot the pair.

    This one actually shipped broken: `./install.sh --voice` installed CPU torch, the
    extras pulled torchaudio from PyPI, and kokoro/silero-vad died on
    `libcudart.so.13` — with every doctor row still saying "ok"."""
    from yurios import doctor

    root = Path(__file__).resolve().parent.parent
    recipes = [root / n for n in ("install.sh", "README.md", "pyproject.toml",
                                  "docs/installation.md")]
    # …plus the install hints the seams print. Only the torchaudio-shaped ones: the
    # sentence_tf embedder needs torch and nothing else, so its hint is right to
    # name only torch.
    recipes += [p for p in sorted((root / "yurios").rglob("*.py"))
                if any(dep in p.read_text() for dep in ("kokoro", "silero"))]
    for path in recipes:
        if path.name == "install.sh":
            source = path.read_text()
            assert 'packages+=(torchaudio)' in source, (
                "install.sh must install torchaudio with torch when the voice stack is selected")
            continue
        # install.sh wraps the command over a shell continuation, so fold those first
        # or the second half looks like a torch-less install.
        for line in path.read_text().replace("\\\n", " ").splitlines():
            if "download.pytorch.org/whl/cpu" in line and "index-url" in line:
                assert "torchaudio" in line, (
                    f"{path.name}: `{line.strip()}` installs torch without torchaudio "
                    f"— kokoro and silero-vad will fall back to fakes")

    versions = {"torch": "2.13.0+cpu", "torchaudio": "2.11.0"}
    monkeypatch.setattr("importlib.metadata.version", lambda pkg: versions[pkg])
    assert "libcudart" not in doctor.torch_pair_mismatch()   # no scary strings, just the fix
    assert "whl/cpu" in doctor.torch_pair_mismatch()

    # …and it is printed even when every seam checks out, which is the whole point.
    report([Check("embeddings", "EMBED_BACKEND", "lm_studio", "", "")])
    assert "torchaudio 2.11.0 came from PyPI" in capsys.readouterr().out

    for pair in (("2.13.0+cpu", "2.11.0+cpu"),      # both from pytorch.org — correct
                 ("2.13.0", "2.11.0"),              # both from PyPI (CUDA) — correct
                 ("2.11.0", "2.11.0+cpu")):         # odd, but not the failure we mean
        versions["torch"], versions["torchaudio"] = pair
        assert doctor.torch_pair_mismatch() == "", pair

    versions.pop("torchaudio")                      # a thin install: nothing to say
    assert doctor.torch_pair_mismatch() == ""


def test_the_mcp_cap_matches_the_module_the_tool_server_imports(monkeypatch, capsys):
    """The same shape of trap, one seam over: mcp 2.0 renamed `mcp.server.fastmcp` to
    `mcp.server.mcpserver` with no alias, so an uncapped fresh install resolved 2.0.0,
    `import mcp` still worked (the tools row said "ok"), and the only symptom was the
    spawned server dying on its import line — "she has no hands this run".

    Two things are pinned, both against the real files: the cap in pyproject, and that
    the doctor probes the module `server.py` actually imports. Move the import and the
    warning has to move with it, or it goes quiet on exactly the install it's for."""
    import inspect

    from yurios import doctor

    root = Path(__file__).resolve().parent.parent
    deps = tomllib.loads(PYPROJECT.read_text())["project"]["dependencies"]
    mcp_dep = next(d for d in deps if re.match(r"mcp\b", d))
    assert "<2" in mcp_dep, (
        f"pyproject asks for `{mcp_dep}` — a fresh install resolves mcp 2.x, which "
        f"has no `mcp.server.fastmcp`, and she boots hand-less")

    src = (root / "yurios" / "world" / "tools" / "server.py").read_text()
    module = re.search(r"from (mcp\.[\w.]+) import", src).group(1)
    assert module in inspect.getsource(doctor.mcp_api_mismatch), (
        f"server.py imports {module}; the doctor probes something else")

    assert doctor.mcp_api_mismatch() == ""          # this venv obeys the cap
    monkeypatch.setattr(doctor, "_have", lambda m: False)
    monkeypatch.setattr("importlib.metadata.version", lambda pkg: "2.0.0")
    assert "mcp 2.0.0" in doctor.mcp_api_mismatch()

    # …and, like the torch pair, it is printed even when every row checks out.
    report([Check("embeddings", "EMBED_BACKEND", "lm_studio", "", "")])
    out = capsys.readouterr().out
    assert "mcp 2.0.0" in out and "Fix what's flagged above" in out


def test_install_sh_default_installs_what_env_example_selects():
    """`./install.sh` with no flags must cover every backend the .env it writes selects.

    That is what "works out of the box" reduces to: install.sh copies .env.example to
    .env, so any seam the file names and the default extras don't install is a fresh
    checkout that boots into a fake — silent voice, no transcription — with a MISSING
    line in the doctor and no way for the user to know that was on purpose. Pinned via
    the script's own dry run, so the two can't drift."""
    root = Path(__file__).resolve().parent.parent
    installed = set(subprocess.run(
        ["bash", str(root / "install.sh"), "--print-extras"],
        capture_output=True, text=True, check=True,
    ).stdout.strip().split(","))

    cfg = Config(_env_file=str(root / ".env.example"))
    wanted = {c.extra for c in collect(cfg)
              if c.extra and not c.free and not c.advisory}
    missing = set(_collapse(sorted(wanted))) - installed
    assert not missing, (
        f".env.example selects backends the default install doesn't provide: "
        f"{sorted(missing)} (install.sh installs [{','.join(sorted(installed))}])")

    # And the other direction, so --thin stays honest: it must NOT claim the voice.
    thin = subprocess.run(
        ["bash", str(root / "install.sh"), "--thin", "--print-extras"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert thin == "test", f"--thin resolved to [{thin}], which isn't a thin install"

    removed = subprocess.run(
        ["bash", str(root / "install.sh"), "--local-embed", "--print-extras"],
        capture_output=True, text=True,
    )
    assert removed.returncode != 0
    assert "unknown option: --local-embed" in removed.stderr


def test_install_sh_prefers_cuda_when_nvidia_is_detected():
    """Interactive GPU-capable installs should not silently choose the CPU wheel."""
    installer = (Path(__file__).resolve().parent.parent / "install.sh").read_text()
    assert 'local default_choice="cpu" gpu_note=""' in installer
    assert 'default_choice="cuda"' in installer
    assert 'TORCH_CHOICE="$default_choice"' in installer


def test_env_example_selects_the_base_sentence_transformer_embedder():
    """The shipped .env.example must select the local embedder every install provides."""
    env = Path(__file__).resolve().parent.parent / ".env.example"
    settings: dict[str, str] = {}
    for raw in env.read_text().splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, _, value = raw.partition("=")
        settings[key.strip()] = value.split("#")[0].strip()

    assert settings.get("EMBED_BACKEND") == "sentence_tf"

    cfg = Config(_env_file=str(env))
    embed = next(c for c in collect(cfg) if c.knob == "EMBED_BACKEND")
    assert embed.ok, "the shipped .env.example selects an uninstallable embedder"


# ---- what leaves the machine -----------------------------------------------

def test_the_doctor_agrees_with_the_real_router_about_what_is_hosted():
    """`_hosted_on_openrouter` is a copy of providers/openrouter._route's rule,
    kept separate so the doctor doesn't import litellm. Copies drift; this is the
    only thing stopping it — the doctor would otherwise cheerfully tell someone
    their local model is being billed to OpenRouter, or say nothing while it is."""
    from yurios.app.providers.openrouter import _route
    from yurios.doctor import _hosted_on_openrouter

    for model in ("gemma-4", "openrouter/z-ai/glm-5", "lm_studio/gemma-4",
                  "ollama/qwen3", "openai/gpt-5.2", "anthropic/claude-opus-4"):
        assert _hosted_on_openrouter(model) == _route(model).startswith("openrouter/"), \
            f"the doctor and the router disagree about {model!r}"


def test_the_hosted_lines_name_the_app_and_the_client(cfg):
    from yurios.doctor import network_lines

    cfg = cfg.model_copy(update={"chat_model": "openrouter/z-ai/glm-5",
                                 "selfie_backend": "openrouter"})
    body = "\n".join(network_lines(cfg))
    assert "chat, selfies" in body
    assert "https://yurios.org" in body
    assert "YuriOS/" in body and "litellm/" in body     # the composite user-agent


def test_an_all_local_config_says_so(cfg):
    """The default stack bills nobody — the doctor must not imply otherwise."""
    from yurios.doctor import network_lines

    cfg = cfg.model_copy(update={"chat_model": "lm_studio/gemma-4",
                                 "utility_model": "ollama/qwen3",
                                 "selfie_backend": "mock"})
    body = "\n".join(network_lines(cfg))
    assert "nothing hosted selected" in body


def test_the_phone_out_switches_are_reported_as_they_are(cfg, monkeypatch):
    """Both directions: the doctor reads the environment, so it tells the truth
    even when someone has turned the quiet defaults back off."""
    from yurios.doctor import network_lines

    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    monkeypatch.setenv("HF_HUB_DISABLE_TELEMETRY", "1")
    quiet = "\n".join(network_lines(cfg))
    assert "no fetch at start" in quiet and "telemetry off" in quiet

    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "False")
    monkeypatch.setenv("HF_HUB_DISABLE_TELEMETRY", "0")
    loud = "\n".join(network_lines(cfg))
    assert "raw.githubusercontent.com at EVERY start" in loud
    assert "report your torch build" in loud


def test_probing_does_not_import_the_module():
    """`_have` uses find_spec, so running the doctor on a machine with torch
    installed must not pay torch's import cost (or its side effects)."""
    import sys

    from yurios.doctor import _have

    sentinel = "yurios.mind.dream"          # a real module, not yet imported here
    sys.modules.pop(sentinel, None)
    assert _have(sentinel)
    assert sentinel not in sys.modules
