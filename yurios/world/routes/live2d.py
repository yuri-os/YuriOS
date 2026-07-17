"""GET /api/config — the rig registry for the Live2D client (SPEC §6.6).

Build #2's whole web client is under web/live2d/ and served as-is; its
avatar.js asks `/api/config` which Live2D rig to wear (AVATAR_MODEL in .env,
resolved through the registry `desktop/avatar_models.py` — called, not
copied). This is B2's `desktop/routes/avatar.py` re-aimed at one path: that
route checks installs under B2's `web/`, but here the client — and its fetched
`vendor/` runtime — live under `web/live2d/`, so the existence probe must look
there. URLs stay relative ("vendor/<key>/…"), which the page resolves against
its own /live2d/ base; same fallback rule as B2 — a typo or an unfetched rig
gets the default body, never a blank stage.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request

from yurios.desktop.avatar_models import DEFAULT, MODELS

router = APIRouter()
LIVE2D_DIR = Path(__file__).resolve().parent.parent.parent.parent / "web" / "live2d"


def _resolve(key: str) -> tuple[str, str]:
    """(requested key) → (installed key, model3 URL), falling back to the default."""
    key = (key or DEFAULT).strip().lower()
    rel = MODELS.get(key)
    if rel and (LIVE2D_DIR / rel).exists():
        return key, rel
    return DEFAULT, MODELS[DEFAULT]


@router.get("/api/config")
async def config(request: Request):
    cfg = request.app.state.rt.cfg
    key, url = _resolve(getattr(cfg, "avatar_model", DEFAULT))
    return {
        "avatar_model": key,             # the rig actually being served (post-fallback)
        "avatar_model_url": url,         # what avatar.js mounts (relative to /live2d/)
        "avatar_available": [k for k, v in MODELS.items() if (LIVE2D_DIR / v).exists()],
    }
