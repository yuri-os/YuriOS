from __future__ import annotations

import base64
import io
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from yurios.characters import (
    CharacterPaths, CharacterRecord, CharacterRegistry, ConnectionProfile,
    DisplayMetadata, LifecycleFlags,
)
from yurios.world.channels.manager import ChannelManager
from yurios.world.channels.telegram import TelegramChannel
from yurios.world import rewire
from yurios.world.config import Config
from yurios.world.host import (
    config_for_character, create_host_app, telegram_for_character,
)


class FakeRuntime:
    def __init__(self, cfg):
        self.cfg = cfg
        self.name = cfg.companion_name
        self.started = False
        self.mind = None
        self.context = SimpleNamespace(snapshot=lambda: {"used": 12, "limit": 100})

    async def retune(self, wanted):
        """The real diff, without the providers: this is what the runtime does
        to its live Config when her brain settings move (SPEC §31.4)."""
        applied = rewire.apply(None, self.cfg, rewire.differences(self.cfg, wanted))
        return {"applied": applied, "chat_model": self.cfg.chat_model,
                "utility_model": self.cfg.utility_model}

    async def start_async(self):
        self.started = True

    async def stop_async(self):
        self.started = False

    async def set_mind_enabled(self, enabled: bool):
        return None

    def set_hands_enabled(self, enabled: bool):
        self.hands_granted = enabled


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
    app.state.rt = FakeRuntime(cfg)

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


def test_every_body_of_a_character_is_reachable_by_her_own_id(tmp_path, monkeypatch):
    """Three clients, one runtime (SPEC §6.6, §6.7): the 3D sanctuary, the Live2D
    body and the bodyless text room. Each is addressed per character, because the
    path is what shared/runtime.js reads to aim the API and socket calls — a page
    served off an unscoped URL talks to whoever the node calls primary."""
    dist = tmp_path / "dist"
    (dist / "text").mkdir(parents=True)
    (dist / "index.html").write_text("sanctuary", encoding="utf-8")
    (dist / "text" / "index.html").write_text("text room", encoding="utf-8")
    registry = CharacterRegistry(tmp_path)
    registry.add(record(tmp_path))
    monkeypatch.setattr("yurios.world.host.create_app", fake_character_app)
    monkeypatch.setattr("yurios.world.host.DIST_DIR", dist)
    app = create_host_app(Config(data_dir=tmp_path), registry)

    with TestClient(app) as client:
        text = client.get("/characters/yuri/text/")
        assert text.status_code == 200
        assert text.text == "text room"
        assert client.get("/characters/yuri/text").status_code == 200
        assert client.get("/characters/unknown/text/").status_code == 404
        live2d = client.get("/characters/yuri/live2d", follow_redirects=False)
        assert live2d.status_code == 307
        assert live2d.headers["location"] == "/live2d/?character=yuri"


def test_socket_for_a_parked_character_is_refused_in_websocket(tmp_path, monkeypatch):
    """A card imported from elsewhere is registered but not running until it has
    been reviewed (SPEC §28) — and her text room still opens and still dials
    /ws/characters/<id>/voice. Answering that handshake with an HTTP 404 is not
    something a websocket server can put on the wire: uvicorn logs `ASGI callable
    returned without completing handshake` once per reconnect and the room dies
    silently. The refusal has to arrive as a closed socket carrying the reason."""
    registry = CharacterRegistry(tmp_path)
    registry.add(record(tmp_path, "yuri"))
    parked = record(tmp_path, "virelle", enabled=False, autostart=False)
    parked.lifecycle.review_required = True
    registry.add(parked)
    monkeypatch.setattr("yurios.world.host.create_app", fake_character_app)
    app = create_host_app(Config(data_dir=tmp_path), registry)

    with TestClient(app) as client:
        assert app.state.host.runtime("virelle") is None
        with client.websocket_connect("/ws/characters/virelle/voice") as ws:
            first = ws.receive_json()
            assert first["type"] == "error"
            assert "review" in first["message"]      # and what to do about it
            assert "studio" in first["message"]
            closed = ws.receive()
        assert closed["type"] == "websocket.close"
        assert closed["code"] == 4404          # "no runtime", so the client backs off
        assert "Virelle" in closed["reason"]
        # A close is a control frame: over 125 bytes (code included) it is not
        # truncated, it raises, and the socket ends with no close frame at all.
        # TestClient does not enforce that; a real server does.
        assert len(closed["reason"].encode("utf-8")) <= 123
        # the HTTP half of the same dispatcher still answers in HTTP
        denied = client.get("/api/characters/virelle/health")
        assert denied.status_code == 404
        assert "Virelle" in denied.json()["detail"]


def test_close_reason_always_fits_a_control_frame():
    """Whatever the host has to say, the close frame stays sendable."""
    from yurios.world.host import _close_reason

    short = "no active character"
    assert _close_reason(short) == short
    two_sentences = "Virelle is waiting on review. " + "Open her in the studio. " * 8
    assert _close_reason(two_sentences) == "Virelle is waiting on review."
    unbroken = "über " * 60                        # no sentence to fall back to
    assert len(_close_reason(unbroken).encode("utf-8")) <= 123
    assert _close_reason(unbroken).endswith("...")


