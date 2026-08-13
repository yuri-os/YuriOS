from __future__ import annotations

import json

import pytest
from fastapi import FastAPI, Request, WebSocket
from fastapi.testclient import TestClient
from fastapi.responses import StreamingResponse
from starlette.websockets import WebSocketDisconnect
from types import SimpleNamespace

from yurios.desktop.routes import settings as settings_panel
from yurios.app.providers.admission import InferenceBusy
from yurios.security import (
    decode_bounded_base64, install_http_boundaries, install_owner_security,
)
from yurios.world.config import Config


TOKEN = "owner-token-with-at-least-thirty-two-characters"


def secured_app(*, host: str = "0.0.0.0", token: str = TOKEN) -> FastAPI:
    app = FastAPI()
    install_owner_security(
        app, Config(_env_file=None, host=host, owner_token=token))

    @app.get("/api/private")
    async def private():
        return {"ok": True}

    @app.get("/")
    async def root():
        return {"page": True}

    @app.websocket("/ws/private")
    async def private_ws(ws: WebSocket):
        await ws.accept()
        await ws.send_json({"ok": True})
        await ws.close()

    return app


def test_non_loopback_bind_requires_strong_owner_token():
    for token in ("", "too-short"):
        try:
            secured_app(token=token)
        except ValueError as exc:
            assert "OWNER_TOKEN" in str(exc)
        else:
            raise AssertionError("unsafe non-loopback configuration was accepted")


def test_remote_http_accepts_bearer_and_rejects_unauthenticated_requests():
    with TestClient(secured_app(), client=("192.0.2.4", 5000)) as client:
        denied = client.get("/api/private")
        assert denied.status_code == 401
        assert denied.headers["www-authenticate"] == "Bearer"
        assert client.get("/", headers={"Accept": "text/html"},
                          follow_redirects=False).headers["location"].startswith("/auth?")
        assert client.get("/api/private",
                          headers={"Authorization": f"Bearer {TOKEN}"}).json() == {"ok": True}


def test_remote_browser_login_sets_httponly_session_for_http_and_websocket():
    with TestClient(secured_app(), client=("192.0.2.4", 5000)) as client:
        response = client.post("/api/auth/session", data={"token": TOKEN, "next": "/"},
                               follow_redirects=False)
        assert response.status_code == 303
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=strict" in cookie
        assert TOKEN not in cookie
        assert client.get("/api/private").status_code == 200
        with client.websocket_connect("/ws/private") as ws:
            assert ws.receive_json() == {"ok": True}


def test_cross_site_origins_are_rejected_in_remote_and_loopback_modes():
    with TestClient(secured_app(), client=("192.0.2.4", 5000)) as remote:
        response = remote.get(
            "/api/private",
            headers={"Authorization": f"Bearer {TOKEN}", "Origin": "https://evil.example"})
        assert response.status_code == 403
        try:
            with remote.websocket_connect(
                    "/ws/private", headers={"Authorization": f"Bearer {TOKEN}",
                                             "Origin": "https://evil.example"}):
                pass
        except WebSocketDisconnect as exc:
            assert exc.code == 4403
        else:
            raise AssertionError("cross-site WebSocket was accepted")

    with TestClient(secured_app(host="127.0.0.1", token=""),
                    client=("127.0.0.1", 5000)) as local:
        assert local.get("/api/private").status_code == 200
        assert local.get("/api/private", headers={
            "Origin": "http://attacker.example"}).status_code == 403


def test_remote_peer_is_fail_closed_even_if_configured_bind_is_loopback():
    with TestClient(secured_app(host="127.0.0.1", token=""),
                    client=("192.0.2.4", 5000)) as client:
        assert client.get("/api/private").status_code == 401


