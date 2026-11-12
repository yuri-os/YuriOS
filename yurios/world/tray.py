"""Her tray icon (SPEC §18.4.7) — the house, from the corner of the screen.

The switchboard already marks a tile when someone reached out while the room
was empty (§32.5), and the doorbell already pushes a notification (§18.4.4).
Both need you to be looking at something. This is the third case: nothing open,
nothing on screen, and a question worth answering at a glance — *is anyone
waiting for me?*

**It is not presence.** This is the whole design constraint, and it is why the
tray reads the host in-process instead of over HTTP. Attaching to `/api/events`
posts `user_present` and would leave a tray icon — which sits there for days —
telling her you are permanently in the room, so Gate 2 would suppress every
reach-out as an interruption of a conversation already under way and the icon
would silence exactly what it exists to advertise (§18.4.5). Reading
`host.summary()` directly cannot post a signal at all: there is no request, no
subscriber, no socket. The constraint holds by construction rather than by
remembering to hold it.

**Why the protocol, and not a library.** The tray is `org.kde.StatusNotifierItem`
— the freedesktop-era replacement for the XEmbed system tray — plus
`com.canonical.dbusmenu` for the menu. The usual client is `pystray`, which
needs PyGObject; PyGObject has no wheels and builds against `girepository-2.0`,
so it would put a C toolchain and a system package between a fresh clone and a
working install, and it would still be invisible to a `uv` venv, which does not
see system site-packages. Both interfaces are small enough to speak directly
over `dbus-fast` (a wheel, no system dependencies), so that is what this does.

**What it cannot do.** SNI is a protocol for talking to a *host*, and on GNOME
there is no host without the AppIndicator shell extension — GNOME removed the
system tray in 2017. `install.sh` installs it, but gnome-shell only reads
extensions at session start, so it lights up at next login. Nothing here can
change that, and nothing here breaks without it: with no watcher on the bus the
tray simply never registers, waits, and tries again if one appears.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import shutil
import subprocess

log = logging.getLogger("world.tray")

WATCHER = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"

#: How often the tray re-reads the house. A tray is a glance, not a feed: this
#: is a dictionary lookup over a handful of records, but it is also the interval
#: at which the icon can lie to you, so it is seconds rather than minutes.
POLL_SECONDS = 5

#: 22px is the size every panel asks for first. The host scales what it gets,
#: so a second, doubled pixmap is what keeps a HiDPI panel from blowing the
#: 22px one up into a blur; the host picks whichever fits.
ICON_SIZE = 22
ICON_SIZES = (ICON_SIZE, ICON_SIZE * 2)

# Her two states, as colours. Idle is the muted violet the switchboard uses for
# a character at rest; waiting is the acid the unread badge is drawn in, so the
# tray and the tile agree without either one knowing about the other.
IDLE_RGB = (0x8B, 0x7F, 0xA8)
WAITING_RGB = (0xD7, 0xFF, 0x58)

# The mark itself, in each of those two lights: (top of the ring, bottom of the
# ring, the heart). The ring shades top to bottom so it reads as a cut stone
# rather than a line drawing; lit, it takes the acid all the way through.
_GEM_RGB = {
    False: ((0xCF, 0xC6, 0xF0), IDLE_RGB, (0xCF, 0xC6, 0xF0)),
    True: (WAITING_RGB, (0xA8, 0xD9, 0x30), WAITING_RGB),
}
# The near-black that edges every band. Panels come dark, light and blue; the
# acid alone vanishes into a light one, and an edge is what keeps the mark a
# shape on all of them.
_INK_RGB = (0x12, 0x0F, 0x17)
_INK_ALPHA = 0.75
_GLOW_ALPHA = 0.45


def _circle_argb(size: int, rgb: tuple[int, int, int]) -> bytes:
    """A filled circle as ARGB32, big-endian, row-major — a menu row's state dot.

    Drawn rather than shipped, which keeps the icons out of the package data and
    lets a colour follow the model instead of needing one file per state.
    Pillow is a base dependency and could do this, but a few hundred pixels of
    circle is less code than the import and it keeps this module importable in
    a test environment with nothing installed at all. Coverage is sampled on a
    3x3 grid per pixel, which is enough antialiasing that it does not look like
    1998 at 16 pixels across.
    """
    red, green, blue = rgb
    centre = (size - 1) / 2
    radius = size / 2 - 1.5
    out = bytearray()
    for y in range(size):
        for x in range(size):
            hits = 0
            for sub_y in range(3):
                for sub_x in range(3):
                    px = x + (sub_x + 0.5) / 3 - 0.5
                    py = y + (sub_y + 0.5) / 3 - 0.5
                    if math.hypot(px - centre, py - centre) <= radius:
                        hits += 1
            alpha = round(255 * hits / 9)
            out += bytes((alpha, red, green, blue))
    return bytes(out)


def _gem_argb(size: int, *, lit: bool = False, glow: bool = True) -> bytes:
    """Her ◇ — the favicon's mark — as ARGB32, big-endian, row-major: what SNI wants.

    A diamond ring around a diamond heart, each edged in ink, measured in the
    L1 distance from the centre (a diamond is the circle of that metric, so every
    band is one comparison). At rest it is violet; *lit*, when someone is
    waiting, it is the acid with a glow round it, so "waiting" is the same mark
    switched on rather than a second symbol to learn. Proportions are set at 22px
    and scale with `size`. Sampled 4x4 per pixel: the diagonals are all edge.
    `glow=False` drops the halo, which at a menu row's 16px is only a smudge.
    """
    scale = size / 22
    outer, band, heart = 9.4 * scale, 2.9 * scale, 3.3 * scale
    ink = 1.3 * scale
    halo = 2.0 * scale if lit and glow else 0.0
    top, bottom, core = _GEM_RGB[lit]
    centre = (size - 1) / 2
    samples = 4

    def sample(distance: float, dy: float) -> tuple[float, tuple[int, int, int]]:
        if distance <= heart:
            return 1.0, core
        if distance <= heart + ink:
            return _INK_ALPHA, _INK_RGB
        if distance < outer - band - ink:
            return 0.0, _INK_RGB                # the gap between heart and ring
        if distance < outer - band:
            return _INK_ALPHA, _INK_RGB
        if distance <= outer:
            t = min(1.0, max(0.0, (dy + outer) / (2 * outer)))
            return 1.0, tuple(round(a + (b - a) * t) for a, b in zip(top, bottom))  # type: ignore[return-value]
        if distance <= outer + ink:
            return _INK_ALPHA, _INK_RGB
        beyond = distance - outer - ink
        if beyond < halo:
            return _GLOW_ALPHA * (1 - beyond / halo) ** 2, WAITING_RGB
        return 0.0, _INK_RGB

    out = bytearray()
    for y in range(size):
        for x in range(size):
            alpha = red = green = blue = 0.0     # premultiplied, so edges blend true
            for sub_y in range(samples):
                for sub_x in range(samples):
                    dx = x + (sub_x + 0.5) / samples - 0.5 - centre
                    dy = y + (sub_y + 0.5) / samples - 0.5 - centre
                    a, (r, g, b) = sample(abs(dx) + abs(dy), dy)
                    alpha += a
                    red += a * r
                    green += a * g
                    blue += a * b
            if alpha:
                red, green, blue = red / alpha, green / alpha, blue / alpha
            out += bytes((round(255 * alpha / samples ** 2),
                          round(red), round(green), round(blue)))
    return bytes(out)


_ICON_CACHE: dict[tuple[int, bool], bytes] = {}


def _icon(size: int, *, lit: bool = False) -> bytes:
    """`_gem_argb`, drawn once per appearance.

    There are exactly two icons in the whole program and neither ever changes,
    but the properties that return them are read on the host's schedule, not
    ours — a redraw asks for every property at once, so a rasterisation was
    being paid twice each time, on the loop the D-Bus replies come off.
    """
    key = (size, lit)
    icon = _ICON_CACHE.get(key)
    if icon is None:
        icon = _ICON_CACHE[key] = _gem_argb(size, lit=lit)
    return icon


def _open(url: str) -> None:
    """Hand a URL to the desktop. Never raises: a tray click that takes the
    daemon down with it would be a spectacular way to lose a conversation."""
    opener = shutil.which("xdg-open") or shutil.which("open")
    if opener is None:
        log.warning("no xdg-open; cannot open %s", url)
        return
    try:
        subprocess.Popen([opener, url], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError:
        log.warning("could not open %s", url, exc_info=True)


def _session_bus_available() -> bool:
    """Whether there is a session bus to talk to at all.

    `DBUS_SESSION_BUS_ADDRESS` is inherited from the shell that ran
    `yurios start`, but a daemon launched by systemd or cron has no such shell —
    and `$XDG_RUNTIME_DIR/bus` is where the socket lives regardless, so it is
    worth looking before deciding there is no desktop here.
    """
    if os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return True
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and os.path.exists(os.path.join(runtime, "bus")):
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={runtime}/bus"
        return True
    return False


class TrayModel:
    """What the tray knows, kept apart from how it is drawn.

    Split out because the D-Bus half needs a session bus and the decisions —
    which characters are waiting, what the tooltip says, what the menu holds —
    do not. This is the part worth testing.
    """

    def __init__(self, app_name: str = "YuriOS"):
        self.app_name = app_name
        self.characters: list[dict] = []

    def update(self, characters: list[dict]) -> bool:
        """Take a fresh listing. Returns whether anything a viewer would notice
        actually moved, so the tray only signals the host when it must."""
        wanted = [
            {"id": c.get("id", ""), "name": c.get("name") or c.get("id", ""),
             "unread": int((c.get("unread") or {}).get("count") or 0),
             "selfies": int((c.get("unread") or {}).get("selfies") or 0),
             "state": c.get("state") or "offline",
             # Up and reviewed: the one she can be floated on the desktop from.
             "running": c.get("runtime_state") == "ready"
                        and not c.get("review_required"),
             # Out on the desktop in a window of her own right now (SPEC §6.5).
             "desktop": bool(c.get("desktop"))}
            for c in characters
        ]
        changed = wanted != self.characters
        self.characters = wanted
        return changed

    @property
    def waiting(self) -> int:
        return sum(c["unread"] for c in self.characters)

    @property
    def status(self) -> str:
        # 'NeedsAttention' is the only thing SNI offers that a host is allowed to
        # make louder — GNOME turns it a different colour. She earned it: nothing
        # reaches the inbox without passing Gate 2.
        return "NeedsAttention" if self.waiting else "Active"

    def tooltip(self) -> tuple[str, str]:
        """(title, body). The body is the part worth reading on hover."""
        if not self.characters:
            return self.app_name, "No characters registered."
        if not self.waiting:
            live = sum(1 for c in self.characters if c["state"] != "offline")
            return self.app_name, f"{live} of {len(self.characters)} awake · nobody waiting"
        parts = [f"{c['name']} · {self._what(c)}"
                 for c in self.characters if c["unread"]]
        return f"{self.app_name} — {self.waiting} waiting", "\n".join(parts)

    @staticmethod
    def _what(character: dict) -> str:
        count, selfies = character["unread"], character["selfies"]
        if selfies == count:
            return "a picture" if count == 1 else f"{count} pictures"
        if selfies:
            return f"{count} waiting, one a picture"
        return "a message" if count == 1 else f"{count} messages"

    def menu(self) -> list[dict]:
        """The menu as plain data: one row per character, then the switchboard.

        Per character rather than one combined count, because with four of them
        "3 waiting" tells you a number and not who — and who is the part that
        decides whether you open it now.

        Each name is listed once, and her row is the switch for her desktop
        window (SPEC §6.5): ticked while she is out, a click floats her or puts
        her away. It cannot also open her room, because a dbusmenu row is one
        target — GNOME's host draws the tick and the icon but makes neither a
        second button (`MenuItemFactory`, the AppIndicator extension's
        dbusMenu.js) — so the room is the switchboard's to open, one row down.
        A row for someone not running is greyed rather than hidden: the list is
        the house, and a window over a character who is not up opens on nothing.
        """
        rows: list[dict] = []
        for character in self.characters:
            state = _state_word(character["state"])
            what = self._what(character) if character["unread"] else state
            rows.append({"label": f"{character['name']} · {what}",
                         "toggle": character["id"],
                         "name": character["name"],
                         "checked": character["desktop"],
                         # Out but no longer running: still offered, to put her away.
                         "enabled": character["running"] or character["desktop"],
                         "dot": ("waiting" if character["unread"] else state)})
        if rows:
            rows.append({"separator": True})
        rows.append({"label": "Open the switchboard", "url": "/dashboard/"})
        # The icon's own way out, for this run: the house, and anyone out on the
        # desktop, carry on without it, and the next start brings it back.
        rows.append({"label": "Close the tray", "close": True})
        return rows


#: The switchboard's words and colours for a state (web/shared/activity-state.js),
#: so the tray and the tile say the same thing in the same colour: the ladder's
#: own words, and the process's for a character with no mind running.
STATE_RGB = {
    "engaged": (0xD7, 0xFF, 0x58), "idle": (0x76, 0xE8, 0xBD),
    "dream": (0xF4, 0xBD, 0x62), "dormant": (0x69, 0x73, 0x6D),
    "starting": (0xA7, 0xCA, 0x32), "ready": (0xA7, 0xCA, 0x32),
    "attention": (0xEF, 0x78, 0x6F), "offline": (0x69, 0x73, 0x6D),
}
_STATE_ALIASES = {"failed": "attention", "error": "attention", "stopped": "offline"}

#: A menu row's icon: 16px is GNOME's `popup-menu-icon`.
ROW_ICON_SIZE = 16


def _state_word(state: str) -> str:
    word = str(state or "offline").strip().lower()
    return _STATE_ALIASES.get(word, word)


def _png(size: int, argb: bytes) -> bytes:
    """ARGB32 pixels as a PNG — what dbusmenu's `icon-data` wants.

    A menu row's icon is decoded by GdkPixbuf from an encoded image, not read as
    raw pixels like the tray icon's own pixmap, so the same circle has to be
    wrapped. An RGBA PNG with no filtering is a few lines of zlib.
    """
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    raw = bytearray()
    for y in range(size):
        raw.append(0)                                   # filter: none
        row = argb[y * size * 4:(y + 1) * size * 4]
        for x in range(0, len(row), 4):
            raw += row[x + 1:x + 4] + row[x:x + 1]      # ARGB → RGBA
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b""))


_DOT_CACHE: dict[str, bytes] = {}


def _dot(kind: str) -> bytes:
    """The PNG beside a name: the tray icon's own mark, lit, for someone who
    wrote — and otherwise a dot in her state's colour."""
    png = _DOT_CACHE.get(kind)
    if png is None:
        if kind == "waiting":
            argb = _gem_argb(ROW_ICON_SIZE, lit=True, glow=False)
        else:
            argb = _circle_argb(ROW_ICON_SIZE, STATE_RGB.get(kind, STATE_RGB["offline"]))
        png = _DOT_CACHE[kind] = _png(ROW_ICON_SIZE, argb)
    return png


