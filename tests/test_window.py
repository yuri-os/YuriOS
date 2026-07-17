"""The desktop window + the second body (SPEC §6.5–§6.6).

world/window.py must import with no GUI backend installed (pywebview is lazy
inside run()), reuse the shared B2 launcher helpers rather than copy them,
build a URL that carries ?desktop=1 for whichever body is configured and never
points at 0.0.0.0, and refuse an occupied port. The pages must actually honour
the flag, and the Live2D client must be served with its two API needs
answered — all source-scanned or driven over the real app, the §3.4 palette-map
discipline.
"""
from __future__ import annotations

import socket
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from starlette.testclient import TestClient           # noqa: E402

from yurios.desktop import window as b2_window               # noqa: E402
from yurios.desktop.voice.backends.fakes import FakeBrain    # noqa: E402
from yurios.world import window                              # noqa: E402
from yurios.world.config import Config                       # noqa: E402
from yurios.world.main import create_app                     # noqa: E402
from yurios.world.routes import live2d as live2d_route       # noqa: E402

WEB = Path(__file__).resolve().parent.parent / "web"


@pytest.fixture
def client(cfg):
    cfg = cfg.model_copy(update={"tools_backend": "off", "mind_enabled": False})
    app = create_app(cfg, brain=FakeBrain())
    # loopback client host so the settings router's local-only gate opens
    with TestClient(app, client=("127.0.0.1", 5555)) as c:
        c.app = app
        yield c


# ---- the launcher (§6.5) ----------------------------------------------------

def test_module_imports_without_pywebview():
    # importing world.window at the top of this file already proves it; assert
    # the public helpers are present so the loader can't silently no-op.
    assert callable(window.run) and callable(window.desktop_url)


def test_desktop_url_carries_flag_and_is_local():
    assert window.desktop_url(Config(port=8767)) == "http://127.0.0.1:8767/?desktop=1"


def test_desktop_url_never_targets_wildcard_host():
    # a browser can't connect to 0.0.0.0 — it must be rewritten to loopback
    url = window.desktop_url(Config(host="0.0.0.0", port=9000))
    assert url == "http://127.0.0.1:9000/?desktop=1"


def test_desktop_url_picks_the_body():
    assert window.desktop_url(Config(port=8767, desktop_body="live2d")) \
        == "http://127.0.0.1:8767/live2d/?desktop=1"
    with pytest.raises(SystemExit, match="DESKTOP_BODY"):
        window.desktop_url(Config(port=8767, desktop_body="hologram"))


def test_reuses_the_shared_launcher_helpers():
    # §2.2's discipline: shared code is called, not copied — the readiness
    # probe and the engine pick are the desktop module's own functions, reused.
    assert window._wait_for_server is b2_window._wait_for_server
    assert window._pick_gui is b2_window._pick_gui


def test_run_refuses_an_occupied_port():
    # if a previous instance still holds the port, run() must die loudly instead
    # of letting the window connect to the stale server (old code, old .env)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        with pytest.raises(SystemExit, match="already in use"):
            window.run(Config(port=port))


# ---- the VRM page honours the flag (§6.5) ------------------------------------

def test_vrm_page_carries_the_desktop_hooks():
    # source-scanned: the flag the window opens with must be the flag the page
    # reads, or desktop mode silently renders the full sanctuary in a
    # transparent frame.
    index = (WEB / "index.html").read_text()
    assert '"desktop"' in index and "pywebview-drag-region" in index
    main_js = (WEB / "js" / "main.js").read_text()
    assert "classList.contains('desktop')" in main_js
    assert "SanctuaryScene" in main_js             # still built in browser mode
    css = (WEB / "sanctuary.css").read_text()
    assert ":root.desktop" in css
    stage = (WEB / "js" / "stage" / "VrmStage.js").read_text()
    assert "alpha: transparent" in stage           # the see-through renderer seam
    assert "frameBody" in stage                    # full-body framing, no room


# ---- the second body: the Live2D client (§6.6) --------------------------------

def test_live2d_client_is_served(client):
    for name in ("index.html", "avatar.js", "voice.js", "settings.js",
                 "sanctuary.css", "README.md"):
        assert (WEB / "live2d" / name).exists(), f"web/live2d/{name} missing"
    # it speaks the same wire the forked route preserves (SPEC §10)
    assert "/ws/voice" in (WEB / "live2d" / "voice.js").read_text()
    assert ":root.desktop" in (WEB / "live2d" / "sanctuary.css").read_text()
    r = client.get("/live2d/")
    assert r.status_code == 200 and "avatar.js" in r.text


def test_live2d_config_falls_back_to_the_default_rig(client):
    # nothing fetched (vendor/ absent in a fresh checkout, and in CI) → the
    # page still gets *a* body URL, relative so it resolves under /live2d/
    body = client.get("/api/config").json()
    assert body["avatar_model"] == "hiyori"
    assert not body["avatar_model_url"].startswith("/")
    assert body["avatar_model_url"].endswith(".model3.json")


def test_live2d_config_resolves_an_installed_rig(client, tmp_path, monkeypatch):
    (tmp_path / "vendor/miara/runtime").mkdir(parents=True)
    (tmp_path / "vendor/miara/runtime/miara_pro_t03.model3.json").write_text("{}")
    monkeypatch.setattr(live2d_route, "LIVE2D_DIR", tmp_path)
    client.app.state.rt.cfg.avatar_model = "miara"
    body = client.get("/api/config").json()
    assert body["avatar_model"] == "miara"
    assert body["avatar_model_url"] == "vendor/miara/runtime/miara_pro_t03.model3.json"
    assert body["avatar_available"] == ["miara"]


def test_settings_router_answers_here_too(client):
    # B2's settings panel works against Build #4's server (and .env) unchanged
    body = client.get("/api/settings").json()
    keys = {f["key"] for g in body["groups"] for f in g["fields"]}
    assert "AVATAR_MODEL" in keys and "CHAT_MODEL" in keys
