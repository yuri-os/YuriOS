"""Direct GGUF chat and utility providers.

The configured ``gguf/<repo>`` name doubles as the Hugging Face repository. We
resolve its Q4_K_M (or configured) GGUF on first use, preflight the llama.cpp
options out of process so a native assertion cannot kill the daemon, then keep
one context per file/options tuple for both chat and utility work.

Because those contexts live in THIS process, a local selfie render that needs
their VRAM can't evict them over HTTP the way it does LM Studio's models —
`park()` / `unpark()` below close and reload them in place (world/vram.py's
in-process half of the loan).
"""
from __future__ import annotations

import asyncio
import gc
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
import weakref
from dataclasses import dataclass, replace
from pathlib import Path
from typing import AsyncIterator


log = logging.getLogger(__name__)
_models: dict[tuple[Path, int, int, int, bool], "_LoadedModel"] = {}
_models_lock = threading.Lock()
_PROBE_TIMEOUT_SECONDS = 600
_SAFE_CONTEXT_LENGTH = 8192

# --- parking the in-process brain for a local render (world/vram.py's mirror) ----
#
# A resident llama.cpp context holds gigabytes of VRAM that a local selfie
# pipeline needs for its duration. LM Studio models are evicted over HTTP; the
# direct route's contexts live in THIS process, so parking them means closing
# the contexts here. `_load_gate` is the door: cleared for the render's whole
# window, so a mind tick or a turn that slips past the ParkGate queues its load
# instead of putting her brain back onto the card the render is filling.
_providers: weakref.WeakSet = weakref.WeakSet()      # live chat/utility providers
_registry: dict[tuple, tuple[str, object]] = {}      # key → (model, cfg), for reload
_probed: dict[tuple, "_Options"] = {}                # key → preflight that passed
_load_gate = threading.Event()                       # clear = parked; loads wait
_load_gate.set()
_THINKING_CONDITION = re.compile(
    r"enable_thinking\s+is\s+defined\s+and\s+enable_thinking\s+is\s+false")


@dataclass(frozen=True)
class _Options:
    context_length: int
    gpu_layers: int
    threads: int
    flash_attn: bool


def _candidate_options(requested: _Options) -> list[_Options]:
    """Order load attempts by speed, while keeping every step crash-isolated.

    llama.cpp can fail a partial CPU/GPU graph split with a native ``abort()`` —
    notably Gemma 4 edge models before the dynamic split-input fix. Never learn
    that in the daemon process. Try what the user asked for first, then a full
    offload, then CPU; only after those device choices fail relax Flash Attention
    and finally the context window.
    """
    candidates = [requested]
    if requested.gpu_layers > 0:
        candidates.extend((replace(requested, gpu_layers=-1),
                           replace(requested, gpu_layers=0)))
    elif requested.gpu_layers < 0:
        candidates.append(replace(requested, gpu_layers=0))
    if requested.flash_attn:
        candidates.extend(replace(candidate, flash_attn=False)
                          for candidate in list(candidates))
    if requested.context_length > _SAFE_CONTEXT_LENGTH:
        device_layers = (-1, 0) if requested.gpu_layers else (0,)
        flash_modes = dict.fromkeys((requested.flash_attn, False))
        candidates.extend(
            replace(requested, context_length=_SAFE_CONTEXT_LENGTH,
                    gpu_layers=gpu_layers, flash_attn=flash_attn)
            for gpu_layers in device_layers for flash_attn in flash_modes)
    return list(dict.fromkeys(candidates))


def _describe_options(options: _Options) -> str:
    device = ("CPU" if options.gpu_layers == 0 else "full GPU"
              if options.gpu_layers < 0 else f"{options.gpu_layers} GPU layers")
    flash = "Flash Attention on" if options.flash_attn else "Flash Attention off"
    return f"{options.context_length} ctx, {device}, {flash}"


def _validate_gguf_file(path: Path) -> None:
    """Reject a bad cache entry before llama.cpp gets a chance to abort on it."""
    try:
        with path.open("rb") as file:
            header = file.read(24)
        size = path.stat().st_size
    except OSError as exc:
        raise RuntimeError(f"GGUF is not readable: {path}: {exc}") from exc
    if len(header) < 24 or not header.startswith(b"GGUF"):
        raise RuntimeError(f"GGUF cache entry is not a GGUF file: {path}")
    if size < 1024 * 1024:
        raise RuntimeError(f"GGUF cache entry is truncated: {path} ({size} bytes)")


