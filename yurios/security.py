"""Single-owner access boundary for YuriOS HTTP and WebSocket surfaces.

The secret itself lives in an `OwnerBoundary`, not in the middleware that reads
it: `.env` is editable while she runs (SPEC §11), and a rotated token that only
took effect at the next restart would leave the settings panel — and the QR it
just drew — describing an installation that does not exist yet. One boundary per
process, shared by the host app and every character app mounted under it, so
"the owner token" is one fact with one value everywhere.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import html
import ipaddress
import json
import secrets
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, quote, urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse


COOKIE_NAME = "yurios_owner"
MIN_TOKEN_LENGTH = 32
MAX_HTTP_BODY_BYTES = 48 * 1024 * 1024
_SESSION_LABEL = b"yurios-owner-session-v1"


def generate_owner_token() -> str:
    """A fresh owner secret. 32 url-safe bytes — 43 characters, comfortably over
    MIN_TOKEN_LENGTH, and safe in a URL, which is how it reaches a phone."""
    return secrets.token_urlsafe(32)


def is_loopback(host: str | None) -> bool:
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


def _header_hostname(value: str) -> str:
    try:
        parsed = urlsplit(f"//{value}")
    except ValueError:
        return ""
    if parsed.username or parsed.password:
        return ""
    return parsed.hostname or ""


def _forwarded_host(headers: dict[str, str], configured_local: bool) -> str:
    """Return a proxy's public host only for a loopback reverse-proxy hop.

    Tailscale Serve terminates HTTPS on loopback and may leave the backend Host
    as 127.0.0.1 while carrying the browser-visible host in X-Forwarded-Host.
    Trusting that header on a directly exposed bind would let a client choose
    its own same-origin boundary, so it is accepted only when the installation
    itself is loopback-bound, the backend Host is loopback, and the proxy also
    supplied its forwarded client chain.
    """
    if not configured_local or not headers.get("x-forwarded-for"):
        return ""
    if not is_loopback(_header_hostname(headers.get("host", ""))):
        return ""
    value = headers.get("x-forwarded-host", "").split(",", 1)[0].strip()
    return value if _header_hostname(value) else ""


def external_request_origin(headers: dict[str, str], configured_host: str,
                            scheme: str) -> str:
    """The browser-visible origin for pairing links behind a local proxy."""
    configured_local = is_loopback(configured_host)
    forwarded = _forwarded_host(headers, configured_local)
    host = forwarded or headers.get("host", "")
    if not _header_hostname(host):
        return ""
    if forwarded:
        forwarded_scheme = headers.get("x-forwarded-proto", "").split(",", 1)[0].strip()
        if forwarded_scheme in ("http", "https"):
            scheme = forwarded_scheme
    if scheme not in ("http", "https"):
        scheme = "http"
    return f"{scheme}://{host}"


def _origin_allowed(origin: str, host_headers: list[str], local_only: bool) -> bool:
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    if local_only:
        # This also closes DNS rebinding: a browser page on an attacker hostname
        # is rejected even after that hostname resolves to 127.0.0.1.
        return is_loopback(parsed.hostname)
    authority = parsed.netloc.lower().rstrip(".")
    return any(authority == host.lower().rstrip(".") for host in host_headers)


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


class OwnerBoundary:
    """This installation's owner secret, and the session value derived from it.

    Held in one mutable object rather than copied into the middleware, so that
    rotating the token (the settings panel, `yurios pair --new`) takes effect on
    the next request instead of the next boot. Rotation is a revocation too: the
    cookie is an HMAC *of* the token, so every session opened under the old one
    stops verifying the moment the new one lands.
    """

    def __init__(self, token: str = "") -> None:
        self.token = ""
        self.session = ""
        self.set(token)

    def set(self, token: str) -> None:
        token = str(token or "")
        if token and len(token) < MIN_TOKEN_LENGTH:
            raise ValueError(
                f"OWNER_TOKEN must be at least {MIN_TOKEN_LENGTH} characters")
        self.token = token
        self.session = _session_value(token) if token else ""

    def authenticates(self, headers: dict[str, str]) -> bool:
        if not self.token:
            return False
        authorization = headers.get("authorization", "")
        supplied = (authorization[7:].strip()
                    if authorization.lower().startswith("bearer ") else "")
        if supplied and hmac.compare_digest(supplied, self.token):
            return True
        cookie = _cookie_value(headers.get("cookie", ""), COOKIE_NAME)
        return bool(cookie) and hmac.compare_digest(cookie, self.session)


def boundary_for(app) -> OwnerBoundary | None:
    """The boundary guarding this app, if it has one.

    A character app mounted under the host has no middleware of its own — the
    host's covers it — but it is handed the same object, so a settings save
    served through her runtime rotates the secret the host is checking.
    """
    return getattr(app.state, "owner_boundary", None)


class OwnerAccessMiddleware:
    """Require the installation owner secret whenever traffic is non-loopback."""

    def __init__(self, app, *, configured_host: str, boundary: OwnerBoundary):
        self.app = app
        self.configured_local = is_loopback(configured_host)
        self.boundary = boundary

    def _authenticated(self, headers: dict[str, str]) -> bool:
        return self.boundary.authenticates(headers)

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        headers = _headers(scope)
        peer = (scope.get("client") or (None, None))[0]
        remote_mode = not self.configured_local or not is_loopback(peer)
        origin = headers.get("origin")
        allowed_hosts = [headers.get("host", "")]
        forwarded = _forwarded_host(headers, self.configured_local)
        if forwarded:
            allowed_hosts.append(forwarded)
        if origin and not _origin_allowed(origin, allowed_hosts,
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


def issue_session_cookie(response, boundary: OwnerBoundary, *, https: bool):
    """Put this boundary's session on a response.

    Also used after a rotation (`desktop/routes/settings.py`): the new token
    revokes every session including the caller's, and re-issuing here is what
    keeps the hand that turned the key from being locked out by it.
    """
    response.set_cookie(COOKIE_NAME, boundary.session, httponly=True,
                        secure=https, samesite="strict", path="/")
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.get("/auth", include_in_schema=False)
async def login_page(request: Request, next: str = "/", token: str = ""):
    """The unlock page — and, with `?token=`, the pairing link itself.

    That query form is what the QR in the settings panel encodes (SPEC §11.1):
    the phone opens one URL and is inside, with no 43-character secret to read
    off a screen and retype. It costs what every magic link costs — the token is
    in the phone's history and in any proxy log between the two of them — which
    is why it is a LAN pairing affordance and why the redirect leaves the URL
    behind immediately. Rotating the token from the panel invalidates a link that
    leaked.
    """
    target = _safe_next(next)
    boundary = boundary_for(request.app)
    if token and boundary and boundary.token and \
            hmac.compare_digest(token, boundary.token):
        return issue_session_cookie(RedirectResponse(target, status_code=303),
                                    boundary, https=request.url.scheme == "https")
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

    boundary = boundary_for(request.app)
    supplied = str(payload.get("token", ""))
    if boundary is None or not boundary.token or \
            not hmac.compare_digest(supplied, boundary.token):
        return JSONResponse({"detail": "invalid owner token"}, status_code=401,
                            headers={"WWW-Authenticate": "Bearer"})

    target = _safe_next(str(payload.get("next", "/")))
    response = (JSONResponse({"ok": True}) if wants_json else
                RedirectResponse(target, status_code=303))
    return issue_session_cookie(response, boundary,
                                https=request.url.scheme == "https")


def install_owner_security(app, cfg) -> OwnerBoundary:
    """Install the boundary and reject an unsafe advertised bind up front."""
    token = str(getattr(cfg, "owner_token", "") or "")
    configured_host = str(getattr(cfg, "host", "") or "")
    boundary = OwnerBoundary(token)          # raises on a token too short to hold
    if not is_loopback(configured_host) and not token:
        raise ValueError(
            "non-loopback HOST requires OWNER_TOKEN; generate one with "
            "`yurios pair --new`")
    app.state.owner_boundary = boundary
    app.include_router(router)
    app.add_middleware(OwnerAccessMiddleware, configured_host=configured_host,
                       boundary=boundary)
    return boundary


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
    return is_loopback(peer) or bool(
        getattr(request.state, "owner_authenticated", False))
