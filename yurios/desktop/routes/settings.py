"""The settings API — the `.env` panel and the pairing dialog (SPEC §11, §11.1).

The whole config is read once at boot from `.env` (pydantic-settings, see
desktop/config.py + app/config.py), so this endpoint edits *that file* and the
change takes effect on the next restart — it does not hot-reload a running model
into VRAM. The UI (web/shared/settings.js) says so out loud after a save.

The table it renders is not here: `yurios/envfile.py` owns it, because
`yurios settings` on the command line edits the same file and the two must not
be able to disagree about what a knob is called or what it may hold. This module
is the HTTP surface over that table — who may call it, what a save returns, and
the one knob that is more than a line in a file.

That knob is `OWNER_TOKEN`. It is the only setting whose *value* is a thing you
have to get onto another device, so it has an affordance rather than a text box:
`POST /api/pairing/token` generates one, writes it, and applies it to the running
boundary immediately (`security.OwnerBoundary`), and `GET /api/pairing` draws the
QR codes a phone can scan to come in without it ever being typed. Rotating it
this way logs every other session out, including — unless we re-issue it here —
the one that asked, so the response carries a fresh cookie for the caller.

Secret fields are write-only everywhere else: the API reports whether one is
configured but never returns its value. Blank submissions preserve it and JSON
null removes it. The pairing routes are the deliberate exception, and they are
the exception because their entire job is to hand the secret over — to a caller
that is already either on the loopback interface or holding that same token.
"""
from __future__ import annotations

from pathlib import Path

from dotenv import dotenv_values
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from yurios import envfile, pairing
from yurios.app.providers.catalog import provider_models
from yurios.security import (boundary_for, external_request_origin,
                             generate_owner_token, issue_session_cookie,
                             is_loopback, owner_or_loopback)

router = APIRouter()

# Re-exported: `.env` sits at the installation root, and the tests point this at
# a temporary file rather than at the developer's own configuration.
ENV_PATH: Path = envfile.ENV_PATH
# The hand-written half of the table, kept under its old name for the callers
# (and tests) that reach for it.
SCHEMA = envfile.CURATED
_groups_for = envfile.groups_for


def _require_local(request: Request) -> None:
    """Allow local management or a request authenticated by the owner boundary."""
    if not owner_or_loopback(request):
        raise HTTPException(status_code=403, detail="owner authentication required")


def _config(request: Request):
    """The Config this app edits.

    A hosted character runtime carries registry-derived paths and overrides.
    Those are useful runtime facts but they are not values in the house `.env`
    this route edits, so the host gives every child its original house Config.
    Standalone builds have no such split and use their runtime Config directly.
    """
    runtime = getattr(request.app.state, "rt", None)
    if runtime is not None:
        return getattr(request.app.state, "house_config", runtime.cfg)
    host = getattr(request.app.state, "host", None)
    if host is not None:
        return host.base
    raise HTTPException(status_code=503, detail="no configuration behind this app")


def _key_config(request: Request, cfg):
    """Config that names character-scoped `.env` keys such as Telegram's."""
    runtime = getattr(request.app.state, "rt", None)
    return runtime.cfg if runtime is not None else cfg


def _stored_values() -> dict[str, str]:
    try:
        return {str(key): str(value) for key, value in dotenv_values(ENV_PATH).items()
                if key is not None and value is not None}
    except OSError:
        return {}


def _display(field: dict, cfg, key_cfg, stored: dict[str, str]) -> object:
    """The value the panel will write, rather than a character's effective one."""
    if field["key"] not in stored:
        source = key_cfg if field.get("key_env") else cfg
        return envfile.display(field, source)
    raw = stored[field["key"]]
    if field["type"] == "bool":
        return raw.strip().lower() in ("true", "1", "yes", "on")
    if field["type"] == "number":
        source = key_cfg if field.get("key_env") else cfg
        current = getattr(source, field["attr"], 0)
        try:
            return float(raw) if isinstance(current, float) else int(raw)
        except ValueError:
            return raw
    return raw


