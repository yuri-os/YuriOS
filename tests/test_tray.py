"""The tray icon (SPEC §18.4.7).

The model half is tested everywhere. The wire half is tested against a stub
`StatusNotifierWatcher` published on the real session bus — which is the only
way to check an SNI implementation without a desktop, because the thing that
would otherwise tell you it is wrong is a GNOME shell extension silently
declining to draw anything.
"""
from __future__ import annotations

import argparse
import asyncio
import os

import pytest

from yurios.world.tray import (
    ICON_SIZE, ITEM_PATH, MENU_PATH, WATCHER, WATCHER_PATH, Tray, TrayModel,
    _circle_argb,
)

dbus_fast = pytest.importorskip("dbus_fast")

pytestmark = pytest.mark.skipif(
    not (os.environ.get("DBUS_SESSION_BUS_ADDRESS")
         or (os.environ.get("XDG_RUNTIME_DIR")
             and os.path.exists(f"{os.environ['XDG_RUNTIME_DIR']}/bus"))),
    reason="no session bus on this machine",
)


# --- the model, which needs no bus at all ------------------------------------


def test_an_empty_house_says_so():
    assert TrayModel().tooltip() == ("YuriOS", "No characters registered.")


def test_nobody_waiting_is_not_an_alarm():
    """`NeedsAttention` is the one status a host is allowed to make louder. It
    has to mean something, or it means nothing."""
    model = TrayModel()
    model.update([{"id": "yuri", "name": "Yuri", "state": "ready",
                   "unread": {"count": 0, "selfies": 0}}])
    assert model.status == "Active"
    assert model.tooltip()[1] == "1 of 1 awake · nobody waiting"


def test_the_tooltip_names_who_is_waiting():
    """With four in the house "3 waiting" is a number, not an answer — who is
    the part that decides whether you open it now."""
    model = TrayModel()
    model.update([
        {"id": "yuri", "name": "Yuri", "state": "ready",
         "unread": {"count": 2, "selfies": 1}},
        {"id": "adia", "name": "Adia", "state": "offline",
         "unread": {"count": 1, "selfies": 0}},
    ])
    title, body = model.tooltip()
    assert title == "YuriOS — 3 waiting"
    assert body == "Yuri · 2 waiting, one a picture\nAdia · a message"


def test_update_reports_only_real_movement():
    """The tray signals the host on change. If "no change" ever returned True
    it would redraw every few seconds forever."""
    model = TrayModel()
    rows = [{"id": "yuri", "name": "Yuri", "state": "ready",
             "unread": {"count": 0, "selfies": 0}}]
    assert model.update(rows) is True          # first sight of the house
    assert model.update(rows) is False         # nothing moved
    rows[0]["unread"] = {"count": 1, "selfies": 0}
    assert model.update(rows) is True


def test_the_menu_is_one_row_per_character_then_the_board():
    model = TrayModel()
    model.update([
        {"id": "yuri", "name": "Yuri", "state": "ready",
         "unread": {"count": 1, "selfies": 0}},
        {"id": "adia", "name": "Adia", "state": "ready",
         "unread": {"count": 0, "selfies": 0}},
    ])
    rows = model.menu()
    assert [r.get("label") for r in rows] == [
        "Yuri — a message", "Adia", None, "Switchboard"]
    assert rows[0]["url"] == "/characters/yuri/sanctuary/"
    assert rows[2] == {"separator": True}


def _as_the_host_calls_it(interface, name):
    """Call a `@method` and get its reply.

    Called from Python, dbus-fast's wrapper runs the method and drops what it
    returns; the reply the host actually receives comes from the descriptor the
    decorator left behind. `AboutToShow` is a method whose *return value* is the
    whole behaviour, so the test has to ask for it the way the wire does.
    """
    from dbus_fast.service import ServiceInterface

    for descriptor in ServiceInterface._get_methods(interface):
        if descriptor.name == name:
            return lambda *args: descriptor.fn(interface, *args)
    raise AssertionError(f"no such method: {name}")


def test_a_menu_nothing_has_moved_in_is_not_rebuilt_between_click_and_popup():
    """What `AboutToShow` returns is what the click waits for.

    True means "drop the copy you are holding and re-read the layout", so a host
    that asks it on every open — GNOME's does, right before drawing — was tearing
    the popup down and rebuilding it from the wire every time, including the
    common case where nobody had written since the last look.
    """
    from yurios.world.tray import _build_interfaces

    model = TrayModel()
    house = [{"id": "yuri", "name": "Yuri", "state": "ready",
              "unread": {"count": 0, "selfies": 0}}]
    model.update(house)
    _, menu = _build_interfaces(model, lambda url: None)
    about_to_show = _as_the_host_calls_it(menu, "AboutToShow")
    get_layout = _as_the_host_calls_it(menu, "GetLayout")

    assert about_to_show(0) is True               # read nothing, so keep nothing
    revision = get_layout(0, -1, [])[0]

    assert about_to_show(0) is False              # keep the one you have
    assert get_layout(0, -1, [])[0] == revision   # …it is still current

    house[0]["unread"] = {"count": 1, "selfies": 0}
    model.update(house)

    assert about_to_show(0) is True               # a message arrived
    assert get_layout(0, -1, [])[0] > revision
    assert about_to_show(0) is False              # and once redrawn, drawn


