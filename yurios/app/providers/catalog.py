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
    ("openai_compatible", "OpenAI-compatible", "openai_compatible/"),
    ("openrouter", "OpenRouter", "openrouter/"),
    ("custom", "Custom", ""),
)


#: Where each provider's own key is set for the house, for the refusal message.
_HOUSE_KEYS = {"lmstudio": "LMSTUDIO_API_KEY",
               "openai_compatible": "OPENAI_COMPATIBLE_API_KEY",
               "openrouter": "OPENROUTER_API_KEY"}


def provider_for_route(model: str) -> str:
    """The picker's provider id for a LiteLLM model id, or "" when none lists it."""
    for provider, _, prefix in PROVIDERS:
        if prefix and (model or "").startswith(prefix):
            return provider
    return ""


def provider_prefix(provider: str) -> str:
    """The LiteLLM route prefix for a picker provider id, or "" for none."""
    provider = "lmstudio" if provider == "lm_studio" else provider
    return next((prefix for pid, _, prefix in PROVIDERS if pid == provider), "")


#: The base-url field each server-backed provider reads, on a Config.
SERVER_URL_FIELDS = {"lmstudio": "lmstudio_base_url", "ollama": "ollama_base_url",
                     "openai_compatible": "openai_compatible_base_url"}


def _key_refused(provider: str, status: int, sent_key: bool) -> str:
    """A 401 is not "unreachable": the server is there and wants a key. Say
    which, and where the key goes — a character's own server takes it on her
    connection profile, the house's on its .env line (SPEC §31.1–§31.2)."""
    label = next((name for pid, name, _ in PROVIDERS if pid == provider), provider)
    house = _HOUSE_KEYS.get(provider)
    where = ("give her connection profile the key on the switchboard"
             + (f", or set {house} for the house" if house else ""))
    if sent_key:
        return f"{label} refused the API key it was sent (HTTP {status}) — {where}"
    return f"{label} requires an API key (HTTP {status}) — {where}"


async def _fetch_json(url: str, headers: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=8.0) as client:
        response = await client.get(url, headers=headers or {})
        response.raise_for_status()
        return response.json()


async def provider_models(cfg, provider: str) -> dict[str, Any]:
    """`{"models": [...]}`, or `{"models": [], "error": "..."}`.

    lm_studio, ollama and an OpenAI-compatible gateway are asked directly on the
    base url this config points at; openrouter is asked for its public
    catalogue, with the key attached when one is set so private/BYOK models
    show up too.
    """
    provider = (provider or "").strip().lower()
    headers: dict | None = None
    try:
        if provider in ("lmstudio", "lm_studio"):
            base = cfg.lmstudio_base_url.rstrip("/")
            headers = ({"Authorization": f"Bearer {cfg.lmstudio_api_key}"}
                       if cfg.lmstudio_api_key else None)
            data = await _fetch_json(f"{base}/models", headers)
            ids = [item.get("id", "") for item in data.get("data", [])]
        elif provider == "ollama":
            base = cfg.ollama_base_url.rstrip("/")
            headers = ({"Authorization": f"Bearer {cfg.connection_api_key}"}
                       if cfg.connection_api_key else None)
            data = await _fetch_json(f"{base}/api/tags", headers)
            ids = [item.get("name", "") for item in data.get("models", [])]
        elif provider == "openai_compatible":
            base = cfg.openai_compatible_base_url.rstrip("/")
            if not base:
                return {"models": [],
                        "error": "save the OpenAI-compatible server address "
                                 "(OPENAI_COMPATIBLE_BASE_URL) first — or type the id"}
            headers = ({"Authorization": f"Bearer {cfg.openai_compatible_api_key}"}
                       if cfg.openai_compatible_api_key else None)
            data = await _fetch_json(f"{base}/models", headers)
            ids = [item.get("id", "") for item in data.get("data", [])]
        elif provider == "openrouter":
            headers = ({"Authorization": f"Bearer {cfg.openrouter_api_key}"}
                       if cfg.openrouter_api_key else None)
            data = await _fetch_json("https://openrouter.ai/api/v1/models", headers)
            ids = [item.get("id", "") for item in data.get("data", [])]
        else:
            return {"models": [],
                    "error": f"no live listing for '{provider}' — type the id"}
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status in (401, 403):
            return {"models": [], "error": _key_refused(
                "lmstudio" if provider == "lm_studio" else provider, status, bool(headers))}
        return {"models": [], "error": f"couldn't reach {provider}: {str(exc)[:120]}"}
    except Exception as exc:      # unreachable server, bad json, timeout…
        return {"models": [], "error": f"couldn't reach {provider}: {str(exc)[:120]}"}
    return {"models": sorted({item for item in ids if item})}
