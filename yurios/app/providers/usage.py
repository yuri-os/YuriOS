"""Reading one streamed chunk (SPEC §3.1, §11) — text out, token usage teed off.

Its own module for two reasons. It is where both hazards of the streaming shape
live, side by side:

  - **usage arrives on a chunk with no choices.** A server that reports token
    counts (LM Studio, OpenRouter) sends them on a final chunk carrying usage and
    nothing else, so `chunk.choices[0]` — the obvious loop body — raises IndexError
    at the very end of an otherwise perfect reply.
  - **a count may simply never come.** `stream_options={"include_usage": true}` is
    only sent to routes known to accept it (a rejected parameter costs the whole
    reply), so everything here reads what arrives and shrugs at what doesn't —
    the caller falls back to estimating.

And importing it costs nothing — `litellm` is a ~10 s import, so the test suite
gets at this logic here rather than through the provider that uses it.
"""
from __future__ import annotations


def chunk_text(chunk) -> str:
    """The content this chunk carries, or "" — usage-only and empty-delta chunks
    both land here as "" rather than as an exception."""
    choices = getattr(chunk, "choices", None) or []
    if not choices:
        return ""
    delta = choices[0].delta
    if not delta:
        return ""
    content = delta.get("content") if hasattr(delta, "get") else getattr(
        delta, "content", None)
    return content or ""


def chunk_prompt_tokens(chunk) -> int:
    """The prompt-token count this chunk reports, or 0 if it reports none."""
    usage = getattr(chunk, "usage", None)
    if usage is None:
        return 0
    tokens = (usage.get("prompt_tokens") if hasattr(usage, "get")
              else getattr(usage, "prompt_tokens", None))
    try:
        return max(0, int(tokens))
    except (TypeError, ValueError):
        return 0
