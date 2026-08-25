"""Her inbox and the notification channel (SPEC §18.4, §32.5).

The bug these cover: a reach-out that passed Gate 2 with no page open and no
Telegram credentials had exactly zero subscribers, lived in a 200-entry
in-memory ring, and died with the process. She spent one of two or three
interrupts a day and nobody ever found out.
"""
from __future__ import annotations

import asyncio
import subprocess

import pytest

from yurios.world.inbox import MAX_ENTRIES, Inbox

pytest.importorskip("fastapi")
from starlette.testclient import TestClient                  # noqa: E402

from yurios.desktop.voice.backends.fakes import FakeBrain    # noqa: E402
from yurios.world.channels.notify import NotifyChannel       # noqa: E402
from yurios.world.main import create_app                     # noqa: E402
from tests.conftest import make_mind, run_mind               # noqa: E402


# ---- the store -------------------------------------------------------------

def test_the_file_survives_the_process(tmp_path):
    """The whole point: a reach-out made at 3am is still waiting at 9am, on the
    other side of a daemon restart that emptied the transcript ring."""
    box = Inbox(tmp_path)
    box.add({"id": "a1", "ts": "2026-08-14T03:00:00", "text": "about the cat names"})
    assert Inbox(tmp_path).unread()["count"] == 1        # a fresh process, same vault


def test_a_report_carries_the_path_to_the_thing_it_is_about(tmp_path):
    """A briefing line is a pointer, not the document (SPEC §18.2a). The chat
    view turns `report_path` into a card the same way it turns `image_url` into
    a picture, and neither one is the message text."""
    box = Inbox(tmp_path)
    box.add({"id": "r1", "ts": "2026-08-21T04:10:00",
             "text": "I read the tape while you were out.",
             "report_path": "reports/market-brief/2026-08-20.md",
             "report_title": "Overnight market brief",
             "report_job": "market-brief"})
    row = box.pending()[0]
    assert row["kind"] == "report"
    assert row["report_path"] == "reports/market-brief/2026-08-20.md"
    assert row["report_title"] == "Overnight market brief"


def test_the_newest_brief_retires_the_one_you_never_read(tmp_path):
    """A nightly job is a standing answer to a standing question, so what is
    owed to you is *this* morning's. A week away should be one report waiting,
    not seven — the other six are still on her desk, which is the archive."""
    box = Inbox(tmp_path)
    for day in ("18", "19", "20"):
        box.add({"id": f"r{day}", "ts": f"2026-08-{day}T04:00:00",
                 "text": f"brief for the {day}th",
                 "report_path": f"reports/market-brief/2026-08-{day}.md",
                 "report_job": "market-brief"})
    pending = box.pending()
    assert [row["id"] for row in pending] == ["r20"]
    assert len(box.entries()) == 3            # retired, not deleted


def test_one_job_does_not_retire_another_or_anything_else(tmp_path):
    """Scoped to the job, and to reports. A selfie and a reach-out are each a
    separate thing she did and none of them replaces another."""
    box = Inbox(tmp_path)
    box.add({"id": "s1", "ts": "2026-08-20T09:00:00", "text": "",
             "image_url": "/selfies/x.png"})
    box.add({"id": "n1", "ts": "2026-08-20T10:00:00", "text": "thinking of you"})
    box.add({"id": "a1", "ts": "2026-08-20T04:00:00", "text": "papers",
             "report_path": "reports/papers/2026-08-19.md",
             "report_job": "papers"})
    box.add({"id": "b1", "ts": "2026-08-21T04:00:00", "text": "market",
             "report_path": "reports/market-brief/2026-08-20.md",
             "report_job": "market-brief"})
    box.add({"id": "b2", "ts": "2026-08-22T04:00:00", "text": "market again",
             "report_path": "reports/market-brief/2026-08-21.md",
             "report_job": "market-brief"})
    assert {row["id"] for row in box.pending()} == {"s1", "n1", "a1", "b2"}


def test_a_selfie_reads_differently_from_a_line(tmp_path):
    box = Inbox(tmp_path)
    box.add({"id": "a1", "ts": "t1", "text": "hey"})
    box.add({"id": "a2", "ts": "t2", "text": "", "image_url": "/selfies/x.png"})
    assert box.unread() == {"count": 2, "selfies": 1, "latest": "t2"}
    assert [e["kind"] for e in box.pending()] == ["message", "selfie"]


