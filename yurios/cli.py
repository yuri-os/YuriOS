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
_START_TIMEOUT_SECONDS = 180.0
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


def _characters_url(cfg: Config) -> str:
    return f"http://{cfg.host}:{cfg.port}/api/characters"


def _character_health_url(cfg: Config, character_id: str) -> str:
    return f"http://{cfg.host}:{cfg.port}/api/characters/{character_id}/health"


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
    if getattr(args, "clear_character_models", False) and not model and not provider:
        # The flag on its own is "put everyone back on what `.env` already says",
        # which is worth having without re-choosing the model to say it.
        _clear_character_overrides(root, cfg, cfg.chat_model,
                                   interactive=interactive, always=True)
        return 0
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
    _clear_character_overrides(root, _configured_cfg(root), model,
                               interactive=interactive,
                               always=bool(getattr(args, "clear_character_models", False)))
    if configure_selfies:
        return command_configure_selfies_interactively(args, cfg, root)
    return 0


def _registry_and_profiles(root: Path, cfg: Config):
    """Her house's registry and connection profiles, or (None, None).

    A direct single-companion install has no registry, and a fresh one has no
    registry *yet*; neither is a fault worth a message. A registry that exists
    and cannot be read is the daemon's error to report, in its own words — this
    is a report about models, and it does not get to fail the command.
    """
    from yurios.characters import CharacterRegistry, ConnectionProfiles

    try:
        registry = CharacterRegistry(root / cfg.data_dir)
        if not registry.list():
            return None, None
        return registry, ConnectionProfiles(registry.data_root)
    except (OSError, ValueError):
        return None, None


def _character_connections(root: Path, cfg: Config) -> list:
    """Which model each character will actually connect with (SPEC §31.2)."""
    from yurios.characters import overrides

    registry, profiles = _registry_and_profiles(root, cfg)
    if registry is None:
        return []
    return overrides.describe(cfg, registry.list(), profiles)


def _print_character_connections(rows: list, *, indent: str = "  ") -> None:
    """Print the per-character connection table `start` and `configure` share.

    The point of printing it at all: a character's record overrides the house
    `.env` (§31.2), so "YuriOS is dialling LM Studio even though CHAT_MODEL says
    gguf" is a *character* fact, not an `.env` fact, and nothing said so until
    somebody went reading JSON.
    """
    width = max(len(f"{row.name} [{row.id}]") for row in rows)
    print("Characters and the model each one connects with:")
    for row in rows:
        marker = "*" if row.autostart else " "
        print(f"{indent}{marker} {f'{row.name} [{row.id}]':<{width}}  {row.summary()}")
        if not row.overrides:
            continue
        keys = max(len(item.key) for item in row.overrides)
        print(f"{indent}    her own settings, not the house's:")
        for item in row.overrides:
            print(f"{indent}      {item.key:<{keys}} = {item.value}  ({item.note})")


def _report_character_connections(root: Path, cfg: Config) -> None:
    """Say who connects where before the daemon goes up."""
    rows = _character_connections(root, cfg)
    if not rows:
        return
    _print_character_connections(rows)
    if any(row.differs for row in rows):
        print("  Those are character settings, not .env — "
              "`yurios configure` can clear them.")


