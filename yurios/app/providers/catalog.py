"""What a provider can actually serve right now (SPEC §11).

The model pickers ask this: the settings panel's CHAT_MODEL / UTILITY_MODEL
combobox, and the studio's "optimize with AI" dialog. Two surfaces, one answer,
because a model the settings panel offers and the studio refuses would be a lie
about the same server.

Never raises. A provider that is off, unreachable or simply has no listing comes
back as an empty list plus an `error` string the caller renders inline — a model
picker that 500s takes the whole dialog with it, and "type the id" is always
still a working answer.
"""
from __future__ import annotations

from typing import Any

import httpx

#: Provider ids the pickers use ⇄ the LiteLLM route prefix a chosen model gets.
#: `custom` is the escape hatch: the id is written verbatim, so any route
#: LiteLLM understands stays reachable even with nothing to list.
PROVIDERS: tuple[tuple[str, str, str], ...] = (
    ("lmstudio", "LM Studio", "lm_studio/"),
    ("ollama", "Ollama", "ollama/"),
    ("openrouter", "OpenRouter", "openrouter/"),
    ("custom", "Custom", ""),
)


async def _fetch_json(url: str, headers: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=8.0) as client:
        response = await client.get(url, headers=headers or {})
        response.raise_for_status()
        return response.json()


async def provider_models(cfg, provider: str) -> dict[str, Any]:
    """`{"models": [...]}`, or `{"models": [], "error": "..."}`.

    lm_studio and ollama are asked directly on the base url this config points
    at; openrouter is asked for its public catalogue, with the key attached when
    one is set so private/BYOK models show up too.
    """
    provider = (provider or "").strip().lower()
    try:
        if provider in ("lmstudio", "lm_studio"):
            base = cfg.lmstudio_base_url.rstrip("/")
            data = await _fetch_json(f"{base}/models")
            ids = [item.get("id", "") for item in data.get("data", [])]
        elif provider == "ollama":
            base = cfg.ollama_base_url.rstrip("/")
            data = await _fetch_json(f"{base}/api/tags")
            ids = [item.get("name", "") for item in data.get("models", [])]
        elif provider == "openrouter":
            headers = ({"Authorization": f"Bearer {cfg.openrouter_api_key}"}
                       if cfg.openrouter_api_key else None)
            data = await _fetch_json("https://openrouter.ai/api/v1/models", headers)
            ids = [item.get("id", "") for item in data.get("data", [])]
        else:
            return {"models": [],
                    "error": f"no live listing for '{provider}' — type the id"}
    except Exception as exc:      # unreachable server, bad json, timeout, no key…
        return {"models": [], "error": f"couldn't reach {provider}: {str(exc)[:120]}"}
    return {"models": sorted({item for item in ids if item})}
