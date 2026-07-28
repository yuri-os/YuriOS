from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from yurios.characters import (
    CharacterPaths, CharacterRecord, CharacterRegistry, DisplayMetadata,
    LifecycleFlags,
)
from yurios.world.config import Config
from yurios.world.host import create_host_app


class FakeRuntime:
    def __init__(self, name: str):
        self.name = name
        self.started = False
        self.mind = None
        self.context = SimpleNamespace(snapshot=lambda: {"used": 12, "limit": 100})

    async def start_async(self):
        self.started = True

    async def stop_async(self):
        self.started = False

    async def set_mind_enabled(self, enabled: bool):
        return None


def record(root: Path, character_id: str = "yuri", *, enabled=True, autostart=None):
    paths = CharacterPaths.under(root / "characters" / character_id)
    paths.root.mkdir(parents=True)
    paths.portrait.write_bytes(b"portrait")
    return CharacterRecord(
        id=character_id,
        display=DisplayMetadata(name=character_id.title(), description="resident"),
        paths=paths,
        lifecycle=LifecycleFlags(
            enabled=enabled,
            autostart=enabled if autostart is None else autostart,
            review_required=False,
        ),
    )


def fake_character_app(cfg, **kwargs):
    app = FastAPI()
    app.state.rt = FakeRuntime(cfg.companion_name)

    @app.get("/api/health")
    async def health():
        return {"character": cfg.companion_name}

    return app


def test_host_lists_and_dispatches_isolated_characters(tmp_path, monkeypatch):
    registry = CharacterRegistry(tmp_path)
    registry.add(record(tmp_path, "yuri"))
    registry.add(record(tmp_path, "mika"))
    monkeypatch.setattr("yurios.world.host.create_app", fake_character_app)
    app = create_host_app(Config(data_dir=tmp_path), registry)

    with TestClient(app) as client:
        listing = client.get("/api/characters").json()
        assert {item["id"] for item in listing["characters"]} == {"yuri", "mika"}
        assert client.get("/api/characters/yuri/health").json() == {"character": "Yuri"}
        assert client.get("/api/characters/mika/health").json() == {"character": "Mika"}
        assert app.state.host.runtime("yuri") is not app.state.host.runtime("mika")


def test_host_preserves_dashboard_and_sanctuary_routes(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    (dist / "dashboard").mkdir(parents=True)
    (dist / "assets").mkdir()
    (dist / "dashboard" / "index.html").write_text(
        '<script type="module" src="/assets/dashboard-test.js"></script>',
        encoding="utf-8",
    )
    (dist / "index.html").write_text("sanctuary", encoding="utf-8")
    (dist / "assets" / "dashboard-test.js").write_text(
        "export const ready = true;", encoding="utf-8")
    registry = CharacterRegistry(tmp_path)
    registry.add(record(tmp_path))
    monkeypatch.setattr("yurios.world.host.create_app", fake_character_app)
    monkeypatch.setattr("yurios.world.host.DIST_DIR", dist)
    app = create_host_app(Config(data_dir=tmp_path), registry)

    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        asset = client.get("/assets/dashboard-test.js")
        assert asset.status_code == 200
        assert "ready = true" in asset.text
        assert client.get("/characters/yuri/sanctuary/").status_code == 200
        assert client.get("/characters/unknown/sanctuary/").status_code == 404
        assert client.get("/api/health").json() == {"character": "Yuri"}


def test_primary_prefers_running_autostart_character(tmp_path, monkeypatch):
    registry = CharacterRegistry(tmp_path)
    registry.add(record(tmp_path, "mia", autostart=False))
    registry.add(record(tmp_path, "yuri", autostart=True))
    monkeypatch.setattr("yurios.world.host.create_app", fake_character_app)
    app = create_host_app(Config(data_dir=tmp_path), registry)

    with TestClient(app) as client:
        assert app.state.host.primary_id == "yuri"
        assert app.state.host.runtime("mia") is None
        assert client.get("/api/health").json() == {"character": "Yuri"}


def test_character_selfies_are_registry_scoped(tmp_path):
    registry = CharacterRegistry(tmp_path)
    item = record(tmp_path, enabled=False)
    item.paths.selfies.mkdir(parents=True)
    (item.paths.selfies / "photo.png").write_bytes(b"png")
    registry.add(item)
    app = create_host_app(Config(data_dir=tmp_path), registry)

    with TestClient(app) as client:
        response = client.get("/api/characters/yuri/selfies/photo.png")
        assert response.status_code == 200
        assert response.content == b"png"
        assert client.get("/api/characters/yuri/selfies/../portrait.png").status_code == 404


def test_trusted_dashboard_import_starts_and_autostarts(tmp_path, monkeypatch):
    registry = CharacterRegistry(tmp_path)
    imported = record(tmp_path, "mia", autostart=False)
    calls = []

    class FakeImporter:
        def __init__(self, target):
            assert target is registry

        def import_card(self, payload, **kwargs):
            calls.append((payload, kwargs))
            imported.lifecycle.autostart = kwargs["autostart"]
            registry.add(imported)
            return imported

    monkeypatch.setattr("yurios.world.host.CharacterImporter", FakeImporter)
    monkeypatch.setattr("yurios.world.host.create_app", fake_character_app)
    app = create_host_app(Config(data_dir=tmp_path), registry)

    with TestClient(app) as client:
        response = client.post(
            "/api/characters/import",
            files={"file": ("mia.png", b"card", "image/png")},
        )
        assert response.status_code == 200
        assert response.json()["character"]["runtime_state"] == "ready"
        assert app.state.host.runtime("mia") is not None
    assert calls == [(b"card", {"autostart": True})]


def test_character_settings_are_registry_scoped(tmp_path):
    registry = CharacterRegistry(tmp_path)
    registry.add(record(tmp_path, enabled=False))
    app = create_host_app(Config(data_dir=tmp_path), registry)

    with TestClient(app) as client:
        response = client.patch(
            "/api/characters/yuri/profile",
            json={"name": "Yuri Updated", "description": "new", "model": "test/model"},
        )
        # Enabling a reviewed record starts it, so this deliberately remains disabled.
        assert response.status_code == 200
        saved = registry.require("yuri")
        assert saved.display.name == "Yuri Updated"
        assert saved.display.description == "new"
        assert saved.models.chat == "test/model"


def test_archive_removes_registry_but_preserves_tree(tmp_path):
    registry = CharacterRegistry(tmp_path)
    registry.add(record(tmp_path, enabled=False))
    app = create_host_app(Config(data_dir=tmp_path), registry)

    with TestClient(app) as client:
        response = client.post("/api/characters/yuri/archive")
        assert response.status_code == 200
    assert registry.get("yuri") is None
    assert len(list((tmp_path / "archives").iterdir())) == 1


def test_host_refuses_overlapping_character_storage(tmp_path):
    registry = CharacterRegistry(tmp_path)
    first = record(tmp_path, "yuri", enabled=False)
    second = record(tmp_path, "mika", enabled=False)
    second.paths.vault = first.paths.vault
    registry.add(first)
    registry.add(second)
    try:
        create_host_app(Config(data_dir=tmp_path), registry)
    except ValueError as exc:
        assert "storage overlaps" in str(exc)
    else:
        raise AssertionError("overlapping character roots were accepted")
