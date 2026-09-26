"""First-run model selection, validation, and local GGUF recommendations."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx

NONE = "NONE"
DEFAULT_HUGGINGFACE_MODEL = "HauhauCS/Gemma-4-E4B-Uncensored-HauhauCS-Aggressive"

# Keep this data separate from the UI so later recommendations can be added without
# changing the CLI or first-run route. The first entry is intentionally the only
# shipped recommendation until the compatibility matrix grows.
RECOMMENDED_MODELS = (
    {
        "id": "gguf/mradermacher/Qwen3-14B-Uncensored-GGUF",
        "label": "Qwen3 14B Uncensored (GGUF)",
        "hardware": "8+ GB VRAM, or CPU with ample RAM",
        "quant": "Q4_K_M",
    },
)


@dataclass(frozen=True)
class ModelCheck:
    ok: bool
    detail: str


#: Routes that run on this machine. A bare id is OpenRouter
#: (`providers/openrouter._route`); `openai/` and `anthropic/` are hosted too.
#: `doctor._LOCAL_PREFIXES` is the same tuple, kept there so the doctor does
#: not import this module.
LOCAL_MODEL_PREFIXES = ("ollama/", "lm_studio/", "gguf/")
#: Hosted routes LiteLLM reaches directly rather than through OpenRouter.
#: `doctor._OTHER_HOSTS` is the same tuple, for the same reason.
OTHER_HOSTED_PREFIXES = ("openai/", "anthropic/")


def model_is_local(model: str) -> bool:
    """True when this id runs on this machine (Ollama, LM Studio, or GGUF).

    Unset (`NONE`) is not local: there is no model on this machine to contend
    with. A bare id is OpenRouter.
    """
    return (model or "").strip().startswith(LOCAL_MODEL_PREFIXES)


def hosted_on_openrouter(model: str) -> bool:
    """True when this id is routed to OpenRouter: a bare id, or `openrouter/…`."""
    model = (model or "").strip()
    return bool(model and model.upper() != NONE
                and not model.startswith(LOCAL_MODEL_PREFIXES + OTHER_HOSTED_PREFIXES))


def is_configured(model: str) -> bool:
    return bool(model and model.strip() and model.strip().upper() != NONE)


def normalize_model(model: str) -> str:
    model = (model or "").strip()
    return NONE if not model or model.upper() == NONE else model


def huggingface_gguf_model(model: str) -> str:
    """Turn a Hugging Face repository ID into YuriOS's direct-GGUF route."""
    model = (model or "").strip().removeprefix("gguf/")
    return f"gguf/{model}" if model else ""


def gguf_connection_defaults(*, gpu_memory_bytes: int | None = None) -> dict[str, str]:
    """Choose a direct-GGUF profile that leaves VRAM for its KV cache.

    A model repository says nothing about the memory left for its context cache.
    Pick a conservative profile from the detected CUDA card so selecting a model
    never requires learning llama.cpp's offload controls. The saved values remain
    ordinary `.env` settings for people who need to tune an unusual setup.
    """
    if gpu_memory_bytes is None:
        try:
            import torch

            gpu_memory_bytes = (torch.cuda.get_device_properties(0).total_memory
                                if torch.cuda.is_available() else 0)
        except Exception:
            gpu_memory_bytes = 0

    gib = 1024 ** 3
    if gpu_memory_bytes >= 15 * gib:
        context_length, gpu_layers = 32768, 20
    elif gpu_memory_bytes >= 11 * gib:
        context_length, gpu_layers = 16384, 12
    elif gpu_memory_bytes >= 7 * gib:
        context_length, gpu_layers = 8192, 8
    else:
        context_length, gpu_layers = 8192, 0
    return {
        "GGUF_CONTEXT_LENGTH": str(context_length),
        "GGUF_N_GPU_LAYERS": str(gpu_layers),
        "GGUF_FLASH_ATTN": "true",
    }


