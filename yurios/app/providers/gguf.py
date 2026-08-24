"""Direct GGUF chat and utility providers.

The configured ``gguf/<repo>`` name doubles as the Hugging Face repository. We
resolve its Q4_K_M (or configured) GGUF on first use, preflight the llama.cpp
options out of process so a native assertion cannot kill the daemon, then keep
one context per file/options tuple for both chat and utility work. Both facts —
which file a repo resolved to, and which option tuples already proved they load
— are recorded next to the weights, so a daemon restart works offline and never
re-runs a preflight that already passed.

Because those contexts live in THIS process, a local selfie render that needs
their VRAM can't evict them over HTTP the way it does LM Studio's models —
`park()` / `unpark()` below close and reload them in place (world/vram.py's
in-process half of the loan).
"""
from __future__ import annotations

import asyncio
import gc
import json
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

from yurios.app.providers.admission import inference_admission


log = logging.getLogger(__name__)
_models: dict[tuple[Path, int, int, int, bool], "_LoadedModel"] = {}
_models_lock = threading.Lock()
# Signalled when a park ends. Every load decision is made while holding this,
# which is what makes "is the card lent out right now?" answerable without a race.
_models_cv = threading.Condition(_models_lock)
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
#
# The door alone is not enough, and the failure log says why. `_load_gate` used
# to be checked once, at the top of `get_model`, BEFORE the slow part — HF
# resolution, a network round trip or two. A mind tick that passed the door a
# quarter-second before `park()` shut it went on to build a fresh 5.7 GiB
# context straight into the render's window: "park: lending the GPU (8.9 GiB
# free)" and, eleven seconds later, "diffusers: 8.9 GiB VRAM free" — the loan
# handed over nothing, and the render died with 132 MiB left. So the *decision*
# to load is now made under `_models_cv`, where `_parked` cannot change
# underneath it, and the gate above is only an optimisation that keeps a queued
# caller from doing its network work twice.
_providers: weakref.WeakSet = weakref.WeakSet()      # live chat/utility providers
_registry: dict[tuple, tuple[str, object]] = {}      # key → (model, cfg), for reload
_probed: dict[tuple, "_Options"] = {}                # key → preflight that passed
_load_gate = threading.Event()                       # clear = parked; loads wait
_load_gate.set()
_parked = False                                      # authoritative; _models_cv guards it
# How long a load will hold for a render before going through anyway. A park
# window is evict + render + re-pin; past this something is wedged, and a late
# reply beats a mute companion (world/vram.py's ParkGate makes the same trade).
_PARK_WAIT_SECONDS = 180.0
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


# --- the sidecar: what a previous run already proved --------------------------
#
# A cold load used to pay two network round trips (list_repo_files, then the
# hf_hub_download etag HEAD) and a sacrificial-process preflight on EVERY
# daemon start, though neither answer ever changes for a cached file. Both are
# recorded in a small JSON sidecar next to the weights: the repo → file
# resolution, so later loads work fully offline, and the option tuple the
# preflight actually settled on — fallbacks included — so it runs once per
# file/options, ever. State for weights outside the cache dir (tests, ad-hoc
# paths) stays in memory: the record belongs with the weights it describes.
_STORE_NAME = "yurios-gguf.json"


def _store_base(cfg) -> Path:
    if cfg.gguf_cache_dir:
        return Path(cfg.gguf_cache_dir).expanduser()
    return Path.home() / ".cache" / "huggingface" / "hub"


def _store_path_for(cfg, path: Path | None = None) -> Path | None:
    """The sidecar file, or None when `path` lives outside the cache dir."""
    base = _store_base(cfg)
    if path is not None:
        try:
            path.resolve().relative_to(base.resolve())
        except (OSError, ValueError):
            return None
    return base / _STORE_NAME


