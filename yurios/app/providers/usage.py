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

from contextvars import ContextVar


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


# ---- a stream that said nothing (SPEC §10.5) ---------------------------------
#
# A reply stream can end cleanly with no text in it at all, and the turn runner
# can only see the absence: `chunk_text` is all it is handed. Why there is
# nothing is on the chunks it never reads — a `finish_reason`, a reasoning pass
# that ran the whole budget down, a refusal the provider filed beside the
# content rather than in it. So the stream keeps a tally of those as it goes,
# and leaves the tally where the turn that consumed it can read it.

#: The tally of this task's last reply stream that ran to completion, or None.
#: A ContextVar rather than a provider attribute because the provider is
#: shared: the mind can be streaming on the same object while a turn is, and an
#: async generator runs in the context of the task iterating it, so a turn
#: reads its own stream and never the mind's. The reader clears it first.
LAST_STREAM: ContextVar["StreamTally | None"] = ContextVar("last_stream",
                                                           default=None)

#: Whether a hand of hers ran to "ok" in this task's turn. A reply with no words
#: in it after one did is not a turn that didn't happen: the selfie is rendering,
#: the timer is set, the goal is filed. Failing it would roll back the record of
#: the act and tell them to send it again, which does the act twice. Set by
#: `ToolBrain._execute`, in the same context as `LAST_STREAM` and for the same
#: reason; the reader clears it first.
ACTED: ContextVar[bool] = ContextVar("acted", default=False)


def _field(obj, name: str):
    """`obj[name]` or `obj.name` — llama.cpp streams dicts, LiteLLM objects."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


class StreamTally:
    """What one reply stream carried besides its text, for when it carried none."""

    def __init__(self, max_tokens: int = 0, *, window_bound: bool = False):
        self.max_tokens = int(max_tokens or 0)
        #: The ask was cut to what the context window had left (world/context.py
        #: `reply_room`), so the knob that would have helped is the window's.
        self.window_bound = window_bound
        self.spoke = False
        self.finish_reason = ""
        self.reasoning_chars = 0
        self.reasoning_tokens = 0
        self.completion_tokens = 0
        self.refusal = ""

    def note(self, chunk, text: str) -> None:
        if text:
            self.spoke = True
        choices = _field(chunk, "choices") or []
        if choices:
            choice = choices[0]
            self.finish_reason = _field(choice, "finish_reason") or self.finish_reason
            delta = _field(choice, "delta")
            thought = (_field(delta, "reasoning_content")
                       or _field(delta, "reasoning") or "")
            if isinstance(thought, str):
                self.reasoning_chars += len(thought)
            refusal = _field(delta, "refusal") or _field(
                _field(delta, "provider_specific_fields"), "refusal")
            if isinstance(refusal, str):
                self.refusal += refusal
        usage = _field(chunk, "usage")
        if usage is not None:
            self.completion_tokens = _int(_field(usage, "completion_tokens")) \
                or self.completion_tokens
            self.reasoning_tokens = _int(_field(
                _field(usage, "completion_tokens_details"), "reasoning_tokens")) \
                or self.reasoning_tokens

    def reason(self) -> str:
        """Why it said nothing, as a person would want it said; "" if it spoke."""
        if self.spoke:
            return ""
        if self.refusal or self.finish_reason == "content_filter":
            said = f": “{self.refusal.strip()[:200]}”" if self.refusal.strip() else ""
            return f"the model refused to answer{said}"
        if self.finish_reason == "length":
            budget = f"the whole {self.max_tokens}-token" if self.max_tokens \
                else "the whole"
            knob = ("the prompt left no more room — raise CONTEXT_LENGTH"
                    if self.window_bound else "raise MAX_REPLY_TOKENS")
            return (f"she ran out of room thinking — {budget} reply budget went "
                    f"on reasoning ({knob})")
        return "the model sent back no text"

    def detail(self) -> str:
        """Every number the stream gave, for the log line beside `reason`."""
        parts = [f"finish_reason={self.finish_reason or 'none'}",
                 f"max_tokens={self.max_tokens or 'default'}"]
        if self.completion_tokens:
            parts.append(f"completion_tokens={self.completion_tokens}")
        if self.reasoning_tokens:
            parts.append(f"reasoning_tokens={self.reasoning_tokens}")
        if self.reasoning_chars:
            parts.append(f"reasoning_chars={self.reasoning_chars}")
        return f"({', '.join(parts)})"

    def settle(self) -> None:
        """End of a stream that ran to completion: leave the tally for the turn.
        Not called when the consumer closed the stream early — a tool call ends
        a pass mid-stream, and that is not silence. Nor does it warn: a pass
        after a tool call often has nothing left to say, and only the turn knows
        whether the reply as a whole came back empty."""
        LAST_STREAM.set(self)


def unanswered(why: str) -> str:
    """What the person is told when a turn came back empty — one wording for
    every door a turn comes back through (the text runner, the voice socket)."""
    return f"she didn't answer — {why}. Send it again to retry."


def _int(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0
