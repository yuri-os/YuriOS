"""Multi-character host, registry API, and dynamic runtime dispatch."""

from __future__ import annotations

import asyncio
import base64
import datetime
import io
import json
import logging
import os
import re
import shutil
import struct
import zlib
from collections.abc import Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, NamedTuple

import yaml
from dotenv import dotenv_values
from PIL import Image

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.staticfiles import StaticFiles

from yurios.characters import (
    CharacterImporter, CharacterRecord, CharacterRegistry,
    ConnectionProfile, ConnectionProfiles,
)

from .config import Config
from .main import DIST_DIR, WEB_DIR, create_app

log = logging.getLogger("world.host")


def _card_values(record: CharacterRecord) -> tuple[dict[str, Any], dict[str, Any]]:
    if record.paths.card_json.is_file():
        wrapper = json.loads(record.paths.card_json.read_text(encoding="utf-8"))
    else:
        wrapper = {"spec": "chara_card_v3", "spec_version": "3.0", "data": {}}
    data = wrapper.get("data") if isinstance(wrapper.get("data"), dict) else wrapper
    return wrapper, data


def _replace_section(path: Path, heading: str, value: str) -> None:
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    marker = f"## {heading}"
    match = re.search(rf"(?m)^## {re.escape(heading)}\s*$", text)
    block = f"{marker}\n\n{value.strip()}\n"
    if match is None:
        text = text.rstrip() + "\n\n" + block
    else:
        following = re.search(r"(?m)^##\s+", text[match.end():])
        end = match.end() + following.start() if following else len(text)
        text = text[:match.start()] + block + text[end:]
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


def _set_frontmatter(path: Path, key: str, value: str) -> None:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        return
    end = text.index("\n---\n", 4)
    front = yaml.safe_load(text[4:end]) or {}
    front[key] = value
    rendered = yaml.safe_dump(front, sort_keys=False, allow_unicode=True).strip()
    path.write_text(f"---\n{rendered}\n---\n{text[end + 5:].lstrip()}", encoding="utf-8")


def _update_soul(record: CharacterRecord, fields: dict[str, Any]) -> None:
    soul = record.paths.vault / "soul"
    if not soul.is_dir():
        return
    section_map = {
        "description": (soul / "CONSTITUTION.md", "Identity"),
        "scenario": (soul / "SCENARIO.md", "Scenario"),
        "system_prompt": (soul / "CONSTITUTION.md", "Voice law"),
        "post_history_instructions": (soul / "CONSTITUTION.md", "Hard limits"),
    }
    for key, (path, heading) in section_map.items():
        if key in fields:
            _replace_section(path, heading, str(fields[key]))
    if "first_mes" in fields:
        bootstrap = soul / "BOOTSTRAP.md"
        _replace_section(bootstrap if bootstrap.exists() else soul / "SCENARIO.md",
                         "Cold open" if bootstrap.exists() else "First message",
                         str(fields["first_mes"]))
    if "personality" in fields:
        _set_frontmatter(soul / "PERSONA.md", "personality", str(fields["personality"]))
    if "creator_notes" in fields:
        (soul / "NOTES.md").write_text(str(fields["creator_notes"]).rstrip() + "\n",
                                       encoding="utf-8")
    if "name" in fields:
        manifest = soul / "soul.yaml"
        text = manifest.read_text(encoding="utf-8")
        replacement = "name: " + json.dumps(str(fields["name"]), ensure_ascii=False)
        text, count = re.subn(r"(?m)^name\s*:.*$", replacement, text, count=1)
        if count:
            manifest.write_text(text, encoding="utf-8")


def _text_chunk(keyword: str, card: dict[str, Any]) -> bytes:
    encoded = base64.b64encode(json.dumps(card, ensure_ascii=False).encode("utf-8"))
    body = keyword.encode("latin-1") + b"\0" + encoded
    return (struct.pack(">I", len(body)) + b"tEXt" + body
            + struct.pack(">I", zlib.crc32(b"tEXt" + body) & 0xFFFFFFFF))


