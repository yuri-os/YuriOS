"""An embedding never runs on the event loop (SPEC §2.4).

A host is one process holding every character on the node. `embed()` is
blocking by contract — a torch forward pass in process, or an HTTP round trip
to LM Studio or Ollama that a model swap can hold for the client's whole
sixty-second timeout — and every caller used to make it straight from the
loop: recall before a reply's first token, the knowledge shelf beside it,
remember after it, a journal row on every tick. One slow server froze every
room on the node.

The same two tests as `test_vault_off_loop.py`, for the same reasons: the scan
catches the next direct call somebody writes, and the heartbeat catches one
reached *indirectly* — which is how the worst of these hid, inside a
synchronous `_assemble` that an async `stream_reply` called.
"""
from __future__ import annotations

import ast
import asyncio
import json
import pathlib
import threading
import time

import httpx

from tests.conftest import FakeEmbedder, make_mind

ROOT = pathlib.Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "yurios"

#: Methods that embed on the calling thread. `recall` and a knowledge store's
#: `search` wrap one; `write_summary` and `reindex` index what they write.
BLOCKING = ("embed", "recall", "write_summary", "reindex")

#: `search` is too common a name to ban outright (the web tools' own is async);
#: only a knowledge store's is the blocking one.
KNOWLEDGE = ("knowledge", "shelf")


def _blocking_call(node: ast.Call) -> str | None:
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else \
        func.id if isinstance(func, ast.Name) else None
    if name in BLOCKING:
        return name
    if isinstance(func, ast.Attribute) and func.attr == "search":
        owner = func.value
        owner_name = owner.attr if isinstance(owner, ast.Attribute) else \
            owner.id if isinstance(owner, ast.Name) else ""
        if owner_name in KNOWLEDGE:
            return f"{owner_name}.search"
    return None


def _offences(tree: ast.AST) -> list[tuple[int, str]]:
    """Every blocking call lexically inside an `async def` — stopping at a
    nested plain `def` or lambda, which is the shape of the fix."""
    found: list[tuple[int, str]] = []

    def walk(node: ast.AST, *, on_loop: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.AsyncFunctionDef):
                walk(child, on_loop=True)
                continue
            if isinstance(child, (ast.FunctionDef, ast.Lambda, ast.ClassDef)):
                walk(child, on_loop=False)
                continue
            if on_loop and isinstance(child, ast.Call):
                name = _blocking_call(child)
                if name:
                    found.append((child.lineno, name))
            walk(child, on_loop=on_loop)

    walk(tree, on_loop=False)
    return found


def test_no_async_code_embeds_on_the_loop():
    offences = []
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offences += [f"{path.relative_to(ROOT)}:{line} — {name}()"
                     for line, name in _offences(tree)]
    assert not offences, (
        "these embed on the event loop, which stalls every character on the "
        "node while the embedder answers. Use the async form (`arecall`, "
        "`asearch`) or `await asyncio.to_thread(embedder.embed, …)`.\n  "
        + "\n  ".join(offences))


# --- and the same thing, proven rather than read ------------------------------

class SlowEmbedder(FakeEmbedder):
    """FakeEmbedder behind a server that takes its time to answer."""

    def __init__(self, delay: float):
        self.delay = delay
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        time.sleep(self.delay)
        return super().embed(texts)


async def test_a_slow_embedder_does_not_stop_the_node(cfg, seeded_vault):
    """A whole turn — the shelf read into, the reply assembled and streamed,
    the exchange remembered — and a journal line, while every embed takes a
    quarter of a second. The loop keeps beating throughout.

    On the old code the heartbeat stalled for every one of those calls: each
    ran on the only thread the loop has.
    """
    from yurios.app.memory.store import Record

    rig = make_mind(cfg, seeded_vault)
    brain, store = rig.mind.brain, rig.mind.brain.state.store
    # something to recall, or recall rightly skips the embed (an empty Vault)
    await store.remember(Record(session_id="s0", turn_index=0,
                                user_msg="what tea is in the tin?",
                                reply="Sencha, the green one."))
    slow = SlowEmbedder(0.25)
    store.embedder = rig.mind.knowledge.embedder = brain.state.embedder = slow

    beats = 0
    stop = False

    async def heartbeat():
        nonlocal beats
        while not stop:
            beats += 1
            await asyncio.sleep(0.02)

    pulse = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.05)
    started = time.monotonic()

    await rig.mind.knowledge.ingest(
        "sencha.md", "# Sencha\n\nSencha is steamed rather than pan-fired.\n")
    session = brain.resolve_session(None)
    reply = "".join([t async for t in brain.stream_reply(session, "how is sencha made?")])
    await brain.persist(session, "how is sencha made?", reply)
    await rig.mind.journal.write("steeped a pot of sencha")

    elapsed = time.monotonic() - started
    stop = True
    await pulse

    # ingest, recall, the shelf, remember, the journal row
    assert slow.calls >= 5, f"only {slow.calls} embeds — the path was not exercised"
    expected = elapsed / 0.02
    assert beats > expected / 2, (
        f"the loop beat {beats} times in {elapsed:.2f}s (≈{expected:.0f} if "
        f"free) across {slow.calls} slow embeds — it was blocked, not waiting")


# --- one client per server embedder -------------------------------------------

def _lmstudio(transport):
    from yurios.app.providers.lmstudio import LMStudioEmbedder
    return LMStudioEmbedder("nomic", dim=2, base_url="http://lm.test/v1",
                            transport=transport)


def _ollama(transport):
    from yurios.app.providers.ollama import OllamaEmbedder
    return OllamaEmbedder("nomic", dim=2, base_url="http://ollama.test",
                          transport=transport)


def _answer(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/embeddings") and "/api/" not in request.url.path:
        n = len(json.loads(request.content)["input"])
        return httpx.Response(200, json={"data": [
            {"index": i, "embedding": [1.0, 0.0]} for i in range(n)]})
    return httpx.Response(200, json={"embedding": [1.0, 0.0]})


def test_a_server_embedder_keeps_one_client_across_calls():
    """A fresh `httpx.Client` per call was a new pool and a new connection on
    every recall, remember and journal row. One client, kept."""
    for build in (_lmstudio, _ollama):
        embedder = build(httpx.MockTransport(_answer))
        assert embedder.embed(["a"]) == [[1.0, 0.0]]
        first = embedder._http.client()
        embedder.embed(["b", "c"])
        assert embedder._http.client() is first, build.__name__


def test_a_closed_embedder_reopens_rather_than_raising():
    """A host can hand one embedder to several characters, and stopping one
    closes it: the next caller gets a new client, not a closed one."""
    for build in (_lmstudio, _ollama):
        embedder = build(httpx.MockTransport(_answer))
        embedder.embed(["a"])
        before = embedder._http.client()
        embedder.close()
        assert before.is_closed
        assert embedder.embed(["b"]) == [[1.0, 0.0]], build.__name__
        assert embedder._http.client() is not before


def test_one_client_is_shared_by_concurrent_workers():
    """Every loop-side caller reaches `embed()` through `asyncio.to_thread`,
    so two workers can be inside one embedder at once. They share a client."""
    embedder = _lmstudio(httpx.MockTransport(_answer))
    seen: list[int] = []
    gate = threading.Barrier(8)

    def worker():
        gate.wait()
        embedder.embed(["x"])
        seen.append(id(embedder._http.client()))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(seen) == 8 and len(set(seen)) == 1
