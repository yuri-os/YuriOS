"""Single-owner access boundary for YuriOS HTTP and WebSocket surfaces."""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import html
import ipaddress
import json
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, quote, urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse


COOKIE_NAME = "yurios_owner"
MIN_TOKEN_LENGTH = 32
MAX_HTTP_BODY_BYTES = 48 * 1024 * 1024
_SESSION_LABEL = b"yurios-owner-session-v1"


def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    # Starlette's in-process TestClient has no socket address and uses this
    # sentinel. A network server always supplies an IP address here.
    if host == "testclient":
        return True
    if host.lower().rstrip(".") == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _session_value(owner_token: str) -> str:
    return hmac.new(owner_token.encode("utf-8"), _SESSION_LABEL,
                    hashlib.sha256).hexdigest()


def _safe_next(value: str | None) -> str:
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return "/"


def _origin_allowed(origin: str, host_header: str, local_only: bool) -> bool:
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    if local_only:
        # This also closes DNS rebinding: a browser page on an attacker hostname
        # is rejected even after that hostname resolves to 127.0.0.1.
        return _is_loopback(parsed.hostname)
    return parsed.netloc.lower().rstrip(".") == host_header.lower().rstrip(".")


def _headers(scope) -> dict[str, str]:
    return {key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])}


def _cookie_value(raw: str, name: str) -> str:
    try:
        cookies = SimpleCookie(raw)
        morsel = cookies.get(name)
        return morsel.value if morsel else ""
    except Exception:
        return ""


class _RequestBodyTooLarge(Exception):
    pass


class HTTPBodyLimitMiddleware:
    """Reject oversized HTTP bodies without buffering or wrapping responses."""

    def __init__(self, app, *, maximum: int = MAX_HTTP_BODY_BYTES):
        self.app = app
        self.maximum = max(1, int(maximum))

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        lengths = [value.decode("latin-1").strip()
                   for key, value in scope.get("headers", [])
                   if key.lower() == b"content-length"]
        if lengths:
            try:
                parsed = [int(value) for value in lengths]
            except ValueError:
                response = JSONResponse({"detail": "invalid Content-Length"},
                                        status_code=400)
                await response(scope, receive, send)
                return
            if any(value < 0 for value in parsed) or len(set(parsed)) != 1:
                response = JSONResponse({"detail": "invalid Content-Length"},
                                        status_code=400)
                await response(scope, receive, send)
                return
            if parsed[0] > self.maximum:
                response = JSONResponse({"detail": "request body too large"},
                                        status_code=413)
                await response(scope, receive, send)
                return

        consumed = 0

        async def limited_receive():
            nonlocal consumed
            message = await receive()
            if message.get("type") == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > self.maximum:
                    raise _RequestBodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _RequestBodyTooLarge:
            response = JSONResponse({"detail": "request body too large"},
                                    status_code=413)
            await response(scope, receive, send)


class OwnerAccessMiddleware:
    """Require the installation owner secret whenever traffic is non-loopback."""

    def __init__(self, app, *, configured_host: str, owner_token: str):
        self.app = app
        self.configured_local = _is_loopback(configured_host)
        self.owner_token = owner_token
        self.session = _session_value(owner_token) if owner_token else ""

    def _authenticated(self, headers: dict[str, str]) -> bool:
        if not self.owner_token:
            return False
        authorization = headers.get("authorization", "")
        supplied = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
        if supplied and hmac.compare_digest(supplied, self.owner_token):
            return True
        cookie = _cookie_value(headers.get("cookie", ""), COOKIE_NAME)
        return bool(cookie) and hmac.compare_digest(cookie, self.session)

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        headers = _headers(scope)
        peer = (scope.get("client") or (None, None))[0]
        remote_mode = not self.configured_local or not _is_loopback(peer)
        origin = headers.get("origin")
        if origin and not _origin_allowed(origin, headers.get("host", ""),
                                          local_only=not remote_mode):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 4403,
                            "reason": "origin not allowed"})
            else:
                response = JSONResponse({"detail": "origin not allowed"}, status_code=403)
                await response(scope, receive, send)
            return

        authenticated = self._authenticated(headers)
        scope.setdefault("state", {})["owner_authenticated"] = authenticated
        path = scope.get("path", "")
        public_auth_path = path in ("/auth", "/api/auth/session")
        if not remote_mode or authenticated or public_auth_path:
            await self.app(scope, receive, send)
            return

        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4401,
                        "reason": "owner authentication required"})
            return

        if (scope.get("method") == "GET" and not path.startswith("/api/") and
                "text/html" in headers.get("accept", "")):
            query = scope.get("query_string", b"").decode("latin-1")
            target = path + (f"?{query}" if query else "")
            response = RedirectResponse(f"/auth?next={quote(target, safe='')}", status_code=307)
        else:
            response = JSONResponse(
                {"detail": "owner authentication required"}, status_code=401,
                headers={"WWW-Authenticate": "Bearer"})
        await response(scope, receive, send)