def test_a_republished_message_is_not_a_second_message(tmp_path):
    box = Inbox(tmp_path)
    box.add({"id": "a1", "ts": "t1", "text": "hey"})
    assert box.add({"id": "a1", "ts": "t1", "text": "hey"}) is None
    assert box.unread()["count"] == 1


def test_reading_the_room_clears_everything_pending(tmp_path):
    box = Inbox(tmp_path)
    for i in range(3):
        box.add({"id": f"a{i}", "ts": f"t{i}", "text": "…"})
    assert box.mark_read() == 3
    assert box.unread()["count"] == 0
    assert box.mark_read() == 0                          # idempotent
    assert len(box.entries()) == 3                       # …and nothing is deleted


def test_it_truncates_rather_than_growing_without_bound(tmp_path):
    box = Inbox(tmp_path)
    for i in range(MAX_ENTRIES + 10):
        box.add({"id": f"a{i}", "ts": f"t{i:04d}", "text": "…"})
    entries = box.entries()
    assert len(entries) == MAX_ENTRIES
    assert entries[-1]["id"] == f"a{MAX_ENTRIES + 9}"    # the newest survive


def test_an_unreadable_inbox_is_an_empty_one_not_a_crash(tmp_path):
    box = Inbox(tmp_path)
    box.add({"id": "a1", "ts": "t1", "text": "hey"})
    box.path.write_text("{ this is not json", encoding="utf-8")
    assert box.unread()["count"] == 0
    box.add({"id": "a2", "ts": "t2", "text": "again"})   # the next write repairs it
    assert box.unread()["count"] == 1


def test_no_vault_is_a_working_no_op():
    """A bare runtime (tests, a config with no Vault) must not fail a turn over
    a convenience file."""
    box = Inbox(None)
    assert box.add({"id": "a1", "text": "hey"}) is None
    assert box.unread()["count"] == 0
    assert box.mark_read() == 0


def test_a_write_here_does_not_dirty_the_vault(seeded_vault):
    """§34.2's rule for the desk, applied: delivery state flips on every glance
    at her room, and committing it would put one entry per glance in `git log`.
    """
    box = Inbox(seeded_vault)
    box.add({"id": "a1", "ts": "t1", "text": "hey"})
    box.mark_read()
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=seeded_vault,
                           capture_output=True, text=True).stdout
    assert "inbox.json" not in dirty, f"the inbox dirtied the Vault: {dirty}"
    assert dirty.strip() == "", f"the inbox dirtied the Vault: {dirty}"


# ---- the runtime seam ------------------------------------------------------

def test_only_unheard_lines_are_filed(cfg):
    """`proactive` is every line she starts — greetings included. Filing those
    would badge the switchboard for a greeting you were being greeted by."""
    rt = create_app(cfg, brain=FakeBrain()).state.rt
    rt.post_message("assistant", "hello, you're back", proactive=True)
    rt.post_message("user", "hi")
    rt.post_message("assistant", "an ordinary reply")
    assert rt.inbox.unread()["count"] == 0
    rt.post_message("assistant", "I kept thinking about the cat names",
                    proactive=True, unheard=True)
    assert rt.inbox.unread()["count"] == 1


async def test_the_wire_carries_the_flag_so_an_open_page_can_clear_it(cfg):
    rt = create_app(cfg, brain=FakeBrain()).state.rt
    q = rt.hub.subscribe()
    rt.post_message("assistant", "…", proactive=True, unheard=True)
    assert q.get_nowait()["unheard"] is True


# ---- the mind's two reach-out paths ---------------------------------------

async def test_an_undeliverable_reach_out_is_filed_not_lost(cfg, seeded_vault):
    """SPEAK with no page open: `speak()` says no, she falls back to a chat line,
    and *that* is the message that used to evaporate."""
    import datetime

    rig = make_mind(cfg, seeded_vault)
    rig.say("the big interview is tomorrow evening. wish me luck",
            reply="You'll be great.")
    await rig.mind.tick()
    rig.mind.bus.post("user_absent", {}, source="frontend")
    rig.speak.connected = False                          # nobody in the room
    due = datetime.datetime(2026, 7, 7, 18, 0)
    rig.mind.goals.add("ask how the interview went", kind="reach_out",
                       priority=0.8, due=due.isoformat(timespec="seconds"),
                       commitment="single-minded", provenance="promise:her-own-words")

    await run_mind(rig, hours=40)

    proactive = rig.post.proactive()
    assert proactive, "she never reached out at all"
    assert all(m.get("unheard") for m in proactive), \
        f"a reach-out into an empty room must be filed: {proactive}"


