"""Which model she thinks with, house-wide and per character (SPEC §31.2).

Two configuration paths meet here and must not be confused: the house `.env`,
which `/api/brain` edits, and the per-character overrides in
`characters.json`, which `/api/characters/{id}/brain` edits and which the host
can apply to a *running* character without a restart.

`/api/brain` addresses the primary character, and says so with a 409 rather
than guessing when there is not one.
"""
from __future__ import annotations

import copy
import logging
from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from yurios.characters import (
    CharacterRecord,
)
from yurios.characters import overrides as model_overrides

from .. import rewire
from .hosting import (CharacterHost, _env_values, brain_overrides,
                      save_brain_overrides)

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