def test_socket_with_no_active_character_is_refused_in_websocket(tmp_path, monkeypatch):
    """Same rule for the unscoped /ws/voice the single-character clients use: it
    falls through to the primary mount, which may have nobody behind it."""
    registry = CharacterRegistry(tmp_path)
    registry.add(record(tmp_path, enabled=False))
    monkeypatch.setattr("yurios.world.host.create_app", fake_character_app)
    app = create_host_app(Config(data_dir=tmp_path), registry)

    with TestClient(app) as client:
        assert app.state.host.primary_id is None
        with client.websocket_connect("/ws/voice") as ws:
            assert ws.receive_json() == {"type": "error", "message": "no active character"}
            assert ws.receive()["code"] == 4404
        assert client.get("/api/health").status_code == 503


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


def test_portrait_is_revalidated_not_cached_blind(tmp_path):
    """Her face changes behind one stable URL, so the browser has to ask."""
    registry = CharacterRegistry(tmp_path)
    registry.add(record(tmp_path, enabled=False))
    app = create_host_app(Config(data_dir=tmp_path), registry)

    with TestClient(app) as client:
        response = client.get("/api/characters/yuri/portrait")
        assert response.status_code == 200
        assert response.content == b"portrait"
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["etag"]           # 304 still saves the bytes


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


def test_dashboard_import_stays_parked_and_does_not_request_autostart(tmp_path, monkeypatch):
    registry = CharacterRegistry(tmp_path)
    imported = record(tmp_path, "mia", autostart=False)
    imported.lifecycle.enabled = False
    imported.lifecycle.review_required = True
    calls = []
    utility_calls = []

    class FakeImporter:
        def __init__(self, target):
            assert target is registry

        def import_card(self, payload, **kwargs):
            calls.append((payload, kwargs))
            registry.add(imported)
            return imported

    monkeypatch.setattr("yurios.world.host.CharacterImporter", FakeImporter)
    monkeypatch.setattr("yurios.world.host.create_app", fake_character_app)
    monkeypatch.setattr("yurios.app.main.build_utility_model",
                        lambda cfg: utility_calls.append(cfg))
    app = create_host_app(Config(data_dir=tmp_path), registry)

    with TestClient(app) as client:
        response = client.post(
            "/api/characters/import",
            files={"file": ("mia.png", b"card", "image/png")},
        )
        assert response.status_code == 200
        assert response.json()["character"]["review_required"] is True
        assert response.json()["character"]["runtime_state"] == "offline"
        assert app.state.host.runtime("mia") is None
    assert calls == [(b"card", {})]
    assert utility_calls == []


def test_approve_clears_review_and_starts_the_runtime(tmp_path, monkeypatch):
    """The one act that used to be a side effect of saving something else."""
    registry = CharacterRegistry(tmp_path)
    parked = record(tmp_path, "virelle", enabled=False, autostart=False)
    parked.lifecycle.review_required = True
    registry.add(parked)
    monkeypatch.setattr("yurios.world.host.create_app", fake_character_app)
    app = create_host_app(Config(data_dir=tmp_path), registry)

    with TestClient(app) as client:
        assert app.state.host.runtime("virelle") is None
        body = client.post("/api/characters/virelle/approve").json()
        assert body["started"] is True and body["error"] is None
        assert body["character"]["review_required"] is False
        assert body["character"]["runtime_state"] == "ready"
        assert app.state.host.runtime("virelle") is not None
        # and the dispatcher reaches her runtime instead of turning callers away
        assert client.get("/api/characters/virelle/health").status_code == 200

    saved = registry.require("virelle").lifecycle
    assert (saved.review_required, saved.enabled, saved.autostart) == (False, True, True)


def test_approve_reports_a_failed_start_without_reverting_the_approval(tmp_path, monkeypatch):
    """Approved and running are two facts. A runtime that will not come up is
    hers to report — re-parking her behind review would only hide it."""
    registry = CharacterRegistry(tmp_path)
    parked = record(tmp_path, "virelle", enabled=False, autostart=False)
    parked.lifecycle.review_required = True
    registry.add(parked)

    def broken_app(cfg, **kwargs):
        raise RuntimeError("no connection profile named 'default'")

    monkeypatch.setattr("yurios.world.host.create_app", broken_app)
    app = create_host_app(Config(data_dir=tmp_path), registry)

    with TestClient(app) as client:
        body = client.post("/api/characters/virelle/approve").json()
        assert body["started"] is False
        assert "connection profile" in body["error"]
        assert body["character"]["review_required"] is False
        assert body["character"]["runtime_state"] == "failed"
    assert registry.require("virelle").lifecycle.enabled is True


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


