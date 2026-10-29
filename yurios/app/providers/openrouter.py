"""ChatModel + UtilityModel over LiteLLM (SPEC §3).

LiteLLM is the router seam: one interface over OpenRouter — which itself fronts
every frontier and open-weights model, so swapping the reply voice is a
`CHAT_MODEL` config change — and over a *local* model (Ollama, or an LM Studio /
llama.cpp server) by the same one-line change (→ ch. 13). The model id's PREFIX
picks the route:

    ollama/<model>             → local Ollama
    lm_studio/<model>          → local LM Studio server (OpenAI-compatible, :1234/v1)
    openai_compatible/<model>  → any other OpenAI-compatible server or gateway
    openrouter/<model>         → hosted OpenRouter
    <model>  (no prefix)       → assumed OpenRouter (the openrouter/ prefix is added)

For lm_studio/… and openai_compatible/… the server's base url is passed as
`api_base` (LMSTUDIO_BASE_URL, OPENAI_COMPATIBLE_BASE_URL).

No route names the app (SPEC §3.1): nothing here sends attribution headers, and
the ones LiteLLM would send on our behalf are blanked (`_NO_ATTRIBUTION`).
"""
from __future__ import annotations

import logging
import re
from typing import AsyncIterator

import litellm

from yurios.app.providers.admission import inference_admission
from yurios.app.providers.usage import (StreamTally, chunk_prompt_tokens,
                                        chunk_text)

log = logging.getLogger("app.providers")

# LiteLLM asks itself whether a model supports reasoning on every call, and for
# an OpenRouter id missing from its price map (z-ai/glm-5.2, say) that question
# raises inside LiteLLM, is caught there, and first prints a red "Provider List"
# banner straight to stdout — thousands of lines in the daemon log, none of them
# about a failed request. A real failure still reaches us as an exception.
litellm.suppress_debug_info = True


#: Our route for any OpenAI-compatible server, and the LiteLLM provider that
#: carries it. `hosted_vllm` is LiteLLM's plain-httpx OpenAI client: measured on
#: 1.98, its requests carry no `x-stainless-*` headers (the OpenAI SDK's OS, CPU
#: and Python-version report, which `openai/` and `custom_openai/` send) and it
#: never falls back to an `OPENAI_API_KEY` in the environment — `openai/` with a
#: custom api_base and no key sends that key to whatever server it was given.
COMPATIBLE_PREFIX = "openai_compatible/"
_COMPATIBLE_LITELLM = "hosted_vllm/"

#: What a keyless gateway is sent. LiteLLM fills an empty key from
#: HOSTED_VLLM_API_KEY, so an empty one cannot stay empty: it would hand that
#: variable — a credential for some other server — to this one.
_NO_KEY = "none"


def _route(model: str) -> str:
    """The id LiteLLM routes on: OpenRouter's unless the caller already routed
    it somewhere explicit (local `ollama/…` / `lm_studio/…`, or a full path),
    and our `openai_compatible/` spelled as the LiteLLM provider that serves it."""
    if model.startswith(COMPATIBLE_PREFIX):
        return _COMPATIBLE_LITELLM + model.removeprefix(COMPATIBLE_PREFIX)
    if model.startswith(
            ("openrouter/", "ollama/", "lm_studio/", "openai/", "anthropic/")):
        return model
    return f"openrouter/{model}"


def _route_key(model: str, api_key: str) -> str | None:
    """The key LiteLLM is handed for this (already routed) id; see `_NO_KEY`."""
    if model.startswith(_COMPATIBLE_LITELLM):
        return api_key or _NO_KEY
    return api_key or None


# LiteLLM writes `HTTP-Referer: https://litellm.ai` and `X-Title: liteLLM` onto
# every openrouter/… request, which files her usage on LiteLLM's app page. It
# merges caller headers over its own and refuses a None value, so the only way
# to take them off is to send them empty — an empty referer attributes the call
# to no one (SPEC §3.1).
_NO_ATTRIBUTION = {"HTTP-Referer": "", "X-Title": ""}


def _headers(model: str) -> dict:
    """`extra_headers` for this route: blanks over LiteLLM's OpenRouter defaults,
    nothing at all anywhere else."""
    if model.startswith("openrouter/"):
        return {"extra_headers": dict(_NO_ATTRIBUTION)}
    return {}


# Turn a reasoning model's <think> pass OFF for speed (SPEC §3, → ch. 13). The
# real switch is OpenAI-style `reasoning_effort:"none"`, but it MUST ride in the
# raw request body — passed as a top-level arg LiteLLM rewrites it and the server
# never sees it, so it silently keeps thinking (→ empty/slow reply). Forced through
# `extra_body` it reaches an LM Studio reasoning model (e.g. gemma-4-…-qat), which
# then answers directly. This is what makes Build #2's voice loop real-time.
_NO_THINK_BODY = {"reasoning_effort": "none"}