def _export_card(record: CharacterRecord) -> bytes:
    wrapper, data = _card_values(record)
    data.setdefault("name", record.display.name)
    data.setdefault("description", record.display.description)
    v3 = {**wrapper, "spec": "chara_card_v3", "spec_version": "3.0", "data": data}
    v2 = {"spec": "chara_card_v2", "spec_version": "2.0", "data": data}
    if record.paths.portrait.is_file():
        png = record.paths.portrait.read_bytes()
    else:
        output = io.BytesIO()
        Image.new("RGB", (512, 768), "#11111a").save(output, format="PNG")
        png = output.getvalue()
    ihdr_end = 33
    return png[:ihdr_end] + _text_chunk("chara", v2) + _text_chunk("ccv3", v3) + png[ihdr_end:]


def _tail_jsonl(path: Path, limit: int = 100) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


class TelegramCredentials(NamedTuple):
    """What one character answers on, and the two variables it is written in —
    the names travel with the values so pairing mode and the settings panel both
    name the pair that actually feeds this runtime."""
    token: str
    chat_id: str
    token_env: str
    chat_id_env: str


def telegram_env_suffix(character_id: str) -> str:
    """The env-var tail that names one character's own bot: `mia` → `MIA`,
    `yuri-2` → `YURI_2`."""
    return re.sub(r"[^A-Z0-9]+", "_", character_id.upper()).strip("_")


def _env_values(base: Config, environ: Mapping[str, str] | None) -> Mapping[str, str]:
    """Every variable in play, `.env` under the real environment.

    The per-character names are not Config fields — they can't be, the ids are
    only known at runtime — so pydantic-settings never reads them, and it does
    not export the `.env` it parsed into `os.environ` either. Left to
    `os.environ` alone, a `TELEGRAM_BOT_TOKEN_MIA` line in `.env` would be
    silently ignored: read the file ourselves, with the real environment winning
    the same way pydantic-settings resolves it.
    """
    if environ is not None:
        return environ
    env_file = base.model_config.get("env_file") or ".env"
    values = {k: v for k, v in dotenv_values(env_file).items() if v is not None}
    values.update(os.environ)
    return values


def telegram_for_character(base: Config, character_id: str,
                           environ: Mapping[str, str] | None = None,
                           ) -> TelegramCredentials:
    """Her Telegram credentials, and where they are configured.

    An outside account belongs to exactly one character (SPEC §10.5) — Telegram
    answers all but the last long-poller with "Conflict: terminated by other
    getUpdates request" — so sharing one bot between characters leaves her
    reachable nowhere. Every character therefore has her own pair,
    `TELEGRAM_BOT_TOKEN_<ID>` / `TELEGRAM_CHAT_ID_<ID>`, and nothing is shared.

    The unsuffixed pair is the single-companion install's, kept working:
    `TELEGRAM_CHARACTER` names who keeps it once there are others, and with that
    unset it is offered to everyone and the first runtime to start holds it
    (channels/manager.py's claim). A character the shared bot isn't offered to
    is simply not on Telegram — one medium she doesn't have, not a fault — and
    the names come back pointing at her own pair, so the settings panel offers
    her a bot of her own rather than an edit to somebody else's.
    """
    env = _env_values(base, environ)
    suffix = telegram_env_suffix(character_id)
    token_env = f"TELEGRAM_BOT_TOKEN_{suffix}" if suffix else "TELEGRAM_BOT_TOKEN"
    chat_env = f"TELEGRAM_CHAT_ID_{suffix}" if suffix else "TELEGRAM_CHAT_ID"
    token = env.get(token_env, "").strip()
    if token:
        return TelegramCredentials(token, env.get(chat_env, "").strip(),
                                   token_env, chat_env)
    owner = base.telegram_character.strip().casefold()
    if owner and owner != character_id.casefold():
        return TelegramCredentials("", "", token_env, chat_env)
    return TelegramCredentials(base.telegram_bot_token, base.telegram_chat_id,
                               "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")


