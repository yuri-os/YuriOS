"""Direct GGUF chat and utility providers.

The configured ``gguf/<repo>`` name doubles as the Hugging Face repository. We
resolve its Q4_K_M (or configured) GGUF on first use, then keep one llama.cpp
context per file/options tuple for both chat and utility work.
"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import AsyncIterator


_models: dict[tuple[Path, int, int, int], "_LoadedModel"] = {}
_models_lock = threading.Lock()


def _repo_for(model: str, cfg) -> str:
    if cfg.gguf_repo:
        return cfg.gguf_repo
    if model.startswith(("gguf/", "lm_studio/")):
        return model.split("/", 1)[1]
    raise ValueError(f"GGUF needs a gguf/<Hugging Face repo> model, got {model!r}")


def resolve_model_file(model: str, cfg) -> Path:
    """Download the configured repo's matching quant once into the HF cache."""
    try:
        from huggingface_hub import HfApi, hf_hub_download
    except ImportError as e:
        raise RuntimeError("GGUF fallback needs `pip install -e '.[llm]'`") from e

    repo = _repo_for(model, cfg)
    suffix = f".{cfg.gguf_quant}.gguf".lower()
    files = HfApi().list_repo_files(repo_id=repo, repo_type="model")
    matches = [name for name in files if name.lower().endswith(suffix)]
    if len(matches) != 1:
        raise RuntimeError(
            f"{repo} has no unambiguous {cfg.gguf_quant} GGUF file; set GGUF_QUANT "
            f"to one of: {', '.join(Path(name).stem.rsplit('.', 1)[-1] for name in files if name.lower().endswith('.gguf'))}")
    path = hf_hub_download(repo_id=repo, filename=matches[0], repo_type="model",
                           cache_dir=cfg.gguf_cache_dir or None)
    return Path(path).resolve()


class _LoadedModel:
    def __init__(self, path: Path, *, context_length: int, gpu_layers: int, threads: int):
        try:
            from llama_cpp import Llama
        except ImportError as e:
            raise RuntimeError("GGUF fallback needs `pip install -e '.[llm]'`") from e
        kwargs = {"model_path": str(path), "n_ctx": context_length,
                  "n_gpu_layers": gpu_layers, "verbose": False}
        if threads > 0:
            kwargs["n_threads"] = threads
        self.llama = Llama(**kwargs)
        self.lock = threading.Lock()


def _context_length(cfg) -> int:
    return cfg.gguf_context_length or cfg.context_length or 8192


def get_model(model: str, cfg) -> _LoadedModel:
    path = resolve_model_file(model, cfg)
    key = (path, _context_length(cfg), cfg.gguf_n_gpu_layers, cfg.gguf_n_threads)
    with _models_lock:
        loaded = _models.get(key)
        if loaded is None:
            loaded = _LoadedModel(path, context_length=key[1], gpu_layers=key[2], threads=key[3])
            _models[key] = loaded
        return loaded


def _without_thinking(messages: list[dict]) -> list[dict]:
    if messages and messages[0].get("role") == "system":
        return [{**messages[0], "content": messages[0]["content"] + "\n/no_think"},
                *messages[1:]]
    return messages


def _next(iterator):
    try:
        return True, next(iterator)
    except StopIteration:
        return False, None


def _stream_content(chunk: dict) -> str:
    choices = chunk.get("choices") or []
    if not choices:
        return ""
    delta = choices[0].get("delta") or {}
    return delta.get("content") or ""


class GGUFChatModel:
    def __init__(self, model: str, cfg, *, temperature: float, meter=None):
        self.model = model
        self._cfg = cfg
        self.temperature = temperature
        self.meter = meter
        self._loaded: _LoadedModel | None = None
        self.thinking = cfg.chat_thinking

    def _load(self) -> _LoadedModel:
        if self._loaded is None:
            self._loaded = get_model(self.model, self._cfg)
        return self._loaded

    async def stream(self, messages: list[dict], **params) -> AsyncIterator[str]:
        if not self.thinking:
            messages = _without_thinking(messages)
        if self.meter is not None:
            self.meter.note_prompt(messages)
        loaded = await asyncio.to_thread(self._load)
        await asyncio.to_thread(loaded.lock.acquire)
        try:
            response = await asyncio.to_thread(
                loaded.llama.create_chat_completion,
                messages=messages,
                temperature=params.get("temperature", self.temperature),
                max_tokens=params.get("max_tokens", 1024), stream=True)
            for_more, chunk = await asyncio.to_thread(_next, response)
            while for_more:
                text = _stream_content(chunk)
                if text:
                    yield text
                for_more, chunk = await asyncio.to_thread(_next, response)
        finally:
            loaded.lock.release()


class GGUFUtilityModel:
    def __init__(self, model: str, cfg, *, max_tokens: int, thinking: bool):
        self.model = model
        self._cfg = cfg
        self.max_tokens = max_tokens
        self.thinking = thinking
        self._loaded: _LoadedModel | None = None

    def _load(self) -> _LoadedModel:
        if self._loaded is None:
            self._loaded = get_model(self.model, self._cfg)
        return self._loaded

    async def complete(self, messages: list[dict], **params) -> str:
        text, _meta = await self.complete_detailed(messages, **params)
        return text

    async def complete_detailed(self, messages: list[dict], **params) -> tuple[str, dict]:
        if not self.thinking:
            messages = _without_thinking(messages)
        loaded = await asyncio.to_thread(self._load)

        def run():
            with loaded.lock:
                return loaded.llama.create_chat_completion(
                    messages=messages, temperature=params.get("temperature", 0.2),
                    max_tokens=params.get("max_tokens", self.max_tokens), stream=False)

        response = await asyncio.to_thread(run)
        choices = response.get("choices") or []
        message = choices[0].get("message") if choices else {}
        usage = response.get("usage") or {}
        return ((message or {}).get("content") or "", {
            "finish_reason": (choices[0].get("finish_reason") if choices else "") or "",
            "prompt_tokens": usage.get("prompt_tokens", 0) or 0,
            "completion_tokens": usage.get("completion_tokens", 0) or 0,
            "total_tokens": usage.get("total_tokens", 0) or 0,
            "reasoning_tokens": 0,
        })
