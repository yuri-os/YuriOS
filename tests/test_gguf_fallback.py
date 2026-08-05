"""The direct GGUF route used when the configured LM Studio server is down."""
from __future__ import annotations

import sys
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

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


def _gguf_file(path):
    path.write_bytes(b"GGUF" + b"\0" * (1024 * 1024 - 4))
    return path


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


def test_qwen_template_can_be_rendered_with_thinking_disabled():
    template = (
        "{% if enable_thinking is defined and enable_thinking is false %}"
        "<think>\n\n</think>\n\n{% endif %}")

    assert gguf._template_without_thinking(template) == (
        "{% if true %}<think>\n\n</think>\n\n{% endif %}")
    assert gguf._template_without_thinking("{{ messages }}") is None


def test_default_gguf_cache_dir_stays_out_of_the_home_volume():
    assert Config(_env_file=".env.example").gguf_cache_dir == "./models"


def test_direct_gguf_download_accepts_a_dash_before_the_quantization(tmp_path, monkeypatch):
    downloaded = {}

    class HfApi:
        def list_repo_files(self, **kwargs):
            return ["Gemma4-12B-QAT-Uncensored-Balanced-Q4_K_M.gguf"]

    def download(**kwargs):
        downloaded.update(kwargs)
        return tmp_path / kwargs["filename"]

    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(
        HfApi=HfApi, hf_hub_download=download))
    cfg = Config(_env_file=None, gguf_cache_dir=str(tmp_path))

    path = gguf.resolve_model_file("gguf/HauhauCS/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced", cfg)

    assert path == tmp_path / "Gemma4-12B-QAT-Uncensored-Balanced-Q4_K_M.gguf"
    assert downloaded["filename"] == "Gemma4-12B-QAT-Uncensored-Balanced-Q4_K_M.gguf"


def test_direct_gguf_enables_flash_attention_by_default(monkeypatch, tmp_path):
    kwargs = {}

    class Llama:
        metadata = {}

        def __init__(self, **received):
            kwargs.update(received)

    monkeypatch.setitem(sys.modules, "llama_cpp", SimpleNamespace(Llama=Llama))

    gguf._LoadedModel(tmp_path / "model.gguf", context_length=32768, gpu_layers=0,
                      threads=0, flash_attn=True)

    assert kwargs["flash_attn"] is True


def test_gguf_preflight_tries_full_gpu_then_cpu_after_a_partial_gpu_failure():
    requested = gguf._Options(context_length=32768, gpu_layers=20, threads=0,
                              flash_attn=True)

    candidates = gguf._candidate_options(requested)

    assert candidates[:3] == [
        requested,
        replace(requested, gpu_layers=-1),
        replace(requested, gpu_layers=0),
    ]
    assert replace(requested, gpu_layers=-1, context_length=8192) in candidates


def test_gguf_preflight_isolates_a_native_abort_and_chooses_full_gpu(tmp_path, monkeypatch):
    path = _gguf_file(tmp_path / "model.gguf")
    requested = gguf._Options(context_length=32768, gpu_layers=20, threads=0,
                              flash_attn=True)
    calls = []

    def probe(_path, options, *, timeout):
        calls.append(options)
        if options.gpu_layers == 20:
            return False, "SIGABRT: GGML_SCHED_MAX_SPLIT_INPUTS failed"
        return True, ""

    monkeypatch.setattr(gguf, "_probe_options", probe)

    selected = gguf._probe_model(path, requested)

    assert selected == replace(requested, gpu_layers=-1)
    assert calls == [requested, selected]


def test_gguf_preflight_failure_is_an_exception_not_a_daemon_abort(tmp_path, monkeypatch):
    path = _gguf_file(tmp_path / "model.gguf")
    monkeypatch.setattr(gguf, "_probe_options",
                        lambda *_args, **_kwargs: (False, "SIGABRT"))

    with pytest.raises(RuntimeError, match="failed every llama.cpp preflight"):
        gguf._probe_model(path, gguf._Options(32768, 20, 0, True))


def test_gguf_cache_entries_are_checked_before_llama_cpp_loads_them(tmp_path):
    bad = tmp_path / "not-a-model.gguf"
    bad.write_text("not gguf", encoding="utf-8")

    with pytest.raises(RuntimeError, match="not a GGUF file"):
        gguf._validate_gguf_file(bad)


def test_gguf_probe_runs_out_of_process_without_a_native_backtrace(monkeypatch, tmp_path):
    received = {}

    def run(*args, **kwargs):
        received.update(kwargs)
        return SimpleNamespace(returncode=-6, stdout="GGML_ASSERT failed\n")

    monkeypatch.setattr(gguf.subprocess, "run", run)
    options = gguf._Options(32768, 20, 4, True)

    ok, detail = gguf._probe_options(tmp_path / "model.gguf", options)

    assert not ok
    assert received["env"]["GGML_NO_BACKTRACE"] == "1"
    assert received["timeout"] == gguf._PROBE_TIMEOUT_SECONDS
    assert received["check"] is False
    assert "SIGABRT" in detail


def test_get_model_loads_the_preflighted_options(monkeypatch, tmp_path):
    path = _gguf_file(tmp_path / "model.gguf")
    kwargs = {}

    class Llama:
        metadata = {}

        def __init__(self, **received):
            kwargs.update(received)

    monkeypatch.setitem(sys.modules, "llama_cpp", SimpleNamespace(Llama=Llama))
    monkeypatch.setattr(gguf, "_models", {})
    monkeypatch.setattr(gguf, "resolve_model_file", lambda *_args: path)
    monkeypatch.setattr(gguf, "_probe_model",
                        lambda _path, requested: replace(requested, gpu_layers=-1))
    cfg = Config(_env_file=None, chat_model="gguf/example/model-GGUF",
                 gguf_context_length=32768, gguf_n_gpu_layers=20)

    loaded = gguf.get_model(cfg.chat_model, cfg)

    assert loaded.context_length == 32768
    assert kwargs["n_gpu_layers"] == -1


async def test_direct_gguf_meter_learns_the_preflighted_context_limit(monkeypatch):
    class Meter:
        def __init__(self):
            self.prompts = 0
            self.limit = None

        def note_prompt(self, messages):
            self.prompts += 1

        def set_limit(self, tokens, source):
            self.limit = (tokens, source)

    loaded = _Loaded()
    loaded.context_length = 4096
    monkeypatch.setattr(gguf, "get_model", lambda *args: loaded)
    cfg = Config(_env_file=None, chat_model="gguf/example/model-GGUF",
                 context_length=32768)
    meter = Meter()
    chat = gguf.GGUFChatModel(cfg.chat_model, cfg, temperature=0.8, meter=meter)

    assert chat.context_limit == 32768
    assert [part async for part in chat.stream([{"role": "user", "content": "hi"}])]
    assert chat.context_limit == 4096
    assert meter.limit == (4096, "direct gguf")


def test_direct_gguf_reports_its_effective_context_limit():
    cfg = Config(_env_file=None, chat_model="gguf/example/model-GGUF",
                 context_length=32768, gguf_context_length=8192)

    assert gguf.GGUFChatModel(cfg.chat_model, cfg, temperature=0.8).context_limit == 8192


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
