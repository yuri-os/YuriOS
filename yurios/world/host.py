"""Multi-character host, registry API, and dynamic runtime dispatch."""

from __future__ import annotations

import asyncio
import copy
import datetime
import json
import logging
import os
import re
import secrets
import shutil
import time
from collections.abc import Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, NamedTuple

import yaml
from dotenv import dotenv_values

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import (FileResponse, JSONResponse, RedirectResponse,
                               Response, StreamingResponse)
from starlette.staticfiles import StaticFiles

from yurios.characters import (
    CardLimits, CharacterImporter, CharacterRecord, CharacterRegistry,
    ConnectionProfile, ConnectionProfiles,
)
from yurios.characters import overrides as model_overrides
from yurios.characters import selfiebook
from yurios.characters import studio as studio_model
from yurios.characters.appearance import ensure_appearance
from yurios.characters import setting as setting_model
from yurios.characters.setting import ensure_setting
from yurios.characters.creator import create_character, template_draft
from yurios.characters.exporter import ExportOptions, build_export, preview_export
from yurios.characters.optimize import CardOptimizeError, optimize_draft
from yurios.characters.privacy import CardExportError
from yurios.app.providers.catalog import provider_models
from yurios.mind.journal import canonical_day, is_canonical_day, parse_day_entries
from yurios.mind.util import jsonl_page
from yurios.desktop.voice.ws_limits import VoiceConnectionLimiter

from yurios.app import vaultgit

from . import debug, rewire
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


# The soul writers live in `characters/studio.py` — the settings modal below and
# the studio page must move a section the same way, and two copies of a markdown
# section-replacer that disagree would show up as a persona that silently loses a
# heading depending on which surface last saved it.
_replace_section = studio_model._replace_section
_set_frontmatter = studio_model._set_frontmatter


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


def _images(record: CharacterRecord) -> dict[str, Any]:
    """Every picture that could become the card's face."""
    selfies: list[dict[str, Any]] = []
    if record.paths.selfies.is_dir():
        for path in sorted(record.paths.selfies.glob("*.png"),
                           key=lambda p: p.stat().st_mtime, reverse=True)[:60]:
            stat = path.stat()
            selfies.append({
                "name": path.name,
                "url": f"/api/characters/{record.id}/selfies/{path.name}",
                "bytes": stat.st_size,
                "taken_at": datetime.datetime.fromtimestamp(
                    stat.st_mtime, datetime.timezone.utc).isoformat(),
            })
    return {"portrait": record.paths.portrait.is_file(), "selfies": selfies}


