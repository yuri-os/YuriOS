"""GET /api/config — the runtime config the browser needs before it mounts her
body (SPEC §6, §8).

Right now that's just which Live2D rig to wear: AVATAR_MODEL (.env) names a key
in the registry (desktop/avatar_models.py); here we resolve it to the model3.json
URL the page fetches, and confirm the files are actually installed under
web/vendor/ (scripts/fetch_avatar.py puts them there). If the chosen model isn't
present — a typo, or it hasn't been fetched yet — we fall back to the default so
the page still gets *a* body instead of a blank stage.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request

from ..avatar_models import DEFAULT, MODELS

router = APIRouter()
WEB_DIR = Path(__file__).resolve().parent.parent.parent.parent / "web"


def _resolve(key: str) -> tuple[str, str]:
    """(requested key) → (installed key, model3 URL), falling back to the default."""
    key = (key or DEFAULT).strip().lower()
    rel = MODELS.get(key)
    if rel and (WEB_DIR / rel).exists():
        return key, rel
    return DEFAULT, MODELS[DEFAULT]


@router.get("/api/config")
async def config(request: Request):
    cfg = request.app.state.rt.cfg
    key, url = _resolve(getattr(cfg, "avatar_model", DEFAULT))
    return {
        "avatar_model": key,             # the rig actually being served (post-fallback)
        "avatar_model_url": url,         # what avatar.js mounts
        # which rigs are fetched and ready — handy for a future picker in the UI
        "avatar_available": [k for k, v in MODELS.items() if (WEB_DIR / v).exists()],
    }
