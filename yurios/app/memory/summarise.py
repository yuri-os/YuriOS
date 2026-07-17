"""Rolling summarisation (SPEC §7.3).

Every SUMMARY_EVERY_N turns, fold the last N exchanges into the previous
`memory/summary.md` — third person, present-continuous, bounded. The summary
carries older context cheaply so the raw window stays small (§7.2, "Lost in
the Middle"). It is also indexed (`kind='summary'`, salience 2.0) so recall
can surface it. This is the seed of Build #5's DREAM consolidation (→ ch. 18).
"""
from __future__ import annotations

import datetime
import uuid
from pathlib import Path

from yurios.app import vaultgit
from yurios.app.memory.index import ChunkIndex

SUMMARISE_SYSTEM = """\
You maintain the rolling summary of an ongoing companionship between {char} and {user}.
Update the previous summary with the new exchanges: keep what still matters, fold in
what is new, drop what has resolved. Third person, present-continuous
("{user} is preparing for…", "they have been talking about…").
Hard cap: about {budget} tokens. Return ONLY the updated summary text."""


async def update_summary(utility, *, prev_summary: str, exchanges: str,
                         char_name: str, user_name: str, budget_tokens: int) -> str:
    system = SUMMARISE_SYSTEM.format(char=char_name, user=user_name,
                                     budget=budget_tokens)
    return (await utility.complete([
        {"role": "system", "content": system},
        {"role": "user", "content":
            f"Previous summary:\n\n{prev_summary or '(none yet)'}\n\n---\n"
            f"New exchanges:\n\n{exchanges}"},
    ])).strip()


def write_summary(vault: Path, text: str, index: ChunkIndex, embedder) -> None:
    """Write memory/summary.md (atomic, committed with the turn) and index it
    as a recallable chunk (§7.3)."""
    path = Path(vault) / "memory" / "summary.md"
    vaultgit.atomic_write(path, text.rstrip() + "\n")
    index.upsert(
        id=f"summary-{uuid.uuid4()}",
        kind="summary",
        source_path="memory/summary.md",
        source_span="1-",
        text=text,
        embedding=embedder.embed([text])[0],
        created_at=datetime.datetime.now(datetime.UTC).isoformat(),
        salience=2.0,
    )
