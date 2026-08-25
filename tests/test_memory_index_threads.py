"""`ChunkIndex` outlives the thread that opened it (§4.3).

`CharacterHost.start` builds a character through `asyncio.to_thread`, because
seating her chat model and loading the embedder is seconds of blocking work and
a host is one process holding every character on the node. That means the index
is opened on a pool worker and then used from the event loop on every turn — so
a connection carrying sqlite3's default `check_same_thread` raises
`ProgrammingError` on the first recall of every character on the node, which is
exactly what shipped. These pin the shape that made it possible, not the bug.
"""
from __future__ import annotations

import concurrent.futures
import threading

from yurios.app.memory.index import ChunkIndex


def _open(tmp_path) -> ChunkIndex:
    return ChunkIndex(tmp_path / "memory" / "index" / "chunks.db", 4)


def _on_another_thread(fn):
    """Run `fn` somewhere that is definitely not this thread."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(fn).result()


def test_an_index_opened_on_a_worker_is_readable_from_the_caller(tmp_path):
    index = _on_another_thread(lambda: _open(tmp_path))
    assert index.count() == 0          # the first thing a recall asks (store.py)
    assert index.search([1.0, 0, 0, 0], 4) == []
    assert index.all() == []


def test_an_index_opened_on_a_worker_is_writable_from_the_caller(tmp_path):
    index = _on_another_thread(lambda: _open(tmp_path))
    index.upsert(id="a", kind="turn", source_path="t", source_span="",
                 text="a line she said", embedding=[1.0, 0.0, 0.0, 0.0],
                 created_at="2026-01-01T00:00:00+00:00")
    index.set_embedder_id("fake/dim-4")
    assert index.count() == 1
    assert index.get_embedder_id() == "fake/dim-4"


def test_an_index_opened_on_a_worker_can_be_closed_from_the_caller(tmp_path):
    # stop_async closes it on the loop; a raise there leaks the connection on
    # every character stop, archive included.
    _on_another_thread(lambda: _open(tmp_path)).close()


def test_concurrent_writers_do_not_lose_rows(tmp_path):
    # What the lock buys once the same-thread check is off: execute+commit is
    # one critical section, so a tick writing memory and a turn writing memory
    # cannot interleave inside it.
    index = _open(tmp_path)
    start = threading.Barrier(8)

    def write(n: int) -> None:
        start.wait()
        for i in range(25):
            index.upsert(id=f"{n}-{i}", kind="turn", source_path="t",
                         source_span="", text=f"{n}-{i}",
                         embedding=[float(n), float(i), 0.0, 0.0],
                         created_at="2026-01-01T00:00:00+00:00")

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for f in [pool.submit(write, n) for n in range(8)]:
            f.result()
    assert index.count() == 200
