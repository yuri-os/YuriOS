"""Getting the owner token onto a phone (SPEC §11.1).

`OWNER_TOKEN` is the one setting whose value has to end up on another device,
and the honest way to do that is not to make somebody read 43 random characters
off a screen. So: generate it server-side, apply it to the live boundary, and
hand it over as a QR code that opens `/auth?token=…`.

Two properties matter more than the drawing. Rotation is *live* — the running
boundary honours the new token on the next request, because a token that only
worked after a restart would make the QR a lie. And rotation is a *revocation* —
the session cookie is an HMAC of the token, so every device signed in under the
old one is out.
"""
from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from yurios import pairing
from yurios.desktop.routes import settings as panel
from yurios.security import (COOKIE_NAME, generate_owner_token,
                             install_owner_security)
from yurios.world.config import Config

TOKEN = "owner-token-with-at-least-thirty-two-characters"


def _app(tmp_path, monkeypatch, host="0.0.0.0"):
    env = tmp_path / ".env"
    env.write_text(f"HOST={host}\nOWNER_TOKEN={TOKEN}\n", encoding="utf-8")
    monkeypatch.setattr(panel, "ENV_PATH", env)
    # Pairing tests describe only the network topology they set up. The
    # developer machine may itself have a live Tailscale Serve configuration.
    monkeypatch.setattr(pairing, "tailscale_origins", lambda _cfg: [])
    cfg = Config(_env_file=None, host=host, port=8768, owner_token=TOKEN)
    app = FastAPI()
    app.state.rt = SimpleNamespace(cfg=cfg)
    boundary = install_owner_security(app, cfg)
    app.include_router(panel.router)
    return app, env, boundary


def test_a_generated_token_is_written_live_and_scannable(tmp_path, monkeypatch):
    app, env, boundary = _app(tmp_path, monkeypatch)
    headers = {"Authorization": f"Bearer {TOKEN}"}

    with TestClient(app, client=("192.0.2.4", 5000)) as client:
        body = client.post("/api/pairing/token", headers=headers).json()

        assert len(body["token"]) >= 32 and body["token"] != TOKEN
        assert f"OWNER_TOKEN={body['token']}" in env.read_text()
        assert boundary.token == body["token"]          # honoured without a restart
        assert body["live"] is True and body["reachable"] is True

        link = body["links"][0]
        assert link["url"].startswith(link["origin"] + "/auth?token=")
        assert body["token"] in link["url"]
        assert link["qr"].startswith("<svg") and "<path" in link["qr"]

        # the old token is gone the moment the new one lands (the caller keeps
        # its own session — see the next test — so drop the cookie to ask as
        # somebody else)
        client.cookies.clear()
        assert client.get("/api/settings", headers=headers).status_code == 401
        assert client.get("/api/settings", headers={
            "Authorization": f"Bearer {body['token']}"}).status_code == 200


def test_the_caller_that_rotated_the_token_is_not_signed_out(tmp_path, monkeypatch):
    app, _, _ = _app(tmp_path, monkeypatch)

    with TestClient(app, client=("192.0.2.4", 5000)) as client:
        client.post("/api/auth/session", json={"token": TOKEN})
        assert client.get("/api/settings").status_code == 200

        client.post("/api/pairing/token")            # rides the cookie it holds
        assert client.get("/api/settings").status_code == 200


def test_scanning_the_link_signs_a_browser_in_without_typing_anything(
        tmp_path, monkeypatch):
    app, _, boundary = _app(tmp_path, monkeypatch)

    with TestClient(app, client=("192.0.2.4", 5000)) as client:
        wrong = client.get("/auth?token=nope", follow_redirects=False)
        assert wrong.status_code == 200 and "owner token" in wrong.text.lower()
        assert COOKIE_NAME not in wrong.cookies

        landing = client.get(f"/auth?token={boundary.token}&next=%2Fdashboard%2F",
                             follow_redirects=False)
        assert landing.status_code == 303
        assert landing.headers["location"] == "/dashboard/"
        cookie = landing.headers["set-cookie"]
        assert "HttpOnly" in cookie and boundary.token not in cookie
        # The two attributes the QR flow lives or dies by: the camera app hands
        # the phone a navigation the browser did not start, so a Strict cookie
        # would be withheld from the 303 that follows and the phone would land
        # on the unlock form holding a session it could not spend. And it has to
        # outlive the browser process, or the next reap of the tab means pairing
        # the same phone again.
        assert "SameSite=lax" in cookie and "Max-Age=" in cookie
        assert client.get("/api/settings").status_code == 200


def test_the_token_typed_into_the_form_behaves_like_a_generated_one(
        tmp_path, monkeypatch):
    app, env, boundary = _app(tmp_path, monkeypatch)
    typed = generate_owner_token()

    with TestClient(app, client=("192.0.2.4", 5000)) as client:
        saved = client.post("/api/settings", json={"OWNER_TOKEN": typed},
                            headers={"Authorization": f"Bearer {TOKEN}"}).json()

        assert saved["written"] == ["OWNER_TOKEN"] and saved["owner_token_applied"]
        assert saved["restart_required"] is False    # this one really did apply
        assert boundary.token == typed
        assert f"OWNER_TOKEN={typed}" in env.read_text()


