"""House settings' own surfaces (SPEC §11.3, §11.4).

The `.env` table is tests/test_envfile.py's. These are the things House settings
does that are not lines in that file: restarting the server it was saved for,
editing the files `.env` only names (her third-party MCP servers, the house
scene overlay), and moving a character's night jobs and scene library in and
out as files.
"""
from __future__ import annotations

import io
import json
import zipfile
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from yurios import daemon
from yurios.characters import CharacterRegistry
from yurios.world.config import Config
from yurios.world.host import create_host_app
from yurios.world.host import house as house_routes
from yurios.world.host import modules as module_routes

from tests.test_host import fake_character_app, record

JOB = """---
name: {name}
title: A small night
enabled: true
---

You are {{char}}. Look back over the day and write one line.
"""


@pytest.fixture
def board(tmp_path, monkeypatch):
    """A switchboard with one parked character (no runtime: the file routes
    must work without one) and a scratch `.env` every house file hangs off."""
    from yurios.desktop.routes import settings as panel

    env = tmp_path / ".env"
    env.write_text("CHAT_MODEL=old\n", encoding="utf-8")
    monkeypatch.setattr(panel, "ENV_PATH", env)
    monkeypatch.setattr("yurios.world.host.hosting.create_app", fake_character_app)
    registry = CharacterRegistry(tmp_path)
    registry.add(record(tmp_path, "yuri", enabled=False))
    registry.add(record(tmp_path, "mia", enabled=False))
    app = create_host_app(Config(data_dir=tmp_path), registry)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, app=app, env=env, root=tmp_path,
                              registry=registry)


# --- restart ----------------------------------------------------------------------

def test_an_unsupervised_server_will_not_end_itself(board, monkeypatch):
    """Nobody would start it again — so the button says so instead of stopping
    the house."""
    monkeypatch.delenv(daemon.SUPERVISED_ENV, raising=False)
    board.app.state.restartable = True
    assert board.client.get("/api/house").json()["restartable"] is False
    assert board.client.post("/api/house/restart").status_code == 409


def test_a_supervised_server_ends_itself_to_be_started_again(board, monkeypatch):
    monkeypatch.setenv(daemon.SUPERVISED_ENV, "1")
    monkeypatch.setattr(house_routes, "RESTART_DELAY_S", 0.0)
    board.app.state.restartable = True
    server = SimpleNamespace(should_exit=False)
    board.app.state.server = server
    house = board.client.get("/api/house").json()
    assert house["restartable"] is True and house["boot_id"]

    answer = board.client.post("/api/house/restart")
    assert answer.status_code == 200
    assert board.app.state.restart_requested is True
    board.client.get("/api/house")              # let the loop run the scheduled stop
    assert server.should_exit is True


# --- MCP servers --------------------------------------------------------------------

def test_mcp_servers_are_written_where_env_will_look_and_env_is_pointed_at_them(board):
    first = board.client.get("/api/house/mcp-servers").json()
    assert first["configured"] is False and first["servers"] == {}

    saved = board.client.put("/api/house/mcp-servers", json={"servers": {
        "fetch": {"command": "uvx", "args": ["mcp-server-fetch"], "rate": 4},
        "later": {"command": "node", "args": ["x.js"], "disabled": True},
    }})
    assert saved.status_code == 200
    path = board.root / "mcp-servers.json"
    data = json.loads(path.read_text())
    assert data["mcpServers"]["fetch"]["args"] == ["mcp-server-fetch"]
    assert data["mcpServers"]["later"]["disabled"] is True
    assert "MCP_SERVERS=./mcp-servers.json" in board.env.read_text()

    again = board.client.get("/api/house/mcp-servers").json()
    assert again["configured"] is True and set(again["servers"]) == {"fetch", "later"}


def test_an_mcp_server_the_loader_would_refuse_is_refused_at_the_form(board):
    bad = board.client.put("/api/house/mcp-servers",
                           json={"servers": {"x": {"args": ["y"]}}})
    assert bad.status_code == 422 and "no command" in bad.json()["detail"]
    worse = board.client.put("/api/house/mcp-servers",
                             json={"servers": {"../evil": {"command": "sh"}}})
    assert worse.status_code == 422
    assert not (board.root / "mcp-servers.json").exists()


def test_a_disabled_server_stays_in_the_file_and_is_not_spawned(tmp_path):
    from yurios.world.tools.client import load_servers

    path = tmp_path / "mcp-servers.json"
    path.write_text(json.dumps({"mcpServers": {
        "on": {"command": "uvx"}, "off": {"command": "node", "disabled": True}}}))
    assert [s["name"] for s in load_servers(path)] == ["on"]


def test_testing_a_server_reports_why_it_did_not_start(board):
    answer = board.client.post("/api/house/mcp-servers/test", json={
        "name": "nothing", "server": {"command": "/no/such/binary-for-yurios"}}).json()
    assert answer["ok"] is False and answer["error"]


