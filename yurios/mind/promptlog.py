"""PromptLog — every context window she was ever given (SPEC §24.2).

`corpus/turns.jsonl` has always held the full assembled prompt for a *committed
conversational turn*, because that is the training asset. But a turn is the
minority of what an always-on mind does. Self-talk, a timer announcement, the
arrival greeting, a reach-out being written, one bounded step of goal work, a
night of DREAM consolidation — all of those call a model, and none of them left
a trace of what they were actually asked. "Why did she say that at 3am" was a
question the files could not answer.

So one sink, `traces/prompts.jsonl`, records every model call as a uniform row,
stamped with the `corr_id` of the work that caused it (world/correlate.py) so it
lines up with the tick trace and the tool audit.

Chat turns are the deliberate exception: they write a *pointer* here and keep
their body in the corpus. Copying whole prompts onto the hottest path in the
build would roughly double its largest write to say nothing new, and the corpus
is the record `ratings.jsonl` joins against. So this file is the complete index
of every model call, and the detail view resolves the pointer when you open one.

Not in `corpus/` because that directory means "trainable"; not in the Vault
because this is derived, not memory. `traces/` is already a private surface
(characters/privacy.py), which matters more here than anywhere: an assembled
prompt contains USER.md and recalled memories verbatim.
"""
from __future__ import annotations

import logging
from pathlib import Path

from yurios.kernel import correlate
from yurios.kernel.clock import Clock

from .util import estimate_tokens, iso_of, jsonl_append, new_id

log = logging.getLogger("mind.promptlog")

#: One message longer than this is truncated. A knowledge shelf that swallowed a
#: PDF can otherwise put a megabyte on one line, and rotation would then throw
#: away the whole history to make room for a single record.
MAX_MESSAGE_CHARS = 200_000


class PromptLog:
    def __init__(self, trace_dir: Path, clock: Clock, *,
                 max_bytes: int | None = 32_000_000,
                 max_chars: int = MAX_MESSAGE_CHARS, enabled: bool = True):
        self.path = Path(trace_dir) / "prompts.jsonl"
        self.clock = clock
        self.max_bytes = max_bytes
        self.max_chars = max_chars
        self.enabled = enabled

    def _messages(self, messages: list[dict] | None) -> tuple[list[dict] | None, bool]:
        if messages is None:
            return None, False
        out, truncated = [], False
        for m in messages:
            content = m.get("content") or ""
            if isinstance(content, str) and len(content) > self.max_chars:
                content, truncated = content[:self.max_chars] + "…", True
            out.append({"role": m.get("role", "user"), "content": content})
        return out, truncated

    def record(self, *, kind: str, messages: list[dict] | None = None,
               completion: str | None = None, model: str = "",
               template_version: str = "", messages_ref: dict | None = None,
               **extra) -> str | None:
        """Write one row. Best-effort by construction: a debug sink must never
        be the reason the turn it is watching fails."""
        if not self.enabled:
            return None
        try:
            body, truncated = self._messages(messages)
            row = {
                "id": new_id("pr"),
                "ts": iso_of(self.clock.now()), "at": self.clock.now(),
                "kind": kind,
                **correlate.stamp(),
                "model": model, "template_version": template_version,
                "messages": body, "messages_ref": messages_ref,
                "completion": completion,
                "n_messages": len(body) if body is not None else None,
                "tokens_in": sum(estimate_tokens(m["content"]) for m in body)
                             if body else None,
                "tokens_out": estimate_tokens(completion or "") if completion else None,
                "truncated": truncated,
                **extra,
            }
            jsonl_append(self.path, row, max_bytes=self.max_bytes)
            return row["id"]
        except Exception:
            log.exception("prompt log write failed")
            return None

    @classmethod
    def from_config(cls, cfg, clock: Clock) -> "PromptLog":
        """The one construction the runtime and the mind both use, so they agree
        on the file and on the caps without threading five settings each."""
        return cls(cfg.trace_dir, clock,
                   max_bytes=getattr(cfg, "mind_prompt_log_max_bytes", None),
                   max_chars=getattr(cfg, "mind_prompt_max_chars", MAX_MESSAGE_CHARS),
                   enabled=getattr(cfg, "mind_prompt_capture", True))
