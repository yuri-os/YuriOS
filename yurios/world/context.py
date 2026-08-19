"""The context meter (SPEC §11) — how full her window is, on screen.

A local model's context window is finite and silent about it: the prompt grows
turn by turn (the system block, the raw window, recalled memories, the situation,
the tools directive) and the first sign that it stopped fitting is the server
refusing a turn outright — LM Studio's "Context size has been exceeded". By then
the reply is already lost.

So the numbers go in the masthead instead. This class holds two of them:

  - **the ceiling** — CONTEXT_LENGTH from .env when set (the same number sent to
    LM Studio when her model is pinned, so the readout measures against the
    window she is actually running in), otherwise whatever the server admits to
    when probed at boot. Unknown is a legitimate state: a hosted route never
    says, and the readout then shows the used side alone rather than a guess.
  - **what's used** — the exact `prompt_tokens` when the route will report usage
    on the stream (LM Studio and OpenRouter do, if asked — see `_ask_for_usage`
    in providers/openrouter.py), else a ~4-chars/token estimate of the assembled
    messages. Which one it is rides along in the event, so the UI can mark an
    estimate as approximate rather than imply a number it doesn't have.

Every update publishes one sticky `context` event on the hub, so a page that
opens mid-conversation sees the last measurement instead of a blank gauge.
`reserve` (MAX_REPLY_TOKENS) is on the wire too: the real ceiling for a turn is
prompt + reply, and a gauge that ignores the reply half reads green right up to
the failure.
"""
from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path

from yurios.app.core.assemble import est_tokens

log = logging.getLogger("world.context")

# One prompt in every N over the line gets a log line — the meter runs per model
# pass (the tool loop makes several a turn), and a wall of identical warnings
# helps nobody. The UI is the real signal; this is for the terminal.
_WARN_EVERY = 5


class ContextMeter:
    """Prompt size vs the context window, published for the UI (SPEC §11)."""

    def __init__(self, hub=None, *, limit: int = 0, limit_source: str = "",
                 reserve: int = 0, trace_dir: Path | None = None,
                 max_trace_bytes: int = 2_000_000):
        self.hub = hub
        self.limit = int(limit or 0)               # 0 = unknown, and say so
        self.limit_source = limit_source or ("env" if limit else "")
        self.reserve = int(reserve or 0)           # MAX_REPLY_TOKENS
        self.used = 0
        self.exact = False                         # True once a server said so
        self._over = 0
        self.trace_path = Path(trace_dir) / "context.jsonl" if trace_dir else None
        self.max_trace_bytes = max_trace_bytes
        if self.limit:
            # a window known before the first turn is worth showing at once: the
            # gauge reads 0 / 32k on an empty conversation, not a blank space
            self._publish()

    # ---- learning the ceiling ------------------------------------------------

    def set_limit(self, tokens: int, source: str) -> None:
        """Adopt a window observed at boot — and it outranks CONTEXT_LENGTH.

        .env is what we *asked* for; this is what the server says it loaded, and
        the two come apart for real reasons (a window that wouldn't fit in RAM,
        a model that clamps it). A gauge measuring against a ceiling she doesn't
        actually have is the failure this whole file exists to prevent, so the
        observation wins and the caller logs the discrepancy."""
        if tokens <= 0:
            return
        self.limit = int(tokens)
        self.limit_source = source
        self._publish()

    # ---- measuring the prompt ------------------------------------------------

    def note_prompt(self, messages: list[dict]) -> None:
        """Estimate, from the messages about to be sent. Cheap and always
        available — the exact number, if it comes at all, comes after the reply."""
        self.used = estimate_messages(messages)
        self.exact = False
        self._warn()
        self._publish()
        self._record("estimate")

    def note_usage(self, prompt_tokens: int) -> None:
        """The server's own count, from the usage the stream volunteered."""
        if prompt_tokens <= 0:
            return
        self.used = int(prompt_tokens)
        self.exact = True
        self._warn()
        self._publish()
        self._record("usage")

    # ---- what the UI and /api/context read -----------------------------------

    def snapshot(self) -> dict:
        pct = round(100 * self.used / self.limit, 1) if self.limit else None
        return {"used": self.used,
                "limit": self.limit or None,
                "limit_source": self.limit_source or None,
                "reserve": self.reserve,
                "exact": self.exact,
                "pct": pct}

    def _publish(self) -> None:
        if self.hub is not None:
            self.hub.publish("context", self.snapshot(), sticky="context")

    def _record(self, source: str) -> None:
        if self.trace_path is None:
            return
        try:
            self.trace_path.parent.mkdir(parents=True, exist_ok=True)
            if (self.trace_path.exists()
                    and self.trace_path.stat().st_size >= self.max_trace_bytes):
                rotated = self.trace_path.with_suffix(self.trace_path.suffix + ".1")
                rotated.unlink(missing_ok=True)
                self.trace_path.replace(rotated)
            row = {"timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                   "source": source, **self.snapshot()}
            with self.trace_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            log.exception("could not append context history")

    def _warn(self) -> None:
        """The prompt plus the reply she still has to write is what must fit."""
        if not self.limit or self.used + self.reserve <= self.limit:
            self._over = 0
            return
        if self._over % _WARN_EVERY == 0:
            log.warning(
                "context: %d prompt tokens + %d reserved for the reply exceeds "
                "the %d-token window — turns will start failing. Raise "
                "CONTEXT_LENGTH in .env (and restart), or lower "
                "SYSTEM_BUDGET_TOKENS / RAW_WINDOW_TURNS / MAX_REPLY_TOKENS.",
                self.used, self.reserve, self.limit)
        self._over += 1


def short_tokens(n: int) -> str:
    """8192 → "8k" — token counts as people say them (the boot panel's register;
    the UI does its own formatting in js/main.js)."""
    if n >= 1000:
        k = n / 1000
        return f"{k:.0f}k" if abs(k - round(k)) < 0.05 else f"{k:.1f}k"
    return str(int(n))


#: What one picture costs the window, near enough (SPEC §35). A real number
#: exists — it is a function of the tiling the vision encoder does — and it is
#: different for every model, so this is one figure for the whole class: about
#: what a 1024px image costs the qwen/gemma vision stacks the local routes run.
#: Measuring the base64 at four characters per token would be off by an order of
#: magnitude in the *wrong* direction (a 300 KB photo reading as 100k tokens),
#: and counting nothing at all lets a gauge sit at half while the turn overflows.
#: Where the server volunteers usage (LM Studio does) the exact number replaces
#: this on the next chunk anyway.
IMAGE_TOKENS = 1200


def estimate_messages(messages: list[dict]) -> int:
    """~4 chars/token over the whole message array, plus the per-message framing
    every chat template adds (role tags, separators). Guardrail arithmetic, the
    same estimator the §7.2 prompt budgets use — never billing."""
    total = 0
    for m in messages or []:
        content = m.get("content") or ""
        if not isinstance(content, str):           # a multimodal part list
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "image_url":
                    total += IMAGE_TOKENS
                else:
                    total += est_tokens(part.get("text", ""))
            total += 4
            continue
        total += est_tokens(content) + 4
    return total
