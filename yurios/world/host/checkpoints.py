"""House settings' Checkpoints page: Civitai, for her local camera (SPEC §11.5).

`forge/civitai.py` knows Civitai and the three checkpoint families; this is the
page's side of it — where the key and the folder come from, and the routes:

- `GET  /api/house/checkpoints` — the cached catalog, what is in the folder
  (each file's verdict), the downloads in flight, and which file her camera is
  set to. Never fetches: a catalog that is missing or a day old says so, and
  the page asks for a refresh, so opening the page is never fourteen API calls.
- `POST /api/house/checkpoints/refresh` — list Civitai again and cache it,
  keeping what earlier searches found.
- `POST /api/house/checkpoints/search` — ask Civitai for what the cached lists
  don't carry, and fold the answer into the cache.
- `PUT  /api/house/civitai-key` — `CIVITAI_API_KEY` into `.env` (null removes it).
- `GET  /api/house/checkpoints/downloads` — progress, which the page polls while
  something is downloading. A poll and not the `EventHub`: the hub is a
  character's, a download belongs to the house, and the boot panel set the
  precedent for a house page with no stream of its own (world/boot.py).
- `POST /api/house/checkpoints/downloads` — fetch one file of a listed version:
  the recommended one, or another of its variants by name. The file, size and
  address come from the cached catalog, never from the request.
- `DELETE /api/house/checkpoints/downloads/{file}` — stop one and drop its
  partial file.
- `POST /api/house/checkpoints/use` — check one file's header and point
  `SELFIE_LOCAL_MODEL` at it, and `SELFIE_BACKEND` at `diffusers` (which routes
  by header) when it named something that can't render it.

The key and the folder are read from `.env` on every call, like the other house
files (house.py `_configured`), so a key saved here works at once and the page
never has to wait for a restart to download. Owner-or-loopback throughout: these
write `.env`, spend a stranger's bandwidth and fill a disk.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request

from yurios import envfile
from yurios.forge import civitai
from yurios.forge.backends.sniff import verify_local_checkpoint

from .hosting import CharacterHost
from .house import _configured, _env_path, _require_local, _resolve

DEFAULT_FOLDER = "models/checkpoints"
CATALOG_FILE = "civitai/catalog.json"
LOCAL_BACKENDS = ("diffusers", "krea2", "anima")


def register(app: FastAPI, host: CharacterHost, require) -> None:  # noqa: ARG001
    base = host.base
    store = civitai.CatalogStore(Path(base.data_dir) / CATALOG_FILE)
    downloads = civitai.Downloads()
    app.state.checkpoint_downloads = downloads
    refreshing = asyncio.Lock()

    def api_key() -> str:
        return _configured("CIVITAI_API_KEY", getattr(base, "civitai_api_key", ""))

    def current() -> str:
        return _configured("SELFIE_LOCAL_MODEL", getattr(base, "selfie_local_model", ""))

    def folder() -> Path:
        raw = _configured("SELFIE_CHECKPOINT_DIR", getattr(base, "selfie_checkpoint_dir", ""))
        if raw:
            return _resolve(raw)
        model = current()
        return _resolve(model).parent if model else _resolve(DEFAULT_FOLDER)

    def catalog_view(catalog: dict | None) -> dict | None:
        if catalog is None:
            return None
        age = time.time() - float(catalog.get("fetched_at") or 0)
        return {**catalog, "stale": age > civitai.CATALOG_TTL_S
                or catalog.get("schema") != civitai.CATALOG_SCHEMA}

    @app.get("/api/house/checkpoints")
    async def checkpoints(request: Request):
        _require_local(request)
        where = folder()
        model = current()
        local = await asyncio.to_thread(civitai.scan_folder, where)
        running = str(getattr(base, "selfie_local_model", "") or "")
        return {
            "key_configured": bool(api_key()),
            "folder": str(where),
            "current": {"path": model, "file": Path(model).name if model else "",
                        "in_folder": bool(model) and _resolve(model).parent == where,
                        "pending": model != running},
            "backend": _configured("SELFIE_BACKEND", getattr(base, "selfie_backend", "off")),
            "catalog": catalog_view(await asyncio.to_thread(store.load)),
            "local": local,
            "downloads": downloads.snapshot(),
            "families": [*(f.label for f in civitai.FAMILIES), civitai.OTHER_FAMILY],
        }

    @app.post("/api/house/checkpoints/refresh")
    async def refresh(request: Request):
        _require_local(request)
        async with refreshing:                  # a second press waits for the first
            try:
                catalog = await civitai.fetch_catalog(api_key())
            except civitai.CatalogError as exc:
                raise HTTPException(502, str(exc)) from exc
            catalog = civitai.carry_searches(await asyncio.to_thread(store.load), catalog)
            await asyncio.to_thread(store.save, catalog)
        return catalog_view(catalog)

    @app.post("/api/house/checkpoints/search")
    async def search(request: Request):
        _require_local(request)
        payload = await request.json()
        query = civitai.normalize_query((payload or {}).get("query"))
        if len(query) < civitai.QUERY_MIN:
            raise HTTPException(422, "type at least two letters to search Civitai")
        try:
            found = await civitai.search(query, api_key())
        except civitai.CatalogError as exc:
            raise HTTPException(502, str(exc)) from exc
        async with refreshing:                  # one writer of the cache at a time
            catalog, added = civitai.merge_search(await asyncio.to_thread(store.load),
                                                  found, query)
            await asyncio.to_thread(store.save, catalog)
        return {"query": query, "found": len(found), "added": added,
                "catalog": catalog_view(catalog)}

    @app.put("/api/house/civitai-key")
    async def save_key(request: Request):
        _require_local(request)
        payload = await request.json()
        key = (payload or {}).get("key")
        if key is not None:
            key = str(key).strip()
            if not key or any(c.isspace() for c in key):
                raise HTTPException(422, "paste the key on its own, with no spaces")
        try:
            envfile.apply(base, {"CIVITAI_API_KEY": key}, path=_env_path())
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"key_configured": bool(api_key())}

    @app.get("/api/house/checkpoints/downloads")
    async def progress(request: Request):
        _require_local(request)
        return {"downloads": downloads.snapshot()}

    @app.post("/api/house/checkpoints/downloads")
    async def download(request: Request):
        _require_local(request)
        payload = await request.json()
        try:
            version_id = int((payload or {}).get("version_id") or "")
        except (TypeError, ValueError):
            raise HTTPException(422, "version_id must be a Civitai model version id") from None
        catalog = await asyncio.to_thread(store.load) or {}
        card = next((m for m in catalog.get("models", [])
                     if m.get("version_id") == version_id), None)
        if card is None:
            raise HTTPException(404, "that version is not in the cached list — refresh it")
        variants = civitai.variants_of(card)
        want = (payload or {}).get("file") or variants[0]["file"]
        variant = next((v for v in variants if v["file"] == want), None)
        if variant is None:
            raise HTTPException(404, f"{want} is not one of that version's files")
        if not variant.get("loads"):
            raise HTTPException(422, "this build can't load that file yet")
        key = api_key()
        if not key:
            raise HTTPException(409, "add your Civitai API key first")
        fetch = {**card, "file": variant["file"], "size_bytes": variant["size_bytes"],
                 "download_url": variant["download_url"]}
        try:
            job = downloads.start(fetch, folder(), key)
        except FileExistsError:
            raise HTTPException(409, f"{card['file']} is already in the folder") from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return job.as_dict()

    @app.delete("/api/house/checkpoints/downloads/{file}")
    async def cancel(file: str, request: Request):
        _require_local(request)
        if not downloads.cancel(file):
            raise HTTPException(404, f"{file} isn't downloading")
        return {"cancelled": file}

    @app.post("/api/house/checkpoints/use")
    async def use(request: Request):
        _require_local(request)
        payload = await request.json()
        name = civitai.safe_name((payload or {}).get("file", ""))
        if not name:
            raise HTTPException(422, "name a .safetensors file in the checkpoint folder")
        path = folder() / name
        if not path.is_file():
            raise HTTPException(404, f"{name} is not in {path.parent}")
        verdict = await asyncio.to_thread(verify_local_checkpoint, path)
        if not verdict.usable:
            raise HTTPException(422, f"{name}: {verdict.note}")
        updates = {"SELFIE_LOCAL_MODEL": str(path)}
        backend = _configured("SELFIE_BACKEND", getattr(base, "selfie_backend", "off"))
        named = {"sdxl": "diffusers"}.get(verdict.architecture, verdict.architecture)
        if backend not in LOCAL_BACKENDS or (backend != "diffusers" and backend != named):
            updates["SELFIE_BACKEND"] = "diffusers"   # routes by the file's header
        try:
            written, _ = envfile.apply(base, updates, path=_env_path())
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"file": name, "path": str(path), "written": written,
                "verdict": {"architecture": verdict.architecture,
                            "status": verdict.status, "note": verdict.note},
                "restart_required": True}