def _sync_card_json(record: CharacterRecord, draft: "studio_model.Draft") -> None:
    """Keep `card.json` in step with a studio save.

    It is not the export's source — the exporter reads the SOUL (§7.3) — but the
    settings modal and the dashboard tiles still read it, so a studio edit that
    left it stale would show the old description on the card grid.
    """
    wrapper, data = _card_values(record)
    data.update({
        "name": draft.name, "description": draft.description,
        "personality": draft.personality, "scenario": draft.scenario,
        "first_mes": draft.first_mes, "system_prompt": draft.system_prompt,
        "post_history_instructions": draft.post_history_instructions,
        "creator_notes": draft.creator_notes, "creator": draft.creator,
        "character_version": draft.character_version, "tags": list(draft.tags),
    })
    wrapper["data"] = data
    record.paths.card_json.parent.mkdir(parents=True, exist_ok=True)
    record.paths.card_json.write_text(
        json.dumps(wrapper, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")


def _tail_jsonl(path: Path, limit: int = 100) -> list[dict[str, Any]]:
    """The last `limit` records, oldest-first. Reads backwards from the end
    rather than pulling the whole file in — these logs are bounded by rotation,
    but the prompt sink is 32 MB of assembled contexts and the debug page pages
    over all of them (mind/util.py)."""
    rows, _, _ = jsonl_page(path, limit=limit)
    rows.reverse()
    return rows


JOURNAL_PAGE_SIZE = 20  # diary days per page, newest first
PURGE_CHALLENGE_TTL_S = 60
MAX_OPTIMIZER_INSTRUCTIONS = 16_384
MAX_OPTIMIZER_DRAFT_BYTES = 1024 * 1024
_IMAGE_LIMITS = CardLimits()


def _image_field(value: object, field: str) -> bytes:
    from yurios.security import decode_bounded_base64
    try:
        return decode_bounded_base64(
            value, maximum=_IMAGE_LIMITS.max_file_bytes, field=field)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _log_sort_key(row: dict[str, Any]) -> float:
    """Tick traces stamp `ts` as a local ISO string (`iso_of`); tool-call audits
    stamp it as the raw epoch-seconds float underneath. Both come from the same
    clock, so this recovers one comparable epoch value to interleave the two
    logs chronologically instead of ticks-then-calls."""
    ts = row.get("ts")
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        try:
            return datetime.datetime.fromisoformat(ts).timestamp()
        except ValueError:
            return 0.0
    return 0.0


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


def _apply_connection(update: dict[str, Any], base: Config, record: CharacterRecord,
                       profile: ConnectionProfile | None,
                       env: Mapping[str, str]) -> None:
    """Point her models at *her* server, with *her* key (SPEC §31.1–§31.2).

    Which url that is — the profile's, or none at all — is decided by
    `characters.overrides.resolve_endpoint`, so the report `yurios start` prints
    and the config her runtime is built from cannot drift apart.

    A hosted route has no base url to move; it has a key, and the host-owned
    profile names the variable holding it. The value is read here and never
    stored: the registry is a file people copy between machines, and a card must
    never carry a secret (§30.5)."""
    field, endpoint = model_overrides.resolve_endpoint(
        update.get("chat_model", base.chat_model),
        update.get("utility_model", base.utility_model),
        profile_endpoint=profile.endpoint if profile else "",
        lmstudio_url=base.lmstudio_base_url, ollama_url=base.ollama_base_url)
    if field:
        update[field] = endpoint
    update["connection_api_key"] = ""
    key_env = profile.api_key_env if profile else ""
    if field:
        # Endpoint and credential come from the same host-owned profile. Never
        # substitute the house OpenRouter key when the profile key is absent.
        if key_env:
            update["connection_api_key"] = env.get(key_env, "").strip()
    elif profile and not profile.endpoint and key_env:
        # A key-only profile is an explicit hosted-OpenRouter credential grant.
        update["openrouter_api_key"] = (
            base.openrouter_api_key if key_env == "OPENROUTER_API_KEY"
            else env.get(key_env, "").strip()
        )


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
        "selfie_character": str(record.paths.appearance),
        "selfie_templates": str(record.paths.selfie_templates),
        "mind_enabled": record.loops.mind,
        "utility_enabled": record.loops.utility,
        "dream_enabled": record.loops.dream,
    }
    env = _env_values(base, environ)       # read once; both resolutions want it
    (update["telegram_bot_token"], update["telegram_chat_id"],
     update["telegram_bot_token_env"], update["telegram_chat_id_env"]) = (
        telegram_for_character(base, record.id, env))
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
    _apply_connection(update, base, record, profile, env)
    # Her own knobs (SPEC §31.2): anything the record names that is a real Config
    # field. Coerced against that field's type — the registry is JSON and a
    # hand-edited `"0.8"` must not reach LiteLLM as text — and a value that will
    # not coerce is dropped with a warning rather than taking her runtime down.
    for key, value in record.models.options.items():
        if key not in Config.model_fields:
            continue
        try:
            update[key] = rewire.coerce(base, key, value)
        except ValueError as exc:
            log.warning("character %s: ignoring %s — %s", record.id, key, exc)
    return base.model_copy(update=update)


# The knobs that live in `models.options` rather than in a named binding — the
# profile form accepts them alongside the two model ids (SPEC §31.2).
_OPTION_KEYS = frozenset(
    spec["key"] for spec in rewire.OVERRIDE_SCHEMA if spec["store"] == "options")


def _construction_fingerprint(record: CharacterRecord) -> tuple:
    """What a runtime bakes in when it is built (SPEC §31.4).

    Her name (it is the runtime's `companion_name`), her voice, her body, and the
    two loops that are wired rather than switched. Everything else a profile save
    can touch reaches her while she runs: her brain settings through `retune`,
    the mind switch through `set_mind_enabled`, and the card fields through the
    SOUL, which the brain re-reads on every turn (§5).

    Compared before and after the save rather than by which keys were *sent* —
    the switchboard's form posts every field on every save, and re-submitting the
    same voice is not a reason to take her conversation down."""
    return (record.display.name, record.voice.tts_backend, record.voice.stt_backend,
            record.voice.voice_id, record.body.backend, record.body.model,
            record.loops.utility, record.loops.dream)


def brain_overrides(record: CharacterRecord) -> dict[str, Any]:
    """What this character has taken for herself — blank fields left out.

    The inverse of `save_brain_overrides`, and the reason both live here: the
    settings screen must round-trip, and an override that reads back as
    something the record does not hold is a form that lies."""
    values: dict[str, Any] = {}
    for spec in rewire.OVERRIDE_SCHEMA:
        key, store = spec["key"], spec["store"]
        if store == "options":
            if key in record.models.options:
                values[key] = record.models.options[key]
            continue
        current = getattr(record.models, store, "") or ""
        if current:
            values[key] = current
    return values


def save_brain_overrides(record: CharacterRecord, body: Mapping[str, Any],
                          base: Config) -> list[str]:
    """Write the submitted overrides onto *record*; return the keys that moved.

    A key that is absent is left alone; a key sent **empty** is *cleared*, which
    is how the screen says "go back to inheriting the house's". Values are
    coerced and validated here, before anything is persisted — a bad number must
    fail the request, not the next turn."""
    forbidden = {"endpoint", "api_key_env"}.intersection(body)
    if forbidden:
        raise ValueError(
            f"{sorted(forbidden)[0]} is host-owned; select a connection_profile instead"
        )
    touched: list[str] = []
    for spec in rewire.OVERRIDE_SCHEMA:
        key, store = spec["key"], spec["store"]
        if key not in body:
            continue
        raw = body[key]
        blank = raw is None or (isinstance(raw, str) and not raw.strip())
        if store == "options":
            previous = record.models.options.get(key)
            if blank:
                record.models.options.pop(key, None)
                if previous is not None:
                    touched.append(key)
                continue
            value = rewire.coerce(base, key, raw)
            if previous != value:
                record.models.options[key] = value
                touched.append(key)
            continue
        value = "" if blank else str(raw).strip()
        if (getattr(record.models, store) or "") != value:
            setattr(record.models, store, value)
            touched.append(key)
    return touched


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
        # One process-wide budget, not one allowance per character runtime.
        self.voice_ws_limiter = VoiceConnectionLimiter(base.voice_ws_max_connections)
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

    def effective_config(self, record: CharacterRecord) -> Config:
        """Her `.env`, with her record's overrides laid over it (SPEC §31.2)."""
        profile = self.connections.get(record.connection.profile)
        if profile is None:
            raise ValueError(f"unknown connection profile: {record.connection.profile}")
        return config_for_character(self.base, record, profile)

    async def retune(self, record: CharacterRecord) -> dict[str, Any]:
        """Move a *running* character onto her current brain settings, live.

        The registry has already been written by the time this is called, so a
        character with no runtime is not a failure — she will read the same
        settings the moment she starts. What this buys is the other case: she is
        mid-conversation, and the next thing she says comes from the new model
        without her losing the thread (SPEC §31.4)."""
        rt = self.runtime(record.id)
        if rt is None:
            return {"applied": [], "running": False}
        result = await rt.retune(rewire.snapshot(self.effective_config(record)))
        return {**result, "running": True}

    def why_not_running(self, character_id: str) -> str:
        """The reason a character has no runtime, in words for the client.

        "not running" is the one thing the room already knows; which of the four
        reasons it is decides what the user does next, and only the host can
        tell them apart."""
        record = self.registry.get(character_id)
        if record is None:
            return "no such character on this node"
        name = record.display.name or character_id
        if record.lifecycle.review_required:
            # First sentence carries the whole reason on its own: it is what fits
            # in a close frame once _close_reason has had it (SPEC §28).
            return (f"{name} is waiting on review, so nothing is running behind this "
                    f"room. Open her in the studio, or save her settings from the "
                    f"dashboard, and she starts.")
        if not record.lifecycle.enabled:
            return f"{name} is disabled on this node."
        error = self.errors.get(character_id)
        if error:
            return f"{name} failed to start: {error}"
        return f"{name} is not running yet."

    async def start(self, character_id: str) -> None:
        async with self._lock:
            if character_id in self.apps:
                return
            record = self.registry.require(character_id)
            if record.lifecycle.review_required or not record.lifecycle.enabled:
                raise RuntimeError("character is disabled or still requires review")
            self.states[character_id] = "starting"
            ensure_appearance(record)   # her own face, before her camera (§7.6)
            ensure_setting(record)      # …and her own room, before her prompt (§2.5)
            try:
                app = create_app(self.effective_config(record),
                                 manage_lifespan=False, mount_frontend=False,
                                 protect_access=False, limit_http_body=False)
                app.state.rt.voice_ws_limiter = self.voice_ws_limiter
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


async def _turn_away(scope, receive, send, detail: str, *, status: int = 404) -> None:
    """Refuse a request in the protocol it arrived in.

    A websocket handshake cannot be answered with an HTTP response: uvicorn has
    no wire to put one on, so it logs `ASGI callable returned without completing
    handshake` and drops the connection — one line per retry, and a browser left
    with a bare 1006 that says nothing about why her room is empty. A character
    who imported as a foreign card sits exactly there: registered, page served,
    no runtime behind it until she has been reviewed (§28).

    So the socket is accepted and closed with a reason, and the frame in between
    is the `{"type":"error"}` every client already knows how to read (js/voice.js,
    world/routes/voice_ws.py). `4404` is the private-range code for "this
    character has no runtime" — a client can back off on it instead of
    reconnecting into the same wall every 1.5 seconds."""
    if scope.get("type") == "websocket":
        await receive()                          # the handshake's websocket.connect
        await send({"type": "websocket.accept"})
        await send({"type": "websocket.send",
                    "text": json.dumps({"type": "error", "message": detail})})
        await send({"type": "websocket.close", "code": 4404,
                    "reason": _close_reason(detail)})
        return
    await JSONResponse({"detail": detail}, status_code=status)(scope, receive, send)


_CLOSE_REASON_LIMIT = 123      # a close frame carries 125 bytes; 2 of them are the code


def _close_reason(detail: str) -> str:
    """*detail* cut down to what a close frame can actually carry.

    A websocket close is a control frame, and an oversized one is a protocol
    error the server raises on rather than sends — the connection then ends with
    no close frame at all, which is worse than the truncation. The whole sentence
    already went out in the error frame ahead of this; the close keeps its
    first line so a client that only ever sees `onclose` still learns something."""
    if len(detail.encode("utf-8")) <= _CLOSE_REASON_LIMIT:
        return detail
    head = detail.split(". ")[0] + "."
    if len(head.encode("utf-8")) <= _CLOSE_REASON_LIMIT:
        return head
    return detail.encode("utf-8")[:_CLOSE_REASON_LIMIT - 3].decode("utf-8", "ignore") + "..."


async def _optimize_events(utility, draft, *, model: str, instructions: str):
    """`/api/studio/optimize` as NDJSON: one line per pass, then the result.

    The run has to keep going while its lines go out, so it is a task feeding a
    queue and this generator only drains. Two consequences are deliberate. The
    status is 200 before the first pass has happened, so a failure arrives as a
    final `{"event": "error"}` line rather than as an HTTP code — the dialog
    reads the last line either way. And a client that closes the tab cancels the
    task on the way out, because there is nobody left to hand the answer to.
    """
    queue: asyncio.Queue = asyncio.Queue()

    async def run() -> None:
        try:
            result = await optimize_draft(utility, draft, model=model,
                                          instructions=instructions,
                                          on_progress=queue.put)
            await queue.put({"event": "done", "result": result.to_dict()})
        except CardOptimizeError as exc:
            await queue.put({"event": "error", "message": str(exc)})
        except asyncio.CancelledError:
            raise
        except Exception as exc:                       # pragma: no cover - defensive
            log.exception("studio: optimize failed")
            await queue.put({"event": "error",
                             "message": f"the optimisation failed: {exc}"})
        finally:
            await queue.put(None)

    task = asyncio.create_task(run())
    try:
        while True:
            event = await queue.get()
            if event is None:
                break
            yield json.dumps(event, ensure_ascii=False) + "\n"
    finally:
        task.cancel()


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
            await _turn_away(scope, receive, send, self.host.why_not_running(character_id))
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

    app = FastAPI(title="YuriOS Host", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    app.state.host = host
    app.state.lifecycle_lock = asyncio.Lock()
    app.state.purge_challenges = {}
    from yurios.security import install_http_boundaries, install_owner_security
    install_http_boundaries(app)
    install_owner_security(app, base)

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
        env = _env_values(base, None)
        return {"profiles": [
            {"name": item.name, "backend": item.backend, "endpoint": item.endpoint,
             "api_key_env": item.api_key_env, "secret_configured":
             bool(item.api_key_env and env.get(item.api_key_env, "").strip())}
            for item in host.connections.list()
        ]}

    @app.put("/api/connections/{profile_name}")
    async def save_connection(profile_name: str, request: Request):
        body = await request.json()
        if not isinstance(body, Mapping):
            raise HTTPException(400, "connection profile must be a JSON object")
        try:
            profile = ConnectionProfile(
                name=profile_name,
                backend=str(body.get("backend") or "litellm").strip(),
                endpoint=str(body.get("endpoint") or "").strip(),
                api_key_env=str(body.get("api_key_env") or "").strip(),
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        host.connections.upsert(profile)
        applied: dict[str, list[str]] = {}
        for record in registry:
            if record.connection.profile == profile.name and host.runtime(record.id) is not None:
                applied[record.id] = (await host.retune(record))["applied"]
        env = _env_values(base, None)
        return {"profile": {
            "name": profile.name, "backend": profile.backend,
            "endpoint": profile.endpoint, "api_key_env": profile.api_key_env,
            "secret_configured": bool(
                profile.api_key_env and env.get(profile.api_key_env, "").strip()),
        }, "applied": applied}

    # ---- her own brain (SPEC §31.2, §31.4) --------------------------------
    #
    # The gear in every room opens two panels stacked: the house's `.env`
    # (desktop/routes/settings.py — loopback-only, restart to apply) and this
    # one, which is hers. Nothing here is a secret: the key is named, never
    # carried, so it needs no stricter guard than the rest of the same-origin
    # switchboard API (§32.4).
    #
    # `/api/brain` without an id answers for the primary character, because the
    # single-companion install's pages carry no character in their URL (§29.7).

    def _require_primary() -> CharacterRecord:
        record = registry.get(host.primary_id or "")
        if record is None:
            raise HTTPException(503, "no active character")
        return record

    def _brain_payload(record: CharacterRecord) -> dict[str, Any]:
        effective = host.effective_config(record)
        overrides = brain_overrides(record)
        profile = host.connections.get(record.connection.profile)
        # One endpoint names one server: the local provider her models actually
        # route to (hosted routes have a key instead, and no url to show).
        endpoint_field = (model_overrides.endpoint_field(effective.chat_model)
                          or model_overrides.endpoint_field(effective.utility_model))
        inherited = {
            "endpoint": getattr(effective, endpoint_field) if endpoint_field else "",
            "api_key_env": (profile.api_key_env if profile else "") or "",
        }
        key_env = inherited["api_key_env"]
        return {
            "character": record.id,
            "name": record.display.name,
            "running": host.runtime(record.id) is not None,
            "connection_profile": record.connection.profile,
            "fields": [{**spec, "value": overrides.get(spec["key"], ""),
                        "inherited": inherited.get(spec["key"],
                                                   getattr(base, spec["key"], ""))}
                       for spec in rewire.OVERRIDE_SCHEMA],
            # the name is safe to show; whether it is *set* is the useful part
            "key_configured": bool(
                key_env and _env_values(base, None).get(key_env, "").strip()),
            "effective": {
                "chat_model": effective.chat_model,
                "utility_model": effective.utility_model,
                "endpoint": getattr(effective, endpoint_field) if endpoint_field else "",
                "api_key_env": key_env,
            },
        }

    async def _save_brain(record: CharacterRecord, body: Mapping[str, Any]) -> dict:
        candidate = copy.deepcopy(record)
        try:
            changed = save_brain_overrides(candidate, body, base)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if changed:
            registry.upsert(candidate)
        result = await host.retune(candidate)
        return {**_brain_payload(candidate), "changed": changed,
                "applied": result["applied"]}

    @app.get("/api/characters/{character_id}/brain")
    async def brain(character_id: str):
        return _brain_payload(require(character_id))

    @app.patch("/api/characters/{character_id}/brain")
    async def save_brain(character_id: str, request: Request):
        body = await request.json()
        if not isinstance(body, Mapping):
            raise HTTPException(400, "brain settings must be a JSON object")
        return await _save_brain(require(character_id), body)

    @app.get("/api/brain")
    async def primary_brain():
        return _brain_payload(_require_primary())

    @app.patch("/api/brain")
    async def save_primary_brain(request: Request):
        body = await request.json()
        if not isinstance(body, Mapping):
            raise HTTPException(400, "brain settings must be a JSON object")
        return await _save_brain(_require_primary(), body)

    @app.post("/api/characters/import")
    async def import_card(file: UploadFile):
        payload = await file.read(32 * 1024 * 1024 + 1)
        try:
            record = CharacterImporter(registry).import_card(payload)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
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

    # ---- the card studio (SPEC §28) --------------------------------------
    #
    # Export is the importer's mirror, and the one place in this API where a
    # refusal is a feature: `CardExportError` carries a code the studio renders
    # differently — `leak` is "no", `review_required` is "not yet, read this
    # first". Both come back as 422 with the offending passages, never as a 500.

    def _options(body: Mapping[str, Any]) -> ExportOptions:
        image_bytes = None
        if body.get("image_data"):
            image_bytes = _image_field(body["image_data"], "image_data")
        try:
            return ExportOptions(
                spec=str(body.get("spec", "v3")),
                include_soul=bool(body.get("include_soul", True)),
                image=str(body.get("image", "portrait")),
                image_bytes=image_bytes,
                fit=str(body.get("fit", "contain")),
                attribution=bool(body.get("attribution", True)),
                timestamps=bool(body.get("timestamps", True)),
                filename=body.get("filename") or None,
                acknowledged=bool(body.get("acknowledged", False)))
        except CardExportError as exc:
            raise HTTPException(400, str(exc)) from exc

    def _refused(exc: CardExportError) -> HTTPException:
        return HTTPException(422, exc.to_dict())

    @app.get("/api/characters/{character_id}/export")
    async def export_card(character_id: str):
        """The drawer's one-click export: defaults, and the scrub in front."""
        record = require(character_id)
        try:
            result = build_export(record, ExportOptions(),
                                  user_name=base.user_name)
        except CardExportError as exc:
            raise _refused(exc) from exc
        return Response(result.png, media_type="image/png", headers={
            "Content-Disposition": f'attachment; filename="{result.filename}"'})

    @app.post("/api/characters/{character_id}/export")
    async def export_card_configured(character_id: str, request: Request):
        record = require(character_id)
        try:
            result = build_export(record, _options(await request.json()),
                                  user_name=base.user_name)
        except CardExportError as exc:
            raise _refused(exc) from exc
        return Response(result.png, media_type="image/png", headers={
            "Content-Disposition": f'attachment; filename="{result.filename}"',
            "X-Yurios-Card-Bytes": str(len(result.png))})

    @app.post("/api/characters/{character_id}/studio/preview")
    async def studio_preview(character_id: str, request: Request):
        """Everything the review pane renders, and no file."""
        record = require(character_id)
        try:
            result = preview_export(record, _options(await request.json()),
                                    user_name=base.user_name)
        except CardExportError as exc:
            raise _refused(exc) from exc
        return result.to_dict()

    @app.get("/api/studio/template")
    async def studio_template():
        return {"draft": template_draft().to_dict(),
                "sections": [{"field": name, "ref": ref, "label": label}
                             for name, ref, label in studio_model.SECTION_FIELDS],
                "constitution_fields": sorted(studio_model.CONSTITUTION_FIELDS)}

    # ---- optimise with AI (SPEC §30.6) -----------------------------------
    #
    # Two routes, and neither of them writes anything. The optimiser hands back
    # a draft and a diff; the studio shows the diff, and it is the ordinary
    # PATCH above that saves — so a card from the internet can propose an edit
    # to itself but never make one.

    @app.get("/api/studio/models")
    async def studio_models(provider: str = "", character: str = ""):
        """What the chosen provider is serving, for the optimize dialog's picker.

        Declared on the host rather than reached through the primary runtime's
        `/api/models`, because the studio is a host surface: it opens for a
        character under review, who by definition has no runtime yet."""
        record = registry.get(character) if character else None
        cfg = host.effective_config(record) if record else base
        return await provider_models(cfg, provider)

    @app.post("/api/studio/optimize")
    async def studio_optimize(request: Request):
        body = await request.json()
        if not isinstance(body, Mapping):
            raise HTTPException(400, "optimizer request must be a JSON object")
        try:
            draft_size = len(json.dumps(body.get("draft") or {}, ensure_ascii=False).encode())
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, "draft must be JSON data") from exc
        if draft_size > MAX_OPTIMIZER_DRAFT_BYTES:
            raise HTTPException(422, "draft exceeds the optimizer input limit")
        draft = studio_model.Draft.from_dict(body.get("draft") or {})
        record = registry.get(str(body.get("character") or "")) or None
        cfg = host.effective_config(record) if record else base
        # A model named in the dialog is used as given — it is a full LiteLLM id,
        # prefix and all, so the picker's provider choice is already in it. With
        # none named she is optimised by whatever her own utility model is.
        model = str(body.get("model") or "").strip()
        if len(model) > 512:
            raise HTTPException(422, "model name is too long")
        model = model or cfg.utility_model
        from yurios.app.main import model_api_base, model_api_key
        from yurios.app.providers.openrouter import LiteLLMUtilityModel
        utility = LiteLLMUtilityModel(
            model, model_api_key(cfg, model), thinking=cfg.utility_thinking,
            api_base=model_api_base(cfg, model))
        instructions = str(body.get("instructions") or "")
        if len(instructions) > MAX_OPTIMIZER_INSTRUCTIONS:
            raise HTTPException(422, "optimizer instructions are too long")
        # Three sequential passes over a long card is minutes of nothing to look
        # at. A client that asks for the stream gets one line per pass as it
        # happens; everyone else gets the single JSON object this has always
        # answered with, so a script does not have to learn a protocol to call it.
        if "application/x-ndjson" not in (request.headers.get("accept") or ""):
            try:
                result = await optimize_draft(utility, draft, model=model,
                                              instructions=instructions)
            except CardOptimizeError as exc:
                raise HTTPException(502, str(exc)) from exc
            return result.to_dict()
        return StreamingResponse(
            _optimize_events(utility, draft, model=model, instructions=instructions),
            media_type="application/x-ndjson",
            # Nothing may sit on these lines: the whole point is that they arrive
            # while the run is still going.
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.post("/api/characters")
    async def create(request: Request):
        body = await request.json()
        draft = studio_model.Draft.from_dict(body.get("draft") or {})
        portrait = None
        if body.get("portrait"):
            portrait = _image_field(body["portrait"], "portrait")
        try:
            record = create_character(registry, draft, portrait=portrait,
                                      character_id=body.get("character_id") or None)
        except (ValueError, CardExportError) as exc:
            raise HTTPException(400, str(exc)) from exc
        if record.lifecycle.enabled:
            await host.start(record.id)
        return JSONResponse({"character": host.summary(record)}, status_code=201)

    @app.get("/api/characters/{character_id}/studio")
    async def studio_draft(character_id: str):
        record = require(character_id)
        try:
            draft, provenance = studio_model.read_draft(record)
        except CardExportError as exc:
            raise _refused(exc) from exc
        return {
            "id": record.id,
            "draft": draft.to_dict(),
            "provenance": {name: item.to_dict() for name, item in provenance.items()},
            "grown": studio_model.grown_fields(provenance),
            "sections": [{"field": name, "ref": ref, "label": label}
                         for name, ref, label in studio_model.SECTION_FIELDS],
            "constitution_fields": sorted(studio_model.CONSTITUTION_FIELDS),
            "images": _images(record),
            "portrait_url": f"/api/characters/{record.id}/portrait",
            # Not a card field, and shown on the same page on purpose: where she
            # is (§2.5) is the one thing the card decides that the *card* never
            # carries back out. It rides beside the draft rather than in it —
            # `Draft` is what ships, and this file stays in the Vault.
            "setting": setting_model.derived_material(record),
        }

    # ---- where she is (SPEC §2.5, §19.2) ---------------------------------
    #
    # Her standing place, derived from her card at import and edited here. Three
    # verbs, same shape as her selfie library: read hers, write hers, and ask
    # the utility model for a better one — which, like `/api/studio/optimize`,
    # hands the prose back for you to look at and never writes it itself.

    @app.get("/api/characters/{character_id}/setting")
    async def get_setting(character_id: str):
        return {"id": character_id,
                **setting_model.derived_material(require(character_id))}

    @app.put("/api/characters/{character_id}/setting")
    async def save_setting(character_id: str, request: Request):
        record = require(character_id)
        body = await request.json()
        place = str((body or {}).get("setting") or "").strip()
        try:
            if place:
                # Saved without the derived marker: you wrote it, so no later
                # background pass gets to redecorate it.
                setting_model.write_authored(record.paths.setting, place)
            else:
                # Emptied on purpose. The file goes, and `ensure_setting` will
                # write a fresh mechanical one at her next start rather than
                # leaving her nowhere.
                Path(record.paths.setting).unlink(missing_ok=True)
        except OSError as exc:
            raise HTTPException(500, f"could not write her setting: {exc}") from exc
        try:
            from yurios.app import vaultgit
            vaultgit.commit(record.paths.vault, "studio: edit where she is")
        except Exception:
            log.exception("could not commit the setting edit")
        # No restart: `WorldModelStore.situation()` reads the file every turn,
        # so the next prompt already has it.
        return {"id": record.id, **setting_model.derived_material(record)}

    @app.post("/api/characters/{character_id}/setting/derive")
    async def derive_setting(character_id: str, request: Request):
        """A better room, from her card — proposed, never saved."""
        record = require(character_id)
        body = await request.json() if await request.body() else {}
        cfg = host.effective_config(record)
        model = str((body or {}).get("model") or "").strip() or cfg.utility_model
        from yurios.app.main import model_api_base, model_api_key
        from yurios.app.providers.openrouter import LiteLLMUtilityModel
        utility = LiteLLMUtilityModel(
            model, model_api_key(cfg, model), thinking=cfg.utility_thinking,
            api_base=model_api_base(cfg, model))
        name, scenario, description, first_mes = setting_model.card_material(record)
        place = await setting_model.derive_place(
            utility, name=name or record.display.name, scenario=scenario,
            description=description, first_mes=first_mes, busy_is_error=True)
        if not place:
            raise HTTPException(502, "the model had nothing to say about where "
                                     "she is — her card may not say either")
        return {"id": record.id, "setting": place, "model": model}

    @app.patch("/api/characters/{character_id}/studio")
    async def studio_save(character_id: str, request: Request):
        record = require(character_id)
        body = await request.json()
        draft = studio_model.Draft.from_dict(body.get("draft") or body)
        try:
            touched = studio_model.apply_draft(record, draft)
        except CardExportError as exc:
            raise _refused(exc) from exc
        record.display.name = draft.name
        record.display.description = draft.description
        record.display.creator = draft.creator
        record.display.tags = list(draft.tags)
        # A card edited in the studio has been looked at by definition.
        if record.lifecycle.review_required:
            record.lifecycle.review_required = False
            record.lifecycle.enabled = True
            record.lifecycle.autostart = True
        registry.upsert(record)
        _sync_card_json(record, draft)
        touched_constitution = "CONSTITUTION.md" in touched
        try:
            from yurios.app import vaultgit
            vaultgit.commit(record.paths.vault,
                            "studio: edit constitution" if touched_constitution
                            else "studio: edit character card")
        except Exception:
            log.exception("could not commit studio edit")
        if host.runtime(character_id) is not None:
            await host.restart(character_id)
        elif record.lifecycle.enabled:
            await host.start(character_id)
        return {"character": host.summary(record), "touched": touched}

    @app.get("/api/characters/{character_id}/selfies")
    async def list_selfies(character_id: str):
        return {"selfies": _images(require(character_id))["selfies"]}

    async def _reload_camera(character_id: str) -> None:
        """Pick a saved selfie library up. The forge is built once at runtime
        start and the `take_selfie` description once at tool-server start, so a
        library nobody restarted for is a page telling you about scenes she
        cannot name."""
        if host.runtime(character_id) is not None:
            await host.restart(character_id)

    # Her camera's vocabulary (§7.6) — a whole library per character, not an
    # overlay: hers *replaces* the shipped book, because the shipped one is one
    # character's world down to the tail in half its scenes and an overlay can
    # only ever add to that. The three verbs are the whole lifecycle: read hers
    # (or ours, until she has one), write hers, throw hers away.
    @app.get("/api/characters/{character_id}/selfie-templates")
    async def selfie_templates(character_id: str):
        record = require(character_id)
        book, source = selfiebook.read_for(record)
        return {
            "id": record.id,
            "book": book,
            "source": source,
            "slots": [{"key": name, "label": label, "hint": hint}
                      for name, label, hint in selfiebook.SLOTS],
            "shipped": selfiebook.shipped(),
        }

    @app.put("/api/characters/{character_id}/selfie-templates")
    async def save_selfie_templates(character_id: str, request: Request):
        record = require(character_id)
        body = await request.json()
        book = selfiebook.normalise(body.get("book") if isinstance(body, Mapping)
                                    else None)
        if not any(book["slots"].values()):
            # Every slot empty is not a library, it is a camera with no words:
            # `compose` would fall through to free-form only and an unprompted
            # shot would have nothing at all to rotate in. Deleting is how you
            # say "use the shipped one" — this is how you say it by accident.
            raise HTTPException(400, "a selfie library needs at least one row — "
                                     "delete it to go back to the shipped one")
        try:
            selfiebook.write(record.paths.selfie_templates, book,
                             record.display.name)
        except OSError as exc:
            raise HTTPException(500, f"could not write her selfie library: {exc}") from exc
        await _reload_camera(character_id)
        return {"book": book, "source": "character"}

    @app.delete("/api/characters/{character_id}/selfie-templates")
    async def reset_selfie_templates(character_id: str):
        record = require(character_id)
        Path(record.paths.selfie_templates).unlink(missing_ok=True)
        await _reload_camera(character_id)
        return {"book": selfiebook.shipped(), "source": "shipped"}

    @app.post("/api/characters/{character_id}/portrait")
    async def set_portrait(character_id: str, request: Request):
        """Adopt an upload or one of her selfies as the character's face."""
        from yurios.characters.importer import _sanitize_portrait
        from yurios.characters import CardLimits

        record = require(character_id)
        body = await request.json()
        if body.get("selfie"):
            base_dir = record.paths.selfies.resolve()
            path = (base_dir / str(body["selfie"])).resolve()
            if path.parent != base_dir or not path.is_file():
                raise HTTPException(404, "no such selfie")
            raw = path.read_bytes()
        elif body.get("image"):
            raw = _image_field(body["image"], "image")
        else:
            raise HTTPException(400, "send a selfie name or a base64 image")
        try:
            record.paths.portrait.parent.mkdir(parents=True, exist_ok=True)
            record.paths.portrait.write_bytes(_sanitize_portrait(raw, _IMAGE_LIMITS))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"portrait_url": f"/api/characters/{record.id}/portrait"}

    @app.get("/api/characters/{character_id}/profile")
    async def get_settings(character_id: str):
        record = require(character_id)
        _, card = _card_values(record)
        profile = host.connections.get(record.connection.profile)
        return {"settings": {
            "name": record.display.name, "description": record.display.description,
            "model": record.models.chat, "utility_model": record.models.utility,
            "voice": record.voice.voice_id,
            "connection": profile.backend if profile else "",
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
        current = require(character_id)
        body = await request.json()
        if not isinstance(body, Mapping):
            raise HTTPException(400, "character profile must be a JSON object")
        forbidden = {"endpoint", "api_key_env"}.intersection(body)
        if forbidden:
            field = sorted(forbidden)[0]
            raise HTTPException(
                400, f"{field} is host-owned; select a connection_profile instead")
        record = copy.deepcopy(current)
        built_with = _construction_fingerprint(current)
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
        # …and her own model knobs, accepted here too, so one save can move a
        # model and the temperature it wants together (an empty value clears).
        try:
            save_brain_overrides(record, {key: value for key, value in body.items()
                                          if key in _OPTION_KEYS}, base)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
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
        applied: list[str] = []
        if was_running:
            # Which model she thinks with is not worth a rebuild (SPEC §31.4):
            # unless this save moved something the runtime was *built* with, she
            # keeps her session, her mind and her voice, and simply answers the
            # next line through the new brain.
            if _construction_fingerprint(record) != built_with:
                await host.restart(character_id)
            else:
                applied = (await host.retune(record))["applied"]
                if "mind" in body:
                    await host.runtime(character_id).set_mind_enabled(record.loops.mind)
        elif record.lifecycle.enabled:
            await host.start(character_id)
        return {"character": host.summary(record), "applied": applied}

    @app.post("/api/characters/{character_id}/approve")
    async def approve(character_id: str):
        """Say out loud what saving her settings has always said quietly.

        A card written elsewhere imports parked (SPEC §28) and the only way out
        used to be a side effect of some other save — nothing on the switchboard
        named the act. This does the one thing and nothing else: review cleared,
        enabled, autostart on, runtime up. A start that fails leaves her approved
        and reports why, because "she is allowed to run" and "she ran" are two
        different facts and the dashboard shows both."""
        record = require(character_id)
        record.lifecycle.review_required = False
        record.lifecycle.enabled = True
        record.lifecycle.autostart = True
        registry.upsert(record)
        started, detail = True, None
        if host.runtime(character_id) is None:
            try:
                await host.start(character_id)
            except Exception as exc:                       # already logged by start()
                started, detail = False, str(exc)
        return {"character": host.summary(record), "started": started, "error": detail}

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
    async def journal(character_id: str, page: int = 0, day: str | None = None):
        """The diary index (paged 20 days at a time, newest day first) or,
        with `day=YYYY-MM-DD`, that one day's entries newest-first. Reads the
        episodic files straight off disk (like /log and /context-history) so
        history is visible whether or not the mind loop is currently running."""
        record = require(character_id)
        episodic = Path(record.paths.vault) / "memory" / "episodic"
        if day:
            try:
                day = canonical_day(day)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            path = episodic / f"{day}.md"
            text = path.read_text(encoding="utf-8") if path.is_file() else ""
            entries = parse_day_entries(text)
            entries.reverse()
            return {"day": day, "entries": entries}
        all_days = sorted((p.stem for p in episodic.glob("*.md")
                           if is_canonical_day(p.stem)), reverse=True) \
            if episodic.is_dir() else []
        page = max(0, page)
        start = page * JOURNAL_PAGE_SIZE
        page_days = all_days[start:start + JOURNAL_PAGE_SIZE]
        out = [{"day": d, "count": len(parse_day_entries((episodic / f"{d}.md").read_text(encoding="utf-8")))}
               for d in page_days]
        return {"days": out, "page": page,
                "has_more": start + JOURNAL_PAGE_SIZE < len(all_days), "total": len(all_days)}

    @app.get("/api/characters/{character_id}/log")
    async def logs(character_id: str):
        record = require(character_id)
        rows = _tail_jsonl(record.paths.traces / "ticks.jsonl", 60)
        rows += _tail_jsonl(record.paths.tool_logs / "calls.jsonl", 40)
        rows.sort(key=_log_sort_key)
        return {"entries": rows[-100:]}

    @app.get("/api/characters/{character_id}/context-history")
    async def context(character_id: str):
        record = require(character_id)
        rt = host.runtime(character_id)
        return {"context": rt.context.snapshot() if rt else {"used": 0, "limit": None},
                "history": _tail_jsonl(record.paths.traces / "context.jsonl", 500)}

    # ---- /debug/*: the mind debug page (SPEC §24.3) --------------------------
    # Namespaced under `debug/` on purpose. `/api/characters/{id}/mind` is
    # already the sanctuary's inner-life surface — the dispatcher below rewrites
    # it to the child app's `/api/mind` — and a host route by that name would
    # silently shadow a working page with differently-shaped disk reads.
    #
    # Every route here reads files and needs no runtime. `/api/mind` answers 503
    # when the loop is off, which is exactly the moment you want to read what
    # happened; these answer anyway.

    @app.get("/api/characters/{character_id}/debug/overview")
    async def debug_overview(character_id: str):
        return debug.overview(require(character_id), host.runtime(character_id))

    @app.get("/api/characters/{character_id}/debug/activity")
    async def debug_activity(character_id: str, page: int = 0, limit: int = 100):
        return debug.activity(require(character_id), page=page, limit=limit)

    @app.get("/api/characters/{character_id}/debug/ticks")
    async def debug_ticks(character_id: str, page: int = 0, limit: int = 25,
                          state: str | None = None, q: str | None = None):
        return debug.ticks(require(character_id), page=page, limit=limit,
                           state=state, q=q)

    @app.get("/api/characters/{character_id}/debug/ticks/{tick_id}")
    async def debug_tick(character_id: str, tick_id: str):
        found = debug.tick_detail(require(character_id), tick_id)
        if found is None:
            raise HTTPException(404, "no such tick in the live trace")
        return found

    @app.get("/api/characters/{character_id}/debug/signals")
    async def debug_signals(character_id: str, page: int = 0, limit: int = 100,
                            type: str | None = None):
        return debug.signals(require(character_id), page=page, limit=limit, type=type)

    @app.get("/api/characters/{character_id}/debug/goals")
    async def debug_goals(character_id: str):
        return debug.goals(require(character_id))

    @app.get("/api/characters/{character_id}/debug/self-edits")
    async def debug_self_edits(character_id: str):
        return debug.self_edits(require(character_id))

    @app.get("/api/characters/{character_id}/debug/calls")
    async def debug_calls(character_id: str, page: int = 0, limit: int = 50,
                          tool: str | None = None, verdict: str | None = None,
                          corr_id: str | None = None):
        return debug.calls(require(character_id), page=page, limit=limit,
                           tool=tool, verdict=verdict, corr_id=corr_id)

    @app.get("/api/characters/{character_id}/debug/selfies")
    async def debug_selfies(character_id: str, page: int = 0, limit: int = 24):
        return debug.selfies(require(character_id), page=page, limit=limit)

    @app.get("/api/characters/{character_id}/debug/prompts/days")
    async def debug_prompt_days(character_id: str, page: int = 0, limit: int = 20):
        return debug.prompt_days(require(character_id), page=page, limit=limit)

    @app.get("/api/characters/{character_id}/debug/prompts")
    async def debug_prompts(character_id: str, day: str | None = None,
                            kind: str | None = None, page: int = 0,
                            limit: int = 25):
        return debug.prompts(require(character_id), day=day, kind=kind,
                             page=page, limit=limit)

    @app.get("/api/characters/{character_id}/debug/prompts/{prompt_id}")
    async def debug_prompt(character_id: str, prompt_id: str):
        found = debug.prompt_detail(require(character_id), prompt_id)
        if found is None:
            raise HTTPException(404, "no such prompt in the live log")
        return found

    @app.get("/api/characters/{character_id}/debug/vault/commits")
    async def debug_commits(character_id: str, page: int = 0, limit: int = 25,
                            path: str | None = None):
        return debug.vault_commits(require(character_id), page=page,
                                   limit=limit, path=path)

    @app.get("/api/characters/{character_id}/debug/vault/commits/{sha}")
    async def debug_commit(character_id: str, sha: str):
        record = require(character_id)
        if not vaultgit.is_rev(sha):
            raise HTTPException(400, "not a commit id")
        found = vaultgit.show(Path(record.paths.vault), sha)
        if found is None:
            raise HTTPException(404, "no such commit")
        return found

    @app.get("/api/characters/{character_id}/debug/vault/tree")
    async def debug_tree(character_id: str, path: str = ""):
        record = require(character_id)
        entries = vaultgit.tree(Path(record.paths.vault), path)
        if entries is None:
            raise HTTPException(400, "not a directory inside this vault")
        return {"path": path, "entries": entries}

    @app.get("/api/characters/{character_id}/debug/vault/file")
    async def debug_file(character_id: str, path: str, rev: str | None = None):
        record = require(character_id)
        found = vaultgit.read_at(Path(record.paths.vault), path, rev=rev)
        if found is None:
            raise HTTPException(400, "not a readable file inside this vault")
        return found

    @app.get("/api/characters/{character_id}/debug/vault/history")
    async def debug_file_history(character_id: str, path: str, limit: int = 25):
        record = require(character_id)
        if vaultgit.in_vault(Path(record.paths.vault), path) is None:
            raise HTTPException(400, "not a path inside this vault")
        return {"path": path,
                "items": vaultgit.log_records(Path(record.paths.vault),
                                              limit=max(1, min(limit, 200)),
                                              path=path)}

    @app.get("/api/characters/{character_id}/debug/memory")
    async def debug_memory(character_id: str):
        return debug.memory(require(character_id))

    @app.get("/api/characters/{character_id}/debug/memory/chunks")
    async def debug_chunks(character_id: str, page: int = 0, limit: int = 50,
                           kind: str | None = None, q: str | None = None):
        return debug.chunks(require(character_id), page=page, limit=limit,
                            kind=kind, q=q)

    @app.get("/api/characters/{character_id}/debug/memory/chunks/{chunk_id}")
    async def debug_chunk(character_id: str, chunk_id: str):
        found = debug.chunk(require(character_id), chunk_id)
        if found is None:
            raise HTTPException(404, "no such chunk in the index")
        return found

    @app.get("/api/characters/{character_id}/debug/economics")
    async def debug_economics(character_id: str):
        return debug.economics(require(character_id))

    @app.get("/api/characters/{character_id}/debug/utility")
    async def debug_utility(character_id: str, page: int = 0, limit: int = 25,
                            kind: str | None = None):
        return debug.utility(require(character_id), page=page, limit=limit, kind=kind)

    @app.post("/api/characters/{character_id}/archive")
    async def archive(character_id: str):
        async with app.state.lifecycle_lock:
            record = require(character_id)
            was_running = host.runtime(character_id) is not None
            await host.stop(character_id)
            archive_root = registry.data_root / "archives"
            archive_root.mkdir(parents=True, exist_ok=True)
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            target = archive_root / f"{record.id}-{stamp}"
            if target.exists():
                if was_running:
                    await host.start(character_id)
                raise HTTPException(409, "archive target already exists")
            try:
                os.replace(record.paths.root, target)
            except OSError as exc:
                if was_running:
                    await host.start(character_id)
                raise HTTPException(500, "could not move character into archive") from exc
            try:
                registry.remove(character_id)
            except Exception as exc:
                try:
                    os.replace(target, record.paths.root)
                except OSError as rollback:
                    log.exception("archive rollback failed for %s", character_id)
                    raise HTTPException(
                        500, "archive registry update failed and data rollback failed") \
                        from rollback
                if was_running:
                    try:
                        await host.start(character_id)
                    except Exception:
                        log.exception("archive runtime restore failed for %s", character_id)
                raise HTTPException(500, "archive registry update failed; data restored") from exc
            host.states.pop(character_id, None)
            if host.primary_id == character_id:
                host.primary_id = next((r.id for r in registry if r.lifecycle.enabled), None)
            return {"archived": True, "id": character_id}

    @app.post("/api/characters/{character_id}/purge/prepare")
    async def prepare_purge(character_id: str):
        require(character_id)
        now = time.monotonic()
        challenges: dict[str, tuple[str, float]] = app.state.purge_challenges
        for token, (_owner, expires) in list(challenges.items()):
            if expires <= now:
                challenges.pop(token, None)
        while len(challenges) >= 128:
            challenges.pop(next(iter(challenges)))
        challenge = secrets.token_urlsafe(24)
        challenges[challenge] = (character_id, now + PURGE_CHALLENGE_TTL_S)
        return JSONResponse(
            {"challenge": challenge, "expires_in": PURGE_CHALLENGE_TTL_S},
            headers={"Cache-Control": "no-store"})

    @app.delete("/api/characters/{character_id}/purge")
    async def purge(character_id: str, request: Request):
        if request.headers.get("content-type", "").split(";", 1)[0].lower() != \
                "application/json":
            raise HTTPException(415, "purge confirmation must be a JSON body")
        try:
            body = await request.json()
        except (TypeError, ValueError):
            raise HTTPException(400, "purge confirmation must be valid JSON") from None
        challenge = body.get("challenge") if isinstance(body, Mapping) else None
        if not isinstance(challenge, str) or len(challenge) > 128:
            raise HTTPException(400, "valid purge challenge required")
        entry = app.state.purge_challenges.pop(challenge, None)
        if (entry is None or entry[0] != character_id or
                entry[1] <= time.monotonic()):
            raise HTTPException(400, "purge challenge is invalid or expired")

        async with app.state.lifecycle_lock:
            record = require(character_id)
            was_running = host.runtime(character_id) is not None
            await host.stop(character_id)
            tombstone_root = registry.data_root / ".purging"
            tombstone_root.mkdir(parents=True, exist_ok=True)
            tombstone = tombstone_root / f"{record.id}-{secrets.token_hex(12)}"
            try:
                os.replace(record.paths.root, tombstone)
            except OSError as exc:
                if was_running:
                    await host.start(character_id)
                raise HTTPException(500, "could not stage character for purge") from exc
            try:
                registry.remove(character_id)
            except Exception as exc:
                try:
                    os.replace(tombstone, record.paths.root)
                except OSError as rollback:
                    log.exception("purge rollback failed for %s", character_id)
                    raise HTTPException(
                        500, "purge registry update failed and data rollback failed") \
                        from rollback
                if was_running:
                    try:
                        await host.start(character_id)
                    except Exception:
                        log.exception("purge runtime restore failed for %s", character_id)
                raise HTTPException(500, "purge registry update failed; data restored") from exc

            host.states.pop(character_id, None)
            if host.primary_id == character_id:
                host.primary_id = next((r.id for r in registry if r.lifecycle.enabled), None)
            cleanup_pending = False
            try:
                shutil.rmtree(tombstone)
            except OSError:
                cleanup_pending = True
                log.exception("purge tombstone cleanup failed for %s", character_id)
            return {"purged": True, "id": character_id,
                    "cleanup_pending": cleanup_pending}

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

    # The bodyless client (SPEC §6.7): the transcript, the composer and the voice
    # loop with no renderer behind them. Same bundle root, so /assets/* below
    # serves it; shared/runtime.js reads the character out of this path.
    @app.get("/characters/{character_id}/text")
    @app.get("/characters/{character_id}/text/")
    async def character_text(character_id: str):
        require(character_id)
        return FileResponse(DIST_DIR / "text" / "index.html", media_type="text/html")

    # The mind debug page (SPEC §24.3): not a room — it never speaks to her, it
    # reads her files. Character-scoped by path like the rooms above, so
    # shared/runtime.js can aim its /api/characters/{id}/debug/* calls.
    @app.get("/characters/{character_id}/mind")
    @app.get("/characters/{character_id}/mind/")
    async def character_mind(character_id: str):
        require(character_id)
        return FileResponse(DIST_DIR / "mind" / "index.html", media_type="text/html")

    # Both Vite entry points emit their hashed bundles into this shared root.
    # Keep it ahead of the primary-runtime fallback so /assets/* is not
    # dispatched to a character app that was created without its frontend.
    app.mount("/assets", StaticFiles(directory=DIST_DIR / "assets", check_dir=False),
              name="frontend-assets")
    app.mount("/dashboard", StaticFiles(directory=DIST_DIR / "dashboard", html=True,
                                         check_dir=False), name="dashboard")
    # The studio selects its character by query parameter (`/studio/?character=…`)
    # rather than by path, following `/live2d/?character=…`: a `/studio/{id}` route
    # would have to be declared before this mount and would shadow its assets.
    app.mount("/studio", StaticFiles(directory=DIST_DIR / "studio", html=True,
                                     check_dir=False), name="studio")
    app.mount("/api/characters", _RuntimeDispatcher(host, "api"), name="character-api")
    app.mount("/ws/characters", _RuntimeDispatcher(host, "ws"), name="character-ws")

    # Root compatibility APIs and shared assets continue to address the primary.
    class Primary:
        async def __call__(self, scope, receive, send):
            child = host.apps.get(host.primary_id or "")
            if child is None:
                # /ws/voice lands here on a single-character node, so this refusal
                # has to speak websocket too (see _turn_away).
                await _turn_away(scope, receive, send, "no active character",
                                 status=503)
                return
            child.state.server = getattr(app.state, "server", None)
            await child(scope, receive, send)

    app.mount("/", Primary(), name="primary-character")
    return app