def config_for_character(base: Config, record: CharacterRecord,
                         profile: ConnectionProfile | None = None,
                         *, environ: Mapping[str, str] | None = None) -> Config:
    update: dict[str, Any] = {
        "companion_name": record.display.name,
        "vault_dir": record.paths.vault,
        "corpus_dir": record.paths.corpus,
        "trace_dir": record.paths.traces,
        "tool_log_dir": record.paths.tool_logs,
        "selfie_dir": record.paths.selfies,
        "mind_enabled": record.loops.mind,
        "utility_enabled": record.loops.utility,
        "dream_enabled": record.loops.dream,
    }
    (update["telegram_bot_token"], update["telegram_chat_id"],
     update["telegram_bot_token_env"], update["telegram_chat_id_env"]) = (
        telegram_for_character(base, record.id, environ))
    if record.models.chat:
        update["chat_model"] = record.models.chat
    if record.models.utility:
        update["utility_model"] = record.models.utility
    if record.voice.tts_backend:
        update["tts_backend"] = record.voice.tts_backend
    if record.voice.stt_backend:
        update["stt_backend"] = record.voice.stt_backend
    if record.voice.voice_id:
        update["tts_register"] = record.voice.voice_id
    if record.body.backend in {"vrm", "live2d"}:
        update["desktop_body"] = record.body.backend
    if record.body.model:
        update["avatar_model"] = record.body.model
    endpoint = profile.endpoint if profile and profile.endpoint else record.connection.endpoint
    if endpoint:
        model = record.models.chat
        update["lmstudio_base_url" if model.startswith("lm_studio/") else "ollama_base_url"] = endpoint
    for key, value in record.models.options.items():
        if key in Config.model_fields:
            update[key] = value
    return base.model_copy(update=update)


class CharacterHost:
    def __init__(self, base: Config, registry: CharacterRegistry):
        self.base = base
        self.registry = registry
        claimed: list[tuple[str, Path]] = []
        for record in registry:
            for path in (record.paths.vault, record.paths.corpus, record.paths.traces,
                         record.paths.tool_logs, record.paths.selfies):
                resolved = path.resolve()
                for owner, existing in claimed:
                    if (resolved == existing or resolved in existing.parents
                            or existing in resolved.parents):
                        raise ValueError(
                            f"character storage overlaps: {owner} and {record.id}: {resolved}")
                claimed.append((record.id, resolved))
        self.connections = ConnectionProfiles(registry.data_root)
        if not self.connections.list():
            model = base.chat_model
            endpoint = (base.lmstudio_base_url if model.startswith("lm_studio/")
                        else base.ollama_base_url if model.startswith("ollama/") else "")
            self.connections.upsert(ConnectionProfile(
                name="legacy-default", endpoint=endpoint,
                api_key_env="" if endpoint else "OPENROUTER_API_KEY"))
            self.connections.upsert(ConnectionProfile(
                name="default", endpoint=endpoint,
                api_key_env="" if endpoint else "OPENROUTER_API_KEY"))
        self.apps: dict[str, FastAPI] = {}
        self.states: dict[str, str] = {r.id: "offline" for r in registry}
        self.errors: dict[str, str] = {}
        self.primary_id: str | None = next((
            r.id for r in registry
            if r.lifecycle.enabled and r.lifecycle.autostart
            and not r.lifecycle.review_required
        ), None)
        self._lock = asyncio.Lock()

    def runtime(self, character_id: str):
        app = self.apps.get(character_id)
        return app.state.rt if app is not None else None

    async def start(self, character_id: str) -> None:
        async with self._lock:
            if character_id in self.apps:
                return
            record = self.registry.require(character_id)
            if record.lifecycle.review_required or not record.lifecycle.enabled:
                raise RuntimeError("character is disabled or still requires review")
            self.states[character_id] = "starting"
            try:
                app = create_app(
                    config_for_character(
                        self.base, record,
                        self.connections.get(record.connection.profile)),
                    manage_lifespan=False, mount_frontend=False)
                await app.state.rt.start_async()
                self.apps[character_id] = app
                self.states[character_id] = "ready"
                self.errors.pop(character_id, None)
                if self.primary_id not in self.apps:
                    self.primary_id = character_id
            except Exception as exc:
                self.states[character_id] = "failed"
                self.errors[character_id] = str(exc)
                log.exception("character %s failed to start", character_id)
                raise

    async def stop(self, character_id: str) -> None:
        async with self._lock:
            app = self.apps.pop(character_id, None)
            if app is not None:
                await app.state.rt.stop_async()
            self.states[character_id] = "offline"
            if self.primary_id == character_id:
                self.primary_id = next(iter(self.apps), None)

    async def restart(self, character_id: str) -> None:
        await self.stop(character_id)
        await self.start(character_id)

    async def start_all(self) -> None:
        for record in self.registry:
            if record.lifecycle.enabled and record.lifecycle.autostart and not record.lifecycle.review_required:
                try:
                    await self.start(record.id)
                except Exception:
                    continue

    async def stop_all(self) -> None:
        for character_id in list(self.apps)[::-1]:
            try:
                await self.stop(character_id)
            except Exception:
                log.exception("character %s failed to stop", character_id)

    def summary(self, record: CharacterRecord) -> dict[str, Any]:
        rt = self.runtime(record.id)
        activity = rt.mind.activity.state.lower() if rt and rt.mind else None
        state = "attention" if record.lifecycle.review_required else (
            activity or self.states.get(record.id, "offline"))
        return {
            "id": record.id,
            "name": record.display.name,
            "description": record.display.description,
            "creator": record.display.creator,
            "tags": record.display.tags,
            "state": state,
            "runtime_state": self.states.get(record.id, "offline"),
            "error": self.errors.get(record.id),
            "enabled": record.lifecycle.enabled,
            "review_required": record.lifecycle.review_required,
            "loop_enabled": record.loops.mind and self.states.get(record.id) == "ready",
            "loops": {"mind": record.loops.mind, "utility": record.loops.utility,
                      "dream": record.loops.dream},
            "model": record.models.chat or self.base.chat_model,
            "voice": record.voice.voice_id or record.voice.tts_backend or self.base.tts_backend,
            "connection_profile": record.connection.profile,
            "body_backend": record.body.backend,
            "body_model": record.body.model,
            "portrait_url": f"/api/characters/{record.id}/portrait",
            "context": rt.context.snapshot() if rt else None,
            "activity": activity,
        }


