"""Fresh installs remain offline until the user chooses a chat model."""
from __future__ import annotations

from yurios.app.main import UnconfiguredChatModel, build_chat_model
from yurios.models import (DEFAULT_HUGGINGFACE_MODEL, NONE, ModelCheck,
                            RECOMMENDED_MODELS, gguf_connection_defaults,
                            save_model_choice, validate_model)
from yurios.world.config import Config


def test_defaults_have_no_language_model_connection():
    cfg = Config(_env_file=None)

    assert cfg.chat_model == cfg.utility_model == NONE
    assert DEFAULT_HUGGINGFACE_MODEL == "HauhauCS/Gemma-4-E4B-Uncensored-HauhauCS-Aggressive"
    assert cfg.embed_backend == "sentence_tf"
    assert cfg.selfie_backend == "off"
    assert isinstance(build_chat_model(cfg), UnconfiguredChatModel)


def test_saving_none_preserves_other_environment_configuration(tmp_path):
    env = tmp_path / ".env"
    env.write_text("EMBED_BACKEND=sentence_tf\nCHAT_MODEL=old\n", encoding="utf-8")

    save_model_choice(env, NONE)

    text = env.read_text(encoding="utf-8")
    assert "EMBED_BACKEND=sentence_tf" in text
    assert "CHAT_MODEL=NONE" in text
    assert "UTILITY_MODEL=NONE" in text


def test_gguf_recommendation_is_valid_without_contacting_a_remote_server():
    cfg = Config(_env_file=None)
    model = RECOMMENDED_MODELS[0]["id"]

    check = validate_model(cfg, model)

    assert model.startswith("gguf/")
    assert check.ok and "download" in check.detail.lower()


def test_gguf_connection_defaults_choose_a_16_gb_profile():
    profile = gguf_connection_defaults(gpu_memory_bytes=16 * 1024 ** 3)

    assert profile == {
        "GGUF_CONTEXT_LENGTH": "32768",
        "GGUF_N_GPU_LAYERS": "20",
        "GGUF_FLASH_ATTN": "true",
    }


