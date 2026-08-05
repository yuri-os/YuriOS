"""Command-line control plane for a local YuriOS installation."""
from __future__ import annotations

import argparse
import getpass
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

from yurios.models import (DEFAULT_HUGGINGFACE_MODEL, NONE, RECOMMENDED_MODELS,
                            download_gguf, gguf_connection_defaults,
                            huggingface_gguf_model, is_configured, normalize_model,
                            save_model_choice, update_env, validate_model)
from yurios.world.config import Config

_PROVIDER_PREFIXES = {
    "lmstudio": "lm_studio/",
    "ollama": "ollama/",
    "openrouter": "openrouter/",
}
_SELFIE_BACKENDS = ("openrouter", "diffusers", "off")
_START_TIMEOUT_SECONDS = 120.0
_STOP_TIMEOUT_SECONDS = 10.0
_POLL_INTERVAL_SECONDS = 0.25


def _root() -> Path:
    return Path.cwd()


def _env_path(root: Path) -> Path:
    return root / ".env"


def _pid_path(root: Path) -> Path:
    return root / ".yurios" / "yurios.pid"


def _read_pid(path: Path) -> int | None:
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
        return pid if _process_is_running(pid) else None
    except (OSError, ValueError):
        return None


def _process_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _health_url(cfg: Config) -> str:
    return f"http://{cfg.host}:{cfg.port}/api/health"


def _wait_for_ready(cfg: Config, *, proc: subprocess.Popen | None = None) -> str | None:
    """Return an error when the server fails to become healthy before the deadline."""
    deadline = time.monotonic() + _START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            return f"daemon exited with status {proc.returncode}"
        try:
            response = httpx.get(_health_url(cfg), timeout=1.0)
            response.raise_for_status()
            return None
        except httpx.HTTPError:
            time.sleep(_POLL_INTERVAL_SECONDS)
    return f"timed out after {_START_TIMEOUT_SECONDS:.0f} seconds"


def _wait_for_exit(pid: int) -> bool:
    deadline = time.monotonic() + _STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not _process_is_running(pid):
            return True
        time.sleep(_POLL_INTERVAL_SECONDS)
    return not _process_is_running(pid)


def _wait_for_shutdown(cfg: Config) -> bool:
    """Confirm that YuriOS no longer serves requests before removing its runtime."""
    deadline = time.monotonic() + _STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            response = httpx.get(_health_url(cfg), timeout=1.0)
            response.raise_for_status()
        except httpx.HTTPError:
            return True
        time.sleep(_POLL_INTERVAL_SECONDS)
    return False


def _configured_cfg(root: Path) -> Config:
    return Config(_env_file=str(_env_path(root)))


def _prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    return input(f"{label}{suffix}: ").strip() or default


def _provider_model(provider: str, model: str) -> str:
    prefix = _PROVIDER_PREFIXES[provider]
    return model if model.startswith(prefix) else prefix + model.removeprefix("/")


def _interactive_choice() -> str:
    print("Choose a language model connection:")
    print("  1. Download a Hugging Face GGUF model")
    for index, item in enumerate(RECOMMENDED_MODELS, start=2):
        print(f"  {index}. {item['label']} [{item['hardware']}]")
    print("  l. LM Studio")
    print("  o. Ollama")
    print("  r. OpenRouter")
    print("  c. Enter another LiteLLM model id")
    print("  n. NONE (leave YuriOS without an LLM)")
    return input("Selection [1]: ").strip().lower() or "1"


def _interactive_selfie_choice() -> str:
    print("Choose a selfie camera:")
    print("  1. OFF (no camera)")
    print("  2. OpenRouter")
    print("  3. Local Diffusers checkpoint")
    return input("Selection [1]: ").strip() or "1"