class _RuntimeDispatcher:
    def __init__(self, host: CharacterHost, kind: str):
        self.host = host
        self.kind = kind

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "/")
        mounted_prefix = "/api/characters/" if self.kind == "api" else "/ws/characters/"
        if path.startswith(mounted_prefix):
            path = path[len(mounted_prefix):]
        else:
            path = path.lstrip("/")
        character_id, separator, remainder = path.partition("/")
        child = self.host.apps.get(character_id)
        if not separator or child is None:
            response = JSONResponse({"detail": "character runtime is not running"}, status_code=404)
            await response(scope, receive, send)
            return
        target = ("/api/" if self.kind == "api" else "/ws/") + remainder
        child_scope = dict(scope)
        child_scope["path"] = target
        child_scope["raw_path"] = target.encode("utf-8")
        child_scope["root_path"] = ""
        await child(child_scope, receive, send)


def create_host_app(base: Config, registry: CharacterRegistry | None = None) -> FastAPI:
    if registry is None:
        registry = CharacterRegistry(base.data_dir)
    host = CharacterHost(base, registry)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await host.start_all()
        yield
        await host.stop_all()

    app = FastAPI(title="YuriOS Host", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.host = host

    def require(character_id: str) -> CharacterRecord:
        record = registry.get(character_id)
        if record is None:
            raise HTTPException(404, "no such character")
        return record

    @app.get("/api/characters")
    async def characters():
        return {"version": "0.2.0", "primary": host.primary_id,
                "characters": [host.summary(record) for record in registry]}

    @app.get("/api/connections")
    async def connections():
        return {"profiles": [
            {"name": item.name, "backend": item.backend, "endpoint": item.endpoint,
             "api_key_env": item.api_key_env, "secret_configured":
             bool(item.api_key_env and os.environ.get(item.api_key_env))}
            for item in host.connections.list()
        ]}

    @app.post("/api/characters/import")
    async def import_card(file: UploadFile):
        payload = await file.read(32 * 1024 * 1024 + 1)
        try:
            record = CharacterImporter(registry).import_card(payload, autostart=True)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if record.lifecycle.enabled and not record.lifecycle.review_required:
            await host.start(record.id)
        return {"character": host.summary(record)}

    @app.get("/api/characters/{character_id}/portrait")
    async def portrait(character_id: str):
        path = require(character_id).paths.portrait
        if not path.is_file():
            raise HTTPException(404, "portrait unavailable")
        # One stable URL whose bytes genuinely change — a fresh install on the port
        # a previous one used, a forge re-render, a portrait the user replaced. With
        # only the ETag and Last-Modified FileResponse sends, a browser is free to
        # invent its own freshness window and show yesterday's face for hours;
        # no-cache makes it ask, and the ETag still answers 304 when nothing moved.
        return FileResponse(path, media_type="image/png",
                            headers={"Cache-Control": "no-cache"})

    @app.get("/api/characters/{character_id}/selfies/{name}")
    async def selfie(character_id: str, name: str):
        base = require(character_id).paths.selfies.resolve()
        path = (base / name).resolve()
        if path.parent != base or not path.is_file():
            raise HTTPException(404, "no such selfie")
        return FileResponse(path, media_type="image/png")

    @app.get("/api/characters/{character_id}/export")
    async def export_card(character_id: str):
        record = require(character_id)
        filename = re.sub(r"[^a-z0-9_-]+", "-", record.display.name.lower()).strip("-")
        return Response(_export_card(record), media_type="image/png", headers={
            "Content-Disposition": f'attachment; filename="{filename or record.id}.png"'})

    @app.get("/api/characters/{character_id}/profile")
    async def get_settings(character_id: str):
        record = require(character_id)
        _, card = _card_values(record)
        return {"settings": {
            "name": record.display.name, "description": record.display.description,
            "model": record.models.chat, "utility_model": record.models.utility,
            "voice": record.voice.voice_id, "connection": record.connection.backend,
            "connection_profile": record.connection.profile,
            "body_backend": record.body.backend, "body_model": record.body.model,
            "enabled": record.lifecycle.enabled, "review_required": record.lifecycle.review_required,
            "mind": record.loops.mind, "utility": record.loops.utility,
            "dream": record.loops.dream,
            "personality": card.get("personality", ""),
            "scenario": card.get("scenario", ""),
            "first_mes": card.get("first_mes", ""),
        }}

    @app.patch("/api/characters/{character_id}/profile")
    async def save_settings(character_id: str, request: Request):
        record = require(character_id)
        body = await request.json()
        if "name" in body and str(body["name"]).strip():
            record.display.name = str(body["name"]).strip()
        if "description" in body:
            record.display.description = str(body["description"])
        if "model" in body:
            record.models.chat = str(body["model"])
        if "utility_model" in body:
            record.models.utility = str(body["utility_model"])
        if "voice" in body:
            record.voice.voice_id = str(body["voice"])
        if "connection_profile" in body:
            profile = str(body["connection_profile"])
            if host.connections.get(profile) is None:
                raise HTTPException(400, "unknown connection profile")
            record.connection.profile = profile
        if "body_backend" in body:
            backend = str(body["body_backend"])
            if backend not in {"", "vrm", "live2d"}:
                raise HTTPException(400, "body backend must be vrm or live2d")
            record.body.backend = backend
        if "body_model" in body:
            record.body.model = str(body["body_model"])
        for key in ("mind", "utility", "dream"):
            if key in body:
                setattr(record.loops, key, bool(body[key]))
        card_fields = {key: body[key] for key in (
            "name", "description", "personality", "scenario", "first_mes",
            "system_prompt", "post_history_instructions", "creator_notes")
            if key in body}
        if card_fields:
            wrapper, card = _card_values(record)
            card.update({key: str(value) for key, value in card_fields.items()})
            record.paths.card_json.parent.mkdir(parents=True, exist_ok=True)
            record.paths.card_json.write_text(
                json.dumps(wrapper, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8")
            _update_soul(record, card_fields)
            try:
                from yurios.app import vaultgit
                vaultgit.commit(record.paths.vault, "user: edit character card")
            except Exception:
                log.exception("could not commit character-card edit")
        was_running = host.runtime(character_id) is not None
        if record.lifecycle.review_required:
            record.lifecycle.review_required = False
            record.lifecycle.enabled = True
            record.lifecycle.autostart = True
        registry.upsert(record)
        if was_running:
            await host.restart(character_id)
        elif record.lifecycle.enabled:
            await host.start(character_id)
        return {"character": host.summary(record)}

    @app.patch("/api/characters/{character_id}/loop")
    async def set_loop(character_id: str, request: Request):
        record = require(character_id)
        enabled = bool((await request.json()).get("enabled"))
        record.loops.mind = enabled
        registry.upsert(record)
        rt = host.runtime(character_id)
        if rt is None and record.lifecycle.enabled:
            await host.start(character_id)
            rt = host.runtime(character_id)
        if rt is not None:
            await rt.set_mind_enabled(enabled)
        return {"character": host.summary(record)}

    @app.patch("/api/characters/{character_id}/controls")
    async def controls(character_id: str, request: Request):
        record = require(character_id)
        body = await request.json()
        restart = False
        for key in ("mind", "utility", "dream"):
            if key in body:
                value = bool(body[key])
                restart = restart or (key != "mind" and value != getattr(record.loops, key))
                setattr(record.loops, key, value)
        registry.upsert(record)
        if restart and host.runtime(character_id):
            await host.restart(character_id)
        elif "mind" in body and host.runtime(character_id):
            await host.runtime(character_id).set_mind_enabled(record.loops.mind)
        return {"character": host.summary(record)}

    @app.get("/api/characters/{character_id}/journal")
    async def journal(character_id: str, days: int = 7):
        rt = host.runtime(character_id)
        if rt is None or rt.mind is None:
            return {"days": []}
        out = []
        now = rt.clock.now()
        for index in range(max(1, min(days, 30))):
            day = datetime.datetime.fromtimestamp(now - index * 86400).strftime("%Y-%m-%d")
            entries = rt.mind.journal.day_entries(day)
            if entries:
                out.append({"day": day, "entries": entries})
        return {"days": out}

    @app.get("/api/characters/{character_id}/log")
    async def logs(character_id: str):
        record = require(character_id)
        rows = _tail_jsonl(record.paths.traces / "ticks.jsonl", 60)
        rows += _tail_jsonl(record.paths.tool_logs / "calls.jsonl", 40)
        return {"entries": rows[-100:]}

    @app.get("/api/characters/{character_id}/context-history")
    async def context(character_id: str):
        record = require(character_id)
        rt = host.runtime(character_id)
        return {"context": rt.context.snapshot() if rt else {"used": 0, "limit": None},
                "history": _tail_jsonl(record.paths.traces / "context.jsonl", 500)}

    @app.post("/api/characters/{character_id}/archive")
    async def archive(character_id: str):
        record = require(character_id)
        await host.stop(character_id)
        archive_root = registry.data_root / "archives"
        archive_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        target = archive_root / f"{record.id}-{stamp}"
        if target.exists():
            raise HTTPException(409, "archive target already exists")
        os.replace(record.paths.root, target)
        registry.remove(character_id)
        host.states.pop(character_id, None)
        if host.primary_id == character_id:
            host.primary_id = next((r.id for r in registry if r.lifecycle.enabled), None)
        return {"archived": True, "id": character_id}

    @app.delete("/api/characters/{character_id}/purge")
    async def purge(character_id: str, confirm: str):
        record = require(character_id)
        if confirm not in {record.id, record.display.name}:
            raise HTTPException(400, "confirmation does not match character")
        await host.stop(character_id)
        registry.remove(character_id)
        shutil.rmtree(record.paths.root)
        return {"purged": True, "id": character_id}

    @app.get("/")
    async def dashboard(request: Request):
        if "desktop" in request.query_params and host.primary_id:
            query = request.url.query
            return RedirectResponse(
                f"/characters/{host.primary_id}/sanctuary/" + (f"?{query}" if query else ""))
        path = DIST_DIR / "dashboard" / "index.html"
        if not path.is_file():
            return JSONResponse({"detail": "frontend not built; run npm run build in web"}, 503)
        return FileResponse(path, media_type="text/html")

    @app.get("/characters/{character_id}/sanctuary")
    @app.get("/characters/{character_id}/sanctuary/")
    async def sanctuary(character_id: str):
        require(character_id)
        return FileResponse(DIST_DIR / "index.html", media_type="text/html")

    @app.get("/characters/{character_id}/live2d")
    async def character_live2d(character_id: str):
        require(character_id)
        return RedirectResponse(f"/live2d/?character={character_id}")

    # Both Vite entry points emit their hashed bundles into this shared root.
    # Keep it ahead of the primary-runtime fallback so /assets/* is not
    # dispatched to a character app that was created without its frontend.
    app.mount("/assets", StaticFiles(directory=DIST_DIR / "assets", check_dir=False),
              name="frontend-assets")
    app.mount("/dashboard", StaticFiles(directory=DIST_DIR / "dashboard", html=True,
                                         check_dir=False), name="dashboard")
    app.mount("/api/characters", _RuntimeDispatcher(host, "api"), name="character-api")
    app.mount("/ws/characters", _RuntimeDispatcher(host, "ws"), name="character-ws")

    # Root compatibility APIs and shared assets continue to address the primary.
    class Primary:
        async def __call__(self, scope, receive, send):
            child = host.apps.get(host.primary_id or "")
            if child is None:
                response = JSONResponse({"detail": "no active character"}, status_code=503)
                await response(scope, receive, send)
                return
            child.state.server = getattr(app.state, "server", None)
            await child(scope, receive, send)

    app.mount("/", Primary(), name="primary-character")
    return app
