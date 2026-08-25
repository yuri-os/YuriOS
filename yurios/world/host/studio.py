"""The card studio: writing a companion, and what she looks like (SPEC §30).

Import a card, edit the draft, derive her setting, choose her face, curate the
scenes her camera may compose, and export the whole of her back out as a PNG
somebody else can boot. One surface, because in the studio they are one job.

Two rules run through all of it. A card that did not come from YuriOS stays
disabled until it has been read (`_refused` is the shape of that refusal), and
what leaves in an export is filtered by `characters/privacy.py` — your name,
her memory of you and her journal stay on the machine.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import (FileResponse, JSONResponse, Response, StreamingResponse)

from yurios.characters import (
    CharacterImporter,
)
from yurios.characters import selfiebook
from yurios.characters import studio as studio_model
from yurios.characters import setting as setting_model
from yurios.characters.creator import create_character, template_draft
from yurios.characters.exporter import ExportOptions, build_export, preview_export
from yurios.characters.optimize import CardOptimizeError, optimize_draft
from yurios.characters.privacy import CardExportError
from yurios.app.providers.catalog import provider_models

from .hosting import (_IMAGE_LIMITS, MAX_OPTIMIZER_DRAFT_BYTES,
                      MAX_OPTIMIZER_INSTRUCTIONS, CharacterHost, _image_field, _images,
                      _optimize_events, _sync_card_json)

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