def _load_llama(path: Path, options: _Options):
    # A CUDA llama-cpp-python wheel dynamically loads the CUDA runtime bundled
    # with torch even for a CPU context. Import it first when it is available.
    try:
        import torch  # noqa: F401
    except Exception:
        pass
    try:
        from llama_cpp import Llama
    except ImportError as e:
        raise RuntimeError("GGUF fallback needs `pip install -e '.[llm]'`") from e
    kwargs = {"model_path": str(path), "n_ctx": options.context_length,
              "n_gpu_layers": options.gpu_layers,
              "flash_attn": options.flash_attn, "verbose": False}
    if options.threads > 0:
        kwargs["n_threads"] = options.threads
    return Llama(**kwargs)


def _process_detail(output: str | bytes | None, returncode: int) -> str:
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    lines = [line.strip() for line in (output or "").splitlines() if line.strip()]
    detail = lines[-1][:300] if lines else "no llama.cpp diagnostic"
    if returncode < 0:
        try:
            signame = signal.Signals(-returncode).name
        except ValueError:
            signame = f"signal {-returncode}"
        return f"{signame}: {detail}"
    return f"exit {returncode}: {detail}"


def _probe_options(path: Path, options: _Options,
                   timeout: float = _PROBE_TIMEOUT_SECONDS) -> tuple[bool, str]:
    """Load one option tuple in a sacrificial process.

    A native GGML assertion is not catchable in-process. The child proves that
    this exact file/context/offload combination can create its llama context;
    only a passing tuple is then loaded by the daemon itself.
    """
    command = [sys.executable, "-m", "yurios.app.providers.gguf", "--probe",
               str(path), str(options.context_length), str(options.gpu_layers),
               str(options.threads), "1" if options.flash_attn else "0"]
    env = {**os.environ, "GGML_NO_BACKTRACE": "1"}
    try:
        completed = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", timeout=timeout, env=env,
            check=False)
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout.decode("utf-8", errors="replace") if isinstance(
            exc.stdout, bytes) else exc.stdout
        lines = [line.strip() for line in (output or "").splitlines() if line.strip()]
        detail = f" — {lines[-1][:300]}" if lines else ""
        return False, f"timed out after {int(timeout)} seconds{detail}"
    if completed.returncode == 0:
        return True, ""
    return False, _process_detail(completed.stdout, completed.returncode)


def _probe_model(path: Path, requested: _Options) -> _Options:
    _validate_gguf_file(path)
    failures = []
    deadline = time.monotonic() + _PROBE_TIMEOUT_SECONDS
    for candidate in _candidate_options(requested):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            failures.append(f"preflight budget exhausted after {_PROBE_TIMEOUT_SECONDS} seconds")
            break
        ok, detail = _probe_options(path, candidate, timeout=remaining)
        if ok:
            if candidate != requested:
                log.warning("GGUF %s did not load with %s; using %s instead",
                            path.name, _describe_options(requested),
                            _describe_options(candidate))
            return candidate
        failures.append(f"{_describe_options(candidate)} — {detail}")
    raise RuntimeError("GGUF failed every llama.cpp preflight: " + "; ".join(failures))


def _probe_main(argv: list[str]) -> int:
    if len(argv) != 6 or argv[0] != "--probe":
        print("usage: python -m yurios.app.providers.gguf --probe "
              "<file> <ctx> <gpu-layers> <threads> <flash-attn>", file=sys.stderr)
        return 2
    options = _Options(context_length=int(argv[2]), gpu_layers=int(argv[3]),
                       threads=int(argv[4]), flash_attn=argv[5] == "1")
    _load_llama(Path(argv[1]), options)
    return 0


def _template_without_thinking(template: str) -> str | None:
    """Make a Qwen3 template prefill its empty no-think block.

    Qwen3's own template already knows how to emit ``<think>\n\n</think>``
    before an answer. llama-cpp-python 0.3.34 renders that template but does
    not expose its ``enable_thinking`` argument, so force just that condition.
    Other model templates stay untouched.
    """
    rewritten, count = _THINKING_CONDITION.subn("true", template, count=1)
    return rewritten if count else None