def update_env(path: Path, updates: dict[str, str], *, section: str = "# --- model selected by YuriOS ---") -> list[str]:
    """Upsert values while leaving comments and unrelated user configuration alone."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)

    def matches(line: str, key: str) -> bool:
        stripped = line.lstrip()
        body = stripped[1:].lstrip() if stripped.startswith("#") else stripped
        return "=" in body and body.split("=", 1)[0].strip() == key

    for index, line in enumerate(lines):
        for key in list(remaining):
            if matches(line, key):
                lines[index] = f"{key}={remaining.pop(key)}"
                break
    if remaining:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(section)
        lines.extend(f"{key}={value}" for key, value in remaining.items())
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return list(updates)


def save_model_choice(path: Path, model: str, *, connection: dict[str, str] | None = None) -> str:
    model = normalize_model(model)
    updates = {"CHAT_MODEL": model, "UTILITY_MODEL": model}
    updates.update(connection or {})
    update_env(path, updates)
    return model


@dataclass(frozen=True)
class ModelProbe:
    ok: bool | None                 # None means there is no HTTP endpoint to test
    detail: str


def _ollama_tagged(name: str) -> str:
    """Ollama's own spelling of a model name: an untagged `qwen3` is `qwen3:latest`.

    `/api/tags` always lists the tag, while chat accepts either. Only the last
    path segment can carry one — a registry host's `:port` is not a tag."""
    return name if ":" in name.rsplit("/", 1)[-1] else f"{name}:latest"


def probe_model(cfg, model: str, *, timeout: float = 3.0) -> ModelProbe:
    """Check a model's configured HTTP endpoint without generating a billable turn.

    SPEC §3. The model-list routes prove that a local server is answering and
    serving the selected id; OpenRouter's authenticated key route proves the
    configured key works (its public model list cannot do that). Never include
    a response body or exception text in the result: either may contain a key.
    """
    model = normalize_model(model)
    if model == NONE:
        return ModelProbe(None, "no model selected")
    if model.startswith("gguf/"):
        return ModelProbe(None, "in-process GGUF has no HTTP endpoint")

    key = ""
    wanted = ""
    listing = ""
    id_field = ""
    if model.startswith("lm_studio/"):
        url = f"{cfg.lmstudio_base_url.rstrip('/')}/models"
        key = cfg.connection_api_key
        wanted = model.removeprefix("lm_studio/")
        listing, id_field = "data", "id"
    elif model.startswith("ollama/"):
        url = f"{cfg.ollama_base_url.rstrip('/')}/api/tags"
        key = cfg.connection_api_key
        wanted = model.removeprefix("ollama/")
        listing, id_field = "models", "name"
    elif hosted_on_openrouter(model):
        if not cfg.openrouter_api_key:
            return ModelProbe(False, "OPENROUTER_API_KEY is not configured")
        url = "https://openrouter.ai/api/v1/auth/key"
        key = cfg.openrouter_api_key
    else:
        return ModelProbe(None, "this model route has no connection check")

    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            response = client.get(url, headers=headers)
        if response.status_code in (401, 403):
            return ModelProbe(False, f"HTTP {response.status_code}: authentication rejected")
        if response.status_code != 200:
            return ModelProbe(False, f"HTTP {response.status_code} from model endpoint")
        if not listing:
            return ModelProbe(True, "reachable; API key authenticated")
        rows = response.json()[listing]
        if not isinstance(rows, list):
            raise ValueError("invalid model listing")
        ids = {row.get(id_field) for row in rows if isinstance(row, dict)}
        if id_field == "name":
            ids = {_ollama_tagged(name) for name in ids if isinstance(name, str)}
            wanted = _ollama_tagged(wanted)
        if wanted not in ids:
            return ModelProbe(False, f"reachable, but selected model {wanted} is absent")
        return ModelProbe(True, "reachable; selected model is listed"
                          + ("; request with configured key succeeded" if key else ""))
    except httpx.TimeoutException:
        return ModelProbe(False, f"timed out after {timeout:g}s")
    except httpx.RequestError:
        return ModelProbe(False, "could not connect to model endpoint")
    except httpx.InvalidURL:
        return ModelProbe(False, "configured model endpoint URL is invalid")
    except (ValueError, KeyError, TypeError):
        return ModelProbe(False, "model endpoint returned an invalid listing")


def validate_model(cfg, model: str) -> ModelCheck:
    """Check a selected connection without loading an LLM into memory.

    The same probe `yurios doctor --probe-model` runs; a route with nothing to
    probe is accepted rather than refused."""
    model = normalize_model(model)
    if model == NONE:
        return ModelCheck(True, "no language model selected")
    if model.startswith("gguf/"):
        return ModelCheck(True, "GGUF will download into the configured model cache")
    probe = probe_model(cfg, model)
    return ModelCheck(probe.ok is not False, probe.detail)


def download_gguf(cfg, model: str) -> Path:
    """Ensure a selected GGUF is cached, returning its local path."""
    model = normalize_model(model)
    if not model.startswith("gguf/"):
        raise ValueError("download needs a gguf/<Hugging Face repo> model")
    from yurios.app.providers.gguf import resolve_model_file

    return resolve_model_file(model, cfg)
