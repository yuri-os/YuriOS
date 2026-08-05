"""First-run language-model selection for an intentionally offline install."""
from __future__ import annotations

import threading

from fastapi import APIRouter, HTTPException, Request

from yurios.models import (NONE, RECOMMENDED_MODELS, download_gguf, is_configured,
                           normalize_model, save_model_choice, validate_model)
from yurios.desktop.routes.settings import ENV_PATH, _require_local

router = APIRouter()


def _state(request: Request) -> dict:
    state = getattr(request.app.state, "model_setup", None)
    if state is None:
        state = {"state": "idle", "detail": ""}
        request.app.state.model_setup = state
    return state


@router.get("/api/onboarding")
async def onboarding(request: Request):
    _require_local(request)
    cfg = request.app.state.rt.cfg
    state = _state(request)
    return {
        "configured": request.app.state.rt.model_configured,
        "model": cfg.chat_model,
        "recommendations": RECOMMENDED_MODELS,
        "download": dict(state),
    }


@router.post("/api/onboarding")
async def choose_model(request: Request):
    _require_local(request)
    payload = await request.json()
    model = normalize_model(str((payload or {}).get("model", "")))
    cfg = request.app.state.rt.cfg
    check = validate_model(cfg, model)
    if not check.ok:
        raise HTTPException(status_code=400, detail=check.detail)
    save_model_choice(ENV_PATH, model)
    state = _state(request)
    if model == NONE:
        state.update(state="idle", detail="No language model selected")
        return {"ok": True, "detail": state["detail"], "restart_required": True}
    if not model.startswith("gguf/"):
        state.update(state="ready", detail=check.detail)
        return {"ok": True, "detail": check.detail, "restart_required": True}

    state.update(state="downloading", detail="Downloading GGUF model…")

    def download() -> None:
        try:
            path = download_gguf(cfg, model)
        except Exception as exc:  # network/auth/file errors are surfaced to the poller
            state.update(state="failed", detail=str(exc))
        else:
            state.update(state="ready", detail=f"Ready: {path}")

    threading.Thread(target=download, name="yurios-model-download", daemon=True).start()
    return {"ok": True, "detail": state["detail"], "restart_required": True}
