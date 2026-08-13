from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from yurios.app.providers import gguf
from yurios.app.providers.admission import InferenceAdmission, InferenceBusy
from yurios.app.providers.openrouter import LiteLLMChatModel, LiteLLMUtilityModel


async def test_gate_bounds_its_queue_and_recovers_from_waiter_cancellation():
    gate = InferenceAdmission(active=1, queue=1)
    release = asyncio.Event()

    async def hold():
        async with gate:
            await release.wait()

    active = asyncio.create_task(hold())
    await asyncio.sleep(0)
    waiting = asyncio.create_task(hold())
    await asyncio.sleep(0)

    with pytest.raises(InferenceBusy):
        async with gate:
            pass

    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert gate.waiting == 0

    replacement = asyncio.create_task(hold())
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(active, replacement)


async def test_gate_releases_after_an_exception():
    gate = InferenceAdmission()

    with pytest.raises(RuntimeError, match="failed"):
        async with gate:
            raise RuntimeError("failed")

    async with gate:
        pass


async def test_gate_releases_after_active_task_cancellation():
    gate = InferenceAdmission()
    started = asyncio.Event()

    async def hold():
        async with gate:
            started.set()
            await asyncio.Event().wait()

    active = asyncio.create_task(hold())
    await started.wait()
    active.cancel()
    with pytest.raises(asyncio.CancelledError):
        await active

    async with asyncio.timeout(1):
        async with gate:
            pass


async def test_per_character_turn_policy_allows_only_two_waiters():
    gate = InferenceAdmission(active=1, queue=2)
    release = asyncio.Event()

    async def hold():
        async with gate:
            await release.wait()

    tasks = [asyncio.create_task(hold()) for _ in range(3)]
    await asyncio.sleep(0)
    assert gate.waiting == 2
    with pytest.raises(InferenceBusy):
        async with gate:
            pass
    release.set()
    await asyncio.gather(*tasks)


async def test_litellm_instances_share_the_default_one_active_eight_waiting(monkeypatch):
    release = asyncio.Event()
    entered = 0

    async def completion(**kwargs):
        nonlocal entered
        entered += 1
        await release.wait()
        message = SimpleNamespace(content="ok")
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    monkeypatch.setattr("litellm.acompletion", completion)
    models = [LiteLLMUtilityModel("some/model") for _ in range(10)]
    tasks = [asyncio.create_task(model.complete([])) for model in models[:9]]
    await asyncio.sleep(0)

    assert entered == 1
    with pytest.raises(InferenceBusy):
        await models[9].complete([])

    release.set()
    assert await asyncio.gather(*tasks) == ["ok"] * 9


async def test_litellm_stream_holds_shared_capacity_until_closed_for_gguf(
        monkeypatch):
    finish_stream = asyncio.Event()
    gguf_entered = asyncio.Event()

    async def chunks():
        yield SimpleNamespace(choices=[SimpleNamespace(
            delta=SimpleNamespace(content="first"))])
        await finish_stream.wait()

    async def completion(**kwargs):
        assert kwargs["stream"] is True
        return chunks()

    monkeypatch.setattr("litellm.acompletion", completion)
    loaded = SimpleNamespace(
        lock=threading.Lock(),
        llama=SimpleNamespace(create_chat_completion=lambda **kwargs: {
            "choices": [{"message": {"content": "utility"}}]}),
        closed=False,
    )

    async def acquire_current(provider):
        gguf_entered.set()
        loaded.lock.acquire()
        return loaded

    monkeypatch.setattr(gguf, "_acquire_current", acquire_current)
    cfg = SimpleNamespace()
    chat = LiteLLMChatModel("some/model")
    utility = gguf.GGUFUtilityModel("gguf/example/model", cfg,
                                    max_tokens=32, thinking=True)

    stream = chat.stream([])
    assert await anext(stream) == "first"
    utility_task = asyncio.create_task(utility.complete([]))
    await asyncio.sleep(0)
    assert not gguf_entered.is_set()

    await stream.aclose()
    assert await utility_task == "utility"
    assert gguf_entered.is_set()
