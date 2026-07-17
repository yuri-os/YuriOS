"""ChatModel + UtilityModel over LiteLLM (SPEC §3).

LiteLLM is the router seam: one interface over OpenRouter — which itself fronts
every frontier and open-weights model, so swapping the reply voice is a
`CHAT_MODEL` config change — and over a *local* model (Ollama, or an LM Studio /
llama.cpp server) by the same one-line change (→ ch. 13). The model id's PREFIX
picks the route:

    ollama/<model>       → local Ollama
    lm_studio/<model>    → local LM Studio server (OpenAI-compatible, :1234/v1)
    openrouter/<model>   → hosted OpenRouter
    <model>  (no prefix) → assumed OpenRouter (the openrouter/ prefix is added)

For lm_studio/… the server's base url is passed as `api_base` (LMSTUDIO_BASE_URL).
"""
from __future__ import annotations

from typing import AsyncIterator

import litellm


def _route(model: str) -> str:
    """Prefix for LiteLLM's OpenRouter routing, unless the caller already routed
    it somewhere explicit (local `ollama/…` / `lm_studio/…`, or a full path)."""
    if model.startswith(
            ("openrouter/", "ollama/", "lm_studio/", "openai/", "anthropic/")):
        return model
    return f"openrouter/{model}"


# Turn a reasoning model's <think> pass OFF for speed (SPEC §3, → ch. 13). The
# real switch is OpenAI-style `reasoning_effort:"none"`, but it MUST ride in the
# raw request body — passed as a top-level arg LiteLLM rewrites it and the server
# never sees it, so it silently keeps thinking (→ empty/slow reply). Forced through
# `extra_body` it reaches an LM Studio reasoning model (e.g. gemma-4-…-qat), which
# then answers directly. This is what makes Build #2's voice loop real-time.
_NO_THINK_BODY = {"reasoning_effort": "none"}


def _no_think_messages(messages: list[dict]) -> list[dict]:
    """Belt-and-suspenders for models that ignore `reasoning_effort` (e.g. Ollama
    qwen3): append qwen's `/no_think` soft-switch to the system message. Inert on
    models that don't honour the token."""
    if messages and messages[0].get("role") == "system":
        return [{**messages[0], "content": messages[0]["content"] + "\n/no_think"},
                *messages[1:]]
    return messages


class LiteLLMChatModel:
    """The reply voice (§3): streams tokens for /api/chat and /api/greeting.

    `thinking=False` disables a reasoning model's <think> pass (see `_no_think`) so
    short replies come back fast and non-empty — the Build #2 real-time default."""

    def __init__(self, model: str, api_key: str = "", temperature: float = 0.9,
                 *, api_base: str = "", thinking: bool = True):
        self.model = _route(model)
        self.api_base = api_base or None
        self.api_key = api_key or None
        self.temperature = temperature
        self.thinking = thinking

    async def stream(self, messages: list[dict], **params) -> AsyncIterator[str]:
        extra = {}
        if not self.thinking:
            messages = _no_think_messages(messages)
            extra["extra_body"] = _NO_THINK_BODY
        response = await litellm.acompletion(
            model=self.model,
            messages=messages,
            api_key=self.api_key,
            api_base=self.api_base,
            temperature=params.get("temperature", self.temperature),
            max_tokens=params.get("max_tokens", 1024),
            stream=True,
            **extra,
        )
        async for chunk in response:
            delta = chunk.choices[0].delta
            if delta and delta.get("content"):
                yield delta["content"]


class LiteLLMUtilityModel:
    """The cheap model (§3): partner-model fact extraction (§6.3) + summarisation (§7.3).

    Reasoning models (qwen3, r1, gemma-…-qat, …) are first-class here: their <think>
    block runs before the JSON answer, so the budget must leave room for both
    (`max_tokens`) — too small a budget truncates the answer to an empty string and
    silently loses the fact. Thinking stays ON by default; `thinking=False` disables
    the reasoning pass (see `_no_think`) for callers who want speed over quality."""

    def __init__(self, model: str, api_key: str = "", *,
                 max_tokens: int = 2048, thinking: bool = True, api_base: str = ""):
        self.model = _route(model)
        self.api_base = api_base or None
        self.api_key = api_key or None
        self.max_tokens = max_tokens
        self.thinking = thinking

    async def complete(self, messages: list[dict], **params) -> str:
        extra = {}
        if not self.thinking:
            messages = _no_think_messages(messages)
            extra["extra_body"] = _NO_THINK_BODY
        response = await litellm.acompletion(
            model=self.model,
            messages=messages,
            api_key=self.api_key,
            api_base=self.api_base,
            temperature=params.get("temperature", 0.2),
            max_tokens=params.get("max_tokens", self.max_tokens),
            **extra,
        )
        return response.choices[0].message.content or ""