# --- the wire ---------------------------------------------------------------
#
# Everything below needs `dbus-fast` and a session bus. It is imported inside
# the function on purpose: `TrayModel` above is the part with the decisions in
# it, and it must stay importable — and testable — on a machine with no desktop
# and no D-Bus at all.

def _build_interfaces(model: "TrayModel", on_open, on_toggle=None, on_close=None,
                      refresh=None):
    from dbus_fast import PropertyAccess, Variant
    from dbus_fast.service import ServiceInterface, dbus_property, method, signal

    class StatusNotifierItem(ServiceInterface):
        """org.kde.StatusNotifierItem — the icon itself."""

        def __init__(self):
            super().__init__("org.kde.StatusNotifierItem")

        # -- identity ---------------------------------------------------------
        @dbus_property(access=PropertyAccess.READ)
        def Category(self) -> "s":            # noqa: N802 — the wire names it
            return "ApplicationStatus"

        @dbus_property(access=PropertyAccess.READ)
        def Id(self) -> "s":                  # noqa: N802
            return "yurios"

        @dbus_property(access=PropertyAccess.READ)
        def Title(self) -> "s":               # noqa: N802
            return model.app_name

        @dbus_property(access=PropertyAccess.READ)
        def Status(self) -> "s":              # noqa: N802
            return model.status

        @dbus_property(access=PropertyAccess.READ)
        def WindowId(self) -> "u":            # noqa: N802
            return 0                          # she has no X window to point at

        # -- what it looks like ------------------------------------------------
        #
        # Pixmaps rather than IconName: a themed name that the user's icon theme
        # does not carry renders as a blank space or a "missing image" glyph,
        # and there is no theme anywhere that ships one for this program.
        @dbus_property(access=PropertyAccess.READ)
        def IconName(self) -> "s":            # noqa: N802
            return ""

        @dbus_property(access=PropertyAccess.READ)
        def IconPixmap(self) -> "a(iiay)":    # noqa: N802
            lit = bool(model.waiting)
            return [[size, size, _icon(size, lit=lit)] for size in ICON_SIZES]

        @dbus_property(access=PropertyAccess.READ)
        def AttentionIconName(self) -> "s":   # noqa: N802
            return ""

        @dbus_property(access=PropertyAccess.READ)
        def AttentionIconPixmap(self) -> "a(iiay)":   # noqa: N802
            return [[size, size, _icon(size, lit=True)] for size in ICON_SIZES]

        @dbus_property(access=PropertyAccess.READ)
        def OverlayIconName(self) -> "s":     # noqa: N802
            return ""

        @dbus_property(access=PropertyAccess.READ)
        def ToolTip(self) -> "(sa(iiay)ss)":  # noqa: N802
            title, body = model.tooltip()
            return ["", [], title, body]

        # -- the menu ----------------------------------------------------------
        @dbus_property(access=PropertyAccess.READ)
        def Menu(self) -> "o":                # noqa: N802
            return MENU_PATH

        @dbus_property(access=PropertyAccess.READ)
        def ItemIsMenu(self) -> "b":          # noqa: N802
            # True: this icon is a menu and nothing else. Which is also why
            # there is no Activate method below — see the note there.
            return True

        # -- what a click does -------------------------------------------------
        #
        # There is deliberately no `Activate`. GNOME's AppIndicator extension
        # introspects for one (`supportsActivation = !!lookup_method('Activate')`,
        # appIndicator.js) and, when it finds one, a left click can no longer
        # open the menu straight away: it has to wait out the whole double-click
        # interval first, in case a second click is coming that it should turn
        # into an Activate instead (`_waitForDoubleClick`, indicatorStatusIcon.js).
        # That interval is a desktop setting — 763ms on the machine this was
        # found on — and it was the entire delay between clicking her icon and
        # seeing the names. An item with no Activate takes the other branch and
        # opens on the press. `ItemIsMenu` is the spec's way of saying the same
        # thing, and KDE honours it, but this extension only ever asks the
        # introspection, so both are set.
        #
        # What it costs: no left-click-straight-to-the-switchboard. That gesture
        # never existed here anyway — it needed a *double* click, and the price
        # of offering it was making every single click slow. The switchboard is
        # the last row of the menu, and the middle click below still goes there.
        @method()
        def SecondaryActivate(self, x: "i", y: "i"):   # noqa: N802, ARG002
            on_open("/dashboard/")

        @method()
        def Scroll(self, delta: "i", orientation: "s"):   # noqa: N802, ARG002
            return

        @signal()
        def NewIcon(self):                    # noqa: N802
            return

        @signal()
        def NewToolTip(self):                 # noqa: N802
            return

        @signal()
        def NewStatus(self) -> "s":           # noqa: N802
            return model.status

    class DBusMenu(ServiceInterface):
        """com.canonical.dbusmenu — the right-click menu.

        A tree, addressed by integer id, where 0 is the invisible root. The ids
        are re-derived from the model on every rebuild — depth first, so a
        submenu's rows follow it — and `revision` is bumped with them, which is
        what tells a host its cached copy is stale.
        """

        def __init__(self):
            super().__init__("com.canonical.dbusmenu")
            self._revision = 1
            self._rows: list[dict] = []
            self._items: dict[int, dict] = {}        # id → row
            self._children: dict[int, list[int]] = {0: []}
            self._served: int | None = None   # the revision the host last read
            self.rebuild()

        def _index(self) -> None:
            self._items, self._children = {}, {0: []}

            def walk(rows: list[dict], parent: int) -> None:
                for row in rows:
                    item_id = len(self._items) + 1
                    self._items[item_id] = row
                    self._children[parent].append(item_id)
                    if row.get("children"):
                        self._children[item_id] = []
                        walk(row["children"], item_id)

            walk(self._rows, 0)

        def rebuild(self) -> bool:
            """Re-derive the rows. True when they actually moved.

            The answer is what `AboutToShow` owes the host and the only reason to
            bump the revision: both mean "the copy you are holding is stale".
            """
            rows = model.menu()
            if rows == self._rows:
                return False
            self._rows = rows
            self._index()
            self._revision += 1
            return True

        def _properties(self, row: dict) -> dict:
            if row.get("separator"):
                return {"type": Variant("s", "separator")}
            props = {"label": Variant("s", row["label"]),
                     "enabled": Variant("b", bool(row.get("enabled", True))),
                     "visible": Variant("b", True)}
            if row.get("children"):
                props["children-display"] = Variant("s", "submenu")
            if row.get("toggle"):
                # GNOME draws a checkmark toggle as a tick in the row's ornament
                # column — which it keeps, empty, on an unticked row, so the
                # names line up either way.
                props["toggle-type"] = Variant("s", "checkmark")
                props["toggle-state"] = Variant("i", 1 if row.get("checked") else 0)
            if row.get("dot"):
                # Drawn at the row's far end. It is a picture, not a second
                # target: a click anywhere on the row is the row's one action.
                props["icon-data"] = Variant("ay", _dot(row["dot"]))
            return props

        def _layout(self, item_id: int, depth: int) -> list:
            props = ({"children-display": Variant("s", "submenu")} if item_id == 0
                     else self._properties(self._items[item_id]))
            children = [] if depth == 0 else [
                Variant("(ia{sv}av)", self._layout(child, depth - 1))
                for child in self._children.get(item_id, [])]
            return [item_id, props, children]

        @method()
        def GetLayout(self, parentId: "i", recursionDepth: "i",   # noqa: N802
                      propertyNames: "as") -> "u(ia{sv}av)":      # noqa: N802, ARG002
            self._served = self._revision
            if parentId != 0 and parentId not in self._items:
                return [self._revision, [parentId, {}, []]]
            return [self._revision, self._layout(parentId, recursionDepth)]

        @method()
        def GetGroupProperties(self, ids: "ai",                   # noqa: N802
                               propertyNames: "as") -> "a(ia{sv})":   # noqa: N802, ARG002
            return [[item_id, self._properties(self._items[item_id])]
                    for item_id in ids if item_id in self._items]

        @method()
        def GetProperty(self, id: "i", name: "s") -> "v":         # noqa: N802, A002
            if id in self._items:
                value = self._properties(self._items[id]).get(name)
                if value is not None:
                    return value
            return Variant("s", "")

        @method()
        def Event(self, id: "i", eventId: "s", data: "v",         # noqa: N802, A002, ARG002
                  timestamp: "u"):                                # noqa: ARG002
            if eventId != "clicked" or id not in self._items:
                return
            row = self._items[id]
            if row.get("url"):
                on_open(row["url"])
            elif row.get("toggle") and row.get("enabled") and on_toggle is not None:
                on_toggle(row["toggle"], row["name"])
            elif row.get("close") and on_close is not None:
                on_close()

        @method()
        def EventGroup(self, events: "a(isvu)") -> "ai":          # noqa: N802
            for item_id, event_id, data, timestamp in events:
                self.Event(item_id, event_id, data, timestamp)
            return []

        @method()
        def AboutToShow(self, id: "i") -> "b":                    # noqa: N802, A002, ARG002
            # The host is about to draw the menu — the one moment its copy has
            # to be right, and the one moment it is waiting on us to draw. True
            # means "throw yours away and re-read the layout", so answering it
            # unconditionally made the shell tear the popup down and rebuild it
            # from the wire on every single open, including the overwhelmingly
            # common one where nothing has changed since the last. That rebuild
            # is what a click waits for. So: only when the rows really moved…
            # (read fresh, since a window can be closed from its own ✕ between
            # two polls, and a stale tick is the one thing this moment can't show)
            if refresh is not None:
                refresh()
            if self.rebuild():
                return True
            # …or when this host has never read them, which is every host on its
            # first open and any host that fetches the layout lazily. Saying "keep
            # what you have" to one that has nothing would leave it empty forever.
            return self._served != self._revision

        @method()
        def AboutToShowGroup(self, ids: "ai") -> "aiai":          # noqa: N802, ARG002
            # (ids that need updating, ids that don't exist) — 0 is the root, so
            # "the menu" is the honest answer when the rows moved.
            if refresh is not None:
                refresh()
            return [[0], []] if self.rebuild() else [[], []]

        @dbus_property(access=PropertyAccess.READ)
        def Version(self) -> "u":             # noqa: N802
            return 3

        @dbus_property(access=PropertyAccess.READ)
        def Status(self) -> "s":              # noqa: N802
            return "normal"

        @dbus_property(access=PropertyAccess.READ)
        def TextDirection(self) -> "s":       # noqa: N802
            return "ltr"

        @dbus_property(access=PropertyAccess.READ)
        def IconThemePath(self) -> "as":      # noqa: N802
            return []

        @signal()
        def LayoutUpdated(self) -> "ui":      # noqa: N802
            return [self._revision, 0]

    return StatusNotifierItem(), DBusMenu()


