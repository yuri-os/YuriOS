from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from yurios.characters import (
    CharacterPaths, CharacterRecord, CharacterRegistry, DisplayMetadata,
    LifecycleFlags,
)
from yurios.world.channels.manager import ChannelManager
from yurios.world.channels.telegram import TelegramChannel
from yurios.world.config import Config
from yurios.world.host import (
    config_for_character, create_host_app, telegram_for_character,
)


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


# ---- one bot, one character (SPEC §10.5) ------------------------------------

def shared_bot(tmp_path, **kw) -> Config:
    # _env_file=None: the developer's own .env must never decide a test (§13)
    return Config(_env_file=None, data_dir=tmp_path, telegram_bot_token="shared",
                  telegram_chat_id="1", **kw)


def test_each_character_reads_her_own_telegram_credentials(tmp_path):
    """Sharing a bot is the bug: getUpdates is exclusive per token, so two
    characters on one would fight over it forever. Her own pair wins outright."""
    base = shared_bot(tmp_path)
    env = {"TELEGRAM_BOT_TOKEN_MIA": "mia-token", "TELEGRAM_CHAT_ID_MIA": "99"}

    assert telegram_for_character(base, "mia", env) == (
        "mia-token", "99", "TELEGRAM_BOT_TOKEN_MIA", "TELEGRAM_CHAT_ID_MIA")
    # …and the character without her own pair still has the unsuffixed one
    assert telegram_for_character(base, "yuri", env) == (
        "shared", "1", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")


def test_the_shared_bot_belongs_to_the_named_character_only(tmp_path):
    base = shared_bot(tmp_path, telegram_character="yuri")

    assert telegram_for_character(base, "yuri", {})[:2] == ("shared", "1")
    # nobody else inherits it: no bot of her own means no Telegram, not a fight —
    # and the names point at hers, so the settings panel offers her one
    assert telegram_for_character(base, "mia", {}) == (
        "", "", "TELEGRAM_BOT_TOKEN_MIA", "TELEGRAM_CHAT_ID_MIA")
    # …until she is given one, and then both are up on different tokens
    assert telegram_for_character(
        base, "mia", {"TELEGRAM_BOT_TOKEN_MIA": "hers"}).token == "hers"


def test_a_per_character_bot_set_in_dotenv_is_read(tmp_path, monkeypatch):
    """The suffixed names can't be Config fields — the ids only exist at runtime —
    so pydantic-settings never parses them, and it doesn't export the .env it read
    into os.environ either. Read from the file the config was built from, or a
    token pasted into .env does nothing at all."""
    env_file = tmp_path / ".env"
    env_file.write_text('TELEGRAM_BOT_TOKEN_MIA=from-the-file\n'
                        'TELEGRAM_CHAT_ID_MIA="77"\n')
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN_MIA", raising=False)
    monkeypatch.chdir(tmp_path)          # env_file=".env" is relative to cwd
    base = Config(data_dir=tmp_path)

    assert telegram_for_character(base, "mia")[:2] == ("from-the-file", "77")
    # the real environment still wins over the file, as pydantic-settings resolves it
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_MIA", "from-the-shell")
    assert telegram_for_character(base, "mia").token == "from-the-shell"


def test_character_config_carries_her_own_telegram_pair(tmp_path):
    base = shared_bot(tmp_path, telegram_character="yuri")
    mia = record(tmp_path, "mia")

    cfg = config_for_character(base, mia, environ={})
    assert (cfg.telegram_bot_token, cfg.telegram_chat_id) == ("", "")
    assert cfg.telegram_chat_id_env == "TELEGRAM_CHAT_ID_MIA"
    assert not ChannelManager.from_config(cfg).configured   # no bot, no adapter

    cfg = config_for_character(base, mia, environ={"TELEGRAM_BOT_TOKEN_MIA": "hers"})
    (channel,) = ChannelManager.from_config(cfg).channels
    assert channel.token == "hers"                          # her own bot, uncontended
    assert channel.claim != TelegramChannel("shared").claim
    assert channel.chat_id_env == "TELEGRAM_CHAT_ID_MIA"


def test_the_settings_panel_edits_this_characters_own_bot(tmp_path, monkeypatch):
    """The gear in Mia's room must never write Yuri's credentials: the key the
    panel shows is the variable this runtime actually reads the bot from."""
    from yurios.desktop.routes import settings as panel

    base = shared_bot(tmp_path, telegram_character="yuri")

    def channel_keys(character_id):
        cfg = config_for_character(base, record(tmp_path, character_id), environ={})
        group = next(g for g in panel._groups_for(cfg) if g["group"] == "Channels")
        return cfg, [f["key"] for f in group["fields"]]

    mia, keys = channel_keys("mia")
    assert keys == ["TELEGRAM_BOT_TOKEN_MIA", "TELEGRAM_CHAT_ID_MIA"]
    _, keys = channel_keys("yuri")
    assert keys == ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]   # hers, and hers alone

    # a build with no channel knobs at all (B2's desktop config) drops the group
    bare = SimpleNamespace(**{f["attr"]: "" for g in panel.SCHEMA
                              for f in g["fields"] if not f.get("key_env")})
    assert not any(g["group"] == "Channels" for g in panel._groups_for(bare))

    # …and the round trip writes the key it showed
    env_path = tmp_path / "written.env"
    monkeypatch.setattr(panel, "ENV_PATH", env_path)
    app = FastAPI()
    app.state.rt = SimpleNamespace(cfg=mia)
    app.include_router(panel.router)
    with TestClient(app, client=("127.0.0.1", 5000)) as client:   # panel is local-only
        shown = {f["key"]: f["value"] for g in client.get("/api/settings").json()["groups"]
                 for f in g["fields"]}
        assert shown["TELEGRAM_BOT_TOKEN_MIA"] == ""      # she has no bot yet
        saved = client.post("/api/settings",
                            json={"TELEGRAM_BOT_TOKEN_MIA": "hers"}).json()
        assert saved["written"] == ["TELEGRAM_BOT_TOKEN_MIA"]
    assert "TELEGRAM_BOT_TOKEN_MIA=hers" in env_path.read_text()


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