# OpenRouter's own switch (docs/guides/best-practices/reasoning-tokens):
# `reasoning.enabled=false` in the body. `reasoning_effort: none` is not
# honoured by every provider behind it, and `/no_think` is a Qwen idiom that
# would sit in her system message for the model to read. So the hosted route
# gets the native switch and nothing in the prompt (SPEC §2.4).
_OPENROUTER_NO_THINK_BODY = {"reasoning": {"enabled": False}}


def _thinking_off(model: str, messages: list[dict]) -> tuple[list[dict], dict]:
    """(messages, body) that turn a reasoning pass off for this route.

    An OpenAI-compatible gateway gets the OpenAI switch and nothing in the
    prompt: what model it serves is anyone's guess, and `/no_think` would sit in
    her system message as text for that model to read (SPEC §2.4)."""
    if model.startswith("openrouter/"):
        return messages, dict(_OPENROUTER_NO_THINK_BODY)
    if model.startswith(_COMPATIBLE_LITELLM):
        return messages, dict(_NO_THINK_BODY)
    return _no_think_messages(messages), dict(_NO_THINK_BODY)


def _schema_refused(answer: str, meta: dict, call: dict) -> bool:
    """Did this route answer nothing *because* of the JSON Schema we sent?

    The tell is a completion that stopped normally having written no answer: a
    model that ran out of room says `length`, one that was cut off says so, and
    one that had nothing to say still writes `{"goal": null}`. Empty at `stop` is
    the route declining a schema it cannot serve, in the one way the OpenAI shape
    gives it to decline in. Narrow on purpose — it must never eat a real empty.

    Not "zero completion tokens", which is what glm-5.2 showed: a reasoning
    route counts its thought there. Venice's deepseek-v4-1-flash thinks a
    thousand tokens under the schema, writes none of the answer, and reports
    them all — so the token count kept this retry from ever firing on it.
    """
    fmt = call.get("response_format") or {}
    return (not answer.strip()
            and fmt.get("type") == "json_schema"
            and meta.get("finish_reason") == "stop")

def _thought(answer: str, meta: dict) -> bool:
    """Did this completion carry a reasoning pass?

    Three ways a route shows one: the usage count, the reasoning text beside the
    content, or — on a local server that streams it inline — a `<think>` block
    at the head of the content. Any of them is a thought; none is not.
    """
    if meta.get("reasoning_tokens") or meta.get("reasoning_chars"):
        return True
    head = re.match(r"\s*<think>(.*?)</think>", answer or "", flags=re.S)
    return bool(head and head.group(1).strip())


def _thought_lost(answer: str, meta: dict, call: dict) -> bool:
    """Did a response format cost this call the reasoning it asked for?

    Measured on `openrouter/z-ai/glm-5.2` (SPEC §2.4): the Together upstream
    serves a `json_schema` or a `json_object` request with no reasoning pass
    at all — while Wafer, Mistral and DigitalOcean reason under the same
    format — and answers the promise review with a confident, well-formed,
    wrong `{"goal": null}`. An answer, at `stop`, with no thought in it, from a
    call that sent a format: narrow, like `_schema_refused`, so an empty or
    truncated answer is left to the rule that owns it.
    """
    return (bool(call.get("response_format"))
            and bool((answer or "").strip())
            and meta.get("finish_reason") == "stop"
            and not _thought(answer, meta))


# Routes that accept OpenAI's `stream_options` — verified against LM Studio 0.4,
# which otherwise sends no usage at all (its streams end on a plain finish_reason
# chunk). Asking turns the context gauge from a ~4-chars/token estimate into the
# server's own prompt_tokens. It is an allowlist and not a default because a
# provider that rejects an unknown parameter fails the whole request — which
# would cost her voice for the sake of a number — and Ollama is exactly that
# kind of maybe. So is an openai_compatible/ gateway, which could be anything,
# so it is left off. Only sent when something is actually reading the count, so
# a meter-less brain (Build #1, the desktop) puts nothing new on the wire.
_USAGE_ROUTES = ("lm_studio/", "openrouter/", "openai/")


def _ask_for_usage(model: str, meter) -> dict:
    if meter is None or not model.startswith(_USAGE_ROUTES):
        return {}
    return {"stream_options": {"include_usage": True}}