def test_journal_rejects_noncanonical_days_and_ignores_bad_stems(tmp_path):
    registry = CharacterRegistry(tmp_path)
    item = record(tmp_path, enabled=False)
    episodic = item.paths.vault / "memory" / "episodic"
    episodic.mkdir(parents=True)
    (episodic / "2026-08-12.md").write_text("### 10:00  hello\n")
    (episodic / "2026-02-30.md").write_text("### 10:00  impossible\n")
    (episodic / "notes.md").write_text("### 10:00  malformed\n")
    registry.add(item)

    with TestClient(create_host_app(Config(data_dir=tmp_path), registry)) as client:
        listing = client.get("/api/characters/yuri/journal").json()
        assert [row["day"] for row in listing["days"]] == ["2026-08-12"]
        for day in ("../card", "2026-2-03", "2026-02-30"):
            assert client.get("/api/characters/yuri/journal",
                              params={"day": day}).status_code == 400


def test_portrait_route_accepts_jpeg_but_stores_sanitized_png(tmp_path):
    registry = CharacterRegistry(tmp_path)
    item = record(tmp_path, enabled=False)
    registry.add(item)
    source = io.BytesIO()
    Image.new("RGB", (11, 13), (30, 50, 70)).save(source, "JPEG")

    with TestClient(create_host_app(Config(data_dir=tmp_path), registry)) as client:
        response = client.post("/api/characters/yuri/portrait", json={
            "image": base64.b64encode(source.getvalue()).decode("ascii")})
        assert response.status_code == 200

    assert item.paths.portrait.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_purge_uses_single_use_json_challenge_and_removes_via_tombstone(tmp_path):
    registry = CharacterRegistry(tmp_path)
    item = record(tmp_path, enabled=False)
    registry.add(item)
    app = create_host_app(Config(data_dir=tmp_path), registry)

    with TestClient(app) as client:
        assert client.delete("/api/characters/yuri/purge?confirm=yuri").status_code == 415
        prepared = client.post("/api/characters/yuri/purge/prepare")
        challenge = prepared.json()["challenge"]
        assert len(challenge) >= 32
        assert prepared.headers["cache-control"] == "no-store"
        response = client.request("DELETE", "/api/characters/yuri/purge",
                                  json={"challenge": challenge})
        assert response.status_code == 200
        assert response.json() == {
            "purged": True, "id": "yuri", "cleanup_pending": False}
        assert challenge not in str(response.request.url)

    assert registry.get("yuri") is None
    assert not item.paths.root.exists()
    assert list((tmp_path / ".purging").iterdir()) == []


def test_purge_challenge_is_character_bound_and_consumed_on_use(tmp_path):
    registry = CharacterRegistry(tmp_path)
    registry.add(record(tmp_path, "yuri", enabled=False))
    registry.add(record(tmp_path, "mika", enabled=False))

    with TestClient(create_host_app(Config(data_dir=tmp_path), registry)) as client:
        challenge = client.post(
            "/api/characters/yuri/purge/prepare").json()["challenge"]
        wrong = client.request("DELETE", "/api/characters/mika/purge",
                               json={"challenge": challenge})
        reused = client.request("DELETE", "/api/characters/yuri/purge",
                                json={"challenge": challenge})
    assert wrong.status_code == 400 and reused.status_code == 400
    assert registry.get("yuri") is not None and registry.get("mika") is not None


def test_purge_rolls_back_data_and_running_state_if_registry_commit_fails(
        tmp_path, monkeypatch):
    registry = CharacterRegistry(tmp_path)
    item = record(tmp_path)
    registry.add(item)
    monkeypatch.setattr("yurios.world.host.create_app", fake_character_app)
    app = create_host_app(Config(data_dir=tmp_path), registry)

    with TestClient(app) as client:
        challenge = client.post(
            "/api/characters/yuri/purge/prepare").json()["challenge"]
        monkeypatch.setattr(registry, "remove",
                            lambda _id: (_ for _ in ()).throw(OSError("disk full")))
        response = client.request("DELETE", "/api/characters/yuri/purge",
                                  json={"challenge": challenge})
        assert response.status_code == 500
        assert item.paths.root.is_dir()
        assert registry.get("yuri") is item
        assert app.state.host.runtime("yuri") is not None


def test_archive_rolls_back_rename_when_registry_commit_fails(tmp_path, monkeypatch):
    registry = CharacterRegistry(tmp_path)
    item = record(tmp_path, enabled=False)
    registry.add(item)
    monkeypatch.setattr(registry, "remove",
                        lambda _id: (_ for _ in ()).throw(OSError("disk full")))

    with TestClient(create_host_app(Config(data_dir=tmp_path), registry)) as client:
        response = client.post("/api/characters/yuri/archive")
        assert response.status_code == 500

    assert item.paths.root.is_dir()
    assert registry.get("yuri") is item
    assert list((tmp_path / "archives").iterdir()) == []