# ---- the notification channel ---------------------------------------------

class Recorder:
    def __init__(self):
        self.calls = []

    async def __call__(self, title, body, event):
        self.calls.append((title, body, event))


async def test_the_doorbell_rings_only_for_unheard_lines(cfg):
    rt = create_app(cfg, brain=FakeBrain()).state.rt
    seen = Recorder()
    channel = NotifyChannel(notifier=seen)
    await channel.start(rt)
    try:
        rt.post_message("assistant", "hello, you're back", proactive=True)
        rt.post_message("user", "hi")
        rt.post_message("assistant", "an ordinary reply")
        rt.post_message("assistant", "I kept thinking about the cat names",
                        proactive=True, unheard=True)
        await asyncio.sleep(0)                           # let the deliver task drain
        await asyncio.sleep(0)
    finally:
        await channel.stop()
    assert [c[1] for c in seen.calls] == ["I kept thinking about the cat names"]


async def test_a_picture_gets_a_body_a_notification_can_show(cfg):
    rt = create_app(cfg, brain=FakeBrain()).state.rt
    seen = Recorder()
    channel = NotifyChannel(notifier=seen)
    await channel.start(rt)
    try:
        rt.post_message("assistant", "", image_url="/selfies/x.png",
                        proactive=True, unheard=True)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    finally:
        await channel.stop()
    assert seen.calls and seen.calls[0][1] == "sent you a picture."


async def test_a_long_reach_out_is_a_doorbell_not_the_message(cfg):
    rt = create_app(cfg, brain=FakeBrain()).state.rt
    seen = Recorder()
    channel = NotifyChannel(notifier=seen)
    await channel.start(rt)
    try:
        rt.post_message("assistant", "x" * 500, proactive=True, unheard=True)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    finally:
        await channel.stop()
    assert len(seen.calls[0][1]) <= 180 and seen.calls[0][1].endswith("…")


async def test_the_shell_wins_when_one_is_attached(cfg):
    """`auto` prefers the desktop shell and degrades to notify-send when it goes
    away — plugging in the app should upgrade the notification, not silence it."""
    rt = create_app(cfg, brain=FakeBrain()).state.rt
    channel = NotifyChannel(backend="auto")
    await channel.start(rt)
    try:
        q = channel.subscribe()
        assert channel.shell_attached
        rt.post_message("assistant", "still awake?", proactive=True, unheard=True)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        payload = q.get_nowait()
        assert payload["type"] == "notify"
        assert payload["body"] == "still awake?"
        assert payload["kind"] == "message"
    finally:
        await channel.stop()


async def test_off_rings_nothing(cfg):
    rt = create_app(cfg, brain=FakeBrain()).state.rt
    seen = Recorder()
    channel = NotifyChannel(backend="off", notifier=seen)
    await channel.start(rt)
    try:
        rt.post_message("assistant", "…", proactive=True, unheard=True)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    finally:
        await channel.stop()
    assert seen.calls == []


def test_the_channel_is_off_by_default(cfg):
    """Nothing should start drawing on your desktop because you installed it."""
    from yurios.world.channels.manager import ChannelManager
    assert cfg.notify_enabled is False
    assert [c.name for c in ChannelManager.from_config(cfg).channels] == []
    on = cfg.model_copy(update={"notify_enabled": True})
    assert [c.name for c in ChannelManager.from_config(on).channels] == ["notify"]


def test_the_notification_stream_takes_no_presence_and_no_hub_slot(cfg):
    """The trap this design exists to avoid: a shell in the tray is attached for
    hours, and reading /api/events would post `user_present` for all of them —
    Gate 2 would then suppress every reach-out as an interruption of a
    conversation already under way, silencing exactly what this delivers."""
    rt = create_app(cfg, brain=FakeBrain()).state.rt
    channel = NotifyChannel()
    before_signals, before_subs = len(rt.signals), rt.hub.subscribers
    channel.subscribe()
    assert len(rt.signals) == before_signals
    assert rt.hub.subscribers == before_subs


# ---- the HTTP surface ------------------------------------------------------

