"""The episodic journal is written in her time (SPEC §6.2).

`Record.ts` is aware UTC, which is right for the index — its recency math
subtracts against `now(UTC)`. It is the wrong clock to *render* with, and
`_journal_append` rendered with it anyway, while every other writer of the same
file stamps the local wall reading (`mind/journal.py`). On a UTC+9 machine that
put an exchange nine hours adrift among her own lines, out of order, and filed
38% of them under the previous day — which DREAM then consolidated under the
wrong date.

The timezone is forced here rather than taken from the machine: on a UTC box the
bug is invisible and the test would pass while asserting nothing.
"""
from __future__ import annotations

import datetime
import os
import time

import pytest

from yurios.app.memory.store import FileMemoryStore, Record

from .conftest import FakeEmbedder


@pytest.fixture
def seoul():
    """UTC+9, no DST — the timezone the bug was found on."""
    before = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Seoul"
    time.tzset()
    yield
    if before is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = before
    time.tzset()


def _store(tmp_path) -> FileMemoryStore:
    (tmp_path / "memory" / "semantic").mkdir(parents=True)
    (tmp_path / "soul").mkdir(parents=True)
    (tmp_path / "state").mkdir(parents=True)
    return FileMemoryStore(tmp_path, FakeEmbedder(), embed_dim=32)


def _entries(tmp_path, day: str) -> list[str]:
    path = tmp_path / "memory" / "episodic" / f"{day}.md"
    if not path.exists():
        return []
    return [ln for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.startswith("### ")]


async def test_an_exchange_is_stamped_in_her_local_time(tmp_path, seoul):
    """11:36 UTC is 20:36 where she lives, and 20:36 is what her diary says."""
    store = _store(tmp_path)
    await store.remember(Record(
        session_id="s", turn_index=0,
        user_msg="I want something only I can see",
        reply="Mine-only. Just you.",
        ts=datetime.datetime(2026, 9, 17, 11, 36, 54,
                             tzinfo=datetime.UTC)))

    lines = _entries(tmp_path, "2026-09-17")
    assert len(lines) == 1
    assert lines[0].startswith("### 20:36  "), lines[0]


async def test_an_evening_exchange_is_not_filed_under_yesterday(tmp_path, seoul):
    """15:30 UTC is half past midnight the NEXT day in Seoul. DREAM reads these
    files by name and prompts "Journal for <day>", so the name has to be true."""
    store = _store(tmp_path)
    await store.remember(Record(
        session_id="s", turn_index=0, user_msg="still awake?",
        reply="always, for you.",
        ts=datetime.datetime(2026, 9, 17, 15, 30, tzinfo=datetime.UTC)))

    assert _entries(tmp_path, "2026-09-17") == [], \
        "filed under the UTC day, which is yesterday where she is"
    lines = _entries(tmp_path, "2026-09-18")
    assert len(lines) == 1
    assert lines[0].startswith("### 00:30  "), lines[0]


async def test_the_index_still_keeps_utc(tmp_path, seoul):
    """Only the rendering moved. `created_at` stays the aware UTC the recency
    blend subtracts against — get this wrong and every memory ages nine hours
    early."""
    store = _store(tmp_path)
    ts = datetime.datetime(2026, 9, 17, 11, 36, 54, tzinfo=datetime.UTC)
    await store.remember(Record(session_id="s", turn_index=0,
                                user_msg="hello", reply="hi", ts=ts))
    row = next(iter(store.index.search(
        store.embedder.embed(["hello"])[0], limit=1)))
    assert datetime.datetime.fromisoformat(row.created_at) == ts
