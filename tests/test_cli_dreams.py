"""Dream job CLI (SPEC §21.2, §36)."""
from __future__ import annotations

import json

import httpx

from yurios.cli import main as cli_main
from yurios.ctl.client import HostClient
from yurios.world.config import Config

CONNECT_SITES = (
    "yurios.ctl.dreams.connect",
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


def test_dream_list_prints_job_files(tmp_path, monkeypatch, capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/characters/yuri/mind/dream/jobs":
            return httpx.Response(200, json={"jobs": [
                {"name": "diary", "builtin": True, "front": {"title": "Diary"}},
                {"name": "briefing", "builtin": False, "front": {"title": "Brief"}},
            ]})
        return httpx.Response(404)

    install_host(monkeypatch, handler, tmp_path)
    assert cli_main(["dream", "list", "yuri"]) == 0
    out = capsys.readouterr().out
    assert "diary" in out and "builtin" in out
    assert "briefing" in out


def test_dream_run_dry_posts_the_flag(tmp_path, monkeypatch, capsys):
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/characters/yuri/mind/dream/run":
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={"jobs": [
                {"name": "diary", "status": "ok"}]})
        return httpx.Response(404)

    install_host(monkeypatch, handler, tmp_path)
    assert cli_main(["dream", "run", "yuri", "diary", "--dry-run"]) == 0
    assert seen == [{"dry_run": True, "job": "diary"}]
    assert "diary" in capsys.readouterr().out


def test_dream_write_sends_the_file(tmp_path, monkeypatch, capsys):
    job = tmp_path / "job.md"
    job.write_text("---\ntitle: Brief\n---\nAsk about the weather.\n", encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT" and request.url.path.endswith("/jobs/briefing"):
            body = json.loads(request.content)
            assert "Ask about the weather" in body["text"]
            return httpx.Response(200, json={"name": "briefing", "text": body["text"]})
        return httpx.Response(404)

    install_host(monkeypatch, handler, tmp_path)
    assert cli_main(["dream", "write", "yuri", "briefing", "--file", str(job)]) == 0
    assert "briefing" in capsys.readouterr().out
