"""How often a half-finished reply goes out on the bus (SPEC §10.5).

A `draft` carries the whole reply so far, not a delta. That is fine at one
event per sentence and quietly dangerous at one per token: the bytes are
quadratic in the length of the reply, and `EventHub` queues are bounded at 256
and *drop* when full rather than block the publisher (`kernel/hub.py`). The
drop does not land politely on drafts. It lands on whatever is published next,
which at the end of a turn is her committed `message` — so a long reply to a
client that is slow, backgrounded or merely one of six open tabs could stream
beautifully and then never commit, logged at `debug` and nowhere else.

The suite did not notice when the runner started publishing per token, because
every test here asserts what a draft *says* and its fake clients drain
instantly. So the last test in this file keeps a subscriber that never drains —
which is what a stalled browser is — and asks the only question that matters:
did her message survive the drafts.
"""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi")
from yurios.desktop.voice.backends.fakes import FakeBrain     # noqa: E402
from yurios.world.main import create_app                      # noqa: E402
from yurios.world.turns import DRAFT_FLUSH_CHARS, _Drafts     # noqa: E402


class Recorder:
    """Stands in for the hub: remembers what a draft said and when."""

    def __init__(self):
        self.drafts: list[str] = []

    def publish(self, type_, payload, sticky=None):
        assert type_ == "draft"
        self.drafts.append(payload["text"])


def feed(text: str, *, enabled: bool = True) -> Recorder:
    """Push *text* through `_Drafts` one character at a time — the worst case a
    tokeniser can produce, and close enough to one for cadence."""
    hub = Recorder()
    drafts = _Drafts(hub, enabled=enabled)
    for ch in text:
        drafts.push(ch)
    drafts.flush()
    return hub


# ---- the cadence ------------------------------------------------------------

def test_a_draft_goes_out_on_a_sentence_not_on_a_token():
    hub = feed("Hey. I missed you. Tell me about your day.")

    assert hub.drafts == [
        "Hey.",
        "Hey. I missed you.",
        "Hey. I missed you. Tell me about your day.",
    ]


def test_a_line_break_is_a_beat_of_its_own():
    """Three lines are three bubbles (§10.5), so each one is worth a repaint —
    a paragraph with no full stop is still somewhere the reader's eye stops."""
    hub = feed("morning\nstill in bed\ncoffee first")

    assert hub.drafts == [
        "morning",
        "morning\nstill in bed",
        "morning\nstill in bed\ncoffee first",
    ]


def test_prose_with_no_boundary_at_all_still_streams():
    """A URL, a code block, a model that forgot its punctuation: without the
    character budget the room would sit empty until the turn committed."""
    run = "x" * (DRAFT_FLUSH_CHARS * 3)
    hub = feed(run)

    assert len(hub.drafts) == 3
    assert hub.drafts[0] == "x" * DRAFT_FLUSH_CHARS
    assert hub.drafts[-1] == run


def test_the_tail_is_flushed_when_the_stream_ends():
    """The commit behind it waits on a memory-extractor call, so a draft cut
    mid-sentence would be what the room showed for as long as that took."""
    hub = feed("Hey. And one more thing")

    assert hub.drafts[-1] == "Hey. And one more thing"


def test_a_cold_open_publishes_no_drafts_of_its_own():
    """Its text is given whole, once, before the stream starts."""
    assert feed("Hey. I missed you.", enabled=False).drafts == []


def test_throttling_changed_the_cadence_and_not_the_text():
    """The guard on the guard. `_Drafts.text` is what gets committed, so if a
    flush rule ever started dropping or reordering a token, the reply itself
    would change — silently, since every draft before the last is transient."""
    written = "Hey.\n\nI missed you  today.   Tell me   everything.\n"
    hub = Recorder()
    drafts = _Drafts(hub)
    for ch in written:
        drafts.push(ch)
    drafts.flush()

    assert drafts.text == "Hey.\n\nI missed you today. Tell me everything."
    assert hub.drafts[-1] == drafts.text


# ---- and what it is for -----------------------------------------------------

LONG = " ".join(f"word{n}" for n in range(400))       # no sentence enders at all


class LongWindedBrain(FakeBrain):
    """One token per word, and never a full stop — the shape that fills a queue
    fastest, because only the character budget can flush it."""

    async def stream_reply(self, session_id, text):
        for word in LONG.split(" "):
            yield word + " "


def test_a_long_reply_does_not_flood_a_stalled_client_off_the_bus(cfg):
    """The failure this file exists for, end to end.

    The subscriber here never drains, which is what a backgrounded tab is. Its
    queue holds 256 events. Per-token drafts put ~400 into it before her message
    was published, so the `message` was the event that got dropped: the room
    streamed a reply and then never committed it.
    """
    cfg = cfg.model_copy(update={"tools_backend": "off", "mind_enabled": False})
    rt = create_app(cfg, brain=LongWindedBrain()).state.rt

    async def scenario():
        # `subscribe` binds the hub to the running loop, as the SSE route does.
        stalled = rt.hub.subscribe(viewer=True)
        drained = rt.hub.subscribe(viewer=True)      # …and one that keeps up

        reply = await rt.turns.run("tell me everything", channel="cli")
        assert reply["message"]["text"].startswith("word0 ")      # she did answer

        return ([stalled.get_nowait()["type"] for _ in range(stalled.qsize())],
                [drained.get_nowait()["type"] for _ in range(drained.qsize())])

    seen, _ = asyncio.run(scenario())

    assert len(seen) < 256, (
        f"{len(seen)} events for one reply filled a subscriber's queue; "
        "anything published after this point is dropped")
    assert "message" in seen, (
        "her committed message never reached a subscriber that was merely slow")