# --- the house scene overlay ------------------------------------------------------------

def test_the_scene_overlay_is_kept_verbatim_and_named_in_env(board):
    text = "# my rooftop\nscenes:\n  rooftop: on a rainy rooftop at night\n"
    assert board.client.put("/api/house/selfie-overlay",
                            json={"text": text}).status_code == 200
    assert (board.root / "data" / "selfie-extra.yaml").read_text() == text
    assert "SELFIE_TEMPLATES_EXTRA=./data/selfie-extra.yaml" in board.env.read_text()
    assert board.client.get("/api/house/selfie-overlay").json()["text"] == text

    broken = board.client.put("/api/house/selfie-overlay", json={"text": "scenes: [a, b"})
    assert broken.status_code == 422


# --- night jobs as modules ------------------------------------------------------------

def test_a_parked_characters_night_can_be_written_and_read_back(board):
    saved = board.client.put("/api/characters/yuri/dream-jobs/stars",
                             json={"text": JOB.format(name="stars")})
    assert saved.status_code == 200
    jobs = board.client.get("/api/characters/yuri/dream-jobs").json()
    stars = next(job for job in jobs["jobs"] if job["name"] == "stars")
    assert stars["front"]["title"] == "A small night" and jobs["running"] is False

    refused = board.client.put("/api/characters/yuri/dream-jobs/stars",
                               json={"text": JOB.format(name="other")})
    assert refused.status_code == 422 and "agree" in refused.json()["detail"]
    assert board.client.put("/api/characters/yuri/dream-jobs/..%2Fsoul",
                            json={"text": "x"}).status_code in (400, 404)

    assert board.client.delete("/api/characters/yuri/dream-jobs/stars").json()["deleted"]


def test_night_jobs_export_as_a_zip_and_import_into_another_character(board):
    for name in ("stars", "letters"):
        board.client.put(f"/api/characters/yuri/dream-jobs/{name}",
                         json={"text": JOB.format(name=name)})
    exported = board.client.get("/api/characters/yuri/dream-jobs/export")
    assert exported.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(exported.content)).namelist()
    assert {"stars.md", "letters.md"} <= set(names)
    # the folder's README is its documentation, not a job: never exported
    assert not any(n.lower() == "readme.md" for n in names)

    imported = board.client.post(
        "/api/characters/mia/dream-jobs/import",
        files=[("files", ("yuri-night-jobs.zip", exported.content, "application/zip")),
               ("files", ("broken.md", b"no frontmatter here", "text/markdown"))]).json()
    assert {"stars", "letters"} <= set(imported["imported"]), imported
    assert imported["refused"][0]["file"] == "broken.md"
    # one line per refusal — the runner's worked example is for its own editor
    assert "\n" not in imported["refused"][0]["reason"]
    assert imported["refused"][0]["reason"].startswith("a job starts with YAML frontmatter")

    # the same again: hers now, so skipped unless asked to replace
    again = board.client.post(
        "/api/characters/mia/dream-jobs/import",
        files=[("files", ("stars.md", JOB.format(name="stars").encode(), "text/markdown"))]).json()
    assert again["skipped"] == ["stars"] and again["imported"] == []


def test_an_imported_job_is_filed_under_the_name_it_declares():
    text = JOB.format(name="diary")
    assert module_routes.declared_name(text, "fallback") == "diary"
    renamed = module_routes.with_name(text, "night-diary")
    assert "name: night-diary" in renamed and "name: diary" not in renamed
    assert renamed.endswith("write one line.\n")
    # only the name line moves: the blank line before the prompt survives
    assert renamed == text.replace("name: diary", "name: night-diary")
    assert module_routes.is_job_file("diary.md")
    assert not module_routes.is_job_file("README.md")
    assert not module_routes.is_job_file("dreams/.hidden.md")
    bare = "---\ntitle: t\n---\n\nbody\n"
    assert module_routes.declared_name(bare, "from-file") == "from-file"
    assert module_routes.with_name(bare, "x").startswith("---\nname: x\ntitle: t\n---\n")


# --- scene libraries as modules --------------------------------------------------------

def test_a_scene_library_exports_as_yaml_and_parses_back_into_rows(board):
    exported = board.client.get("/api/characters/yuri/selfie-templates/export")
    assert exported.status_code == 200
    assert 'filename="yuri-selfie.yaml"' in exported.headers["content-disposition"]

    parsed = board.client.post("/api/selfie-templates/parse",
                               json={"text": exported.text}).json()["book"]
    assert parsed["slots"]["scenes"], "the shipped library has scenes"
    assert board.client.post("/api/selfie-templates/parse",
                             json={"text": "tool_hint: only words\n"}).status_code == 422
    assert board.client.post("/api/selfie-templates/parse",
                             json={"text": "scenes: [not, a, mapping]\n"}).status_code == 422