def test_failed_purge_cleanup_leaves_unregistered_tombstone(tmp_path, monkeypatch):
    registry = CharacterRegistry(tmp_path)
    item = record(tmp_path, enabled=False)
    registry.add(item)
    monkeypatch.setattr("yurios.world.host.shutil.rmtree",
                        lambda _path: (_ for _ in ()).throw(OSError("busy")))

    with TestClient(create_host_app(Config(data_dir=tmp_path), registry)) as client:
        challenge = client.post(
            "/api/characters/yuri/purge/prepare").json()["challenge"]
        response = client.request("DELETE", "/api/characters/yuri/purge",
                                  json={"challenge": challenge})
        assert response.status_code == 200
        assert response.json()["cleanup_pending"] is True

    assert registry.get("yuri") is None
    assert not item.paths.root.exists()
    assert len(list((tmp_path / ".purging").iterdir())) == 1


def test_optimizer_rejects_unbounded_instructions_before_building_a_model(tmp_path):
    app = create_host_app(Config(data_dir=tmp_path), CharacterRegistry(tmp_path))
    with TestClient(app) as client:
        response = client.post("/api/studio/optimize", json={
            "draft": {}, "instructions": "x" * 16_385})
    assert response.status_code == 422
    assert "instructions" in response.json()["detail"]


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
    assert keys == ["TELEGRAM_BOT_TOKEN_MIA", "TELEGRAM_CHAT_ID_MIA",
                    "TELEGRAM_SEND_NON_TELEGRAM"]
    _, keys = channel_keys("yuri")
    assert keys == ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                    "TELEGRAM_SEND_NON_TELEGRAM"]   # hers, and hers alone

    # a build with no channel knobs at all (B2's desktop config) drops the group
    bare = SimpleNamespace(**{f["attr"]: "" for g in panel.SCHEMA
                              if g["group"] != "Channels"
                              for f in g["fields"] if not f.get("key_env")})
    assert not any(g["group"] == "Channels" for g in panel._groups_for(bare))

    # …and the round trip writes the key it showed
    env_path = tmp_path / "written.env"
    monkeypatch.setattr(panel, "ENV_PATH", env_path)
    app = FastAPI()
    app.state.rt = SimpleNamespace(cfg=mia)
    app.include_router(panel.router)
    with TestClient(app, client=("127.0.0.1", 5000)) as client:   # panel is local-only
        shown = {f["key"]: f for g in client.get("/api/settings").json()["groups"]
                 for f in g["fields"]}
        assert shown["TELEGRAM_BOT_TOKEN_MIA"]["configured"] is False
        assert "value" not in shown["TELEGRAM_BOT_TOKEN_MIA"]
        assert shown["TELEGRAM_SEND_NON_TELEGRAM"]["value"] is False
        saved = client.post("/api/settings",
                            json={"TELEGRAM_BOT_TOKEN_MIA": "hers"}).json()
        assert saved["written"] == ["TELEGRAM_BOT_TOKEN_MIA"]
        saved = client.post("/api/settings",
                            json={"TELEGRAM_SEND_NON_TELEGRAM": True}).json()
        assert saved["written"] == ["TELEGRAM_SEND_NON_TELEGRAM"]
    assert "TELEGRAM_BOT_TOKEN_MIA=hers" in env_path.read_text()
    assert "TELEGRAM_SEND_NON_TELEGRAM=true" in env_path.read_text()


def test_the_board_carries_the_house_settings_panel_with_nothing_running(
        tmp_path, monkeypatch):
    """The switchboard is where you go when she is NOT up (SPEC §32.3).

    The `.env` panel used to live only inside a character runtime, reached at
    the root by the primary-character fallback — so on the node that most needs
    it (a fresh install, or every character parked) the settings screen answered
    503. The host declares it itself, against the house config, and serves the
    panel's own source too."""
    from yurios.desktop.routes import settings as panel

    env = tmp_path / "house.env"
    env.write_text("CHAT_MODEL=old\n", encoding="utf-8")
    monkeypatch.setattr(panel, "ENV_PATH", env)
    registry = CharacterRegistry(tmp_path)
    app = create_host_app(Config(data_dir=tmp_path, chat_model="old"), registry)

    with TestClient(app) as client:
        assert client.get("/api/characters").json()["characters"] == []

        body = client.get("/api/settings").json()
        fields = {f["key"]: f for g in body["groups"] for f in g["fields"]}
        assert fields["CHAT_MODEL"]["value"] == "old"
        assert body["env_path"] == str(env)

        saved = client.post("/api/settings", json={"CHAT_MODEL": "new"}).json()
        assert saved["written"] == ["CHAT_MODEL"]
        assert "CHAT_MODEL=new" in env.read_text()

        # …and the panel's own source, which the board loads by path
        assert "/api/settings" in client.get("/shared/settings.js").text
        assert client.get("/shared/settings.css").status_code == 200

    board = (Path(__file__).resolve().parents[1] / "web" / "dashboard" / "index.html")
    markup = board.read_text()
    assert 'id="settings-open"' in markup and 'data-scope="house"' in markup
    assert '<script src="/shared/settings.js"></script>' in markup