def _no_think_handler(llama):
    template = _template_without_thinking(
        str(getattr(llama, "metadata", {}).get("tokenizer.chat_template", "")))
    if template is None:
        return None
    from llama_cpp.llama_chat_format import Jinja2ChatFormatter

    eos = llama.token_eos()
    bos = llama.token_bos()

    def token_text(token: int) -> str:
        return llama.detokenize([token], special=True).decode("utf-8") if token >= 0 else ""

    return Jinja2ChatFormatter(
        template=template,
        eos_token=token_text(eos),
        bos_token=token_text(bos),
        stop_token_ids=[eos],
    ).to_chat_handler()


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
    # GGUF publishers use dots, dashes, or underscores before quant labels.
    quant_file = re.compile(rf"(?:^|[._-]){re.escape(cfg.gguf_quant)}\.gguf$", re.IGNORECASE)
    files = HfApi().list_repo_files(repo_id=repo, repo_type="model")
    matches = [name for name in files if quant_file.search(name)]
    if len(matches) != 1:
        raise RuntimeError(
            f"{repo} has no unambiguous {cfg.gguf_quant} GGUF file; set GGUF_QUANT "
            f"to one of: {', '.join(Path(name).stem.rsplit('.', 1)[-1] for name in files if name.lower().endswith('.gguf'))}")
    path = hf_hub_download(repo_id=repo, filename=matches[0], repo_type="model",
                           cache_dir=cfg.gguf_cache_dir or None)
    return Path(path).resolve()


class _LoadedModel:
    def __init__(self, path: Path, *, context_length: int, gpu_layers: int, threads: int,
                 flash_attn: bool):
        self.options = _Options(context_length=context_length, gpu_layers=gpu_layers,
                                threads=threads, flash_attn=flash_attn)
        self.context_length = context_length
        self.llama = _load_llama(path, self.options)
        self.no_think_handler = _no_think_handler(self.llama)
        self.lock = threading.Lock()
        self.closed = False             # parked out from under a stale holder


def _context_length(cfg) -> int:
    return cfg.gguf_context_length or cfg.context_length or 8192


def _key_for(path: Path, cfg) -> tuple:
    return (path, _context_length(cfg), cfg.gguf_n_gpu_layers, cfg.gguf_n_threads,
            cfg.gguf_flash_attn)


def get_model(model: str, cfg) -> _LoadedModel:
    # Outside the cache lock: a parked render holds this gate for its whole
    # window, and a load that queues here must not freeze every other reader.
    _load_gate.wait()
    path = resolve_model_file(model, cfg)
    key = _key_for(path, cfg)
    with _models_lock:
        _registry[key] = (model, cfg)
        loaded = _models.get(key)
        if loaded is None:
            requested = _Options(context_length=key[1], gpu_layers=key[2],
                                 threads=key[3], flash_attn=key[4])
            # A reload after a park skips the sacrificial-process preflight:
            # this exact tuple already proved it loads, earlier this run.
            options = _probed.get(key)
            if options is None:
                options = _probe_model(path, requested)
                _probed[key] = options
            loaded = _LoadedModel(path, context_length=options.context_length,
                                  gpu_layers=options.gpu_layers, threads=options.threads,
                                  flash_attn=options.flash_attn)
            _models[key] = loaded
        return loaded


# ---- parking: lend the LLM's VRAM to a local render, then take it back -------

def resident_count() -> int:
    """How many llama contexts this process is holding right now."""
    with _models_lock:
        return len(_models)


def park() -> list[tuple[str, object]]:
    """Close every resident llama context so the card belongs to the camera.

    The in-process half of world/vram.py's loan: LM Studio models are evicted
    over HTTP, these are closed here. The load gate drops FIRST and stays down
    for the whole render window, so no completion — a turn that slipped the
    ParkGate, a mind tick — can start loading her brain back onto the card the
    render is filling. Each context's own lock waits out a generation already
    in flight before its weights are freed.

    Returns the (model, cfg) registrations `unpark()` reloads. Never raises:
    a context that will not close cleanly is dropped all the same, and the
    render keeps its offload fallback.
    """
    _load_gate.clear()
    with _models_lock:
        snapshot = list(_models.items())
        _models.clear()
    for provider in list(_providers):
        provider._loaded = None
    handles = []
    for key, loaded in snapshot:
        try:
            with loaded.lock:            # wait out an in-flight completion
                loaded.closed = True     # stale holders reload instead of crashing
                close = getattr(loaded.llama, "close", None)
                if callable(close):
                    close()
                loaded.llama = None
        except Exception as e:
            log.warning("GGUF park: %s would not close cleanly (%s)", key[0].name, e)
        handle = _registry.get(key)
        if handle is not None:
            handles.append(handle)
    gc.collect()
    try:                                 # release torch's cached blocks too;
        import torch                     # never required — best effort only
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass
    if handles:
        log.info("GGUF park: closed %d context(s) — the card belongs to the "
                 "camera for one render", len(handles))
    return handles