def test_first_run_endpoint_exposes_recommendation_and_saves_none(cfg, tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    from yurios.desktop.voice.backends.fakes import FakeBrain
    from yurios.world.main import create_app
    from yurios.world.routes import onboarding

    env = tmp_path / ".env"
    env.write_text("EMBED_BACKEND=sentence_tf\n", encoding="utf-8")
    monkeypatch.setattr(onboarding, "ENV_PATH", env)
    app = create_app(cfg, brain=FakeBrain())
    # The injected test brain is operational, but this endpoint test represents
    # the normal fresh server, which has no configured model.
    app.state.rt.model_configured = False

    with TestClient(app, client=("127.0.0.1", 5555)) as client:
        shown = client.get("/api/onboarding").json()
        saved = client.post("/api/onboarding", json={"model": "NONE"}).json()

    assert not shown["configured"]
    assert shown["recommendations"] == list(RECOMMENDED_MODELS)
    assert saved["ok"] and saved["restart_required"]
    assert "CHAT_MODEL=NONE" in env.read_text(encoding="utf-8")


def test_first_run_endpoint_saves_the_same_gguf_profile_the_cli_does(
        cfg, tmp_path, monkeypatch):
    """A GGUF id says nothing about the card it runs on, so the browser panel
    has to write the offload profile too. Without it the same model chosen in
    the browser ran on the CPU at the fallback window while the terminal's
    choice got the GPU — a divergence whose only symptom is "she is slow"."""
    from starlette.testclient import TestClient

    from yurios.desktop.voice.backends.fakes import FakeBrain
    from yurios.world.main import create_app
    from yurios.world.routes import onboarding

    env = tmp_path / ".env"
    env.write_text("EMBED_BACKEND=sentence_tf\n", encoding="utf-8")
    monkeypatch.setattr(onboarding, "ENV_PATH", env)
    monkeypatch.setattr(onboarding, "gguf_connection_defaults", lambda: {
        "GGUF_CONTEXT_LENGTH": "32768", "GGUF_N_GPU_LAYERS": "20",
        "GGUF_FLASH_ATTN": "true"})
    monkeypatch.setattr(onboarding, "download_gguf",
                        lambda cfg, model: tmp_path / "model.gguf")
    app = create_app(cfg, brain=FakeBrain())
    app.state.rt.model_configured = False

    model = f"gguf/{DEFAULT_HUGGINGFACE_MODEL}"
    with TestClient(app, client=("127.0.0.1", 5555)) as client:
        assert client.post("/api/onboarding", json={"model": model}).json()["ok"]

    saved = env.read_text(encoding="utf-8")
    assert f"CHAT_MODEL={model}" in saved
    assert "GGUF_CONTEXT_LENGTH=32768" in saved
    assert "GGUF_N_GPU_LAYERS=20" in saved
    assert "GGUF_FLASH_ATTN=true" in saved


def test_first_run_endpoint_writes_no_gguf_profile_for_a_hosted_model(
        cfg, tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    from yurios.desktop.voice.backends.fakes import FakeBrain
    from yurios.world.main import create_app
    from yurios.world.routes import onboarding

    env = tmp_path / ".env"
    env.write_text("EMBED_BACKEND=sentence_tf\n", encoding="utf-8")
    monkeypatch.setattr(onboarding, "ENV_PATH", env)
    monkeypatch.setattr(onboarding, "validate_model",
                        lambda cfg, model: ModelCheck(True, "connection verified"))
    app = create_app(cfg, brain=FakeBrain())
    app.state.rt.model_configured = False

    with TestClient(app, client=("127.0.0.1", 5555)) as client:
        client.post("/api/onboarding", json={"model": "ollama/qwen3"})

    assert "GGUF_N_GPU_LAYERS" not in env.read_text(encoding="utf-8")


def test_installer_exposes_the_cli_without_activating_the_venv():
    from pathlib import Path

    installer = (Path(__file__).resolve().parent.parent / "install.sh").read_text()

    assert 'ln -sfn "$VENV_DIR/bin/yurios" "$launcher"' in installer
    assert '"$HOME/.local/bin/yurios" start </dev/null' in installer
    assert "cat <<'EOF'\n\nYuriOS is running as a background daemon:" in installer
    assert "source ${VENV_DIR#$ROOT_DIR}/bin/activate" not in installer


def test_doctor_runs_through_the_yurios_command(monkeypatch):
    from yurios import cli, doctor

    monkeypatch.setattr(doctor, "main", lambda: 1)

    assert cli.main(["doctor"]) == 1


def test_configure_ollama_saves_model_and_endpoint(tmp_path, monkeypatch):
    from argparse import Namespace

    from yurios import cli

    monkeypatch.setattr(cli, "_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "validate_model", lambda cfg, model: (
        ModelCheck(cfg.ollama_base_url == "http://ollama:11434" and model == "ollama/qwen3",
                   "connection verified")))
    args = Namespace(model="qwen3", provider="ollama", base_url="http://ollama:11434",
                     api_key=None)

    assert cli.command_configure(args) == 0
    saved = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "CHAT_MODEL=ollama/qwen3" in saved
    assert "UTILITY_MODEL=ollama/qwen3" in saved
    assert "OLLAMA_BASE_URL=http://ollama:11434" in saved


def test_configure_openrouter_saves_the_prompted_key(tmp_path, monkeypatch):
    from argparse import Namespace

    from yurios import cli

    monkeypatch.setattr(cli, "_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "validate_model", lambda cfg, model: (
        ModelCheck(cfg.openrouter_api_key == "secret" and model == "openrouter/vendor/model",
                   "connection verified")))
    args = Namespace(model="vendor/model", provider="openrouter", base_url=None,
                     api_key="secret")

    assert cli.command_configure(args) == 0
    saved = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "CHAT_MODEL=openrouter/vendor/model" in saved
    assert "OPENROUTER_API_KEY=secret" in saved


def test_configure_openrouter_selfies_prompts_for_and_saves_its_key(tmp_path, monkeypatch):
    from yurios import cli

    monkeypatch.setattr(cli, "_root", lambda: tmp_path)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    prompts = []
    monkeypatch.setattr(cli.getpass, "getpass",
                        lambda prompt: prompts.append(prompt) or "secret")

    assert cli.main(["configure", "--selfie-backend", "openrouter", "--selfie-model",
                     "bytedance-seed/seedream-4.5"]) == 0

    saved = (tmp_path / ".env").read_text(encoding="utf-8")
    assert prompts == ["OpenRouter API key (Enter to keep configured key): "]
    assert "SELFIE_BACKEND=openrouter" in saved
    assert "SELFIE_MODEL=bytedance-seed/seedream-4.5" in saved
    assert "OPENROUTER_API_KEY=secret" in saved


