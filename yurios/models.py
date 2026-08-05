"""First-run model selection, validation, and local GGUF recommendations."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx

NONE = "NONE"

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


def is_configured(model: str) -> bool:
    return bool(model and model.strip() and model.strip().upper() != NONE)


def normalize_model(model: str) -> str:
    model = (model or "").strip()
    return NONE if not model or model.upper() == NONE else model


def update_env(path: Path, updates: dict[str, str]) -> list[str]:
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
        lines.append("# --- model selected by YuriOS ---")
        lines.extend(f"{key}={value}" for key, value in remaining.items())
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return list(updates)


def save_model_choice(path: Path, model: str, *, connection: dict[str, str] | None = None) -> str:
    model = normalize_model(model)
    updates = {"CHAT_MODEL": model, "UTILITY_MODEL": model}
    updates.update(connection or {})
    update_env(path, updates)
    return model


def validate_model(cfg, model: str) -> ModelCheck:
    """Check a selected connection without loading an LLM into memory."""
    model = normalize_model(model)
    if model == NONE:
        return ModelCheck(True, "no language model selected")
    if model.startswith("gguf/"):
        return ModelCheck(True, "GGUF will download into the configured model cache")
    try:
        with httpx.Client(timeout=3.0) as client:
            if model.startswith("lm_studio/"):
                response = client.get(f"{cfg.lmstudio_base_url.rstrip('/')}/models")
                response.raise_for_status()
                data = response.json()
                wanted = model.split("/", 1)[1]
                ids = {item.get("id") for item in data.get("data", [])}
                if wanted not in ids:
                    return ModelCheck(False, f"LM Studio is reachable but does not serve {wanted}")
            elif model.startswith("ollama/"):
                response = client.get(f"{cfg.ollama_base_url.rstrip('/')}/api/tags")
                response.raise_for_status()
                data = response.json()
                wanted = model.split("/", 1)[1]
                ids = {item.get("name") for item in data.get("models", [])}
                if wanted not in ids:
                    return ModelCheck(False, f"Ollama is reachable but has not pulled {wanted}")
            elif model.startswith("openrouter/"):
                if not cfg.openrouter_api_key:
                    return ModelCheck(False, "OPENROUTER_API_KEY is required for an openrouter/ model")
                response = client.get("https://openrouter.ai/api/v1/auth/key", headers={
                    "Authorization": f"Bearer {cfg.openrouter_api_key}",
                })
                response.raise_for_status()
    except (httpx.HTTPError, ValueError) as exc:
        return ModelCheck(False, f"could not reach the configured model connection: {exc}")
    return ModelCheck(True, "connection verified")


def download_gguf(cfg, model: str) -> Path:
    """Ensure a selected GGUF is cached, returning its local path."""
    model = normalize_model(model)
    if not model.startswith("gguf/"):
        raise ValueError("download needs a gguf/<Hugging Face repo> model")
    from yurios.app.providers.gguf import resolve_model_file

    return resolve_model_file(model, cfg)