def _no_think_messages(messages: list[dict]) -> list[dict]:
    """Belt-and-suspenders for models that ignore `reasoning_effort` (e.g. Ollama
    qwen3): append qwen's `/no_think` soft-switch to the system message. Inert on
    models that don't honour the token — and that is more of them than the name
    suggests. Measured against LM Studio 0.4.8 serving a 27B Qwen, `/no_think`
    alone left the pass entirely intact (97 reasoning tokens over four runs,
    against 109 for no system message at all). The braces do the work there and
    the belt does none: `_NO_THINK_BODY` is what actually turns the pass off,
    and the server implements it by rewriting the chat template, which is
    visible as a prompt that shrinks — 63 tokens to 23 on one measured call.
    Keep both anyway; they fail on different stacks."""
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
                 *, api_base: str = "", thinking: bool = True,
                 reasoning_effort: str = "", meter=None):
        self.model = _route(model)
        self.api_base = api_base or None
        self.api_key = _route_key(self.model, api_key)
        self.temperature = temperature
        self.thinking = thinking
        self.reasoning_effort = reasoning_effort
        self.meter = meter

    async def stream(self, messages: list[dict], **params) -> AsyncIterator[str]:
        async with inference_admission():
            extra = {}
            if not self.thinking:
                messages, extra["extra_body"] = _thinking_off(self.model, messages)
            elif self.reasoning_effort:
                extra["extra_body"] = {
                    "reasoning_effort": self.reasoning_effort}
            max_tokens = asked = params.get("max_tokens", 1024)
            if self.meter is not None:
                self.meter.note_prompt(messages)      # the estimate, before the call
                # …and no bigger an ask than the window has left: a server that
                # checks prompt + max_tokens refuses rather than trims.
                room = getattr(self.meter, "reply_room", None)
                if callable(room):
                    max_tokens = room(max_tokens)
            tally = StreamTally(max_tokens, window_bound=max_tokens < asked)
            response = await litellm.acompletion(
                model=self.model,
                messages=messages,
                api_key=self.api_key,
                api_base=self.api_base,
                temperature=params.get("temperature", self.temperature),
                max_tokens=max_tokens,
                stream=True,
                **_headers(self.model),
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
                tally.note(chunk, text)
                if text:
                    yield text
            tally.settle()                    # why, if that was nothing (usage.py)


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
        self.api_key = _route_key(self.model, api_key)
        self.max_tokens = max_tokens
        self.thinking = thinking
        #: Has this model shown a reasoning pass? None until a call says, then
        #: True, or False for a model that never reasons — which is what keeps a
        #: plain local model from paying for `_thought_lost`'s retry every call.
        self.reasons: bool | None = None
        #: Learned: sending a response format with thinking on costs this route
        #: its reasoning pass, so thinking calls stop sending one (SPEC §2.4).
        self.format_costs_thought = False

    async def complete(self, messages: list[dict], **params) -> str:
        text, _meta = await self.complete_detailed(messages, **params)
        return text

    async def complete_detailed(self, messages: list[dict],
                                **params) -> tuple[str, dict]:
        """`complete`, plus the numbers that explain a disappointing answer.

        An empty string out of a reasoning model has two very different causes —
        the model had nothing to say, or it thought until the window ran out and
        never got to speak — and only the usage record tells them apart.

        Which is one reason this stays on `/v1/chat/completions` and not on LM
        Studio's newer `/v1/responses`, whose docs are where its reasoning knob
        is actually documented. Measured against 0.4.8: a call that spent every
        one of its 6,000 allowed tokens thinking, emitted no message item at
        all and broke off mid-word came back as `status: "completed"` with
        `incomplete_details: null` — where OpenAI's own contract for that
        endpoint is `"incomplete"` with `reason: "max_output_tokens"`. A client
        trusting the status field records that as a successful empty answer.
        `finish_reason` here is reported honestly, so the retry that rescues
        this exact failure can still see it coming. The
        second is the common one on a local model whose loaded context is
        smaller than the model can actually do, and a caller that can see
        `finish_reason="length"` next to `reasoning_tokens` can say so in words
        instead of shrugging (see `characters/optimize.py`).

        `reasoning_effort` rides in `extra_body` for the same reason `_no_think`
        does: passed as a top-level argument LiteLLM rewrites it and the server
        never sees it. Unlike `thinking=False` this *keeps* the reasoning pass —
        it only asks for a shorter one, which is what a long structured answer
        on a small window needs.

        `thinking` may also be given **per call**, overriding the constructor.
        One caller can want both: DREAM's research loop asks a reasoning model
        for one line naming its next search — a question no amount of thinking
        improves, and one this model spent 1200 reasoning tokens and 200 seconds
        on — and then asks the same model to write the report, which is exactly
        what the reasoning pass is for. Setting it at construction would force
        that caller to hold two models.
        """
        extra: dict = {}
        body: dict = {}
        if not params.get("thinking", self.thinking):
            messages, off = _thinking_off(self.model, messages)
            body.update(off)
        elif params.get("reasoning_effort"):
            # Only with the pass on: `thinking=False` *is* an effort of "none",
            # so a caller that passes both — a research job handing every call
            # the same effort — must not have its no-think quietly undone.
            body["reasoning_effort"] = params["reasoning_effort"]
        if body:
            extra["extra_body"] = body
        # LiteLLM's own default is 600 seconds, which is a daytime number: it is
        # generous for a turn somebody is waiting on and short for a reasoning
        # model writing a page at 4am. Measured, a report call ran 1,802 seconds
        # and died of this rather than of anything wrong with the answer — so a
        # caller that knows it is asking for a long one may say so.
        if params.get("timeout"):
            extra["timeout"] = params["timeout"]
        thinking = bool(params.get("thinking", self.thinking))
        dropped = False
        if params.get("response_format") is not None:
            if thinking and self.format_costs_thought:
                # Already learned on this model: the format would cost the
                # thought the caller asked for, and the format is not what holds
                # the shape — the prompt states it and the parser re-checks it.
                dropped = True
            else:
                # OpenAI-compatible local servers, including LM Studio, enforce
                # JSON Schema as a top-level completion parameter.
                extra["response_format"] = params["response_format"]
        call = dict(
            model=self.model,
            messages=messages,
            api_key=self.api_key,
            api_base=self.api_base,
            temperature=params.get("temperature", 0.2),
            max_tokens=params.get("max_tokens", self.max_tokens),
            **_headers(self.model),
            **extra,
        )
        async with inference_admission():
            answer, meta = await self._complete(call)
            if _schema_refused(answer, meta, call):
                # A route that cannot serve this schema answers *nothing* rather
                # than refusing (SPEC §2.4). Measured on openrouter/z-ai/glm-5.2
                # with the promise-review schema: finish_reason="stop" and zero
                # completion tokens, strict or not — while the same call with
                # `json_object`, or with no response_format at all, answers
                # correctly. The shape was never being held by the schema: the
                # contract is stated in the system prompt and re-checked by the
                # parser. So dropping to plain JSON mode gives up enforcement
                # this route was not performing, and buys back the answer.
                log.info("%s answered nothing under a json_schema; "
                         "retrying in json_object mode", self.model)
                call = {**call, "response_format": {"type": "json_object"}}
                answer, meta = await self._complete(call)
                meta["schema_downgraded"] = True
            if thinking and self.reasons is not False and _thought_lost(answer, meta, call):
                answer, meta = await self._without_format(call, answer, meta)
            elif thinking and _thought(answer, meta):
                self.reasons = True
            if dropped:
                meta["format_dropped"] = True
            return answer, meta

    async def _without_format(self, call: dict, answer: str,
                              meta: dict) -> tuple[str, dict]:
        """Ask again without the format that may have cost the thought.

        The retry settles which it was. It reasons: the format was the cost, so
        its answer stands and this model stops sending one on a thinking call.
        It doesn't either: this model does not reason, the first answer — which
        had the format's enforcement — stands, and a model never seen to reason
        is never asked twice again. A model already known to reason whose retry
        still shows no thought was an upstream having a moment; the first answer
        stands and nothing is learned.
        """
        log.info("%s answered with no reasoning under a response_format; "
                 "asking again without it", self.model)
        plain = {k: v for k, v in call.items() if k != "response_format"}
        again, again_meta = await self._complete(plain)
        if _thought(again, again_meta):
            self.reasons = True
            self.format_costs_thought = True
            again_meta["format_dropped"] = True
            return again, again_meta
        if self.reasons is None:
            self.reasons = False
        return answer, meta

    async def _complete(self, call: dict) -> tuple[str, dict]:
        """One completion, unpacked. Its own method so a retry is the same call."""
        response = await litellm.acompletion(**call)
        choice = response.choices[0]
        usage = getattr(response, "usage", None)
        details = getattr(usage, "completion_tokens_details", None)
        thought = getattr(choice.message, "reasoning_content", "") or ""
        return (choice.message.content or "", {
            "reasoning_chars": len(thought) if isinstance(thought, str) else 0,
            "finish_reason": getattr(choice, "finish_reason", "") or "",
            "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
            "total_tokens": getattr(usage, "total_tokens", 0) or 0,
            "reasoning_tokens": getattr(details, "reasoning_tokens", 0) or 0,
        })
