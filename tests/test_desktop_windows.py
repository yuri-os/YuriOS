"""Floating a character from the running house (SPEC §6.5).

The switchboard and the tray ask the host for a window; the host spawns
`python -m yurios.world --attach <id>` and keeps the handle. Nothing here starts
a real window: the spawn is a fake, and the attach-side URL is a pure function.
"""
from __future__ import annotations

import secrets
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from yurios.characters import CharacterRegistry
from yurios.world import window
from yurios.world.config import Config
from yurios.world import desktop_windows
from yurios.world.desktop_windows import DesktopWindows, WindowUnavailable
from yurios.world.host import create_host_app

from tests.test_host import fake_character_app, record


class FakeProc:
    _next_pid = 1000

    def __init__(self, argv, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        self.returncode = None
        self.terminated = False
        FakeProc._next_pid += 1
        self.pid = FakeProc._next_pid

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class Spawner:
    def __init__(self):
        self.procs: list[FakeProc] = []

    def __call__(self, argv, **kwargs):
        proc = FakeProc(argv, **kwargs)
        self.procs.append(proc)
        return proc


# --- the window manager ------------------------------------------------------


def test_a_window_is_the_attach_entry_point_aimed_at_this_house():
    spawn = Spawner()
    windows = DesktopWindows("0.0.0.0", 9100, spawn=spawn, python="/py", check=lambda: None)

    assert windows.open("yuri", title="Yuri") is True

    proc = spawn.procs[0]
    assert proc.argv == ["/py", "-m", "yurios.world", "--attach", "yuri", "--title", "Yuri"]
    # The window must reach the server that is running, not whatever .env says now.
    assert proc.kwargs["env"]["HOST"] == "0.0.0.0"
    assert proc.kwargs["env"]["PORT"] == "9100"
    assert proc.kwargs["stdin"] is subprocess.DEVNULL


def test_a_second_click_does_not_stack_a_second_copy_of_her():
    spawn = Spawner()
    windows = DesktopWindows("127.0.0.1", 8768, spawn=spawn, check=lambda: None)

    assert windows.open("yuri") is True
    assert windows.open("yuri") is False
    assert len(spawn.procs) == 1
    assert windows.open("mika") is True            # another character is another window


def test_a_window_closed_by_hand_can_be_opened_again():
    spawn = Spawner()
    windows = DesktopWindows("127.0.0.1", 8768, spawn=spawn, check=lambda: None)
    windows.open("yuri")
    spawn.procs[0].returncode = 0                  # the user closed it

    assert windows.is_open("yuri") is False
    assert windows.open("yuri") is True
    assert len(spawn.procs) == 2


def test_the_windows_go_when_the_house_does():
    spawn = Spawner()
    windows = DesktopWindows("127.0.0.1", 8768, spawn=spawn, check=lambda: None)
    windows.open("yuri")
    windows.open("mika")

    windows.close_all()

    assert all(proc.terminated for proc in spawn.procs)
    assert not windows.is_open("yuri") and not windows.is_open("mika")


def test_a_machine_that_cannot_open_a_window_says_so_instead_of_spawning():
    spawn = Spawner()
    windows = DesktopWindows("127.0.0.1", 8768, spawn=spawn, check=lambda: "no pywebview")
    with pytest.raises(WindowUnavailable, match="no pywebview"):
        windows.open("yuri")
    assert spawn.procs == []


def test_pywebview_missing_is_named(monkeypatch):
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    assert "[desktop]" in desktop_windows.unavailable()


def test_the_default_interpreter_is_the_one_running_the_house():
    assert DesktopWindows("127.0.0.1", 8768)._python == sys.executable


# --- the route ---------------------------------------------------------------


@pytest.fixture
def house(tmp_path, monkeypatch):
    monkeypatch.setattr("yurios.world.host.hosting.create_app", fake_character_app)

    def build(*, in_review=False, check=None, **cfg):
        registry = CharacterRegistry(tmp_path)
        yuri = record(tmp_path, "yuri")
        yuri.lifecycle.review_required = in_review
        registry.add(yuri)
        registry.add(record(tmp_path, "mika", enabled=False))
        app = create_host_app(Config(data_dir=tmp_path, tray_enabled=False, **cfg), registry)
        spawn = Spawner()
        app.state.desktop_windows = DesktopWindows("127.0.0.1", 8768, spawn=spawn,
                                                   check=check or (lambda: None))
        return app, spawn

    return build


def test_the_board_floats_a_running_character(house):
    app, spawn = house()
    with TestClient(app) as client:
        first = client.post("/api/characters/yuri/desktop")
        again = client.post("/api/characters/yuri/desktop")

    assert first.status_code == 200
    assert first.json() == {"id": "yuri", "opened": True, "already_open": False}
    assert again.json()["already_open"] is True
    assert len(spawn.procs) == 1
    assert spawn.procs[0].argv[-2:] == ["--title", "Yuri"]


def test_a_character_who_is_not_running_is_refused(house):
    app, spawn = house()
    with TestClient(app) as client:
        refused = client.post("/api/characters/mika/desktop")
        missing = client.post("/api/characters/nobody/desktop")

    assert refused.status_code == 409
    assert "not running" in refused.json()["detail"]
    assert missing.status_code == 404
    assert spawn.procs == []


def test_a_character_in_review_is_refused(house):
    app, spawn = house(in_review=True)
    with TestClient(app) as client:
        refused = client.post("/api/characters/yuri/desktop")

    assert refused.status_code == 409
    assert "review" in refused.json()["detail"]
    assert spawn.procs == []


def test_a_paired_remote_device_cannot_put_her_on_this_screen(house):
    """Owner-authenticated is not enough: the window opens on the host's display."""
    token = secrets.token_urlsafe(32)
    app, spawn = house(owner_token=token)
    with TestClient(app, client=("192.168.1.40", 5555)) as client:
        refused = client.post("/api/characters/yuri/desktop",
                              headers={"Authorization": f"Bearer {token}"})

    assert refused.status_code == 403
    assert spawn.procs == []


def test_the_board_hears_why_no_window_can_open(house):
    app, spawn = house(check=lambda: "the house has no graphical session")
    with TestClient(app) as client:
        refused = client.post("/api/characters/yuri/desktop")

    assert refused.status_code == 503
    assert "graphical session" in refused.json()["detail"]
    assert spawn.procs == []


def test_stopping_her_from_the_board_closes_her_window(house):
    app, spawn = house()
    with TestClient(app) as client:
        client.post("/api/characters/yuri/desktop")
        assert client.post("/api/characters/yuri/stop").status_code == 200

    assert spawn.procs[0].terminated


# --- the attach side ---------------------------------------------------------


def test_the_attached_window_opens_her_own_sanctuary_in_desktop_mode():
    assert window.character_url(Config(port=8768), "yuri") \
        == "http://127.0.0.1:8768/characters/yuri/sanctuary/?desktop=1"


def test_the_attached_window_honours_the_live2d_body():
    assert window.character_url(Config(port=8768, desktop_body="live2d"), "yuri") \
        == "http://127.0.0.1:8768/live2d/?character=yuri&desktop=1"


def test_the_attached_window_never_targets_the_wildcard():
    url = window.character_url(Config(host="0.0.0.0", port=9000, owner_token=""), "a b")
    assert url.startswith("http://127.0.0.1:9000/")
    assert "/characters/a%20b/" in url


def test_beyond_loopback_the_window_goes_in_through_the_pairing_link():
    token = secrets.token_urlsafe(32)
    url = window.character_url(Config(host="0.0.0.0", port=9000, owner_token=token), "yuri")
    assert url.startswith("http://127.0.0.1:9000/auth?token=")
    assert "next=%2Fcharacters%2Fyuri%2Fsanctuary%2F%3Fdesktop%3D1" in url


def test_on_loopback_the_token_stays_out_of_the_url():
    token = secrets.token_urlsafe(32)
    url = window.character_url(Config(port=9000, owner_token=token), "yuri")
    assert token not in url


def test_each_window_keeps_a_profile_of_its_own(monkeypatch, tmp_path):
    monkeypatch.setenv("YURIOS_ROOT", str(tmp_path))
    assert window.window_storage("yuri") == tmp_path / ".yurios" / "windows" / "yuri"
    assert window.window_storage("../evil").parent == tmp_path / ".yurios" / "windows"


def test_attach_refuses_when_no_house_is_running(monkeypatch):
    monkeypatch.setattr(window, "_wait_for_server", lambda *a, **k: False)
    with pytest.raises(SystemExit, match="yurios start"):
        window.attach(Config(port=9), "yuri")


def test_the_entry_point_routes_attach_without_booting_a_server(monkeypatch):
    from yurios.world import __main__ as entry

    seen = []
    monkeypatch.setattr(window, "attach",
                        lambda cfg, cid, *, title=None: seen.append((cid, title)))
    monkeypatch.setattr(entry, "build_server",
                        lambda *a, **k: pytest.fail("attach must not start a server"))
    entry.main(["--attach", "yuri", "--title", "Yuri"])
    assert seen == [("yuri", "Yuri")]


# --- the way out --------------------------------------------------------------


def test_the_windows_own_close_button_asks_the_host_to_end_it(house):
    """A frameless window has no title bar, and a page cannot close a window it
    did not open — so its ✕ asks the host, which spawned the process."""
    app, spawn = house()
    with TestClient(app) as client:
        client.post("/api/characters/yuri/desktop")
        closed = client.delete("/api/characters/yuri/desktop")
        again = client.delete("/api/characters/yuri/desktop")
        reopened = client.post("/api/characters/yuri/desktop")

    assert closed.json() == {"id": "yuri", "closed": True}
    assert spawn.procs[0].terminated
    assert again.json()["closed"] is False         # nothing of ours left: page falls back
    assert reopened.json()["opened"] is True       # and she can be floated again


def test_only_this_machine_can_put_her_away(house):
    token = secrets.token_urlsafe(32)
    app, spawn = house(owner_token=token)
    with TestClient(app, client=("192.168.1.40", 5555)) as client:
        refused = client.delete("/api/characters/yuri/desktop",
                                headers={"Authorization": f"Bearer {token}"})
    assert refused.status_code == 403


def test_the_window_is_built_without_a_script_bridge():
    """pywebview builds `js_api` with `new Function`, which the page's CSP
    (SPEC §11.6) refuses — a bridge there would only ever fail, noisily."""
    from pathlib import Path

    source = Path(window.__file__).read_text(encoding="utf-8")
    assert "js_api=" not in source


def test_both_bodies_load_the_close_button():
    from pathlib import Path

    web = Path(__file__).resolve().parent.parent / "web"
    for page in (web / "index.html", web / "live2d" / "index.html"):
        html = page.read_text(encoding="utf-8")
        assert '<script src="/shared/desktop-close.js"></script>' in html, page
        assert 'href="/shared/desktop-close.css"' in html, page