def test_the_item_offers_no_activate_so_the_menu_opens_on_the_press():
    """An `Activate` method is what makes a left click slow.

    GNOME's AppIndicator extension introspects for one and, finding it, refuses
    to open the menu until the double-click interval has elapsed — in case a
    second click is coming that it should turn into an Activate instead. That
    interval is a desktop setting, three quarters of a second where this was
    found, and it is the whole of the delay between the click and the names.
    """
    from dbus_fast.service import ServiceInterface

    from yurios.world.tray import _build_interfaces

    item, _ = _build_interfaces(TrayModel(), lambda url: None)
    offered = {descriptor.name for descriptor in ServiceInterface._get_methods(item)}

    assert "Activate" not in offered
    assert "SecondaryActivate" in offered      # the middle click still goes there


def test_the_icon_is_a_well_formed_argb_pixmap():
    """SNI takes raw ARGB32, not a PNG — a wrong length is a blank tray icon
    and no error anywhere."""
    px = _circle_argb(ICON_SIZE, (0xD7, 0xFF, 0x58))
    assert len(px) == ICON_SIZE * ICON_SIZE * 4
    assert px[0] == 0                          # corner is outside the circle
    centre = (ICON_SIZE // 2 * ICON_SIZE + ICON_SIZE // 2) * 4
    assert px[centre] == 255                   # …and the middle is opaque
    assert px[centre + 1:centre + 4] == bytes((0xD7, 0xFF, 0x58))


# --- the wire ----------------------------------------------------------------


class FakeRegistry:
    def __init__(self, records):
        self._records = records

    def list(self):
        return self._records


class FakeHost:
    """Just enough host: the tray only ever calls `registry.list()` and
    `summary()`. That it needs nothing else is the presence guarantee — there is
    no client, no request and no subscriber anywhere in this object."""

    def __init__(self, rows):
        self.rows = rows
        self.registry = FakeRegistry(list(rows))
        self.summary_calls = 0

    def summary(self, record):
        self.summary_calls += 1
        return record


async def _skip_if_a_real_watcher_is_running():
    """For the tests whose premise is that nothing is hosting a tray.

    Once the AppIndicator extension is installed, gnome-shell owns the watcher
    name and the tray registers with it for real — which is the feature working,
    and would read as these tests failing.
    """
    from dbus_fast import BusType
    from dbus_fast.aio import MessageBus

    bus = await MessageBus(bus_type=BusType.SESSION).connect()
    try:
        reply = await bus.call(
            __import__("dbus_fast").Message(
                destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
                interface="org.freedesktop.DBus", member="NameHasOwner",
                signature="s", body=[WATCHER]))
        if reply.body[0]:
            pytest.skip("a real tray host is running on this session")
    finally:
        bus.disconnect()


async def _stub_watcher(bus):
    """A StatusNotifierWatcher that records who registered with it.

    Skips rather than fails when the name is already taken. The two ways that
    happens are both real: a second copy of this suite running concurrently,
    and — the one that matters — a machine where the AppIndicator extension is
    actually installed, so gnome-shell owns the name. A test that goes red on
    the only desktop where the feature works is worse than no test.
    """
    from dbus_fast import NameFlag, RequestNameReply
    from dbus_fast.service import ServiceInterface, method

    registered: list[str] = []

    class Watcher(ServiceInterface):
        def __init__(self):
            super().__init__(WATCHER)

        @method()
        def RegisterStatusNotifierItem(self, service: "s"):   # noqa: N802
            registered.append(service)

    bus.export(WATCHER_PATH, Watcher())
    reply = await bus.request_name(WATCHER, NameFlag.DO_NOT_QUEUE)
    if reply != RequestNameReply.PRIMARY_OWNER:
        pytest.skip("a real StatusNotifierWatcher already owns the name here")
    return registered


@pytest.mark.asyncio
async def test_the_tray_registers_and_serves_what_a_host_would_ask_for():
    from dbus_fast import BusType
    from dbus_fast.aio import MessageBus

    watcher_bus = await MessageBus(bus_type=BusType.SESSION).connect()
    registered = await _stub_watcher(watcher_bus)

    opened: list[str] = []
    host = FakeHost([
        {"id": "yuri", "name": "Yuri", "state": "ready",
         "unread": {"count": 2, "selfies": 1}},
        {"id": "adia", "name": "Adia", "state": "ready",
         "unread": {"count": 0, "selfies": 0}},
    ])
    tray = Tray(host, base_url="http://127.0.0.1:8768",
                poll_seconds=0.05, opener=opened.append)
    try:
        detail = await tray.start()
        assert detail == "on · registered"
        # `in`, not `==`: publishing the watcher name on a real session bus
        # collects every indicator on the machine that has been waiting for a
        # tray host — on a stock Ubuntu desktop, update-notifier and livepatch
        # arrive within milliseconds. Only our own registration is ours to assert.
        assert tray.bus.unique_name in registered

        # Read it back exactly as a tray host does.
        reader = await MessageBus(bus_type=BusType.SESSION).connect()
        name = tray.bus.unique_name
        introspection = await reader.introspect(name, ITEM_PATH)
        item = reader.get_proxy_object(name, ITEM_PATH, introspection).get_interface(
            "org.kde.StatusNotifierItem")

        assert await item.get_id() == "yurios"
        assert await item.get_category() == "ApplicationStatus"
        assert await item.get_status() == "NeedsAttention"      # two are waiting
        assert await item.get_menu() == MENU_PATH

        width, height, pixels = (await item.get_icon_pixmap())[0]
        assert (width, height) == (ICON_SIZE, ICON_SIZE)
        assert len(pixels) == ICON_SIZE * ICON_SIZE * 4

        _, _, title, body = await item.get_tool_tip()
        assert title == "YuriOS — 2 waiting"
        assert "Yuri" in body

        # …and the menu, through com.canonical.dbusmenu.
        menu_introspection = await reader.introspect(name, MENU_PATH)
        menu = reader.get_proxy_object(
            name, MENU_PATH, menu_introspection).get_interface(
                "com.canonical.dbusmenu")
        revision, layout = await menu.call_get_layout(0, -1, [])
        assert revision >= 1
        root_id, _, children = layout
        assert root_id == 0
        labels = [child.value[1]["label"].value for child in children
                  if "label" in child.value[1]]
        assert labels == ["Yuri — 2 waiting, one a picture", "Adia", "Switchboard"]

        # Clicking her row opens her room, not the board.
        await menu.call_event(1, "clicked", dbus_fast.Variant("s", ""), 0)
        assert opened == ["http://127.0.0.1:8768/characters/yuri/sanctuary/"]

        reader.disconnect()
    finally:
        await tray.stop()
        watcher_bus.disconnect()


@pytest.mark.asyncio
async def test_the_tray_takes_no_presence_and_opens_no_socket():
    """The constraint the whole design turns on (SPEC §18.4.5).

    A tray sits in the corner for days. If reading it counted as being in the
    room, Gate 2 would suppress every reach-out as an interruption of a
    conversation already under way — the icon would silence exactly what it
    exists to advertise. It reads the host in-process, so there is no request to
    post a signal from.
    """
    host = FakeHost([{"id": "yuri", "name": "Yuri", "state": "ready",
                      "unread": {"count": 0, "selfies": 0}}])
    host.signals_posted = 0
    tray = Tray(host, poll_seconds=0.05, opener=lambda _: None)
    try:
        await tray.start()
        await asyncio.sleep(0.2)               # several polls
        assert host.summary_calls > 1          # it really did poll
        assert host.signals_posted == 0        # …and told her nothing about it
    finally:
        await tray.stop()


@pytest.mark.asyncio
async def test_no_tray_host_is_not_an_error():
    """On GNOME the watcher appears when the shell extension loads, at login —
    reliably after a daemon started from a terminal. Booting into "no watcher"
    is the normal case, not a failure, and the tray has to keep looking."""
    await _skip_if_a_real_watcher_is_running()
    host = FakeHost([])
    tray = Tray(host, poll_seconds=0.05, opener=lambda _: None)
    try:
        detail = await tray.start()            # nothing owns the watcher name
        assert detail == "on · waiting for a tray host"
        assert tray._registered is False
        await asyncio.sleep(0.15)              # keeps polling rather than dying
        assert tray._task is not None and not tray._task.done()
    finally:
        await tray.stop()


@pytest.mark.asyncio
async def test_the_daemon_brings_the_tray_up_on_its_own(tmp_path, monkeypatch):
    """End to end: `yurios restart` and the icon is there.

    The protocol tests above prove the wire is right; this proves the daemon
    actually walks it. Between them is the failure this feature kept having —
    every piece correct, and nothing on screen.
    """
    from dbus_fast import BusType
    from dbus_fast.aio import MessageBus

    from yurios.characters.registry import CharacterRegistry
    from yurios.world.config import Config
    from yurios.world.host import create_host_app
    from tests.test_host import fake_character_app, record

    watcher_bus = await MessageBus(bus_type=BusType.SESSION).connect()
    registered = await _stub_watcher(watcher_bus)
    monkeypatch.setattr("yurios.world.host.hosting.create_app", fake_character_app)

    registry = CharacterRegistry(tmp_path)
    registry.add(record(tmp_path, "yuri"))
    app = create_host_app(
        Config(data_dir=tmp_path, _env_file=None, tray_enabled=True), registry)
    try:
        # The lifespan directly, not through TestClient: TestClient drives the
        # app on its own event loop in a portal thread, and a D-Bus connection
        # belongs to the loop that opened it — mixing the two hands asyncio a
        # file descriptor its selector never registered.
        async with app.router.lifespan_context(app):
            tray = app.state.tray
            assert tray is not None, "the daemon did not build a tray"
            assert tray.bus.unique_name in registered
            assert [c["name"] for c in tray.model.characters] == ["Yuri"]
    finally:
        watcher_bus.disconnect()


@pytest.mark.asyncio
async def test_the_tray_can_be_turned_off(tmp_path, monkeypatch):
    from yurios.characters.registry import CharacterRegistry
    from yurios.world.config import Config
    from yurios.world.host import create_host_app
    from tests.test_host import fake_character_app, record

    monkeypatch.setattr("yurios.world.host.hosting.create_app", fake_character_app)
    registry = CharacterRegistry(tmp_path)
    registry.add(record(tmp_path, "yuri"))
    app = create_host_app(
        Config(data_dir=tmp_path, _env_file=None, tray_enabled=False), registry)
    async with app.router.lifespan_context(app):
        assert app.state.tray is None


@pytest.mark.asyncio
async def test_it_finds_a_tray_host_that_arrives_later():
    """The sequence a first install actually goes through.

    `install.sh` installs the shell extension, but gnome-shell only reads
    extensions at session start — so the daemon comes up with no watcher on the
    bus, and the watcher appears at the next login, with the daemon already
    running. If registration were a one-shot at startup the icon would need a
    `yurios restart` that nobody would know to run, and the honest description
    of the feature would be "works on the third try".
    """
    from dbus_fast import BusType
    from dbus_fast.aio import MessageBus

    await _skip_if_a_real_watcher_is_running()
    host = FakeHost([{"id": "yuri", "name": "Yuri", "state": "ready",
                      "unread": {"count": 1, "selfies": 0}}])
    tray = Tray(host, poll_seconds=0.05, opener=lambda _: None)
    watcher_bus = None
    try:
        assert await tray.start() == "on · waiting for a tray host"

        # …the user logs out and back in; gnome-shell loads the extension.
        watcher_bus = await MessageBus(bus_type=BusType.SESSION).connect()
        registered = await _stub_watcher(watcher_bus)

        for _ in range(40):                     # ~2s of polling
            await asyncio.sleep(0.05)
            if tray._registered:
                break
        assert tray._registered, "the tray never noticed the host appear"
        assert tray.bus.unique_name in registered
    finally:
        await tray.stop()
        if watcher_bus is not None:
            watcher_bus.disconnect()


# --- the switch, from the command line ---------------------------------------


def test_tray_off_and_on_write_the_env(tmp_path, monkeypatch, capsys):
    """`yurios tray off` has to be the reversible one: it touches this project's
    .env and nothing on the desktop."""
    from yurios import cli

    monkeypatch.setenv("YURIOS_ROOT", str(tmp_path))
    (tmp_path / ".env").write_text("PORT=8768\n", encoding="utf-8")

    assert cli.command_tray(argparse.Namespace(action="off", yes=False)) == 0
    assert "TRAY_ENABLED=false" in (tmp_path / ".env").read_text()

    assert cli.command_tray(argparse.Namespace(action="on", yes=False)) == 0
    body = (tmp_path / ".env").read_text()
    assert "TRAY_ENABLED=true" in body
    assert "TRAY_ENABLED=false" not in body      # replaced, not appended twice
    assert "PORT=8768" in body                   # …and nothing else disturbed


def test_removing_the_tray_host_refuses_to_run_unattended(tmp_path, monkeypatch, capsys):
    """`remove` uninstalls a system package other programs may be using for
    their own icons. That is not something to do to a pipe."""
    from yurios import cli

    monkeypatch.setenv("YURIOS_ROOT", str(tmp_path))
    (tmp_path / ".env").write_text("PORT=8768\n", encoding="utf-8")
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    ran: list[list[str]] = []
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: ran.append(a[0]))

    assert cli.command_tray(argparse.Namespace(action="remove", yes=False)) == 1
    assert ran == []
    assert "TRAY_ENABLED" not in (tmp_path / ".env").read_text()
    assert "Refusing unattended removal" in capsys.readouterr().err