def _clear_character_overrides(root: Path, cfg: Config, model: str, *,
                               interactive: bool, always: bool) -> None:
    """Offer to put every character back on the model just configured.

    Choosing a house model is the moment a stale per-character one becomes
    invisible: `.env` says the new thing, her record says the old thing, and her
    record wins. So this asks here, where the two are both on screen, rather than
    leaving it to be discovered from a connection that should not have happened.
    """
    from yurios.characters import overrides

    rows = [row for row in _character_connections(root, cfg) if row.differs]
    if not rows:
        return
    print()
    print(f"Some characters do not use {model}:")
    _print_character_connections(rows)
    if not always:
        if not interactive:
            print("Run `yurios configure --clear-character-models` to put them "
                  "back on the configured model.")
            return
        answer = input(f"Clear these character settings so every character uses "
                       f"{model}? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("Left the character settings alone.")
            return
    registry, _ = _registry_and_profiles(root, cfg)
    if registry is None:
        return
    cleared = overrides.clear(registry, [row.id for row in rows])
    for character_id, keys in cleared.items():
        print(f"Cleared {', '.join(keys)} for {character_id}.")
    if cleared:
        print("Restart YuriOS for the change to take effect.")


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


def _status_line(label: str, value: object, *, indent: str = "  ") -> str:
    return f"{indent}{label:<10} {value}"


def _status_row(label: str, value: object) -> None:
    print(_status_line(label, value))


def _voice_status(voice: object) -> str:
    """Make the health endpoint's voice details useful in a terminal."""
    if not isinstance(voice, dict):
        return str(voice or "unknown")
    if voice.get("state"):
        return str(voice["state"])
    state = "ready" if voice.get("ready") else (
        "loaded" if voice.get("loaded") else "unloaded")
    parts = [f"{voice.get('listeners', 0)} listeners",
             f"{voice.get('loads', 0)} loads"]
    for name in ("stt", "tts", "vad"):
        if voice.get(name):
            parts.append(f"{name.upper()} {voice[name]}")
    return f"{state} ({'; '.join(parts)})"


def _context_status(context: object) -> str:
    """Summarise the last prompt measurement without inventing a window size."""
    if not isinstance(context, dict):
        return "unavailable"
    used = context.get("used")
    if not isinstance(used, int):
        return "unavailable"
    reserve = context.get("reserve")
    reserve_text = f"; {reserve:,} reserved" if isinstance(reserve, int) else ""
    exact = "exact" if context.get("exact") else "estimated"
    limit = context.get("limit")
    if not isinstance(limit, int) or limit <= 0:
        return f"{used:,} tokens ({exact}; window unknown{reserve_text})"
    pct = context.get("pct")
    pct_text = f"; {pct:g}%" if isinstance(pct, (int, float)) else ""
    source = context.get("limit_source")
    source_text = f"; {source}" if source else ""
    return f"{used:,} / {limit:,} tokens ({exact}{pct_text}{source_text}{reserve_text})"


def _character_records(payload: object) -> tuple[object, list[dict]] | None:
    """Return the host registry, or None for a direct single-character server."""
    if not isinstance(payload, dict) or not isinstance(payload.get("characters"), list):
        return None
    return payload.get("primary"), [
        character for character in payload["characters"] if isinstance(character, dict)]


def _runtime_rows(status: dict, *, indent: str, configured_model: object = None) -> list[str]:
    """The per-character portion of `status`, shared by root and host output."""
    rows = []
    active_model = status.get("model") or "unknown"
    rows.append(_status_line("Model", active_model, indent=indent))
    if configured_model and active_model != configured_model:
        rows.append(_status_line("Configured", f"{configured_model} (restart required)",
                                 indent=indent))
    if not status.get("model_configured", True):
        rows.append(_status_line(
            "Setup", "model selection required (`yurios configure` or dashboard)",
            indent=indent))
    rows.append(_status_line("Utility", status.get("utility_model") or "disabled",
                             indent=indent))
    rows.append(_status_line("Context", _context_status(status.get("context")), indent=indent))
    rows.append(_status_line("Voice", _voice_status(status.get("voice")), indent=indent))
    tools = status.get("tools") or "unknown"
    count = status.get("tool_count")
    tools_text = (f"{tools} ({count} discovered)"
                  if isinstance(count, int) and count else tools)
    rows.append(_status_line("Tools", tools_text, indent=indent))
    rows.append(_status_line("Web", status.get("web") or "off", indent=indent))
    rows.append(_status_line("Camera", status.get("selfies") or "off", indent=indent))
    mind = status.get("mind") or "unknown"
    activity = status.get("activity")
    rows.append(_status_line("Mind", f"{mind} ({activity})" if activity else mind,
                             indent=indent))
    rows.append(_status_line("Channels", status.get("channels") or "off", indent=indent))
    viewers = status.get("viewers")
    rows.append(_status_line("Viewers", viewers if isinstance(viewers, int) else "unknown",
                             indent=indent))
    return rows


def command_status(args) -> int:
    root = _root()
    cfg = _configured_cfg(root)
    pid = _read_pid(_pid_path(root))
    try:
        response = httpx.get(_health_url(cfg), timeout=2.0)
        response.raise_for_status()
        status = response.json()
        if not isinstance(status, dict):
            raise ValueError("health endpoint did not return an object")
    except (httpx.HTTPError, ValueError):
        print("YuriOS status")
        _status_row("Daemon", "stopped")
        _status_row("Address", f"http://{cfg.host}:{cfg.port}")
        _status_row("Model", f"configured: {cfg.chat_model}")
        return 1
    try:
        characters_response = httpx.get(_characters_url(cfg), timeout=2.0)
        characters_response.raise_for_status()
        characters = _character_records(characters_response.json())
    except (httpx.HTTPError, ValueError):
        # A direct single-character server does not mount the character registry.
        characters = None
    print("YuriOS status")
    _status_row("Daemon", "running" + (f" (pid {pid})" if pid else ""))
    _status_row("Address", f"http://{cfg.host}:{cfg.port}")
    if characters is None:
        _status_row("Primary", status.get("character") or "unknown")
        for row in _runtime_rows(status, indent="  ", configured_model=cfg.chat_model):
            print(row)
        return 0

    primary, records = characters
    print("  Characters")
    for index, record in enumerate(records):
        character_id = str(record.get("id") or "unknown")
        name = str(record.get("name") or character_id)
        marker = "*" if character_id == primary else " "
        print(f"    {marker} {name} [{character_id}]")
        character_status = status if character_id == primary else None
        if character_status is None and record.get("runtime_state") == "ready":
            try:
                detail_response = httpx.get(_character_health_url(cfg, character_id), timeout=2.0)
                detail_response.raise_for_status()
                detail = detail_response.json()
                character_status = detail if isinstance(detail, dict) else None
            except (httpx.HTTPError, ValueError):
                character_status = None
        if character_status is not None:
            for row in _runtime_rows(character_status, indent="      ",
                                     configured_model=record.get("model")):
                print(row)
        else:
            runtime = str(record.get("runtime_state") or "unknown")
            state = str(record.get("state") or runtime)
            detail = runtime if runtime == state else f"{runtime}; {state}"
            if record.get("error"):
                detail += f"; {record['error']}"
            print(_status_line("Status", detail, indent="      "))
        if index < len(records) - 1:
            print()
    return 0


def command_log(args) -> int:
    log_path = _pid_path(_root()).parent / "yurios.log"
    try:
        sys.stdout.write(log_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"No YuriOS log found at {log_path}.", file=sys.stderr)
        return 1
    return 0


def command_doctor(args) -> int:
    """Report whether the configured optional backends are installed."""
    from yurios.doctor import main

    return main()


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


def _start_search_instance(root: Path) -> None:
    """Bring her SearXNG container up alongside her (SPEC §7.7).

    Her hands are all in-process except this one: `web_search` talks to a
    service, and a service that isn't running is the difference between a
    companion who can look things up and one who errors every time she tries.
    So starting her starts it — but *only* reports when it can't, because a
    search instance is not a reason she doesn't boot. Same rule as the voice
    stack: degrade loudly, keep talking.
    """
    from yurios import searxng

    cfg = _configured_cfg(root)
    if getattr(cfg, "search_backend", "off") != "searxng":
        return
    ok, why = searxng.ensure_running(cfg, root)
    if ok:
        # Only claim the container when it IS ours — an instance somebody runs
        # another way is a fine thing to use and a wrong thing to take credit
        # for, since `yurios stop` won't touch it either.
        if searxng.state() == "running":
            print(f"Her search instance is up ({searxng.CONTAINER}).")
    else:
        print(f"Web search is configured but not available: {why}", file=sys.stderr)


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
    cfg = _configured_cfg(root)
    # Before anything is launched, and in both paths: a character whose record
    # overrides the house model connects somewhere `.env` never named, and the
    # first sign of it used to be a request to a server nobody configured.
    _report_character_connections(root, cfg)
    # …before the daemon either way: the foreground path is the one people use
    # when something is already wrong, and it should not be the path that
    # quietly skips a dependency.
    _start_search_instance(root)
    # A gguf/ route pays its one-time llama.cpp preflight on the first model use
    # after start — minutes of what looks like a hang. Say so up front; the
    # record the preflight leaves behind makes every later start skip it.
    if (cfg.chat_model or "").startswith("gguf/"):
        from yurios.app.providers import gguf

        if gguf.preflight_pending(cfg.chat_model, cfg):
            print("Note: her first reply after start runs a one-time llama.cpp "
                  "preflight — the model loads in a sacrificial process, which can "
                  "take minutes. It is recorded and never runs again.")
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
    configure.add_argument("--clear-character-models", action="store_true",
                           help="clear every character's own model settings so they "
                                "use the configured one")
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
    doctor = sub.add_parser("doctor", help="check configured backends and dependencies")
    doctor.set_defaults(func=command_doctor)
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
