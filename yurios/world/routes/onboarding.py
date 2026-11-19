"""First-run language-model selection for an intentionally offline install.

A fresh install selects no model (SPEC §11.3.4), and the first page you open
asks for one. Everything `yurios configure` asks in the terminal can be
answered here: a GGUF to download, or a model on LM Studio, Ollama, OpenRouter
or another OpenAI-compatible server, with that server's address and key. The
`.env` lines a choice writes come from `models.provider_connection`, the same
function the CLI uses. Starting her on the choice is the panel's job, through
the house restart (§11.3.1); this route only saves it and fetches the GGUF.
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from yurios.app.providers.catalog import PROVIDERS, provider_models
from yurios.desktop.routes.settings import (ENV_PATH, _as_saved, _config,
                                            _require_local)
from yurios.models import (NONE, PROVIDER_PREFIXES, PROVIDER_SETTINGS,
                           RECOMMENDED_MODELS, download_gguf,
                           gguf_connection_defaults, huggingface_gguf_model,
                           normalize_model, provider_connection, provider_model,
                           save_model_choice, validate_model)

router = APIRouter()

#: The panel's ways to connect, in the order it offers them. `gguf` is a
#: Hugging Face repository YuriOS downloads and runs itself; `custom` is any
#: LiteLLM id, written verbatim.
_LABELS = {pid: label for pid, label, _ in PROVIDERS}
CHOICES = ("gguf", "lmstudio", "ollama", "openrouter", "openai_compatible", "custom")


def _state(request: Request) -> dict:
    state = getattr(request.app.state, "model_setup", None)
    if state is None:
        state = {"state": "idle", "detail": "", "model": ""}
        request.app.state.model_setup = state
    return state


def _house(request: Request):
    """The house config as `.env` holds it now, not as this process booted."""
    house = _config(request)
    return _as_saved(house, house)


def _providers(cfg) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for pid in CHOICES:
        row: dict[str, Any] = {"id": pid, "label": {
            "gguf": "Download a GGUF (runs inside YuriOS)",
            "custom": "Another LiteLLM model id"}.get(pid, _LABELS.get(pid, pid))}
        if pid in PROVIDER_SETTINGS:
            (url_env, url_attr), (key_env, key_attr) = PROVIDER_SETTINGS[pid]
            row["prefix"] = PROVIDER_PREFIXES[pid]
            if url_env:
                row["base_url"] = str(getattr(cfg, url_attr, "") or "")
            if key_env:
                row["key_saved"] = bool(getattr(cfg, key_attr, ""))
        rows.append(row)
    return rows


def _connect(request: Request, payload: dict) -> tuple[str, dict[str, str], Any]:
    """(model route, `.env` connection lines, config that reads them) for a
    panel submission. Raises 400 for one that names no usable model."""
    provider = str(payload.get("provider") or "").strip()
    model = str(payload.get("model") or "").strip()
    cfg = _house(request)
    if provider and provider not in CHOICES:
        raise HTTPException(400, f"unknown provider: {provider}")
    if provider in PROVIDER_SETTINGS:
        if not model:
            raise HTTPException(400, "a model id is required")
        # A blank key field means "keep the saved one", as in the terminal.
        key = payload.get("api_key")
        try:
            connection, cfg = provider_connection(
                cfg, provider, base_url=str(payload.get("base_url") or ""),
                api_key=str(key) if key else None)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return provider_model(provider, model), connection, cfg
    if provider == "gguf":
        model = huggingface_gguf_model(model)
        if not model:
            raise HTTPException(400, "a Hugging Face repository id is required")
    return normalize_model(model), {}, cfg


@router.get("/api/onboarding")
async def onboarding(request: Request):
    _require_local(request)
    state = _state(request)
    return {
        "configured": request.app.state.rt.model_configured,
        "model": request.app.state.rt.cfg.chat_model,
        "recommendations": RECOMMENDED_MODELS,
        "providers": _providers(_house(request)),
        "download": dict(state),
    }


@router.post("/api/onboarding/models")
async def onboarding_models(request: Request):
    """What a server offers, reached with the address and key typed into the
    panel — before anything is saved, so a wrong address costs nothing."""
    _require_local(request)
    payload = await request.json() or {}
    provider = str(payload.get("provider") or "").strip()
    if provider not in PROVIDER_SETTINGS:
        return {"models": [], "error": "nothing to list for this choice — type the id"}
    key = payload.get("api_key")
    try:
        _, cfg = provider_connection(_house(request), provider,
                                     base_url=str(payload.get("base_url") or ""),
                                     api_key=str(key) if key else None)
    except ValueError as exc:
        return {"models": [], "error": str(exc)}
    return await provider_models(cfg, provider)


@router.post("/api/onboarding")
async def choose_model(request: Request):
    _require_local(request)
    payload = await request.json() or {}
    model, connection, cfg = _connect(request, payload)
    state = _state(request)
    if state["state"] == "downloading":
        raise HTTPException(409, f"still downloading {state['model']} — wait for it to finish")
    # A probe is an HTTP round trip with a timeout; off the loop, because every
    # character's room shares it.
    check = await asyncio.to_thread(validate_model, cfg, model)
    if not check.ok:
        raise HTTPException(status_code=400, detail=check.detail)
    # A GGUF id says nothing about the card it has to run on, so the same
    # conservative offload/context profile the CLI writes is written here too
    # (`yurios/cli.py`'s configure). Without it the browser's first-run panel
    # saves a model that then runs entirely on the CPU at the fallback window,
    # while the identical choice made from the terminal gets the GPU — the kind
    # of divergence that surfaces only as "why is she so slow".
    if model.startswith("gguf/"):
        connection.update(gguf_connection_defaults())
    save_model_choice(ENV_PATH, model, connection=connection)
    if model == NONE:
        state.update(state="idle", detail="No language model selected", model=model)
    elif not model.startswith("gguf/"):
        state.update(state="ready", detail=check.detail, model=model)
    else:
        state.update(state="downloading", detail=f"Downloading {model}…", model=model)

        def download() -> None:
            try:
                path = download_gguf(cfg, model)
            except Exception as exc:  # network/auth/file errors are surfaced to the poller
                state.update(state="failed", detail=str(exc))
            else:
                state.update(state="ready", detail=f"Ready: {path}")

        threading.Thread(target=download, name="yurios-model-download", daemon=True).start()
    return {"ok": True, "model": model, "state": state["state"],
            "detail": state["detail"], "restart_required": True}