def _read_store(store: Path | None) -> dict:
    if store is None:
        return {}
    try:
        data = json.loads(store.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def _write_store(store: Path, data: dict) -> None:
    try:
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    except OSError as e:
        log.warning("could not record GGUF state at %s (%s)", store, e)


def _probed_key(key: tuple) -> str:
    return json.dumps([str(key[0]), *key[1:]])


def _preflight_lookup(store: Path | None, key: tuple) -> "_Options | None":
    """The options a previous run's preflight settled on for this tuple."""
    record = _read_store(store).get("probed", {}).get(_probed_key(key))
    if not isinstance(record, list) or len(record) != 4:
        return None
    try:
        return _Options(context_length=int(record[0]), gpu_layers=int(record[1]),
                        threads=int(record[2]), flash_attn=bool(record[3]))
    except (TypeError, ValueError):
        return None


def _preflight_mark(store: Path | None, key: tuple, options: _Options) -> None:
    if store is None:
        return
    data = _read_store(store)
    data.setdefault("probed", {})[_probed_key(key)] = [
        options.context_length, options.gpu_layers, options.threads,
        options.flash_attn]
    _write_store(store, data)


def _preflight_forget(store: Path | None, key: tuple) -> None:
    """A reload with the recorded options FAILED — the proof is stale, so the
    next load re-probes and can fall back to gentler options again."""
    if store is None:
        return
    data = _read_store(store)
    if data.get("probed", {}).pop(_probed_key(key), None) is not None:
        _write_store(store, data)


def resolve_model_file(model: str, cfg) -> Path:
    """The configured repo's matching quant — resolved once, then offline.

    The first resolution asks Hugging Face which file matches GGUF_QUANT and
    downloads it; the answer is recorded in the cache sidecar, and every later
    load — restart included — opens the recorded file directly, no network.
    """
    repo = _repo_for(model, cfg)
    store = _store_path_for(cfg)
    record_key = f"{repo} :: {cfg.gguf_quant}"
    recorded = _read_store(store).get("resolved", {}).get(record_key)
    if recorded:
        cached = Path(recorded)
        if cached.is_file():
            try:
                _validate_gguf_file(cached)
                return cached.resolve()
            except RuntimeError as e:
                log.warning("recorded GGUF %s failed validation (%s) — resolving "
                            "again from Hugging Face", cached.name, e)
    try:
        from huggingface_hub import HfApi, hf_hub_download
    except ImportError as e:
        raise RuntimeError("GGUF fallback needs `pip install -e '.[llm]'`") from e

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
    path = Path(path).resolve()
    if store is not None:
        data = _read_store(store)
        data.setdefault("resolved", {})[record_key] = str(path)
        _write_store(store, data)
    return path


def preflight_pending(model: str, cfg) -> bool:
    """Would the next load of this model run the sacrificial-process preflight?

    Backs the `yurios start` heads-up: True until some run has recorded a
    passing preflight for the resolved file and the configured options."""
    try:
        store = _store_path_for(cfg)
        data = _read_store(store)
        recorded = data.get("resolved", {}).get(
            f"{_repo_for(model, cfg)} :: {cfg.gguf_quant}")
        if not recorded:
            return True
        return _preflight_lookup(store, _key_for(Path(recorded), cfg)) is None
    except Exception:
        return False


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
    deadline = time.monotonic() + _PARK_WAIT_SECONDS
    # Outside the cache lock: a parked render holds this gate for its whole
    # window, and a load that queues here must not freeze every other reader.
    # Cheap and advisory — the binding check is `_parked`, under the lock below.
    _load_gate.wait(max(0.0, deadline - time.monotonic()))
    path = resolve_model_file(model, cfg)
    key = _key_for(path, cfg)
    with _models_cv:
        _registry[key] = (model, cfg)
        loaded = _models.get(key)
        if loaded is not None:
            return loaded                      # already resident: nothing to load
        # The park may have started while we resolved the file above — that gap
        # is a network round trip wide, and it is where the OOM lived. Building
        # the context now would put her brain back onto the card the render is
        # filling, so wait for the render to give it back, then look again.
        if _parked:
            log.info("GGUF: a load is waiting for the render to give the card "
                     "back (%s)", model)
            while _parked and time.monotonic() < deadline:
                _models_cv.wait(max(0.0, deadline - time.monotonic()))
            if _parked:
                log.warning("GGUF: still parked after %.0fs — loading %s anyway "
                            "(it may take VRAM the render is using)",
                            _PARK_WAIT_SECONDS, model)
            loaded = _models.get(key)
            if loaded is not None:
                return loaded                  # unpark() reloaded it while we waited
        # From here to the `return` the lock is never dropped, and `park()` needs
        # it to declare itself — so no park can begin underneath this load. One
        # already waiting for the lock gets a live context to close, which is the
        # outcome the loan wants.
        requested = _Options(context_length=key[1], gpu_layers=key[2],
                             threads=key[3], flash_attn=key[4])
        # A reload after a park skips the sacrificial-process preflight:
        # this exact tuple already proved it loads, earlier this run — or in
        # any earlier run, per the sidecar record next to the weights. The
        # recorded options are the ones the probe SETTLED on, fallbacks and
        # all, so a restart goes straight to the configuration that worked.
        options = _probed.get(key)
        if options is None:
            store = _store_path_for(cfg, path)
            options = _preflight_lookup(store, key)
            if options is not None:
                _probed[key] = options
                log.info("GGUF %s: preflight already proven (%s) — loading "
                         "directly", path.name, _describe_options(options))
            else:
                log.warning(
                    "GGUF %s: one-time llama.cpp preflight starting — the model "
                    "loads in a sacrificial process first, so this can take "
                    "minutes. It runs once per model and options, then never "
                    "again.", path.name)
                options = _probe_model(path, requested)
                _probed[key] = options
                _preflight_mark(store, key, options)
                if options != requested:
                    log.warning("GGUF %s: preflight settled on %s — recorded, "
                                "and future starts use it directly",
                                path.name, _describe_options(options))
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
    global _parked
    _load_gate.clear()
    with _models_cv:
        # Declared under the lock, so a load that is mid-decision either sees it
        # and waits, or holds the lock and finishes — never both. Taking the
        # snapshot in the same critical section is what closes the old race: a
        # context built between "the gate shut" and "the snapshot" no longer
        # exists to be missed.
        _parked = True
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
    global _parked
    with _models_cv:
        _parked = False
        _models_cv.notify_all()      # release the loads queued in `get_model`
    _load_gate.set()
    for model, cfg in handles:
        try:
            get_model(model, cfg)
            log.info("GGUF unpark: %s is resident again", model)
        except Exception as e:
            log.warning("GGUF unpark: %s would not reload (%s) — the next turn "
                        "retries with a fresh preflight", model, e)
            try:
                path = resolve_model_file(model, cfg)
                key = _key_for(path, cfg)
                _probed.pop(key, None)
                _preflight_forget(_store_path_for(cfg, path), key)
            except Exception:
                pass


def _without_thinking(messages: list[dict]) -> list[dict]:
    if messages and messages[0].get("role") == "system":
        return [{**messages[0], "content": messages[0]["content"] + "\n/no_think"},
                *messages[1:]]
    return messages


def _llama_response_format(response_format: dict | None) -> dict | None:
    """Translate OpenAI JSON Schema into llama-cpp-python's JSON grammar shape."""
    if response_format is None or response_format.get("type") != "json_schema":
        return response_format
    wrapper = response_format.get("json_schema")
    schema = wrapper.get("schema") if isinstance(wrapper, dict) else None
    if not isinstance(schema, dict):
        raise ValueError("json_schema response format requires a schema object")
    return {"type": "json_object", "schema": schema}


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
        async with inference_admission():
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
        async with inference_admission():
            # Per-call override, like the LiteLLM route's: DREAM's research
            # loop wants no reasoning pass for the line naming its next search
            # and the full one for the report it writes at the end.
            if not params.get("thinking", self.thinking):
                messages = _without_thinking(messages)
            loaded = await _acquire_current(self)
            response_format = _llama_response_format(params.get("response_format"))

            def run():
                return loaded.llama.create_chat_completion(
                    messages=messages, temperature=params.get("temperature", 0.2),
                    max_tokens=params.get("max_tokens", self.max_tokens), stream=False,
                    response_format=response_format)

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
