"""The host itself: the registry, the per-character config, the dispatcher.

`CharacterHost` owns the storage tree and one isolated runtime per character —
starting them, stopping them, retuning a running one onto new brain settings —
and `_RuntimeDispatcher` is what puts a request in front of the right one.

Everything above the class is the configuration arithmetic that answers one
question: given the house `.env` and this character's record, what `Config` does
she actually run on. The two paths are deliberately separate and must not be
conflated — `.env` is the house, `characters.json` is hers (SPEC §31.2).

The routes that used to live below all this are in the sibling modules, and
`app.py` composes them.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NamedTuple

from dotenv import dotenv_values

from fastapi import FastAPI, HTTPException
from fastapi.responses import (JSONResponse)

from yurios.characters import (
    CardLimits, CharacterRecord, CharacterRegistry,
    ConnectionProfile, ConnectionProfiles,
)
from yurios.characters import overrides as model_overrides
from yurios.characters import studio as studio_model
from yurios.characters.appearance import ensure_appearance
from yurios.characters.setting import ensure_setting
from yurios.characters.optimize import CardOptimizeError, optimize_draft
from yurios.mind.util import jsonl_page
from yurios.desktop.voice.ws_limits import VoiceConnectionLimiter
from yurios.security import OwnerBoundary


from .. import rewire
from ..config import Config
from ..inbox import Inbox
from ..main import create_app

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
    # pydantic-settings allows a sequence of files here; `dotenv_values` takes one,
    # and this project configures exactly one. Take the first rather than guess.
    configured = base.model_config.get("env_file") or ".env"
    env_file: str | Path
    if isinstance(configured, (str, Path)):
        env_file = configured
    else:
        env_file = next(iter(configured), ".env")
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
        "character_id": record.id,
        "vault_dir": record.paths.vault,
        "corpus_dir": record.paths.corpus,
        "trace_dir": record.paths.traces,
        "tool_log_dir": record.paths.tool_logs,
        "selfie_dir": record.paths.selfies,
        "upload_dir": record.paths.uploads,
        "selfie_character": str(record.paths.appearance),
        "selfie_templates": str(record.paths.selfie_templates),
        "mind_enabled": record.loops.mind,
        "utility_enabled": record.loops.utility,
        "dream_enabled": record.loops.dream,
        # Two switches in series, not one overriding the other (SPEC §18.4.6).
        # The house switch says whether anything on this machine may put a
        # notification on your desktop; hers says whether she is one of the ones
        # that may. A character can never talk her way past `NOTIFY_ENABLED`.
        "notify_enabled": base.notify_enabled and record.notify.enabled,
        # …and the same two-switches-in-series rule for her hands (SPEC §26, as
        # amended). The house switch says whether anything on this machine may
        # reach for a tool unasked; hers says whether she is one of the ones
        # that may. A character can never talk her way past MIND_TOOLS_ENABLED.
        #
        # Multiplied at the *point of use* (`Hands.enabled`) rather than here,
        # unlike `notify_enabled` above, because hers is a live switch: folded
        # into the config at boot, revoking it and granting it again inside one
        # process would leave the config saying no forever, and the grant is the
        # only half a running mind can be told about. `start()` seeds the grant
        # from her record before the first tick; this stays the house's word.
        "mind_tools_enabled": base.mind_tools_enabled,
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

    Her name (it is the runtime's `companion_name`), her voice, her body, the
    two loops that are wired rather than switched, and her doorbell — the
    NotifyChannel is built once by the channel manager at start, so a change here
    is invisible until she is rebuilt. Everything else a profile save
    can touch reaches her while she runs: her brain settings through `retune`,
    the mind switch through `set_mind_enabled`, and the card fields through the
    SOUL, which the brain re-reads on every turn (§5).

    Compared before and after the save rather than by which keys were *sent* —
    the switchboard's form posts every field on every save, and re-submitting the
    same voice is not a reason to take her conversation down."""
    return (record.display.name, record.voice.tts_backend, record.voice.stt_backend,
            record.voice.voice_id, record.body.backend, record.body.model,
            record.loops.utility, record.loops.dream, record.notify.enabled)


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
        # The one owner secret in this process (SPEC §32.4). Installed on the
        # host app, handed to every character app below it, so a token rotated
        # from the gear in her room is the token the host's boundary checks.
        self.owner_boundary: OwnerBoundary | None = None
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
                # The settings route edits the house .env, not this character's
                # registry-derived effective paths and backend overrides.
                app.state.house_config = self.base
                app.state.owner_boundary = self.owner_boundary
                app.state.rt.voice_ws_limiter = self.voice_ws_limiter
                # The second of the two switches, before the first tick can run
                # (§26.1). It lives on the runtime rather than in the config so
                # that the switchboard can revoke and restore it live.
                app.state.rt.set_hands_enabled(record.loops.hands)
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
                      "dream": record.loops.dream, "hands": record.loops.hands},
            # Her doorbell (SPEC §18.4.6). `available` is the house switch, sent
            # so the board can show the toggle as inert-with-a-reason instead of
            # letting you flip something that cannot ring: a switch that silently
            # does nothing is worse than one you can see is not connected.
            "notify": {"enabled": record.notify.enabled,
                       "available": self.base.notify_enabled},
            # …and the same shape for her hands (SPEC §26, as amended), for the
            # same reason: `available` is the house switch MIND_TOOLS_ENABLED,
            # and the tile shows hers inert-with-a-reason when the house has not
            # installed the capability at all.
            "hands": {"enabled": record.loops.hands,
                      "available": self.base.mind_tools_enabled},
            "model": record.models.chat or self.base.chat_model,
            "voice": record.voice.voice_id or record.voice.tts_backend or self.base.tts_backend,
            "connection_profile": record.connection.profile,
            "body_backend": record.body.backend,
            "body_model": record.body.model,
            "portrait_url": f"/api/characters/{record.id}/portrait",
            "context": rt.context.snapshot() if rt else None,
            "activity": activity,
            # what she is waiting to tell you (SPEC §32.5). Read from the live
            # runtime when there is one and from her Vault when there isn't —
            # the board lists offline characters too, and a reach-out made
            # before the last restart is exactly the one still worth showing.
            # (via getattr: a runtime mid-construction, or a stand-in, must not
            # be able to 500 the board over a badge)
            "unread": (getattr(rt, "inbox", None) or Inbox(record.paths.vault)).unread(),
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


