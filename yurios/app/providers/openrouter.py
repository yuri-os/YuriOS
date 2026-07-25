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

from yurios.app.providers.usage import chunk_prompt_tokens, chunk_text


def _route(model: str) -> str:
    """Prefix for LiteLLM's OpenRouter routing, unless the caller already routed
    it somewhere explicit (local `ollama/…` / `lm_studio/…`, or a full path)."""
    if model.startswith(
            ("openrouter/", "ollama/", "lm_studio/", "openai/", "anthropic/")):
        return model
    return f"openrouter/{model}"


# OpenRouter attributes each request to an app by two headers: X-Title (the name
# shown in your dashboard and on the model's ranking) and HTTP-Referer (the
# clickable link, and the favicon beside it). Left unset, LiteLLM sends its own
# defaults and usage shows up as "liteLLM"; set ours so it reads as YuriOS.
_APP_TITLE = "YuriOS"
_APP_URL = "https://yurios.org"


def _attribution(model: str) -> dict:
    """`extra_headers` naming this app to OpenRouter — only for openrouter/… ids;
    local routes (ollama / lm_studio) don't want them, so send nothing there."""
    if model.startswith("openrouter/"):
        return {"extra_headers": {"HTTP-Referer": _APP_URL, "X-Title": _APP_TITLE}}
    return {}


# Turn a reasoning model's <think> pass OFF for speed (SPEC §3, → ch. 13). The
# real switch is OpenAI-style `reasoning_effort:"none"`, but it MUST ride in the
# raw request body — passed as a top-level arg LiteLLM rewrites it and the server
# never sees it, so it silently keeps thinking (→ empty/slow reply). Forced through
# `extra_body` it reaches an LM Studio reasoning model (e.g. gemma-4-…-qat), which
# then answers directly. This is what makes Build #2's voice loop real-time.
_NO_THINK_BODY = {"reasoning_effort": "none"}

# Routes that accept OpenAI's `stream_options` — verified against LM Studio 0.4,
# which otherwise sends no usage at all (its streams end on a plain finish_reason
# chunk). Asking turns the context gauge from a ~4-chars/token estimate into the
# server's own prompt_tokens. It is an allowlist and not a default because a
# provider that rejects an unknown parameter fails the whole request — which
# would cost her voice for the sake of a number — and Ollama is exactly that
# kind of maybe. Only sent when something is actually reading the count, so a
# meter-less brain (Build #1, the desktop) puts nothing new on the wire.
_USAGE_ROUTES = ("lm_studio/", "openrouter/", "openai/")


def _ask_for_usage(model: str, meter) -> dict:
    if meter is None or not model.startswith(_USAGE_ROUTES):
        return {}
    return {"stream_options": {"include_usage": True}}


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
    short replies come back fast and non-empty — the Build #2 real-time default.

    `meter` is an optional observer of how big each prompt got (world/context.py):
    it is told the messages before the call, and the server's own `prompt_tokens`
    afterwards when the stream volunteers usage. Nothing here depends on one being
    attached — Build #1's brain runs with `meter = None`."""

    def __init__(self, model: str, api_key: str = "", temperature: float = 0.9,
                 *, api_base: str = "", thinking: bool = True, meter=None):
        self.model = _route(model)
        self.api_base = api_base or None
        self.api_key = api_key or None
        self.temperature = temperature
        self.thinking = thinking
        self.meter = meter

    async def stream(self, messages: list[dict], **params) -> AsyncIterator[str]:
        extra = {}
        if not self.thinking:
            messages = _no_think_messages(messages)
            extra["extra_body"] = _NO_THINK_BODY
        if self.meter is not None:
            self.meter.note_prompt(messages)      # the estimate, before the call
        response = await litellm.acompletion(
            model=self.model,
            messages=messages,
            api_key=self.api_key,
            api_base=self.api_base,
            temperature=params.get("temperature", self.temperature),
            max_tokens=params.get("max_tokens", 1024),
            stream=True,
            **_attribution(self.model),
            **_ask_for_usage(self.model, self.meter),
            **extra,
        )
        async for chunk in response:
            # usage rides a final choice-less chunk when the server volunteers it
            # at all; providers/usage.py holds both hazards and the reasoning
            if self.meter is not None:
                prompt_tokens = chunk_prompt_tokens(chunk)
                if prompt_tokens:
                    self.meter.note_usage(prompt_tokens)
            text = chunk_text(chunk)
            if text:
                yield text


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
            **_attribution(self.model),
            **extra,
        )
        return response.choices[0].message.content or ""
