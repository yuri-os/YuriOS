"""The direct GGUF route used when the configured LM Studio server is down."""
from __future__ import annotations

import httpx

from yurios.app import main
from yurios.app.config import Config
from yurios.app.providers import gguf


class _Llama:
    def create_chat_completion(self, *, stream, **kwargs):
        if stream:
            return iter([
                {"choices": [{"delta": {"content": "hello "}}]},
                {"choices": [{"delta": {"content": "Yuri"}}]},
            ])
        return {"choices": [{"message": {"content": "utility"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1,
                          "total_tokens": 3}}


class _Loaded:
    def __init__(self):
        import threading

        self.lock = threading.Lock()
        self.llama = _Llama()


async def test_direct_gguf_chat_and_utility_share_one_loaded_context(monkeypatch):
    loaded = _Loaded()
    monkeypatch.setattr(gguf, "get_model", lambda *args: loaded)
    cfg = Config(_env_file=None, chat_model="gguf/example/model-GGUF",
                 utility_model="gguf/example/model-GGUF")

    chat = gguf.GGUFChatModel(cfg.chat_model, cfg, temperature=0.8)
    utility = gguf.GGUFUtilityModel(cfg.utility_model, cfg, max_tokens=32, thinking=True)

    assert chat._loaded is utility._loaded is None
    got = [part async for part in chat.stream([{"role": "user", "content": "hi"}])]
    assert "".join(got) == "hello Yuri"
    assert await utility.complete([{"role": "user", "content": "extract"}]) == "utility"
    assert chat._loaded is utility._loaded is loaded


def test_default_gguf_cache_dir_stays_out_of_the_home_volume():
    assert Config(_env_file=".env.example").gguf_cache_dir == "./models"


def test_dead_lmstudio_selects_the_gguf_models(monkeypatch):
    class Chat:
        def __init__(self, *args, **kwargs):
            self.args = args

    class Utility:
        def __init__(self, *args, **kwargs):
            self.args = args

    monkeypatch.setattr(main, "_lmstudio_available", lambda cfg: False)
    monkeypatch.setattr(main.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(gguf, "GGUFChatModel", Chat)
    monkeypatch.setattr(gguf, "GGUFUtilityModel", Utility)
    cfg = Config(_env_file=None, chat_model="lm_studio/example/chat-GGUF",
                 utility_model="lm_studio/example/utility-GGUF")

    assert isinstance(main.build_chat_model(cfg), Chat)
    assert isinstance(main.build_utility_model(cfg), Utility)


def test_explicit_gguf_never_probes_lmstudio(monkeypatch):
    class Chat:
        def __init__(self, *args, **kwargs):
            self.args = args

    monkeypatch.setattr(main, "_lmstudio_available",
                        lambda cfg: (_ for _ in ()).throw(AssertionError("probed LM Studio")))
    monkeypatch.setattr(gguf, "GGUFChatModel", Chat)
    cfg = Config(_env_file=None, chat_model="gguf/example/chat-GGUF")

    assert isinstance(main.build_chat_model(cfg), Chat)


def test_running_lmstudio_keeps_the_configured_remote_route(monkeypatch):
    monkeypatch.setattr(main, "_lmstudio_available", lambda cfg: True)
    cfg = Config(_env_file=None, chat_model="lm_studio/example/chat-GGUF")

    model = main.build_chat_model(cfg)

    assert model.model == "lm_studio/example/chat-GGUF"


def test_unreachable_lmstudio_is_not_a_boot_failure(monkeypatch):
    class RefusingClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get(self, url):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(main.httpx, "Client", lambda **kwargs: RefusingClient())

    assert main._lmstudio_available(Config(_env_file=None)) is False