def test_configure_diffusers_selfies_saves_an_existing_checkpoint(tmp_path, monkeypatch):
    from yurios import cli

    checkpoint = tmp_path / "oneObsession3D_v10Illustrious.safetensors"
    checkpoint.write_bytes(b"checkpoint header")
    monkeypatch.setattr(cli, "_root", lambda: tmp_path)

    assert cli.main(["configure", "--selfie-backend", "diffusers", "--selfie-local-model",
                     str(checkpoint)]) == 0

    saved = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "SELFIE_BACKEND=diffusers" in saved
    assert f"SELFIE_LOCAL_MODEL={checkpoint}" in saved


def test_interactive_configure_selects_the_chat_model_then_selfie_route(tmp_path, monkeypatch):
    from yurios import cli

    monkeypatch.setattr(cli, "_root", lambda: tmp_path)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    answers = iter(("n", "2", "bytedance-seed/seedream-4.5"))
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "secret")

    assert cli.main(["configure"]) == 0

    saved = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "CHAT_MODEL=NONE" in saved
    assert "UTILITY_MODEL=NONE" in saved
    assert "SELFIE_BACKEND=openrouter" in saved
    assert "SELFIE_MODEL=bytedance-seed/seedream-4.5" in saved


def test_configure_huggingface_default_downloads_the_suggested_gguf(tmp_path, monkeypatch):
    from argparse import Namespace

    from yurios import cli

    monkeypatch.setattr(cli, "_root", lambda: tmp_path)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "" if "Selection" in prompt else "")
    monkeypatch.setattr(cli, "gguf_connection_defaults", lambda: {
        "GGUF_CONTEXT_LENGTH": "32768", "GGUF_N_GPU_LAYERS": "20",
        "GGUF_FLASH_ATTN": "true"})
    downloaded = []
    monkeypatch.setattr(cli, "download_gguf", lambda cfg, model: downloaded.append(model) or tmp_path / "model.gguf")
    args = Namespace(model=None, provider=None, base_url=None, api_key=None)

    assert cli.command_configure(args) == 0

    model = f"gguf/{DEFAULT_HUGGINGFACE_MODEL}"
    saved = (tmp_path / ".env").read_text(encoding="utf-8")
    assert f"CHAT_MODEL={model}" in saved
    assert "GGUF_CONTEXT_LENGTH=32768" in saved
    assert "GGUF_N_GPU_LAYERS=20" in saved
    assert "GGUF_FLASH_ATTN=true" in saved
    assert "SELFIE_BACKEND=off" in saved
    assert downloaded == [model]


def test_configure_huggingface_custom_id_uses_direct_gguf_route(tmp_path, monkeypatch):
    from argparse import Namespace

    from yurios import cli

    monkeypatch.setattr(cli, "_root", lambda: tmp_path)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    answers = iter(("1", "owner/custom-model", ""))
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr(cli, "gguf_connection_defaults", lambda: {
        "GGUF_CONTEXT_LENGTH": "32768", "GGUF_N_GPU_LAYERS": "20",
        "GGUF_FLASH_ATTN": "true"})
    monkeypatch.setattr(cli, "download_gguf", lambda cfg, model: tmp_path / "model.gguf")
    args = Namespace(model=None, provider=None, base_url=None, api_key=None)

    assert cli.command_configure(args) == 0

    saved = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "CHAT_MODEL=gguf/owner/custom-model" in saved
    assert "SELFIE_BACKEND=off" in saved


def test_restart_stops_then_starts(monkeypatch):
    from argparse import Namespace

    from yurios import cli

    calls = []
    monkeypatch.setattr(cli, "command_stop", lambda args: calls.append("stop") or 0)
    monkeypatch.setattr(cli, "command_start", lambda args: calls.append("start") or 0)

    assert cli.command_restart(Namespace(foreground=False)) == 0
    assert calls == ["stop", "start"]


def test_start_waits_for_health_before_reporting_success(tmp_path, monkeypatch, capsys):
    from argparse import Namespace

    from yurios import cli

    class Process:
        pid = 1234
        returncode = None

        def poll(self):
            return self.returncode

    proc = Process()
    monkeypatch.setattr(cli, "_root", lambda: tmp_path)
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *args, **kwargs: proc)
    seen = []
    monkeypatch.setattr(cli, "_wait_for_ready",
                        lambda cfg, *, proc=None: seen.append(proc) or None)

    assert cli.command_start(Namespace(foreground=False)) == 0

    assert seen == [proc]
    assert "started and is ready" in capsys.readouterr().out