def test_the_room_serves_and_then_clears_what_was_waiting(cfg):
    app = create_app(cfg, brain=FakeBrain())
    rt = app.state.rt
    rt.post_message("assistant", "I kept thinking about the cat names",
                    proactive=True, unheard=True)
    with TestClient(app) as c:
        payload = c.get("/api/inbox").json()
        assert [e["text"] for e in payload["entries"]] == \
            ["I kept thinking about the cat names"]
        assert payload["unread"]["count"] == 1
        assert c.post("/api/inbox/read").json()["marked"] == 1
        assert c.get("/api/inbox").json()["entries"] == []
        # …and nothing is deleted: ?all=1 still has it
        assert len(c.get("/api/inbox?all=1").json()["entries"]) == 1


def test_the_notification_stream_is_absent_when_the_channel_is_off(cfg):
    """404, so the shell stops asking rather than reconnecting into a stream
    that will never carry anything."""
    with TestClient(create_app(cfg, brain=FakeBrain())) as c:
        assert c.get("/api/notifications").status_code == 404


def test_the_board_badges_a_character_whose_runtime_is_down(tmp_path, monkeypatch):
    """The offline case is not an edge case — it is the one that matters most.
    A reach-out she made before the last restart, on a character the host has
    not started yet, still has to be visible on the board."""
    from fastapi import FastAPI

    from yurios.world.config import Config
    from yurios.world.host import create_host_app
    from tests.test_host import record

    from yurios.characters.registry import CharacterRegistry

    registry = CharacterRegistry(tmp_path)
    registry.add(record(tmp_path, "yuri"))
    registry.add(record(tmp_path, "mika"))
    # she reached out last night; the daemon has been restarted since
    box = Inbox(registry.get("mika").paths.vault)
    box.add({"id": "a1", "ts": "2026-08-14T03:00:00", "text": "about the cat names"})
    box.add({"id": "a2", "ts": "2026-08-14T03:02:00", "image_url": "/selfies/x.png"})

    monkeypatch.setattr("yurios.world.host.hosting.create_app",
                        lambda cfg, **kw: FastAPI())
    app = create_host_app(Config(data_dir=tmp_path), registry)
    with TestClient(app) as client:
        rows = {c["id"]: c["unread"] for c in client.get("/api/characters").json()["characters"]}
    assert rows["mika"] == {"count": 2, "selfies": 1, "latest": "2026-08-14T03:02:00"}
    assert rows["yuri"]["count"] == 0


async def test_a_failing_notify_send_is_said_once_not_swallowed(cfg, monkeypatch, caplog):
    """Installed is not working: notify-send exits non-zero with no notification
    daemon on the session, which is the ordinary state of the headless boxes
    this backend exists for. Silently dropping there looks exactly like a
    companion who never reached out."""
    rt = create_app(cfg, brain=FakeBrain()).state.rt
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/notify-send")

    async def failing(*args, **kwargs):
        class Proc:
            returncode = 1

            async def communicate(self):
                return b"", b"Cannot autolaunch D-Bus without X11 $DISPLAY"
        return Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", failing)
    channel = NotifyChannel(backend="libnotify")
    await channel.start(rt)
    try:
        with caplog.at_level("WARNING"):
            rt.post_message("assistant", "still awake?", proactive=True, unheard=True)
            rt.post_message("assistant", "and again", proactive=True, unheard=True)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
    finally:
        await channel.stop()
    said = [r.getMessage() for r in caplog.records if "NOTIFY_ENABLED" in r.getMessage()]
    assert len(said) == 1, f"say it once, not per reach-out: {said}"
    assert "D-Bus" in said[0] and "still filed in her inbox" in said[0]
    # …and the message is not lost — that is the point of saying "still filed"
    assert rt.inbox.unread()["count"] == 2


async def test_a_missing_notify_send_says_how_to_fix_it(cfg, monkeypatch, caplog):
    rt = create_app(cfg, brain=FakeBrain()).state.rt
    monkeypatch.setattr("shutil.which", lambda name: None)
    channel = NotifyChannel(backend="libnotify")
    await channel.start(rt)
    try:
        with caplog.at_level("WARNING"):
            rt.post_message("assistant", "…", proactive=True, unheard=True)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
    finally:
        await channel.stop()
    said = [r.getMessage() for r in caplog.records if "NOTIFY_ENABLED" in r.getMessage()]
    assert len(said) == 1 and "libnotify-bin" in said[0]
