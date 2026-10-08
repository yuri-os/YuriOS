"""The house itself, from House settings (SPEC §11.3).

Three things the `.env` table cannot be, because they are not lines in it:

- **Restart.** Everything in `.env` is read at boot, so a save is a promise
  about the next start — and the page that made it used to send you to a
  terminal to keep it. `POST /api/house/restart` ends this server with
  `daemon.RESTART_EXIT`, which the supervisor reads as "start me again" rather
  than as a crash. A server nobody supervises (`--foreground`, a bare
  `python -m yurios.world`, the desktop window) would simply stop, so it refuses
  and says what to do instead.
- **Her other people's hands.** `MCP_SERVERS` names a JSON file; the knob was
  editable and the file was not. The editor reads and writes that file through
  `tools.client.check_servers` — the same rules the loader applies at boot — and
  can spawn one server to list what it offers before you commit to it.
- **The house scene overlay.** `SELFIE_TEMPLATES_EXTRA` is the same story for
  her camera: a YAML of scenes merged over every character's library. Written
  verbatim after it parses, so the comments you put in it survive.

Owner-or-loopback, like the `.env` panel these sit beside: each one writes a
file the server will execute or read at boot, or ends the server outright.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path

from dotenv import dotenv_values
from fastapi import FastAPI, HTTPException, Request

from yurios import daemon, envfile
from yurios.characters import selfiebook
from yurios.security import owner_or_loopback

from .hosting import CharacterHost

log = logging.getLogger("world.host")

#: How long after answering the restart request the server starts to shut
#: down — long enough for that answer to reach the browser that asked.
RESTART_DELAY_S = 0.4
#: A server that takes longer than this to list its tools is reported as stuck;
#: `uvx`/`npx` fetching a package on first run is the usual slow case.
MCP_TEST_TIMEOUT_S = 90.0
DEFAULT_MCP_FILE = "mcp-servers.json"
DEFAULT_OVERLAY_FILE = "data/selfie-extra.yaml"


def _require_local(request: Request) -> None:
    if not owner_or_loopback(request):
        raise HTTPException(status_code=403, detail="owner authentication required")


def _env_path() -> Path:
    # read through the settings route so a test pointing it at a scratch file
    # moves every house file with it
    from yurios.desktop.routes import settings as env_panel
    return env_panel.ENV_PATH


def _configured(key: str, running: str) -> str:
    """The path `.env` names for `key` now — what the next boot will read —
    falling back to what this process booted with."""
    try:
        stored = dotenv_values(_env_path()).get(key)
    except OSError:
        stored = None
    return str(stored if stored is not None else running or "").strip()


def _resolve(raw: str) -> Path:
    path = Path(raw).expanduser()
    return path if path.is_absolute() else _env_path().parent / path


def _point_env_at(key: str, raw: str, base) -> None:
    """Name a file in `.env` the first time the editor creates one."""
    try:
        envfile.apply(base, {key: raw}, path=_env_path())
    except ValueError as exc:                     # pragma: no cover - a path always formats
        raise HTTPException(422, str(exc)) from exc


def register(app: FastAPI, host: CharacterHost, require) -> None:  # noqa: ARG001
    base = host.base
    # A new one every process: the page polls for a *different* answer to know
    # the restart it asked for actually happened, not just that the port is open.
    boot_id = uuid.uuid4().hex

    def supervised() -> bool:
        return os.environ.get(daemon.SUPERVISED_ENV) == "1"

    @app.get("/api/house")
    async def house(request: Request):
        _require_local(request)
        restartable = supervised() and bool(getattr(app.state, "restartable", False))
        return {
            "boot_id": boot_id,
            "restartable": restartable,
            "why_not": "" if restartable else (
                "YuriOS is running in the foreground or in its desktop window — "
                "stop it and start it again yourself (yurios restart, if it was "
                "started with yurios start)"),
        }

    @app.post("/api/house/restart")
    async def restart(request: Request):
        _require_local(request)
        if not (supervised() and getattr(app.state, "restartable", False)):
            raise HTTPException(409, "nothing would start YuriOS again: it is not "
                                     "running under `yurios start`. Restart it yourself.")
        server = getattr(app.state, "server", None)
        if server is None:
            raise HTTPException(503, "no server to restart")
        app.state.restart_requested = True
        log.info("restart requested from House settings")

        def stop() -> None:
            server.should_exit = True

        asyncio.get_running_loop().call_later(RESTART_DELAY_S, stop)
        return {"restarting": True, "boot_id": boot_id}

    # --- MCP servers ------------------------------------------------------------

    def mcp_paths() -> tuple[str, Path]:
        raw = _configured("MCP_SERVERS", getattr(base, "mcp_servers", ""))
        return raw, _resolve(raw or DEFAULT_MCP_FILE)

    @app.get("/api/house/mcp-servers")
    async def mcp_servers(request: Request):
        from yurios.world.tools.client import check_servers

        _require_local(request)
        raw, path = mcp_paths()
        body = {"configured": bool(raw), "path": str(path), "exists": path.is_file(),
                "servers": {}, "error": ""}
        if path.is_file():
            try:
                body["servers"] = check_servers(json.loads(path.read_text(encoding="utf-8")))
            except (ValueError, OSError) as exc:
                # shown, not swallowed: the loader refuses this file at boot too
                body["error"] = str(exc)
                body["text"] = path.read_text(encoding="utf-8", errors="replace")
        return body

    @app.put("/api/house/mcp-servers")
    async def save_mcp_servers(request: Request):
        from yurios.world.tools.client import check_servers

        _require_local(request)
        payload = await request.json()
        try:
            servers = check_servers((payload or {}).get("servers"))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        raw, path = mcp_paths()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"mcpServers": servers}, indent=2) + "\n",
                        encoding="utf-8")
        if not raw:
            _point_env_at("MCP_SERVERS", f"./{DEFAULT_MCP_FILE}", base)
        return {"servers": servers, "path": str(path), "restart_required": True}

    @app.post("/api/house/mcp-servers/test")
    async def test_mcp_server(request: Request):
        """Spawn one server, ask it for its tools, and close it again.

        Not a dry run of her whole toolset — exactly the one entry in the form,
        so "does this command even start, and what does it give her" is answered
        before it is saved and a restart spent on it."""
        from yurios.world.tools.client import McpToolRunner, check_servers, start_failure

        _require_local(request)
        payload = await request.json()
        name = str((payload or {}).get("name") or "test")
        try:
            entry = check_servers({name: (payload or {}).get("server")})[name]
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        runner = McpToolRunner(command=[entry["command"], *entry["args"]], env=entry["env"])
        try:
            specs = await asyncio.wait_for(runner.start(), MCP_TEST_TIMEOUT_S)
        except asyncio.TimeoutError:
            await runner.close()
            return {"ok": False, "error": f"no answer within {MCP_TEST_TIMEOUT_S:.0f}s"}
        except Exception as exc:                     # noqa: BLE001 - reported, not raised
            return {"ok": False, "error": start_failure(exc) or str(exc)}
        await runner.close()
        return {"ok": True, "tools": [{"name": s.name, "description": s.description}
                                      for s in specs]}

    # --- the house scene overlay ---------------------------------------------------

    def overlay_paths() -> tuple[str, Path]:
        raw = _configured("SELFIE_TEMPLATES_EXTRA",
                          getattr(base, "selfie_templates_extra", ""))
        return raw, _resolve(raw or DEFAULT_OVERLAY_FILE)

    @app.get("/api/house/selfie-overlay")
    async def selfie_overlay(request: Request):
        _require_local(request)
        raw, path = overlay_paths()
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        return {"configured": bool(raw), "path": str(path), "exists": path.is_file(),
                "text": text,
                "slots": [{"key": n, "label": label, "hint": hint}
                          for n, label, hint in selfiebook.SLOTS]}

    @app.put("/api/house/selfie-overlay")
    async def save_selfie_overlay(request: Request):
        _require_local(request)
        payload = await request.json()
        text = str((payload or {}).get("text") or "")
        try:
            selfiebook.parse_text(text)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        raw, path = overlay_paths()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text if text.endswith("\n") or not text else text + "\n",
                        encoding="utf-8")
        if not raw:
            _point_env_at("SELFIE_TEMPLATES_EXTRA", f"./{DEFAULT_OVERLAY_FILE}", base)
        return {"path": str(path), "restart_required": True}