def test_start_terminates_daemon_when_health_check_times_out(tmp_path, monkeypatch, capsys):
    from argparse import Namespace

    from yurios import cli

    class Process:
        pid = 1234
        returncode = None
        terminated = False

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True

    proc = Process()
    monkeypatch.setattr(cli, "_root", lambda: tmp_path)
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *args, **kwargs: proc)
    monkeypatch.setattr(cli, "_wait_for_ready", lambda cfg, *, proc=None: "timed out after 120 seconds")

    assert cli.command_start(Namespace(foreground=False)) == 1

    assert proc.terminated
    assert not (tmp_path / ".yurios" / "yurios.pid").exists()
    assert "failed to start: timed out after 120 seconds" in capsys.readouterr().err


def test_health_wait_times_out_after_two_minutes(monkeypatch):
    from yurios import cli

    monkeypatch.setattr(cli, "_START_TIMEOUT_SECONDS", 0)

    assert cli._wait_for_ready(Config(_env_file=None)) == "timed out after 0 seconds"


def test_log_prints_the_daemon_log(tmp_path, monkeypatch, capsys):
    from argparse import Namespace

    from yurios import cli

    log = tmp_path / ".yurios" / "yurios.log"
    log.parent.mkdir()
    log.write_text("server started\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_root", lambda: tmp_path)

    assert cli.command_log(Namespace()) == 0
    assert capsys.readouterr().out == "server started\n"


def test_status_distinguishes_saved_model_from_active_daemon(tmp_path, monkeypatch, capsys):
    from argparse import Namespace

    from yurios import cli

    (tmp_path / ".env").write_text("CHAT_MODEL=ollama/qwen3\n", encoding="utf-8")

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"model": "NONE", "model_configured": False,
                    "voice": {"state": "unloaded"}}

    monkeypatch.setattr(cli, "_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "_read_pid", lambda path: 1234)
    monkeypatch.setattr(cli.httpx, "get", lambda *args, **kwargs: Response())

    assert cli.command_status(Namespace()) == 0

    output = capsys.readouterr().out
    assert "Model: NONE" in output
    assert "Configured model: ollama/qwen3 (restart required)" in output


def test_uninstall_removes_only_the_global_launcher_and_venv(tmp_path, monkeypatch, capsys):
    from argparse import Namespace

    from yurios import cli

    venv = tmp_path / ".venv"
    target = venv / "bin" / "yurios"
    target.parent.mkdir(parents=True)
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher = tmp_path / "home" / ".local" / "bin" / "yurios"
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(target)
    preserved = tmp_path / ".env"
    preserved.write_text("CHAT_MODEL=NONE\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_root", lambda: tmp_path)
    monkeypatch.setattr(cli.sys, "prefix", str(venv))
    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(cli, "command_stop", lambda args: 0)
    monkeypatch.setattr(cli, "_wait_for_shutdown", lambda cfg: True)
    exec_calls = []
    monkeypatch.setattr(cli.os, "execv", lambda path, args: exec_calls.append((path, args)))

    assert cli.command_uninstall(Namespace(yes=True)) == 0

    assert not launcher.exists()
    assert venv.exists()
    assert exec_calls == [("/bin/rm", ["rm", "-rf", str(venv)])]
    assert preserved.read_text(encoding="utf-8") == "CHAT_MODEL=NONE\n"
    assert "were preserved" in capsys.readouterr().out


def test_uninstall_refuses_to_remove_files_until_the_server_is_down(tmp_path, monkeypatch, capsys):
    from argparse import Namespace

    from yurios import cli

    venv = tmp_path / ".venv"
    target = venv / "bin" / "yurios"
    target.parent.mkdir(parents=True)
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher = tmp_path / "home" / ".local" / "bin" / "yurios"
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(target)
    monkeypatch.setattr(cli, "_root", lambda: tmp_path)
    monkeypatch.setattr(cli.sys, "prefix", str(venv))
    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(cli, "command_stop", lambda args: 0)
    monkeypatch.setattr(cli, "_wait_for_shutdown", lambda cfg: False)

    assert cli.command_uninstall(Namespace(yes=True)) == 1

    assert launcher.exists()
    assert venv.exists()
    assert "still serving requests" in capsys.readouterr().err


def test_uninstall_refuses_to_remove_another_global_command(tmp_path, monkeypatch, capsys):
    from argparse import Namespace

    from yurios import cli

    venv = tmp_path / ".venv"
    (venv / "bin").mkdir(parents=True)
    launcher = tmp_path / "home" / ".local" / "bin" / "yurios"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("another program\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_root", lambda: tmp_path)
    monkeypatch.setattr(cli.sys, "prefix", str(venv))
    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path / "home")

    assert cli.command_uninstall(Namespace(yes=True)) == 1

    assert launcher.exists()
    assert venv.exists()
    assert "Refusing to remove" in capsys.readouterr().err