def test_house_settings_never_display_a_running_characters_effective_paths(
        tmp_path, monkeypatch):
    from yurios.desktop.routes import settings as panel

    env = tmp_path / "house.env"
    env.write_text("VAULT_DIR=./legacy-vault\nCHAT_MODEL=lm_studio/house\n",
                   encoding="utf-8")
    monkeypatch.setattr(panel, "ENV_PATH", env)
    registry = CharacterRegistry(tmp_path)
    resident = record(tmp_path, "adia")
    resident.models.chat = "ollama/adia"
    registry.add(resident)
    monkeypatch.setattr("yurios.world.host.create_app", fake_character_app)
    app = create_host_app(Config(
        _env_file=None, data_dir=tmp_path, vault_dir=Path("./legacy-vault"),
        chat_model="lm_studio/house"), registry)

    with TestClient(app) as client:
        fields = {field["key"]: field
                  for group in client.get("/api/settings").json()["groups"]
                  for field in group["fields"]}

    assert fields["VAULT_DIR"]["value"] == "./legacy-vault"
    assert fields["CHAT_MODEL"]["value"] == "lm_studio/house"
    assert "data/characters/adia/vault" not in fields["VAULT_DIR"]["value"]


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


# ---- two switches in series (SPEC §26.1, §18.4.6) --------------------------


def test_a_character_cannot_talk_her_way_past_the_house_hands_switch(tmp_path):
    """`MIND_TOOLS_ENABLED` decides whether anything on this machine may reach
    for a tool unasked. Hers decides whether she is one of the ones that may.

    The two are multiplied in `Hands.enabled`, not folded into her config here:
    hers is a live switch, and a config that had already absorbed a `False`
    could never be told `True` again without a restart — which is the one thing
    a kill switch may not need. The config carries the house's word only, so
    what her config says is exactly what the house said."""
    her = record(tmp_path, "yuri")
    off_house = Config(data_dir=tmp_path, _env_file=None, mind_tools_enabled=False)
    on_house = Config(data_dir=tmp_path, _env_file=None, mind_tools_enabled=True)

    for hers in (True, False):
        her.loops.hands = hers
        assert not config_for_character(off_house, her).mind_tools_enabled
        assert config_for_character(on_house, her).mind_tools_enabled


def test_the_hands_switch_goes_back_on_without_a_restart(tmp_path):
    """Found live: revoked, then granted again, and she never reached for
    anything — with nothing in the trace to say why, because off is invisible.
    The grant lives on the runtime so both directions land on the next tick."""
    from yurios.world.main import Runtime

    class FakeRuntime:
        mind = None
        _hands_granted = True
        set_hands_enabled = Runtime.set_hands_enabled

    rt = FakeRuntime()
    rt.set_hands_enabled(False)
    assert rt._hands_granted is False
    rt.set_hands_enabled(True)
    assert rt._hands_granted is True, "a switch that only turns off is a fuse"


def test_the_house_hands_switch_is_off_out_of_the_box(tmp_path):
    """The default-off proof, at the configuration layer: nothing a fresh
    install does turns this on by accident."""
    her = record(tmp_path, "yuri")
    assert not Config(_env_file=None).mind_tools_enabled
    assert not config_for_character(
        Config(data_dir=tmp_path, _env_file=None), her).mind_tools_enabled
    # …and even switched on, no hand is named, so nothing is reachable
    assert Config(_env_file=None).mind_tool_allowlist == ""


# ---- her own brain: the per-character connection (SPEC §31.1–§31.4) --------


def test_profile_grants_custom_endpoint_and_credential_as_one_pair(tmp_path):
    her = record(tmp_path, "yuri")
    her.models.chat = "ollama/llama3"
    her.models.utility = "openrouter/utility"
    shared = ConnectionProfile(name="default", endpoint="http://gpu.lan:11434",
                               api_key_env="YURIOS_MODEL_API_KEY_YURI")

    cfg = config_for_character(Config(data_dir=tmp_path, _env_file=None,
                                      openrouter_api_key="sk-openrouter"), her, shared,
                               environ={"YURIOS_MODEL_API_KEY_YURI": "sk-hers"})

    assert cfg.chat_model == "ollama/llama3"
    assert cfg.ollama_base_url == "http://gpu.lan:11434"
    assert cfg.connection_api_key == "sk-hers"
    assert cfg.openrouter_api_key == "sk-openrouter"

    from yurios.app.main import build_chat_model, build_utility_model
    provider = build_chat_model(cfg)
    assert provider.api_base == "http://gpu.lan:11434"
    assert provider.api_key == "sk-hers"
    utility = build_utility_model(cfg)
    assert utility.api_base is None
    assert utility.api_key == "sk-openrouter"


def test_openrouter_key_is_never_sent_to_a_custom_endpoint(tmp_path):
    her = record(tmp_path, "yuri")
    her.models.chat = "lm_studio/local"
    profile = ConnectionProfile(name="default", endpoint="http://gpu.lan:1234/v1")
    cfg = config_for_character(
        Config(data_dir=tmp_path, _env_file=None, openrouter_api_key="sk-openrouter",
               gguf_fallback=False),
        her, profile, environ={})

    from yurios.app.main import build_chat_model
    provider = build_chat_model(cfg)
    assert provider.api_base == "http://gpu.lan:1234/v1"
    assert provider.api_key is None