def unpark(handles: list[tuple[str, object]]) -> None:
    """Bring her brain back after the render: reopen the door, reload what
    `park()` closed. Best-effort like the LM Studio mirror — a context that
    will not reload drops its cached preflight, so the next turn re-probes and
    can fall back to fewer GPU layers; the gate opens either way, so a failed
    restore never leaves her mute."""
    _load_gate.set()
    for model, cfg in handles:
        try:
            get_model(model, cfg)
            log.info("GGUF unpark: %s is resident again", model)
        except Exception as e:
            log.warning("GGUF unpark: %s would not reload (%s) — the next turn "
                        "retries with a fresh preflight", model, e)
            try:
                _probed.pop(_key_for(resolve_model_file(model, cfg), cfg), None)
            except Exception:
                pass


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


async def _acquire_current(provider) -> _LoadedModel:
    """The resident context with its lock held, riding out a mid-park swap.

    The gap between `_load()` and `lock.acquire()` is exactly where a park can
    close the context under us: take the lock, and if the model died on the way
    in, let it go and load again — the reload queues at the load gate until the
    render gives her brain back, then returns a live context.
    """
    while True:
        loaded = await asyncio.to_thread(provider._load)
        await asyncio.to_thread(loaded.lock.acquire)
        if not loaded.closed:
            return loaded
        loaded.lock.release()


class GGUFChatModel:
    def __init__(self, model: str, cfg, *, temperature: float, meter=None):
        self.model = model
        self._cfg = cfg
        self.temperature = temperature
        self.meter = meter
        self._loaded: _LoadedModel | None = None
        self.thinking = cfg.chat_thinking
        _providers.add(self)

    @property
    def context_limit(self) -> int:
        """The requested window before load; the preflighted window after it."""
        return self._loaded.context_length if self._loaded else _context_length(self._cfg)

    def _load(self) -> _LoadedModel:
        if self._loaded is None or self._loaded.closed:
            self._loaded = get_model(self.model, self._cfg)
        return self._loaded

    async def stream(self, messages: list[dict], **params) -> AsyncIterator[str]:
        if not self.thinking:
            messages = _without_thinking(messages)
        if self.meter is not None:
            self.meter.note_prompt(messages)
        loaded = await _acquire_current(self)
        set_limit = getattr(self.meter, "set_limit", None)
        if callable(set_limit):
            set_limit(loaded.context_length, "direct gguf")
        try:
            args = {"messages": messages,
                    "temperature": params.get("temperature", self.temperature),
                    "max_tokens": params.get("max_tokens", 1024), "stream": True}
            handler = getattr(loaded, "no_think_handler", None)
            if not self.thinking and handler is not None:
                response = await asyncio.to_thread(handler,
                                                   llama=loaded.llama, **args)
            else:
                response = await asyncio.to_thread(
                    loaded.llama.create_chat_completion, **args)
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
        _providers.add(self)

    def _load(self) -> _LoadedModel:
        if self._loaded is None or self._loaded.closed:
            self._loaded = get_model(self.model, self._cfg)
        return self._loaded

    async def complete(self, messages: list[dict], **params) -> str:
        text, _meta = await self.complete_detailed(messages, **params)
        return text

    async def complete_detailed(self, messages: list[dict], **params) -> tuple[str, dict]:
        if not self.thinking:
            messages = _without_thinking(messages)
        loaded = await _acquire_current(self)

        def run():
            return loaded.llama.create_chat_completion(
                messages=messages, temperature=params.get("temperature", 0.2),
                max_tokens=params.get("max_tokens", self.max_tokens), stream=False)

        try:
            response = await asyncio.to_thread(run)
        finally:
            loaded.lock.release()
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


if __name__ == "__main__":
    raise SystemExit(_probe_main(sys.argv[1:]))