def test_a_token_that_would_lock_the_installation_out_is_refused(tmp_path, monkeypatch):
    app, env, boundary = _app(tmp_path, monkeypatch)
    headers = {"Authorization": f"Bearer {TOKEN}"}

    with TestClient(app, client=("192.0.2.4", 5000)) as client:
        short = client.post("/api/settings", json={"OWNER_TOKEN": "too-short"},
                            headers=headers)
        cleared = client.post("/api/settings", json={"OWNER_TOKEN": None},
                              headers=headers)

    assert short.status_code == 422 and "32 characters" in short.json()["detail"]
    # HOST is 0.0.0.0 here, so removing the token would mean a server that
    # refuses to start — with the panel that did it on the far side of the door
    assert cleared.status_code == 422 and "not loopback" in cleared.json()["detail"]
    assert boundary.token == TOKEN and f"OWNER_TOKEN={TOKEN}" in env.read_text()


def test_a_loopback_bind_says_so_rather_than_drawing_a_useless_code(
        tmp_path, monkeypatch):
    app, _, _ = _app(tmp_path, monkeypatch, host="127.0.0.1")

    with TestClient(app) as client:                  # a loopback peer manages locally
        body = client.get("/api/pairing").json()

    assert body["configured"] is True
    assert body["reachable"] is False                # nothing off this box can reach her
    assert body["links"] == []


def test_the_address_the_browser_already_used_is_offered_first():
    cfg = Config(_env_file=None, host="0.0.0.0", port=8768)

    origins = pairing.candidate_origins(
        cfg, request_origin="http://192.168.7.9:8768")
    assert origins[0] == "http://192.168.7.9:8768"
    # …and a loopback Host header is not a candidate, however the browser got here
    assert all("127.0.0.1" not in origin for origin in
               pairing.candidate_origins(
                   cfg, request_origin="http://127.0.0.1:8768"))


def test_the_panel_and_the_terminal_build_the_same_link():
    cfg = Config(_env_file=None, host="0.0.0.0", port=8768)
    token = generate_owner_token()

    described = pairing.describe(cfg, token, live=True,
                                 request_origin="http://10.0.0.5:8768")
    assert described["links"][0]["url"] == pairing.link("http://10.0.0.5:8768", token)
    assert described["links"][0]["url"].endswith("&next=%2F")


def test_an_https_tailscale_request_produces_an_https_pairing_link(
        tmp_path, monkeypatch):
    app, _, _ = _app(tmp_path, monkeypatch)

    with TestClient(app, base_url="https://node.tailnet.ts.net",
                    client=("100.64.0.2", 5000)) as client:
        body = client.get("/api/pairing", headers={
            "Authorization": f"Bearer {TOKEN}"}).json()

    assert body["links"][0]["origin"] == "https://node.tailnet.ts.net"
    assert body["links"][0]["url"].startswith(
        "https://node.tailnet.ts.net/auth?token=")


def test_tailscale_serve_can_publish_a_loopback_bound_installation(
        tmp_path, monkeypatch):
    app, _, _ = _app(tmp_path, monkeypatch, host="127.0.0.1")
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Host": "127.0.0.1:8768",
        "Origin": "https://node.tailnet.ts.net",
        "X-Forwarded-For": "100.64.0.2",
        "X-Forwarded-Host": "node.tailnet.ts.net",
        "X-Forwarded-Proto": "https",
    }

    with TestClient(app, client=("100.64.0.2", 5000)) as client:
        response = client.get("/api/pairing", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["reachable"] is True
    assert body["links"][0]["origin"] == "https://node.tailnet.ts.net"


def test_local_settings_discovers_a_configured_tailscale_serve_qr(
        tmp_path, monkeypatch):
    app, _, _ = _app(tmp_path, monkeypatch, host="127.0.0.1")
    monkeypatch.setattr(
        pairing, "tailscale_origins",
        lambda _cfg: ["https://device.example.ts.net"])

    with TestClient(app) as client:
        body = client.get("/api/pairing").json()

    assert body["reachable"] is True
    assert body["links"][0]["origin"] == "https://device.example.ts.net"
    assert body["links"][0]["qr"].startswith("<svg")


def test_tailscale_discovery_only_accepts_a_root_proxy_to_this_port(monkeypatch):
    status = {
        "Web": {
            "device.example.ts.net:443": {
                "Handlers": {"/": {"Proxy": "http://127.0.0.1:8768"}}},
            "other.tailnet.ts.net:443": {
                "Handlers": {"/": {"Proxy": "http://127.0.0.1:9000"}}},
            "subpath.tailnet.ts.net:443": {
                "Handlers": {"/yurios": {"Proxy": "http://127.0.0.1:8768"}}},
        },
    }
    monkeypatch.setattr(pairing.subprocess, "run", lambda *_args, **_kwargs:
                        SimpleNamespace(returncode=0, stdout=__import__("json").dumps(status)))

    cfg = Config(_env_file=None, host="127.0.0.1", port=8768)
    assert pairing.tailscale_origins(cfg) == ["https://device.example.ts.net"]


def test_pairing_is_owner_only(tmp_path, monkeypatch):
    app, _, _ = _app(tmp_path, monkeypatch)

    with TestClient(app, client=("192.0.2.4", 5000)) as client:
        assert client.get("/api/pairing").status_code == 401
        assert client.post("/api/pairing/token").status_code == 401