def test_an_endpoint_for_the_other_server_is_not_forced_on_her(tmp_path):
    """The seeded `default` profile carries whichever provider the host's own
    model uses — a character who moves to the other one inherits the host's url
    for *hers*, not a pointer at the wrong server."""
    base = Config(data_dir=tmp_path, _env_file=None,
                  chat_model="lm_studio/house", lmstudio_base_url="http://lms:1234/v1",
                  ollama_base_url="http://ollama:11434")
    her = record(tmp_path, "yuri")
    her.models.chat = "ollama/llama3"
    seeded = ConnectionProfile(name="default", endpoint="http://lms:1234/v1")

    cfg = config_for_character(base, her, seeded, environ={})

    assert cfg.ollama_base_url == "http://ollama:11434"


def test_her_knobs_are_coerced_out_of_the_registrys_json(tmp_path):
    her = record(tmp_path, "yuri")
    her.models.options = {"temperature": "0.35", "chat_thinking": "true",
                          "nonsense": 1, "context_length": "not a number"}

    cfg = config_for_character(Config(data_dir=tmp_path, _env_file=None), her,
                               environ={})

    assert cfg.temperature == 0.35 and cfg.chat_thinking is True
    assert not hasattr(cfg, "nonsense")
    assert cfg.context_length == Config(_env_file=None).context_length  # dropped, loudly


def test_the_brain_panel_shows_what_is_hers_and_what_is_inherited(tmp_path, monkeypatch):
    registry = CharacterRegistry(tmp_path)
    her = record(tmp_path, "yuri")
    her.models.chat = "ollama/llama3"
    registry.add(her)
    monkeypatch.setattr("yurios.world.host.create_app", fake_character_app)
    base = Config(data_dir=tmp_path, _env_file=None, chat_model="lm_studio/house")

    with TestClient(create_host_app(base, registry)) as client:
        panel = client.get("/api/characters/yuri/brain").json()

    fields = {f["key"]: f for f in panel["fields"]}
    assert fields["chat_model"]["value"] == "ollama/llama3"
    assert fields["chat_model"]["inherited"] == "lm_studio/house"
    assert fields["utility_model"]["value"] == ""          # blank = inherit
    assert panel["effective"]["chat_model"] == "ollama/llama3"
    assert panel["running"] is True


def test_saving_her_brain_reaches_the_live_runtime_without_a_restart(tmp_path, monkeypatch):
    registry = CharacterRegistry(tmp_path)
    registry.add(record(tmp_path, "yuri"))
    monkeypatch.setattr("yurios.world.host.create_app", fake_character_app)
    base = Config(data_dir=tmp_path, _env_file=None, chat_model="lm_studio/house")

    with TestClient(create_host_app(base, registry)) as client:
        host = client.app.state.host
        before = host.runtime("yuri")
        saved = client.patch("/api/characters/yuri/brain",
                             json={"chat_model": "ollama/llama3",
                                   "temperature": "0.4"}).json()
        after = host.runtime("yuri")

    assert after is before                      # the same runtime, mid-conversation
    assert saved["applied"] == ["chat_model", "temperature"]
    assert before.cfg.chat_model == "ollama/llama3" and before.cfg.temperature == 0.4
    assert registry.require("yuri").models.chat == "ollama/llama3"
    assert registry.require("yuri").models.options["temperature"] == 0.4


def test_clearing_an_override_hands_her_back_to_the_env(tmp_path, monkeypatch):
    registry = CharacterRegistry(tmp_path)
    her = record(tmp_path, "yuri")
    her.models.chat = "ollama/llama3"
    her.models.options = {"temperature": 0.4}
    registry.add(her)
    monkeypatch.setattr("yurios.world.host.create_app", fake_character_app)
    base = Config(data_dir=tmp_path, _env_file=None, chat_model="lm_studio/house",
                  temperature=0.9)

    with TestClient(create_host_app(base, registry)) as client:
        rt = client.app.state.host.runtime("yuri")
        saved = client.patch("/api/characters/yuri/brain",
                             json={"chat_model": "", "temperature": ""}).json()

    assert saved["effective"]["chat_model"] == "lm_studio/house"
    assert rt.cfg.chat_model == "lm_studio/house" and rt.cfg.temperature == 0.9
    assert registry.require("yuri").models.chat == ""
    assert "temperature" not in registry.require("yuri").models.options


