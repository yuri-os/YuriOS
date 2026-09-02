"""The switchboard's own API (SPEC §29, §32).

The house rather than any one companion: who exists, what state each is in,
the connection profiles they share, and the four things you can do to a
character from her tile — approve her, switch her loops, archive her, or
delete her for good. Her *settings* are here too, because the tile is where
you change them.

The two destructive ones are the reason this module is worth reading before
editing: `archive` and `purge` are the only routes in the host that can lose
something, and purge is deliberately a two-step with a challenge that expires.
"""
from __future__ import annotations

import asyncio
import copy
import datetime
import json
import logging
import os
import secrets
import shutil
import time
from collections.abc import Mapping

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (JSONResponse)

from yurios.characters import (
    CharacterCloneError,
    ConnectionProfile,
    clone_character,
)
from yurios.characters import archive as archive_model

from .hosting import (_OPTION_KEYS, PURGE_CHALLENGE_TTL_S,
                      CharacterHost, _env_values, _card_values, _construction_fingerprint,
                      _update_soul, save_brain_overrides)

log = logging.getLogger("world.host")


def register(app: FastAPI, host: CharacterHost, require) -> None:
    """Declare this module's routes on the host app.

    A plain closure rather than an `APIRouter` because these routes read the
    host and the registry out of the enclosing scope the way they always did,
    and rebinding them here keeps the bodies byte-identical to the single
    function they were extracted from. Declaration order is the order these
    `register` calls run in, which matters: every explicit route has to be on
    the app before `create_host_app` mounts the runtime dispatcher over
    `/api/characters`.
    """
    registry = host.registry
    base = host.base

    @app.get("/api/tray")
    async def tray_status():
        """Whether the icon is up, and if not, which of the four reasons it is.

        "I don't see the tray" is the only symptom this feature has, and it
        covers off-in-.env, no session bus, no dbus-fast and no tray host. The
        launcher prints this after start so that question is answered before it
        is asked.
        """
        tray = getattr(app.state, "tray", None)
        if tray is None:
            return {"state": "off",
                    "detail": "TRAY_ENABLED is false" if not base.tray_enabled
                              else "no session bus, or dbus-fast is not installed"}
        return tray.status()

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
            "dream": record.loops.dream, "hands": record.loops.hands,
            "notify": record.notify.enabled,
            # The house switch behind her hands, read-only here for the same
            # reason `notify_available` is: a toggle that quietly does nothing
            # is worse than one shown inert with the reason on it.
            "hands_available": base.mind_tools_enabled,
            # The house switch, read-only here: the form shows her toggle as
            # inert rather than pretending a character can turn on a doorbell
            # this node has not installed.
            "notify_available": base.notify_enabled,
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
        for key in ("mind", "utility", "dream", "hands"):
            if key in body:
                setattr(record.loops, key, bool(body[key]))
        if "notify" in body:
            record.notify.enabled = bool(body["notify"])
        if "enabled" in body:
            record.lifecycle.enabled = bool(body["enabled"])
        if "autostart" in body:
            record.lifecycle.autostart = bool(body["autostart"])
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
                await asyncio.to_thread(vaultgit.commit, record.paths.vault,
                                        "user: edit character card", now=True)
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
                if "hands" in body:
                    host.runtime(character_id).set_hands_enabled(record.loops.hands)
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

    @app.post("/api/characters/{character_id}/start")
    async def start_character(character_id: str):
        """Bring a reviewed, enabled character up without changing her review."""
        record = require(character_id)
        if record.lifecycle.review_required or not record.lifecycle.enabled:
            raise HTTPException(
                409, "character is disabled or still requires review")
        started, detail = True, None
        if host.runtime(character_id) is None:
            try:
                await host.start(character_id)
            except Exception as exc:                       # already logged by start()
                started, detail = False, str(exc)
                raise HTTPException(500, detail) from exc
        return {"character": host.summary(record), "started": started, "error": detail}

    @app.post("/api/characters/{character_id}/stop")
    async def stop_character(character_id: str):
        """Take her runtime down without archiving the tree."""
        record = require(character_id)
        await host.stop(character_id)
        return {"character": host.summary(record), "stopped": True}

    @app.post("/api/characters/{character_id}/clone")
    async def clone(character_id: str, request: Request):
        """Duplicate the whole companion under a new id (SPEC §36.3)."""
        require(character_id)
        body = await request.json() if await request.body() else {}
        if body and not isinstance(body, Mapping):
            raise HTTPException(400, "clone request must be a JSON object")
        body = body or {}
        async with app.state.lifecycle_lock:
            try:
                record = clone_character(
                    registry, character_id,
                    name=str(body.get("name") or "") or None,
                    character_id=str(body.get("character_id") or "") or None)
            except CharacterCloneError as exc:
                raise HTTPException(400, str(exc)) from exc
            started, detail = False, None
            if record.lifecycle.enabled and not record.lifecycle.review_required:
                try:
                    await host.start(record.id)
                    started = True
                except Exception as exc:                   # already logged by start()
                    started, detail = False, str(exc)
            return JSONResponse(
                {"character": host.summary(record), "started": started,
                 "error": detail},
                status_code=201)

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
        for key in ("mind", "utility", "dream", "hands"):
            if key in body:
                value = bool(body[key])
                # `hands` reaches a running mind live (`set_hands_enabled`), so
                # it is not a reason to take her conversation down — and it must
                # not be, because revoking it is the kill switch and a kill
                # switch that needs a restart is not one.
                restart = restart or (key not in ("mind", "hands")
                                      and value != getattr(record.loops, key))
                setattr(record.loops, key, value)
        # Her doorbell rides the same endpoint as the loop switches because it is
        # the same kind of thing to the person flipping it. It restarts her for
        # the same reason `utility`/`dream` do: the NotifyChannel is built once,
        # when the channel manager starts, so nothing else picks this up.
        if "notify" in body:
            value = bool(body["notify"])
            restart = restart or value != record.notify.enabled
            record.notify.enabled = value
        registry.upsert(record)
        if restart and host.runtime(character_id):
            await host.restart(character_id)
        else:
            rt = host.runtime(character_id)
            if rt is not None and "mind" in body:
                await rt.set_mind_enabled(record.loops.mind)
            if rt is not None and "hands" in body:
                rt.set_hands_enabled(record.loops.hands)
        return {"character": host.summary(record)}

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
            snapshot = archive_model.snapshot_payload(record, registry.data_root)
            try:
                os.replace(record.paths.root, target)
            except OSError as exc:
                if was_running:
                    await host.start(character_id)
                raise HTTPException(500, "could not move character into archive") from exc
            try:
                archive_model.write_snapshot(target, snapshot)
            except OSError:
                log.exception("could not write archive snapshot for %s", character_id)
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
            return {"archived": True, "id": character_id, "archive": target.name}

    @app.get("/api/archives")
    async def archives():
        """Every parked companion under `data/archives/` (SPEC §29.6, §36)."""
        return {"archives": archive_model.list_archives(registry.data_root)}

    @app.post("/api/archives/{name}/restore")
    async def restore_archive(name: str, request: Request):
        """Move an archived tree back onto the board (SPEC §29.6)."""
        parsed = archive_model.parse_archive_name(name)
        if parsed is None or "/" in name or "\\" in name or name in (".", ".."):
            raise HTTPException(400, "not an archive name")
        source = (registry.data_root / "archives" / name).resolve()
        archives_root = (registry.data_root / "archives").resolve()
        try:
            source.relative_to(archives_root)
        except ValueError:
            raise HTTPException(400, "not an archive name") from None
        if not source.is_dir():
            raise HTTPException(404, f"no archive called {name}")
        body = await request.json() if await request.body() else {}
        if body and not isinstance(body, Mapping):
            raise HTTPException(400, "restore request must be a JSON object")
        body = body or {}
        snapshot = archive_model.read_snapshot(source)
        default_id = parsed[0]
        if snapshot and isinstance(snapshot.get("record"), Mapping):
            default_id = str(snapshot["record"].get("id") or default_id)
        character_id = str(body.get("id") or default_id).strip() or default_id
        start = bool(body.get("start"))
        dest = registry.data_root / "characters" / character_id
        async with app.state.lifecycle_lock:
            if registry.get(character_id) is not None or dest.exists():
                raise HTTPException(409, f"character already exists: {character_id}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(source, dest)
            except OSError as exc:
                raise HTTPException(500, "could not move archive back onto the board") \
                    from exc
            try:
                if snapshot is not None:
                    record = archive_model.record_from_snapshot(
                        snapshot, character_id=character_id, dest_root=dest,
                        data_root=registry.data_root)
                else:
                    record = archive_model.record_from_tree(
                        dest, character_id, dest)
                registry.add(record)
            except Exception as exc:
                try:
                    os.replace(dest, source)
                except OSError as rollback:
                    log.exception("unarchive rollback failed for %s", name)
                    raise HTTPException(
                        500, "unarchive registry update failed and data rollback failed") \
                        from rollback
                raise HTTPException(
                    500, "unarchive registry update failed; archive restored") from exc
            started, detail = False, None
            if start:
                record.lifecycle.review_required = False
                record.lifecycle.enabled = True
                record.lifecycle.autostart = True
                registry.upsert(record)
                try:
                    await host.start(record.id)
                    started = True
                except Exception as exc:                   # already logged by start()
                    started, detail = False, str(exc)
            return {"character": host.summary(record), "started": started,
                    "error": detail}

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
