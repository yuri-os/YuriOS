"""The character CLI is a host client (SPEC §36) — no second registry path."""
from __future__ import annotations

import json

import httpx

from yurios.cli import main as cli_main
from yurios.ctl.client import DAEMON_DOWN, HostClient
from yurios.world.config import Config

CONNECT_SITES = (
    "yurios.ctl.characters.connect",
    "yurios.ctl.studio.connect",
    "yurios.ctl.camera.connect",
    "yurios.ctl.dreams.connect",
    "yurios.ctl.client.connect",
)


CHARACTERS = {
    "version": "0.2.0",
    "primary": "yuri",
    "characters": [
        {"id": "yuri", "name": "Yuri", "state": "idle", "runtime_state": "ready",
         "review_required": False, "model": "mock/chat",
         "loops": {"mind": True, "utility": True, "dream": True, "hands": True}},
        {"id": "mika", "name": "Mika", "state": "attention", "runtime_state": "offline",
         "review_required": True, "model": "",
         "loops": {"mind": False, "utility": False, "dream": False, "hands": True}},
    ],
}


def install_host(monkeypatch, handler, tmp_path):
    (tmp_path / ".env").write_text("CHAT_MODEL=NONE\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("yurios.cli._root", lambda: tmp_path)
    transport = httpx.MockTransport(handler)
    def connect(cfg=None, **kwargs):
        return HostClient(cfg or Config(_env_file=None), transport=transport)
    for site in CONNECT_SITES:
        monkeypatch.setattr(site, connect)


def test_character_list_prints_the_board(tmp_path, monkeypatch, capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/characters":
            return httpx.Response(200, json=CHARACTERS)
        return httpx.Response(404)

    install_host(monkeypatch, handler, tmp_path)
    assert cli_main(["character", "list"]) == 0
    out = capsys.readouterr().out
    assert "yuri" in out and "Yuri" in out
    assert "mika" in out and "review required" in out


def test_character_list_json(tmp_path, monkeypatch, capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=CHARACTERS)

    install_host(monkeypatch, handler, tmp_path)
    assert cli_main(["character", "list", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["primary"] == "yuri"


def test_daemon_down_is_one_sentence(tmp_path, monkeypatch, capsys):
    class Dead(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("nope")

    (tmp_path / ".env").write_text("CHAT_MODEL=NONE\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("yurios.cli._root", lambda: tmp_path)
    transport = Dead()
    def connect(cfg=None, **kwargs):
        return HostClient(cfg or Config(_env_file=None), transport=transport)
    for site in CONNECT_SITES:
        monkeypatch.setattr(site, connect)
    assert cli_main(["character", "list"]) == 1
    err = capsys.readouterr().err
    assert DAEMON_DOWN in err


def test_import_never_auto_approves(tmp_path, monkeypatch, capsys):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path == "/api/characters/import":
            return httpx.Response(200, json={
                "character": {"id": "card_person", "name": "Card Person",
                              "review_required": True}})
        return httpx.Response(404)

    card = tmp_path / "card.png"
    card.write_bytes(b"\x89PNG\r\n\x1a\nnot-a-real-card")
    install_host(monkeypatch, handler, tmp_path)
    assert cli_main(["character", "import", str(card)]) == 0
    out = capsys.readouterr().out
    assert "card_person" in out
    assert "approve card_person" in out
    assert any(path.endswith("/approve") for path in calls) is False


def test_get_and_set_go_through_studio(tmp_path, monkeypatch, capsys):
    draft = {"name": "Yuri", "personality": "dry", "identity": "a person"}
    patched: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/studio") and request.method == "GET":
            return httpx.Response(200, json={"draft": draft})
        if request.url.path.endswith("/studio") and request.method == "PATCH":
            patched.append(json.loads(request.content))
            return httpx.Response(200, json={"character": {"id": "yuri"},
                                             "touched": ["PERSONA.md"]})
        return httpx.Response(404, json={"detail": request.url.path})

    install_host(monkeypatch, handler, tmp_path)
    assert cli_main(["character", "get", "yuri", "personality"]) == 0
    assert "dry" in capsys.readouterr().out
    assert cli_main(["character", "set", "yuri", "personality", "warmer"]) == 0
    assert patched and patched[0]["draft"]["personality"] == "warmer"


def test_optimize_does_not_patch_without_apply(tmp_path, monkeypatch, capsys):
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(f"{request.method} {request.url.path}")
        if request.url.path.endswith("/studio") and request.method == "GET":
            return httpx.Response(200, json={"draft": {"name": "Yuri"}})
        if request.url.path == "/api/studio/optimize":
            line = json.dumps({"event": "done", "result": {
                "draft": {"name": "Yuri", "personality": "new"},
                "changes": [{"field": "personality", "op": "changed"}],
                "notes": "moved a line"}}) + "\n"
            return httpx.Response(200, content=line.encode(),
                                  headers={"content-type": "application/x-ndjson"})
        return httpx.Response(404)

    install_host(monkeypatch, handler, tmp_path)
    assert cli_main(["character", "optimize", "yuri"]) == 0
    out = capsys.readouterr().out
    assert "proposed only" in out
    assert not any(item.startswith("PATCH") for item in methods)


def test_export_privacy_refusal_prints_the_overlaps(tmp_path, monkeypatch, capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": {
            "detail": "her USER.md overlaps the card",
            "code": "review_required",
            "overlaps": [{"surface": "vault/soul/USER.md", "excerpt": "Sam"}]}})

    install_host(monkeypatch, handler, tmp_path)
    assert cli_main(["character", "export", "yuri"]) == 1
    err = capsys.readouterr().err
    assert "USER.md" in err
    assert "--acknowledged" in err


def test_clone_posts_the_host_route(tmp_path, monkeypatch, capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/characters/yuri/clone":
            return httpx.Response(201, json={
                "character": {"id": "yuri_v2", "name": "Yuri"},
                "started": False, "error": None})
        return httpx.Response(404)

    install_host(monkeypatch, handler, tmp_path)
    assert cli_main(["character", "clone", "yuri", "--name", "Yuri"]) == 0
    assert "yuri_v2" in capsys.readouterr().out