def command_configure_selfies(args, cfg: Config, root: Path) -> int:
    """Save a camera route without loading a renderer or making an image request."""
    backend = args.selfie_backend
    interactive = sys.stdin.isatty()
    selfie_model = getattr(args, "selfie_model", None)
    local_model = getattr(args, "selfie_local_model", None)
    if backend not in _SELFIE_BACKENDS:
        print(f"Unsupported selfie backend: {backend}", file=sys.stderr)
        return 2
    updates = {"SELFIE_BACKEND": backend}

    if backend == "openrouter":
        if local_model:
            print("--selfie-local-model is only valid with --selfie-backend diffusers.",
                  file=sys.stderr)
            return 2
        key = args.api_key
        if key is None and interactive:
            entered = getpass.getpass("OpenRouter API key (Enter to keep configured key): ")
            key = entered or cfg.openrouter_api_key
        key = key if key is not None else cfg.openrouter_api_key
        if not key:
            print("OPENROUTER_API_KEY is required for the openrouter selfie backend.",
                  file=sys.stderr)
            return 2
        updates["OPENROUTER_API_KEY"] = key
        updates["SELFIE_MODEL"] = selfie_model or cfg.selfie_model
    elif backend == "diffusers":
        if selfie_model:
            print("--selfie-model is only valid with --selfie-backend openrouter.",
                  file=sys.stderr)
            return 2
        checkpoint = local_model or cfg.selfie_local_model
        if not checkpoint:
            print("--selfie-local-model is required for the diffusers selfie backend.",
                  file=sys.stderr)
            return 2
        checkpoint_path = Path(checkpoint).expanduser()
        if checkpoint_path.suffix.lower() != ".safetensors" or not checkpoint_path.is_file():
            print(f"Diffusers checkpoint was not found: {checkpoint_path}", file=sys.stderr)
            return 2
        updates["SELFIE_LOCAL_MODEL"] = str(checkpoint_path)
    elif selfie_model or local_model:
        print("A selfie model can only be set for an active selfie backend.", file=sys.stderr)
        return 2

    update_env(_env_path(root), updates, section="# --- selfie settings selected by YuriOS ---")
    if backend == "openrouter":
        print(f"Saved OpenRouter selfies with model {updates['SELFIE_MODEL']}.")
    elif backend == "diffusers":
        print(f"Saved local Diffusers selfies with checkpoint {updates['SELFIE_LOCAL_MODEL']}.")
    else:
        print("Saved SELFIE_BACKEND=off.")
    return 0


def command_configure_selfies_interactively(args, cfg: Config, root: Path) -> int:
    """Collect the settings for the camera selected during interactive setup."""
    choice = _interactive_selfie_choice()
    args.selfie_model = None
    args.selfie_local_model = None
    if choice == "1":
        args.selfie_backend = "off"
    elif choice == "2":
        args.selfie_backend = "openrouter"
        args.selfie_model = _prompt("OpenRouter selfie model", cfg.selfie_model)
    elif choice == "3":
        args.selfie_backend = "diffusers"
        args.selfie_local_model = _prompt("Diffusers checkpoint (.safetensors)",
                                         cfg.selfie_local_model)
    else:
        print("Invalid selection.", file=sys.stderr)
        return 2
    return command_configure_selfies(args, cfg, root)


def command_configure(args) -> int:
    root = _root()
    cfg = _configured_cfg(root)
    selfie_backend = getattr(args, "selfie_backend", None)
    selfie_model = getattr(args, "selfie_model", None)
    selfie_local_model = getattr(args, "selfie_local_model", None)
    if selfie_backend or selfie_model or selfie_local_model:
        if not selfie_backend:
            print("--selfie-backend is required when configuring selfies.", file=sys.stderr)
            return 2
        if args.model or args.provider:
            print("Configure a language model and selfie backend in separate commands.",
                  file=sys.stderr)
            return 2
        return command_configure_selfies(args, cfg, root)
    model = args.model
    provider = args.provider
    interactive = sys.stdin.isatty()
    configure_selfies = interactive and not model and not provider
    if not model and not provider:
        if not interactive:
            print("No model selected. Use `yurios configure --model <model-id>`.", file=sys.stderr)
            return 2
        choice = _interactive_choice()
        if choice == "1":
            model = huggingface_gguf_model(_prompt(
                "Hugging Face GGUF model id", DEFAULT_HUGGINGFACE_MODEL))
        elif choice.isdigit() and 2 <= int(choice) <= len(RECOMMENDED_MODELS) + 1:
            model = RECOMMENDED_MODELS[int(choice) - 2]["id"]
        elif choice == "n":
            model = NONE
        elif choice == "l":
            provider = "lmstudio"
        elif choice == "o":
            provider = "ollama"
        elif choice == "r":
            provider = "openrouter"
        elif choice == "c":
            model = input("Model id (for example ollama/qwen3): ").strip()
        else:
            print("Invalid selection.", file=sys.stderr)
            return 2
    connection: dict[str, str] = {}
    if provider in _PROVIDER_PREFIXES:
        if not model:
            label = {"lmstudio": "LM Studio model id", "ollama": "Ollama model name",
                     "openrouter": "OpenRouter model id"}[provider]
            if not interactive:
                print(f"--model is required with --provider {provider}", file=sys.stderr)
                return 2
            model = _prompt(label)
        if not model:
            print("A model id is required.", file=sys.stderr)
            return 2
        model = _provider_model(provider, model)
        if provider == "lmstudio":
            base = args.base_url or (interactive and _prompt(
                "LM Studio base URL", cfg.lmstudio_base_url)) or cfg.lmstudio_base_url
            connection["LMSTUDIO_BASE_URL"] = str(base)
            cfg = cfg.model_copy(update={"lmstudio_base_url": str(base)})
        elif provider == "ollama":
            base = args.base_url or (interactive and _prompt(
                "Ollama base URL", cfg.ollama_base_url)) or cfg.ollama_base_url
            connection["OLLAMA_BASE_URL"] = str(base)
            cfg = cfg.model_copy(update={"ollama_base_url": str(base)})
        else:
            key = args.api_key
            if key is None and interactive:
                entered = getpass.getpass("OpenRouter API key (Enter to keep configured key): ")
                key = entered or cfg.openrouter_api_key
            key = key if key is not None else cfg.openrouter_api_key
            if key:
                connection["OPENROUTER_API_KEY"] = key
            cfg = cfg.model_copy(update={"openrouter_api_key": key})
    model = normalize_model(model)
    if model.startswith("gguf/"):
        connection.update(gguf_connection_defaults())
    check = validate_model(cfg, model)
    if not check.ok:
        print(f"Model was not saved: {check.detail}", file=sys.stderr)
        return 1
    save_model_choice(_env_path(root), model, connection=connection)
    print(f"Saved CHAT_MODEL and UTILITY_MODEL as {model}.")
    if model.startswith("gguf/"):
        print("Configured direct GGUF runtime: "
              f"{connection['GGUF_CONTEXT_LENGTH']} tokens, "
              f"{connection['GGUF_N_GPU_LAYERS']} GPU layers, Flash Attention enabled.")
        print("Downloading the selected GGUF if it is not already cached…")
        try:
            path = download_gguf(_configured_cfg(root), model)
        except Exception as exc:  # keep the chosen configuration for a later retry
            print(f"Model saved, but download failed: {exc}", file=sys.stderr)
            return 1
        print(f"GGUF ready: {path}")
    else:
        print(check.detail)
    if configure_selfies:
        return command_configure_selfies_interactively(args, cfg, root)
    return 0


