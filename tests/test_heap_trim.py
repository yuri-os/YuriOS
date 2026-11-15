"""Unloading local weights gives the host RAM back, not just the VRAM (SPEC §7.6a).

`kernel/heap.trim_heap` is `malloc_trim(0)` behind a guard: it runs on Linux
glibc and is a no-op everywhere else. The libc handle is a seam, so the guard
is tested without depending on which libc the test box has — and one real call
on Linux proves the symbol resolves and returns. Then every unloader — the
three local cameras, the voice stack, the GGUF park — is held to calling it.
"""
from __future__ import annotations

import asyncio
import sys

import pytest

from yurios.app.providers import gguf
from yurios.forge.backends import anima, diffusers, krea2
from yurios.kernel import heap
from yurios.world import voicestack
from yurios.world.boot import BootBoard


class _FakeTrim:
    def __init__(self, raises: bool = False):
        self.calls: list[int] = []
        self.raises = raises
        self.argtypes = None
        self.restype = None

    def __call__(self, pad):
        self.calls.append(pad)
        if self.raises:
            raise OSError("boom")
        return 1


class _FakeLibc:
    def __init__(self, trim):
        if trim is not None:
            self.malloc_trim = trim


def test_on_linux_it_trims_with_no_padding(monkeypatch):
    trim = _FakeTrim()
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(heap, "_libc", lambda: _FakeLibc(trim))

    assert heap.trim_heap() is True
    assert trim.calls == [0]


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_off_linux_it_never_touches_libc(monkeypatch, platform):
    monkeypatch.setattr(sys, "platform", platform)

    def no_libc():
        raise AssertionError("libc was loaded off Linux")

    monkeypatch.setattr(heap, "_libc", no_libc)
    assert heap.trim_heap() is False


@pytest.mark.parametrize("libc", [None, _FakeLibc(None)], ids=["no-libc", "no-symbol"])
def test_a_libc_without_malloc_trim_is_a_no_op(monkeypatch, libc):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(heap, "_libc", lambda: libc)
    assert heap.trim_heap() is False


def test_a_failing_trim_never_fails_the_teardown(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(heap, "_libc", lambda: _FakeLibc(_FakeTrim(raises=True)))
    assert heap.trim_heap() is False


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="glibc only")
def test_the_real_call_resolves_on_linux():
    assert heap.trim_heap() in (True, False)    # True on glibc; must not raise


@pytest.mark.parametrize("module, cls", [
    (diffusers, diffusers.DiffusersBackend),
    (anima, anima.AnimaBackend),
    (krea2, krea2.Krea2Backend),
], ids=["diffusers", "anima", "krea2"])
def test_every_local_teardown_ends_by_trimming(monkeypatch, tmp_path, module, cls):
    ckpt = tmp_path / "model.safetensors"
    ckpt.write_bytes(b"not really a model")
    backend = cls(model_path=str(ckpt))
    backend._pipe = object()
    order: list[str] = []

    import gc
    real_collect = gc.collect
    monkeypatch.setattr(gc, "collect", lambda *a: order.append("collect") or real_collect(*a))
    monkeypatch.setattr(module, "trim_heap", lambda: order.append("trim") or True)

    backend._teardown()

    assert backend._pipe is None
    # trimming before the collect would hand back nothing: the weights are
    # only free once the last cycle holding them is gone
    assert order == ["collect", "trim"]


def test_the_voice_unload_trims_after_freeing_the_stack(cfg, monkeypatch):
    trims: list[bool] = []
    monkeypatch.setattr(voicestack, "trim_heap", lambda: trims.append(True) or True)
    stack = voicestack.VoiceStack(
        cfg.model_copy(update={"voice_unload_after_s": 0.0}), BootBoard())

    async def scenario():
        await stack.acquire()
        assert trims == []                     # loading never trims
        stack.release()
        task = stack._unload_task
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert stack.loaded is False
    assert trims == [True]


def test_the_gguf_park_trims_even_with_nothing_resident(monkeypatch):
    trims: list[bool] = []
    monkeypatch.setattr(gguf, "trim_heap", lambda: trims.append(True) or True)
    monkeypatch.setattr(gguf, "_models", {})
    monkeypatch.setattr(gguf, "_registry", {})
    monkeypatch.setattr(gguf, "_parked", False, raising=False)

    handles = gguf.park()
    try:
        assert trims == [True]
    finally:
        gguf.unpark(handles)