def test_an_unparseable_knob_is_refused_before_anything_is_written(tmp_path, monkeypatch):
    registry = CharacterRegistry(tmp_path)
    registry.add(record(tmp_path, "yuri"))
    monkeypatch.setattr("yurios.world.host.create_app", fake_character_app)

    with TestClient(create_host_app(Config(data_dir=tmp_path, _env_file=None),
                                    registry)) as client:
        refused = client.patch("/api/characters/yuri/brain",
                               json={"chat_model": "ollama/changed", "temperature": "warm"})
        named = client.patch("/api/characters/yuri/brain",
                              json={"api_key_env": "not a variable"})

    assert refused.status_code == 400 and named.status_code == 400
    assert registry.require("yuri").models.options == {}
    assert registry.require("yuri").models.chat == ""


def test_character_profile_rejects_direct_connection_fields_without_mutating(tmp_path):
    registry = CharacterRegistry(tmp_path)
    registry.add(record(tmp_path, "yuri", enabled=False))

    with TestClient(create_host_app(Config(data_dir=tmp_path, _env_file=None), registry)) as client:
        response = client.patch("/api/characters/yuri/profile", json={
            "name": "Changed First", "endpoint": "http://attacker.invalid/v1",
            "api_key_env": "AWS_SECRET_ACCESS_KEY",
        })

    assert response.status_code == 400
    assert "host-owned" in response.json()["detail"]
    assert registry.require("yuri").display.name == "Yuri"


def test_connection_profile_submission_is_validated_before_storage(tmp_path):
    registry = CharacterRegistry(tmp_path)
    app = create_host_app(Config(data_dir=tmp_path, _env_file=None), registry)

    with TestClient(app) as client:
        bad_url = client.put("/api/connections/gpu", json={
            "endpoint": "file:///tmp/socket", "api_key_env": "GPU_KEY"})
        bad_env = client.put("/api/connections/gpu", json={
            "endpoint": "https://gpu.example/v1", "api_key_env": "not a variable"})
        openrouter_leak = client.put("/api/connections/gpu", json={
            "endpoint": "https://gpu.example/v1",
            "api_key_env": "OPENROUTER_API_KEY"})
        unrelated_secret = client.put("/api/connections/gpu", json={
            "endpoint": "https://gpu.example/v1",
            "api_key_env": "AWS_SECRET_ACCESS_KEY"})

    assert [bad_url.status_code, bad_env.status_code, openrouter_leak.status_code,
            unrelated_secret.status_code] == [400, 400, 400, 400]
    assert app.state.host.connections.get("gpu") is None


def test_authenticated_profile_update_applies_endpoint_and_key_together(
        tmp_path, monkeypatch):
    registry = CharacterRegistry(tmp_path)
    her = record(tmp_path, "yuri")
    her.models.chat = "ollama/llama3"
    registry.add(her)
    monkeypatch.setenv("YURIOS_MODEL_API_KEY_GPU", "sk-gpu")
    monkeypatch.setattr("yurios.world.host.create_app", fake_character_app)

    with TestClient(create_host_app(Config(data_dir=tmp_path, _env_file=None), registry)) as client:
        runtime = client.app.state.host.runtime("yuri")
        response = client.put("/api/connections/default", json={
            "endpoint": "https://gpu.example/v1",
            "api_key_env": "YURIOS_MODEL_API_KEY_GPU"})

    assert response.status_code == 200
    assert response.json()["applied"]["yuri"] == [
        "connection_api_key", "ollama_base_url"]
    assert runtime.cfg.ollama_base_url == "https://gpu.example/v1"
    assert runtime.cfg.connection_api_key == "sk-gpu"


def test_a_model_change_on_the_profile_form_also_skips_the_restart(tmp_path, monkeypatch):
    registry = CharacterRegistry(tmp_path)
    registry.add(record(tmp_path, "yuri"))
    monkeypatch.setattr("yurios.world.host.create_app", fake_character_app)

    with TestClient(create_host_app(Config(data_dir=tmp_path, _env_file=None),
                                    registry)) as client:
        host = client.app.state.host
        before = host.runtime("yuri")
        # the switchboard posts the whole form, not a diff: re-sending the same
        # voice and body must not count as a change
        whole_form = {"name": "Yuri", "voice": "", "model": "ollama/llama3",
                      "utility_model": "", "connection_profile": "default",
                      "body_backend": "", "body_model": "", "description": "resident",
                      "mind": True, "utility": True, "dream": True}
        saved = client.patch("/api/characters/yuri/profile", json=whole_form).json()
        assert host.runtime("yuri") is before
        assert saved["applied"] == ["chat_model"]
        # …but her voice is wired at construction, so that one still rebuilds her
        client.patch("/api/characters/yuri/profile", json={"voice": "af_sky"})
        assert host.runtime("yuri") is not before


def test_the_primary_answers_the_unprefixed_brain_route(tmp_path, monkeypatch):
    """The single-companion install's pages carry no character in the URL."""
    registry = CharacterRegistry(tmp_path)
    registry.add(record(tmp_path, "yuri"))
    monkeypatch.setattr("yurios.world.host.create_app", fake_character_app)

    with TestClient(create_host_app(Config(data_dir=tmp_path, _env_file=None),
                                    registry)) as client:
        assert client.get("/api/brain").json()["character"] == "yuri"
        assert client.patch("/api/brain", json={"chat_model": "ollama/llama3"}
                            ).json()["effective"]["chat_model"] == "ollama/llama3"


