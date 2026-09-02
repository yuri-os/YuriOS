"""Gallery and owner-camera CLI (SPEC §36)."""
from __future__ import annotations

import json

import httpx

from yurios.cli import main as cli_main
from yurios.ctl.client import HostClient
from yurios.world.config import Config

CONNECT_SITES = (
    "yurios.ctl.camera.connect",
    "yurios.ctl.client.connect",
)


def install_host(monkeypatch, handler, tmp_path):
    (tmp_path / ".env").write_text("CHAT_MODEL=NONE\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("yurios.cli._root", lambda: tmp_path)
    transport = httpx.MockTransport(handler)
    def connect(cfg=None, **kwargs):
        return HostClient(cfg or Config(_env_file=None), transport=transport)
    for site in CONNECT_SITES:
        monkeypatch.setattr(site, connect)


def test_gallery_list_prefers_the_ledger(tmp_path, monkeypatch, capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/characters/yuri/gallery":
            return httpx.Response(200, json={
                "items": [{"name": "shot.png", "caption": "window", "score": 8}],
                "has_more": False})
        return httpx.Response(404)

    install_host(monkeypatch, handler, tmp_path)
    assert cli_main(["gallery", "list", "yuri"]) == 0
    out = capsys.readouterr().out
    assert "shot.png" in out and "window" in out and "8/10" in out


def test_gallery_list_falls_back_when_she_is_down(tmp_path, monkeypatch, capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/characters/yuri/gallery":
            return httpx.Response(404, json={"detail": "not running"})
        if request.url.path == "/api/characters/yuri/selfies":
            return httpx.Response(200, json={
                "selfies": [{"name": "old.png", "bytes": 12}]})
        return httpx.Response(404)

    install_host(monkeypatch, handler, tmp_path)
    assert cli_main(["gallery", "list", "yuri"]) == 0
    assert "old.png" in capsys.readouterr().out


def test_selfie_no_wait_prints_the_id(tmp_path, monkeypatch, capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/characters/yuri/selfie":
            body = json.loads(request.content)
            assert body.get("scene") == "window"
            return httpx.Response(200, json={
                "id": "abc123", "kind": "selfie", "status": "started"})
        return httpx.Response(404)

    install_host(monkeypatch, handler, tmp_path)
    assert cli_main(["selfie", "yuri", "--scene", "window", "--no-wait"]) == 0
    assert "abc123" in capsys.readouterr().out


def test_gallery_fetch_writes_the_png(tmp_path, monkeypatch):
    dest = tmp_path / "out.png"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/characters/yuri/selfies/shot.png":
            return httpx.Response(200, content=b"\x89PNG\r\n\x1a\n")
        return httpx.Response(404)

    install_host(monkeypatch, handler, tmp_path)
    assert cli_main(["gallery", "fetch", "yuri", "shot.png", "-o", str(dest)]) == 0
    assert dest.read_bytes().startswith(b"\x89PNG")
