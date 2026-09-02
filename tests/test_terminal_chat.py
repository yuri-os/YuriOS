from __future__ import annotations

import asyncio
import json

import httpx

from yurios.chat import __main__ as terminal


def test_one_shot_posts_the_cli_channel_and_skips_events():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/characters":
            return httpx.Response(200, json={
                "primary": "yuri",
                "characters": [{"id": "yuri", "name": "Yuri"}]})
        if request.url.path == "/api/characters/yuri/health":
            return httpx.Response(200, json={"character": "Yuri"})
        if request.url.path == "/api/characters/yuri/chat":
            return httpx.Response(200, json={
                "session_id": "s1",
                "message": {"id": "m1", "text": "hello back", "role": "assistant"}})
        return httpx.Response(404)

    async def run() -> int:
        transport = httpx.MockTransport(handler)
        # main() builds its own client; drive send_turn the way one-shot does.
        async with httpx.AsyncClient(base_url="http://host", transport=transport) as client:
            state = {"character_id": "yuri", "session_id": None, "name": "Yuri",
                     "awaiting": True, "printed": set(), "url": "http://host"}
            entry = await terminal.send_turn(client, state, {}, "hello")
            assert entry["text"] == "hello back"
            return 0

    assert asyncio.run(run()) == 0
    chat = next(req for req in requests if req.url.path.endswith("/chat"))
    assert json.loads(chat.content)["channel"] == "cli"
    assert json.loads(chat.content)["text"] == "hello"
    assert not any(req.url.path.endswith("/events") for req in requests)


def test_character_endpoints_do_not_fall_back_to_the_host_primary():
    assert terminal.endpoint({"character_id": "mika"}, "chat") == "/api/characters/mika/chat"
    assert terminal.endpoint({"character_id": "mika"}, "events") == "/api/characters/mika/events"
    assert terminal.endpoint({"character_id": None}, "chat") == "/api/chat"


def test_session_cache_migrates_the_old_single_character_file(tmp_path, monkeypatch):
    cache = tmp_path / "cli-sessions.json"
    legacy = tmp_path / "cli-session"
    legacy.write_text("old-session\n", encoding="utf-8")
    monkeypatch.setattr(terminal, "SESSION_FILE", cache)
    monkeypatch.setattr(terminal, "LEGACY_SESSION_FILE", legacy)

    sessions = terminal.load_sessions(fresh=False)

    assert sessions == {"__standalone__": "old-session"}
    sessions["mika"] = "mika-session"
    terminal.save_sessions(sessions)
    assert json.loads(cache.read_text(encoding="utf-8")) == sessions


def test_fresh_session_ignores_saved_character_sessions(tmp_path, monkeypatch):
    cache = tmp_path / "cli-sessions.json"
    cache.write_text('{"yuri": "existing"}\n', encoding="utf-8")
    monkeypatch.setattr(terminal, "SESSION_FILE", cache)

    assert terminal.load_sessions(fresh=True) == {}


def test_legacy_session_moves_to_the_primary_card_once(tmp_path, monkeypatch):
    cache = tmp_path / "cli-sessions.json"
    monkeypatch.setattr(terminal, "SESSION_FILE", cache)
    sessions = {"__standalone__": "existing"}

    assert terminal.saved_session(sessions, "yuri", "yuri") == "existing"
    assert sessions == {"yuri": "existing"}
    assert json.loads(cache.read_text(encoding="utf-8")) == sessions
    assert terminal.saved_session(sessions, "mika", "yuri") is None


def test_message_wrapping_preserves_blank_lines():
    assert terminal.message_lines("one\n\ntwo", width=20) == ["one", "", "two"]


def test_model_changes_patch_only_the_selected_character_card():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"effective": {"chat_model": "ollama/qwen3"}})

    async def change() -> bool:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(base_url="http://host", transport=transport) as client:
            return await terminal.set_model(client, {"character_id": "mika"},
                                            "chat_model", "ollama/qwen3")

    assert asyncio.run(change()) is True
    assert requests[0].method == "PATCH"
    assert requests[0].url.path == "/api/characters/mika/brain"
    assert json.loads(requests[0].content) == {"chat_model": "ollama/qwen3"}