# --- the per-character doorbell (SPEC §18.4.6) --------------------------------


def test_the_house_switch_and_hers_are_in_series(tmp_path):
    """Two switches, both of which must be on.

    The point of the house switch is that nothing on this machine can put a
    notification on your desktop until you say so once. A per-character toggle
    that could override it would make that promise conditional on every
    character record in the registry, including imported ones.
    """
    her = record(tmp_path, "mia")

    off = Config(data_dir=tmp_path, _env_file=None, notify_enabled=False)
    her.notify.enabled = True
    assert config_for_character(off, her, environ={}).notify_enabled is False

    on = Config(data_dir=tmp_path, _env_file=None, notify_enabled=True)
    assert config_for_character(on, her, environ={}).notify_enabled is True
    her.notify.enabled = False
    assert config_for_character(on, her, environ={}).notify_enabled is False


def test_a_character_notifies_unless_told_otherwise(tmp_path):
    """On by default, because the house switch is the opt-in. Two stacked
    opt-ins would leave a new character silent after you enabled notifications,
    with nothing on screen to say why."""
    assert record(tmp_path, "mia").notify.enabled is True


def test_muting_her_doorbell_rebuilds_her(tmp_path):
    """The NotifyChannel is built once, by the channel manager at start — a
    change the runtime cannot see is a switch that does nothing until reboot."""
    from yurios.world.host import _construction_fingerprint

    her = record(tmp_path, "mia")
    before = _construction_fingerprint(her)
    her.notify.enabled = False
    assert _construction_fingerprint(her) != before


def test_muting_her_does_not_stop_her_reaching_out(tmp_path):
    """Off is *delivery* off, not her. The mind loop is untouched, so she still
    decides, still spends the Gate 2 interrupt, and still fills her inbox."""
    her = record(tmp_path, "mia")
    her.notify.enabled = False
    cfg = config_for_character(
        Config(data_dir=tmp_path, _env_file=None, notify_enabled=True),
        her, environ={})
    assert cfg.notify_enabled is False
    assert cfg.mind_enabled is True


def test_the_switchboard_can_mute_one_character(tmp_path, monkeypatch):
    """The gap this closes: notifications were a single `.env` flag, so the only
    answer to "which of them may ring?" was "all of them or none"."""
    registry = CharacterRegistry(tmp_path)
    registry.add(record(tmp_path, "yuri"))
    registry.add(record(tmp_path, "mika"))
    monkeypatch.setattr("yurios.world.host.create_app", fake_character_app)
    app = create_host_app(
        Config(data_dir=tmp_path, _env_file=None, notify_enabled=True), registry)

    with TestClient(app) as client:
        board = {row["id"]: row for row in client.get("/api/characters").json()["characters"]}
        assert board["yuri"]["notify"] == {"enabled": True, "available": True}

        muted = client.patch("/api/characters/mika/controls", json={"notify": False})
        assert muted.status_code == 200
        assert muted.json()["character"]["notify"]["enabled"] is False

        board = {row["id"]: row for row in client.get("/api/characters").json()["characters"]}
        assert board["yuri"]["notify"]["enabled"] is True      # untouched
        assert board["mika"]["notify"]["enabled"] is False

    # …and it survives the process, because it is on her record, not in memory.
    assert CharacterRegistry(tmp_path).require("mika").notify.enabled is False


def test_the_board_says_when_the_house_switch_is_off(tmp_path, monkeypatch):
    """`available` is what stops the toggle being a lie: with NOTIFY_ENABLED off
    the switch saves fine and can never ring, and the panel has to be able to
    say so rather than leaving you to wonder why nothing arrives."""
    registry = CharacterRegistry(tmp_path)
    registry.add(record(tmp_path, "yuri"))
    monkeypatch.setattr("yurios.world.host.create_app", fake_character_app)
    app = create_host_app(
        Config(data_dir=tmp_path, _env_file=None, notify_enabled=False), registry)

    with TestClient(app) as client:
        (row,) = client.get("/api/characters").json()["characters"]
        assert row["notify"] == {"enabled": True, "available": False}
        settings = client.get("/api/characters/yuri/profile").json()["settings"]
        assert (settings["notify"], settings["notify_available"]) == (True, False)


def test_the_profile_form_saves_her_doorbell(tmp_path, monkeypatch):
    registry = CharacterRegistry(tmp_path)
    registry.add(record(tmp_path, "yuri"))
    monkeypatch.setattr("yurios.world.host.create_app", fake_character_app)
    app = create_host_app(
        Config(data_dir=tmp_path, _env_file=None, notify_enabled=True), registry)

    with TestClient(app) as client:
        saved = client.patch("/api/characters/yuri/profile", json={"notify": False})
        assert saved.status_code == 200
        assert saved.json()["character"]["notify"]["enabled"] is False
        assert client.get("/api/characters/yuri/profile").json()["settings"]["notify"] is False
