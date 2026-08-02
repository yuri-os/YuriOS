from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

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
        shown = {f["key"]: f["value"] for g in client.get("/api/settings").json()["groups"]
                 for f in g["fields"]}
        assert shown["TELEGRAM_BOT_TOKEN_MIA"] == ""      # she has no bot yet
        assert shown["TELEGRAM_SEND_NON_TELEGRAM"] is False
        saved = client.post("/api/settings",
                            json={"TELEGRAM_BOT_TOKEN_MIA": "hers"}).json()
        assert saved["written"] == ["TELEGRAM_BOT_TOKEN_MIA"]
        saved = client.post("/api/settings",
                            json={"TELEGRAM_SEND_NON_TELEGRAM": True}).json()
        assert saved["written"] == ["TELEGRAM_SEND_NON_TELEGRAM"]
    assert "TELEGRAM_BOT_TOKEN_MIA=hers" in env_path.read_text()
    assert "TELEGRAM_SEND_NON_TELEGRAM=true" in env_path.read_text()


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


# ---- her own brain: the per-character connection (SPEC §31.1–§31.4) --------


def test_her_own_connection_beats_the_profile_she_points_at(tmp_path):
    """A profile is the house's shared connection; her record is the exception
    she was given, so the more specific one wins."""
    her = record(tmp_path, "yuri")
    her.models.chat = "ollama/llama3"
    her.connection.endpoint = "http://gpu.lan:11434"
    her.connection.api_key_env = "YURI_KEY"
    shared = ConnectionProfile(name="default", endpoint="http://localhost:11434",
                               api_key_env="OPENROUTER_API_KEY")

    cfg = config_for_character(Config(data_dir=tmp_path, _env_file=None), her, shared,
                               environ={"YURI_KEY": "sk-hers"})

    assert cfg.chat_model == "ollama/llama3"
    assert cfg.ollama_base_url == "http://gpu.lan:11434"
    assert cfg.openrouter_api_key == "sk-hers"


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
                               json={"temperature": "warm"})
        named = client.patch("/api/characters/yuri/brain",
                             json={"api_key_env": "not a variable"})

    assert refused.status_code == 400 and named.status_code == 400
    assert registry.require("yuri").models.options == {}


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
                      "utility_model": "", "endpoint": "", "api_key_env": "",
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
