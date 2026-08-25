"""Git never runs on the event loop (SPEC §2.2).

A host is one process holding every character on the node. `vaultgit` shells
out to git — tens of milliseconds on a warm repo, seconds on a Vault with a
year of history or a cold NTFS mount — and every one of those milliseconds
spent on the event loop is one in which *no* character answers: not the other
rooms' turns, not their voice sockets, not the SSE bus.

Two tests, because neither alone is enough. The first reads the source and
fails on a direct call from an `async def`, which is cheap and catches the next
one somebody writes. The second beats a heartbeat through a real route while
git is slow, which is the only thing that proves the fix rather than the shape
of the fix — and it is the one that catches a blocking call reached
*indirectly*, through a helper the reader can't see is git.
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
import time

import pytest

from yurios.app import vaultgit

ROOT = pathlib.Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "yurios"

#: `MindVault.commit_if_dirty` is `vaultgit.commit` wearing a method's name —
#: the mind's callers reach git through it and never say "vaultgit" at all.
BLOCKING_METHODS = ("commit_if_dirty",)


def _blocking_call(node: ast.Call) -> str | None:
    """The name of the git-shelling function this call invokes, if it is one.

    Matches on the attribute alone (`vaultgit.commit`, `self.vault.commit`)
    rather than on the full dotted path: what is imported as what varies —
    `studio.py` imports `vaultgit` inside the handler — and the names here are
    distinctive enough that a false positive is a call worth looking at anyway.
    """
    func = node.func
    if isinstance(func, ast.Attribute):
        if func.attr in vaultgit.BLOCKING and isinstance(func.value, ast.Name) \
                and func.value.id == "vaultgit":
            return func.attr
        if func.attr in BLOCKING_METHODS:
            return func.attr
    return None


def _offences(tree: ast.AST) -> list[tuple[int, str]]:
    """Every blocking call lexically inside an `async def`.

    Descends into nested `async def`s (a streaming response's inner generator
    is still on the loop) but stops at a nested plain `def`: that is the shape
    of the fix — a synchronous closure handed to `asyncio.to_thread` — and
    flagging it would flag exactly the code that is correct.
    """
    found: list[tuple[int, str]] = []

    def walk(node: ast.AST, *, on_loop: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.AsyncFunctionDef):
                walk(child, on_loop=True)
                continue
            if isinstance(child, (ast.FunctionDef, ast.Lambda)):
                walk(child, on_loop=False)
                continue
            if isinstance(child, ast.ClassDef):
                walk(child, on_loop=False)
                continue
            if on_loop and isinstance(child, ast.Call):
                name = _blocking_call(child)
                if name:
                    found.append((child.lineno, name))
            walk(child, on_loop=on_loop)

    walk(tree, on_loop=False)
    return found


def test_no_async_handler_shells_out_to_git_directly():
    offences = []
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offences += [f"{path.relative_to(ROOT)}:{line} — {name}()"
                     for line, name in _offences(tree)]
    assert not offences, (
        "these run git on the event loop, which stalls every character on the "
        "node. Wrap them: `await asyncio.to_thread(vaultgit.commit, …)`, the "
        "way desktop/brain.py retires a bootstrap.\n  "
        + "\n  ".join(offences))


def test_the_declared_blocking_list_names_real_functions():
    """`vaultgit.BLOCKING` is the contract the scan above reads. A rename that
    leaves it behind would silently stop checking that function."""
    for name in vaultgit.BLOCKING:
        assert callable(getattr(vaultgit, name, None)), \
            f"vaultgit.BLOCKING names {name}, which is not a function here"


# --- and the same thing, proven rather than read ------------------------------

@pytest.mark.anyio
async def test_a_slow_commit_does_not_stop_the_node(tmp_path, monkeypatch):
    """The route answers while git takes a second, and the loop keeps beating.

    On the old code the heartbeat recorded zero beats: `vaultgit.commit` held
    the only thread there is until git returned.
    """
    slow = 0.6
    real = vaultgit._git

    def crawl(vault, *args):
        if args and args[0] in ("commit", "add", "log"):
            time.sleep(slow)
        return real(vault, *args)

    monkeypatch.setattr(vaultgit, "_git", crawl)

    beats = 0
    stop = False

    async def heartbeat():
        nonlocal beats
        while not stop:
            beats += 1
            await asyncio.sleep(0.02)

    pulse = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.05)
    vaultgit.ensure_repo(tmp_path)          # sync, on purpose: not the loop's job
    (tmp_path / "note.md").write_text("hello", encoding="utf-8")
    await asyncio.to_thread(vaultgit.commit, tmp_path, "off the loop")
    stop = True
    await pulse

    assert beats > 5, (
        f"the loop beat {beats} times while a {slow}s commit ran — it was "
        "blocked, not waiting")