def _pairing(request: Request, cfg) -> dict:
    """The pairing view, built from the token the server will actually accept.

    Not from `cfg`: that is a boot-time snapshot, and the whole point of the
    boundary is that the token can move underneath it. A QR drawn from the stale
    value would be a code that scans cleanly and then refuses you at the door.
    `live` is the other half of the same fact — whether `.env` still says what
    the boundary is honouring, which is what a hand-edited file or a
    `yurios settings` run while she is up would break.
    """
    boundary = boundary_for(request.app)
    token = boundary.token if boundary is not None else str(getattr(cfg, "owner_token", "") or "")
    try:
        stored = str(dotenv_values(ENV_PATH).get("OWNER_TOKEN") or "")
    except OSError:
        stored = token
    request_origin = external_request_origin(
        dict(request.headers), str(getattr(cfg, "host", "") or ""),
        request.url.scheme)
    peer = request.client.host if request.client else ""
    if (is_loopback(str(getattr(cfg, "host", "") or "")) and is_loopback(peer)
            and not request.headers.get("x-forwarded-for")):
        request_origin = ""
    described = pairing.describe(cfg, token, live=token == stored,
                                 request_origin=request_origin)
    described["env_path"] = str(ENV_PATH)
    # The token in the clear, like the links it is already inside. This is the
    # one place that does that, and it does it for the caller that is either on
    # the loopback interface or holding this very value.
    described["token"] = token
    return described


@router.get("/api/models")
async def list_models(request: Request, provider: str = ""):
    """The models a provider can actually serve right now, for the settings panel's
    model picker. The listing itself lives in `app/providers/catalog.py` because
    the studio's optimize dialog asks the same question of the same servers."""
    _require_local(request)
    cfg = _config(request)
    return await provider_models(_key_config(request, cfg), provider)


@router.get("/api/settings")
async def get_settings(request: Request):
    _require_local(request)
    cfg = _config(request)
    key_cfg = _key_config(request, cfg)
    stored = _stored_values()
    return {
        "env_path": str(ENV_PATH),
        "groups": [
            {"group": g["group"], "advanced": bool(g.get("advanced")),
             "fields": [
                 ({**{k: v for k, v in f.items() if k not in ("attr", "key_env")},
                    "configured": bool(_display(f, cfg, key_cfg, stored))}
                  if f["type"] == "password" else
                  {**{k: v for k, v in f.items() if k not in ("attr", "key_env")},
                    "value": _display(f, cfg, key_cfg, stored)})
                 for f in g["fields"]]}
            for g in _groups_for(cfg, key_cfg=key_cfg)
        ],
    }


@router.post("/api/settings")
async def post_settings(request: Request):
    _require_local(request)
    payload = await request.json()
    cfg = _config(request)
    key_cfg = _key_config(request, cfg)
    try:
        written, ignored = envfile.apply(
            cfg, payload or {}, path=ENV_PATH, key_cfg=key_cfg)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    body = {"ok": True, "written": written, "ignored": ignored,
            "restart_required": bool(written), "env_path": str(ENV_PATH)}
    return _rotated(request, written, payload, body)


def _rotated(request: Request, written: list[str], payload: dict, body: dict):
    """Apply an OWNER_TOKEN written through the ordinary settings form.

    Typing the token by hand is still allowed — the field is there — and it has
    to behave the same as generating one, or the panel would have two owner
    tokens with two different meanings.
    """
    boundary = boundary_for(request.app)
    if boundary is None or "OWNER_TOKEN" not in written:
        return body
    raw = payload.get("OWNER_TOKEN")                # JSON null = remove it
    try:
        boundary.set("" if raw is None else str(raw))
    except ValueError as exc:                       # envfile.check catches this first
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    body["owner_token_applied"] = True
    body["restart_required"] = bool([k for k in written if k != "OWNER_TOKEN"])
    return _keep_caller_signed_in(request, JSONResponse(body), boundary)


@router.get("/api/pairing")
async def get_pairing(request: Request):
    """The owner token as a set of scannable links, one per address that might work."""
    _require_local(request)
    return _pairing(request, _config(request))


@router.post("/api/pairing/token")
async def rotate_pairing_token(request: Request):
    """Generate an owner token, save it, and start honouring it now.

    The generator is the only sane way to produce this value, so it is a button
    rather than advice in a help string. Rotation revokes every session opened
    under the old token — which is also how you throw a device out.
    """
    _require_local(request)
    cfg = _config(request)
    key_cfg = _key_config(request, cfg)
    token = generate_owner_token()
    try:
        envfile.apply(cfg, {"OWNER_TOKEN": token}, path=ENV_PATH,
                      key_cfg=key_cfg)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    boundary = boundary_for(request.app)
    if boundary is not None:
        boundary.set(token)
    described = _pairing(request, cfg)

    return _keep_caller_signed_in(request, JSONResponse(described), boundary)


def _keep_caller_signed_in(request: Request, response, boundary):
    """Re-issue the session for whoever just rotated the token.

    Only for a caller that had one: a loopback request needs no cookie, and
    handing it one would be inventing a session nobody asked for.
    """
    if boundary is not None and boundary.token and \
            getattr(request.state, "owner_authenticated", False):
        issue_session_cookie(response, boundary,
                             https=request.url.scheme == "https")
    return response