def command_download(args) -> int:
    root = _root()
    cfg = _configured_cfg(root)
    model = normalize_model(args.model or cfg.chat_model)
    if not is_configured(model):
        print("No GGUF model is selected. Run `yurios configure` first.", file=sys.stderr)
        return 2
    try:
        print(download_gguf(cfg, model))
    except Exception as exc:
        print(f"Download failed: {exc}", file=sys.stderr)
        return 1
    return 0


def command_status(args) -> int:
    root = _root()
    cfg = _configured_cfg(root)
    pid = _read_pid(_pid_path(root))
    try:
        response = httpx.get(_health_url(cfg), timeout=2.0)
        response.raise_for_status()
        status = response.json()
    except httpx.HTTPError:
        print("YuriOS: stopped")
        print(f"Model: {cfg.chat_model}")
        return 1
    print("YuriOS: running" + (f" (pid {pid})" if pid else ""))
    print(f"URL: http://{cfg.host}:{cfg.port}")
    active_model = status.get("model")
    print(f"Model: {active_model}")
    if active_model != cfg.chat_model:
        print(f"Configured model: {cfg.chat_model} (restart required)")
    if not status.get("model_configured", True):
        print("Setup: model selection required (`yurios configure` or open the dashboard)")
    print(f"Voice: {status.get('voice', {}).get('state', status.get('voice'))}")
    return 0


def command_log(args) -> int:
    log_path = _pid_path(_root()).parent / "yurios.log"
    try:
        sys.stdout.write(log_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"No YuriOS log found at {log_path}.", file=sys.stderr)
        return 1
    return 0