def test_settings_secrets_are_write_only_with_preserve_replace_remove(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("OPENROUTER_API_KEY=original-secret\nHOST=0.0.0.0\n")
    monkeypatch.setattr(settings_panel, "ENV_PATH", env_path)
    cfg = Config(_env_file=None, host="0.0.0.0", owner_token=TOKEN,
                 openrouter_api_key="original-secret",
                 telegram_bot_token="telegram-secret")
    app = FastAPI()
    app.state.rt = SimpleNamespace(cfg=cfg)
    install_owner_security(app, cfg)
    app.include_router(settings_panel.router)

    headers = {"Authorization": f"Bearer {TOKEN}"}
    with TestClient(app, client=("192.0.2.4", 5000)) as client:
        response = client.get("/api/settings", headers=headers)
        assert response.status_code == 200
        serialized = response.text
        assert "original-secret" not in serialized
        assert "telegram-secret" not in serialized
        fields = {f["key"]: f for group in response.json()["groups"]
                  for f in group["fields"]}
        for key in ("OPENROUTER_API_KEY", "TELEGRAM_BOT_TOKEN", "OWNER_TOKEN"):
            assert fields[key]["configured"] is True
            assert "value" not in fields[key]

        preserved = client.post("/api/settings", json={"OPENROUTER_API_KEY": "   "},
                                headers=headers).json()
        assert preserved["written"] == []
        assert "OPENROUTER_API_KEY=original-secret" in env_path.read_text()

        replaced = client.post("/api/settings", json={"OPENROUTER_API_KEY": "replacement"},
                               headers=headers).json()
        assert replaced["written"] == ["OPENROUTER_API_KEY"]
        assert "OPENROUTER_API_KEY=replacement" in env_path.read_text()

        removed = client.post("/api/settings", json={"OPENROUTER_API_KEY": None},
                              headers=headers).json()
        assert removed["written"] == ["OPENROUTER_API_KEY"]
        assert "OPENROUTER_API_KEY=\n" in env_path.read_text()

        ordinary_blank = client.post("/api/settings", json={"HOST": ""},
                                     headers=headers).json()
        assert ordinary_blank["written"] == ["HOST"]
        assert "HOST=\n" in env_path.read_text()


def test_http_body_limit_rejects_content_length_and_chunked_overflow():
    app = FastAPI()
    install_http_boundaries(app, maximum=5)

    @app.post("/body")
    async def body(request: Request):
        return {"size": len(await request.body())}

    @app.get("/events")
    async def events():
        async def stream():
            yield b"data: ok\n\n"
        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.websocket("/socket")
    async def socket(ws: WebSocket):
        await ws.accept()
        await ws.send_text("ok")
        await ws.close()

    with TestClient(app) as client:
        response = client.post("/body", content=b"123456")
        assert response.status_code == 413
        assert response.json() == {"detail": "request body too large"}
        assert client.get("/events").text == "data: ok\n\n"
        with client.websocket_connect("/socket") as socket:
            assert socket.receive_text() == "ok"

    messages = iter([
        {"type": "http.request", "body": b"123", "more_body": True},
        {"type": "http.request", "body": b"456", "more_body": False},
    ])
    sent = []

    async def receive():
        return next(messages)

    async def send(message):
        sent.append(message)

    import asyncio
    asyncio.run(app({"type": "http", "http_version": "1.1", "method": "POST",
                     "scheme": "http", "path": "/body", "raw_path": b"/body",
                     "query_string": b"", "headers": [],
                     "client": ("127.0.0.1", 1), "server": ("test", 80)},
                    receive, send))
    start = next(message for message in sent if message["type"] == "http.response.start")
    payload = b"".join(message.get("body", b"") for message in sent
                       if message["type"] == "http.response.body")
    assert start["status"] == 413
    assert json.loads(payload) == {"detail": "request body too large"}


def test_http_boundaries_map_inference_busy_to_retryable_503():
    app = FastAPI()
    install_http_boundaries(app)

    @app.post("/turn")
    async def turn():
        raise InferenceBusy("full")

    with TestClient(app) as client:
        response = client.post("/turn")
    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"


def test_base64_size_is_rejected_before_decoder_allocation(monkeypatch):
    called = False

    def decode(*_args, **_kwargs):
        nonlocal called
        called = True
        return b""

    monkeypatch.setattr("yurios.security.base64.b64decode", decode)
    with pytest.raises(ValueError, match="size limit"):
        decode_bounded_base64("A" * 9, maximum=4, field="image")
    assert called is False