class Tray:
    """The tray icon's lifetime: connect, register, poll, and survive a host
    that is not there yet.

    A GNOME session gains a StatusNotifierWatcher when the shell extension
    loads, which is at login — reliably *after* a daemon that was started from a
    terminal. So "no watcher" is a normal state to boot into and not an error:
    the tray watches for the name to appear and registers the moment it does.
    """

    def __init__(self, host, *, app_name: str = "YuriOS",
                 base_url: str = "http://127.0.0.1:8768",
                 poll_seconds: float = POLL_SECONDS, opener=None, windows=None):
        self.host = host
        # The host's `DesktopWindows` (SPEC §6.5): who is out on the desktop, and
        # the switch each name in the menu is. None leaves the names inert.
        self._windows = windows
        self.base_url = base_url.rstrip("/")
        self.poll_seconds = poll_seconds
        self.model = TrayModel(app_name)
        self._opener = opener or _open
        self.bus = None
        self.item = None
        self.menu = None
        self._task: asyncio.Task | None = None
        self._jobs: set[asyncio.Task] = set()
        self._registered = False
        self._closed = False         # closed from its own menu, for this run
        self._face: tuple | None = None   # the icon and tooltip the host last heard

    # -- what the icon reads --------------------------------------------------

    def _listing(self) -> list[dict]:
        """The house, straight off the host. No HTTP, and therefore no presence
        — see the module docstring. `summary()` is the same call `/api/characters`
        makes, so the tray and the switchboard cannot disagree."""
        try:
            listing = [self.host.summary(record) for record in self.host.registry.list()]
        except Exception:            # noqa: BLE001 — a tray must not take the house down
            log.warning("tray could not read the character list", exc_info=True)
            return []
        if self._windows is None:
            return listing
        return [{**character, "desktop": self._windows.is_open(character.get("id", ""))}
                for character in listing]

    def _refresh(self) -> None:
        self.model.update(self._listing())

    def _open_path(self, path: str) -> None:
        self._opener(f"{self.base_url}{path}")

    def _later(self, coro) -> None:
        """Run a menu click's work off the D-Bus reply: the host is waiting on
        `Event` to return, and closing a window can take seconds."""
        task = asyncio.get_running_loop().create_task(coro)
        self._jobs.add(task)
        task.add_done_callback(self._jobs.discard)

    def _toggle(self, character_id: str, name: str) -> None:
        if self._windows is not None:
            self._later(self._toggle_now(character_id, name))

    async def _toggle_now(self, character_id: str, name: str) -> None:
        try:
            if self._windows.is_open(character_id):
                await asyncio.to_thread(self._windows.close, character_id)
            else:
                self._windows.open(character_id, title=name)
        except Exception:            # noqa: BLE001 — a menu click must not take the tray down
            log.warning("could not float or put away %s", character_id, exc_info=True)
        self._refresh()
        self._publish()

    def _close(self) -> None:
        self._later(self._close_now())

    async def _close_now(self) -> None:
        await self.stop()
        self._closed = True
        log.info("tray: closed from its menu; it comes back at the next start")

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> str:
        """Bring the tray up. Returns a line for the boot panel, and never
        raises: no desktop, no dbus-fast and no watcher are all ordinary."""
        if not _session_bus_available():
            return "off · no session bus"
        try:
            from dbus_fast import BusType
            from dbus_fast.aio import MessageBus
        except ImportError:
            return "off · dbus-fast not installed"

        try:
            self.bus = await MessageBus(bus_type=BusType.SESSION).connect()
        except Exception as exc:     # noqa: BLE001
            log.debug("tray: no session bus (%s)", exc)
            return "off · session bus refused"

        self.model.update(self._listing())
        self.item, self.menu = _build_interfaces(
            self.model, self._open_path, self._toggle, self._close, self._refresh)
        self.bus.export(ITEM_PATH, self.item)
        self.bus.export(MENU_PATH, self.menu)
        # The spec wants a well-known name of this shape; some hosts key their
        # cache off it rather than off the unique name.
        await self.bus.request_name(f"org.kde.StatusNotifierItem-{os.getpid()}-1")

        await self._try_register()
        self._task = asyncio.create_task(self._run(), name="tray-poll")
        return "on · registered" if self._registered else "on · waiting for a tray host"

    async def _try_register(self) -> bool:
        """Ask the watcher to show us. False simply means nobody is hosting."""
        if self._registered or self.bus is None:
            return False
        try:
            introspection = await self.bus.introspect(WATCHER, WATCHER_PATH)
            proxy = self.bus.get_proxy_object(WATCHER, WATCHER_PATH, introspection)
            watcher = proxy.get_interface(WATCHER)
            await watcher.call_register_status_notifier_item(self.bus.unique_name)
        except Exception as exc:     # noqa: BLE001 — "no host yet" arrives as many types
            log.debug("tray: no StatusNotifierWatcher yet (%s)", exc)
            return False
        self._registered = True
        log.info("tray: registered with %s", WATCHER)
        return True

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.poll_seconds)
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:        # noqa: BLE001
                log.warning("tray poll failed", exc_info=True)

    async def _tick(self) -> None:
        if not self._registered:
            # Cheap, and the only way a tray started before the shell extension
            # ever becomes visible without a restart of the daemon.
            await self._try_register()
        self._refresh()
        self._publish()

    def _publish(self) -> None:
        """Tell the host what moved since it last heard — and only that.

        Measured against what was last *sent* rather than against the previous
        poll, because the model is also refreshed when the menu opens and after
        a click, and a change picked up there must still reach the icon.
        """
        if self.item is None or self.menu is None:
            return
        face = (self.model.status, self.model.tooltip(), bool(self.model.waiting))
        if face != self._face:
            self._face = face
            self._signal_icon()
        # …but the menu only when the rows moved: a state change the rows don't
        # show is no reason to make the host drop a menu it could have kept.
        if self.menu.rebuild():
            self.menu.LayoutUpdated()

    def _signal_icon(self) -> None:
        # Three separate signals because hosts subscribe to them separately, and
        # one that redraws only on NewIcon would keep a stale tooltip forever.
        self.item.emit_properties_changed({"Status": self.model.status})
        self.item.NewIcon()
        self.item.NewToolTip()
        self.item.NewStatus()

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self.bus is not None:
            self.bus.disconnect()
            self.bus = None
        self.item = self.menu = None
        self._face = None
        self._registered = False

    def status(self) -> dict:
        """What the boot panel says about the icon.

        This exists because "I don't see the tray" is the only symptom the tray
        has, and it covers four different causes — off in .env, no session bus,
        no dbus-fast, and no tray host yet. Guessing between them from an absent
        icon is exactly the loop this feature kept people in.
        """
        if self._closed:
            return {"state": "off", "detail": "closed from its menu — back at the next start"}
        if self.bus is None:
            return {"state": "off", "detail": "not running"}
        if not self._registered:
            return {"state": "waiting",
                    "detail": "no tray host — on GNOME, log out and back in "
                              "once to load the AppIndicator extension"}
        return {"state": "on",
                "detail": f"{len(self.model.characters)} character(s), "
                          f"{self.model.waiting} waiting"}