def command_uninstall(args) -> int:
    """Remove the launcher and venv created by install.sh, but not project data."""
    root = _root().resolve()
    venv_dir = Path(sys.prefix).resolve()
    expected_venv = (root / ".venv").resolve()
    launcher = Path.home() / ".local" / "bin" / "yurios"
    expected_launcher = (venv_dir / "bin" / "yurios").resolve()

    if venv_dir != expected_venv:
        print("Refusing to uninstall: yurios is not running from this project's .venv.",
              file=sys.stderr)
        return 1
    if launcher.is_symlink() and launcher.resolve() != expected_launcher:
        print(f"Refusing to remove {launcher}: it is not this YuriOS launcher's symlink.",
              file=sys.stderr)
        return 1
    if launcher.exists() and not launcher.is_symlink():
        print(f"Refusing to remove {launcher}: it is not this YuriOS launcher's symlink.",
              file=sys.stderr)
        return 1
    if not args.yes:
        if not sys.stdin.isatty():
            print("Refusing unattended uninstall; rerun with `yurios uninstall --yes`.",
                  file=sys.stderr)
            return 2
        answer = input(f"Remove {launcher} and {venv_dir}? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("Uninstall cancelled.")
            return 0
    if command_stop(args):
        return 1
    if not _wait_for_shutdown(_configured_cfg(root)):
        print("YuriOS is still serving requests; refusing to uninstall.", file=sys.stderr)
        return 1
    launcher.unlink(missing_ok=True)
    print("Removed the YuriOS launcher. Removing the virtual environment.")
    print("Project files, .env, .yurios, and downloaded models were preserved.")
    sys.stdout.flush()
    # Replacing this process avoids deleting files while this interpreter can still write them.
    try:
        os.execv("/bin/rm", ["rm", "-rf", str(venv_dir)])
    except OSError as exc:
        print(f"Could not remove {venv_dir}: {exc}", file=sys.stderr)
        return 1
    return 0


def command_start(args) -> int:
    root = _root()
    pid_path = _pid_path(root)
    running = _read_pid(pid_path)
    if running:
        failure = _wait_for_ready(_configured_cfg(root))
        if failure:
            print(f"YuriOS daemon (pid {running}) is not ready: {failure}.", file=sys.stderr)
            return 1
        print(f"YuriOS is already running and ready (pid {running}).")
        return 0
    if args.foreground:
        from yurios.world.__main__ import main

        main([])
        return 0
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    log_path = pid_path.parent / "yurios.log"
    with log_path.open("a", encoding="utf-8") as log:
        proc = subprocess.Popen([sys.executable, "-m", "yurios.world"], cwd=root,
                                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                 start_new_session=True)
    pid_path.write_text(f"{proc.pid}\n", encoding="utf-8")
    cfg = _configured_cfg(root)
    failure = _wait_for_ready(cfg, proc=proc)
    if failure:
        if proc.poll() is None:
            proc.terminate()
        pid_path.unlink(missing_ok=True)
        print(f"YuriOS failed to start: {failure}. See {log_path}", file=sys.stderr)
        return 1
    print(f"YuriOS started and is ready (pid {proc.pid}).")
    print(f"Open http://{cfg.host}:{cfg.port}")
    return 0


def command_stop(args) -> int:
    path = _pid_path(_root())
    pid = _read_pid(path)
    if not pid:
        path.unlink(missing_ok=True)
        print("YuriOS is not running.")
        return 0
    os.kill(pid, signal.SIGTERM)
    if not _wait_for_exit(pid):
        print(f"YuriOS did not stop within {_STOP_TIMEOUT_SECONDS:.0f} seconds (pid {pid}).",
              file=sys.stderr)
        return 1
    path.unlink(missing_ok=True)
    print(f"Stopped YuriOS (pid {pid}).")
    return 0


def command_restart(args) -> int:
    """Replace the daemon so changes saved in .env become active."""
    if command_stop(args):
        return 1
    return command_start(args)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="yurios")
    sub = parser.add_subparsers(dest="command")
    configure = sub.add_parser("configure", help="choose a language model or selfie backend")
    configure.add_argument("--model", help="model id, or NONE")
    configure.add_argument("--provider", choices=tuple(_PROVIDER_PREFIXES),
                           help="connection to configure; interactive mode offers all providers")
    configure.add_argument("--base-url", help="LM Studio or Ollama endpoint URL")
    configure.add_argument("--api-key", help="OpenRouter API key (saved in .env)")
    configure.add_argument("--selfie-backend", choices=_SELFIE_BACKENDS,
                           help="configure the selfie route")
    configure.add_argument("--selfie-model", help="OpenRouter image model")
    configure.add_argument("--selfie-local-model",
                           help="path to a local .safetensors checkpoint for Diffusers")
    configure.set_defaults(func=command_configure)
    download = sub.add_parser("download", help="download the selected GGUF model")
    download.add_argument("model", nargs="?", help="gguf/<Hugging Face repo>; defaults to CHAT_MODEL")
    download.set_defaults(func=command_download)
    status = sub.add_parser("status", help="show daemon and model status")
    status.set_defaults(func=command_status)
    log = sub.add_parser("log", help="print the daemon log")
    log.set_defaults(func=command_log)
    uninstall = sub.add_parser("uninstall", help="remove the global launcher and virtual environment")
    uninstall.add_argument("--yes", action="store_true", help="remove without prompting")
    uninstall.set_defaults(func=command_uninstall)
    start = sub.add_parser("start", help="start YuriOS as a background daemon")
    start.add_argument("--foreground", action="store_true", help="run attached to this terminal")
    start.set_defaults(func=command_start)
    stop = sub.add_parser("stop", help="stop the YuriOS daemon")
    stop.set_defaults(func=command_stop)
    restart = sub.add_parser("restart", help="restart the YuriOS daemon")
    restart.set_defaults(func=command_restart, foreground=False)
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        args = parser.parse_args(["start"])
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