router = APIRouter()


@router.get("/auth", include_in_schema=False)
async def login_page(request: Request, next: str = "/"):
    target = _safe_next(next)
    if getattr(request.state, "owner_authenticated", False):
        return RedirectResponse(target, status_code=303)
    body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>YuriOS owner access</title></head><body>
<main><h1>YuriOS owner access</h1><p>Enter this installation's owner token.</p>
<form method="post" action="/api/auth/session">
<input type="hidden" name="next" value="{html.escape(target, quote=True)}">
<label>Owner token <input name="token" type="password" required autocomplete="current-password"></label>
<button type="submit">Unlock</button></form></main></body></html>"""
    return HTMLResponse(body, headers={
        "Cache-Control": "no-store",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    })


@router.post("/api/auth/session", include_in_schema=False)
async def create_owner_session(request: Request):
    raw = await request.body()
    if len(raw) > 8192:
        return JSONResponse({"detail": "request too large"}, status_code=413)
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    wants_json = content_type == "application/json"
    try:
        if wants_json:
            payload = json.loads(raw or b"{}")
        else:
            payload = {key: values[-1] for key, values in
                       parse_qs(raw.decode("utf-8"), keep_blank_values=True).items()}
    except (UnicodeDecodeError, ValueError, TypeError):
        return JSONResponse({"detail": "invalid request"}, status_code=400)

    owner_token = request.app.state.owner_token
    supplied = str(payload.get("token", ""))
    if not owner_token or not hmac.compare_digest(supplied, owner_token):
        return JSONResponse({"detail": "invalid owner token"}, status_code=401,
                            headers={"WWW-Authenticate": "Bearer"})

    target = _safe_next(str(payload.get("next", "/")))
    response = (JSONResponse({"ok": True}) if wants_json else
                RedirectResponse(target, status_code=303))
    response.set_cookie(COOKIE_NAME, _session_value(owner_token), httponly=True,
                        secure=request.url.scheme == "https", samesite="strict", path="/")
    response.headers["Cache-Control"] = "no-store"
    return response


def install_owner_security(app, cfg) -> None:
    """Install the boundary and reject an unsafe advertised bind up front."""
    token = str(getattr(cfg, "owner_token", "") or "")
    configured_host = str(getattr(cfg, "host", "") or "")
    if token and len(token) < MIN_TOKEN_LENGTH:
        raise ValueError(f"OWNER_TOKEN must be at least {MIN_TOKEN_LENGTH} characters")
    if not _is_loopback(configured_host) and not token:
        raise ValueError(
            "non-loopback HOST requires OWNER_TOKEN; generate one with "
            "`python -c \"import secrets; print(secrets.token_urlsafe(32))\"`")
    app.state.owner_token = token
    app.include_router(router)
    app.add_middleware(OwnerAccessMiddleware, configured_host=configured_host,
                       owner_token=token)


def install_http_boundaries(app, *, maximum: int = MAX_HTTP_BODY_BYTES) -> None:
    """Install process-facing HTTP limits and overload mapping once per app."""
    if getattr(app.state, "http_boundaries_installed", False):
        return
    app.state.http_boundaries_installed = True
    app.add_middleware(HTTPBodyLimitMiddleware, maximum=maximum)

    from yurios.app.providers.admission import InferenceBusy

    async def inference_busy(_request: Request, _exc: InferenceBusy):
        return JSONResponse(
            {"detail": "inference capacity is busy; retry shortly"},
            status_code=503,
            headers={"Retry-After": "1"},
        )

    app.add_exception_handler(InferenceBusy, inference_busy)


def decode_bounded_base64(value: object, *, maximum: int, field: str) -> bytes:
    """Validate encoded size before allocating a bounded decoded payload."""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be base64")
    comma = value.find(",")
    start = comma + 1 if comma >= 0 else 0
    max_encoded = ((maximum + 2) // 3) * 4
    if len(value) - start > max_encoded:
        raise ValueError(f"{field} exceeds the size limit")
    encoded = value[start:]
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"{field} must be base64") from exc
    if len(decoded) > maximum:
        raise ValueError(f"{field} exceeds the size limit")
    return decoded


def owner_or_loopback(request: Request) -> bool:
    """Whether a sensitive local-management route may serve this request."""
    peer = request.client.host if request.client else None
    return _is_loopback(peer) or bool(
        getattr(request.state, "owner_authenticated", False))
