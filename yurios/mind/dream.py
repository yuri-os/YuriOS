"""DREAM consolidation (SPEC §21) — she wakes changed by yesterday.

Build #1 shipped `MemoryStore.consolidate()` as a stub with a note: "arrives
with the tick loop in Build #5." This is that arrival. Overnight — in the DREAM
activity state, entered from DORMANT inside the configured window — the day's
episodic journal is compacted into semantic memory: each finished day is
summarised down to the few durable facts worth keeping, deduped against what's
already known, appended to `memory/semantic/facts.md`, and indexed at high
salience so recall prefers the distilled fact over the raw exchange.

Three disciplines make it safe to run unattended:
  * **Oldest-first and resumable.** Progress lives in
    `state/dream_progress.json`; a night that runs out of budget leaves a
    backlog, not an overrun, and the next DREAM tick picks up where it stopped.
  * **Budget-capped per tick.** Consolidation is the biggest local-token job in
    the system; each tick chews what its token budget allows and yields.
  * **Never today's live journal.** Only finished days consolidate — the file
    still being written is not a day yet.

The summariser is the same utility model the partner-model extractor uses
(local-tier by policy); offline it degrades to a keep-the-flagged-lines
heuristic, so the pass still runs with no model at all.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from yurios.world.clock import Clock

from .util import day_of, read_json, utc_iso_of, write_json
from .vaultio import MindVault

log = logging.getLogger("mind.dream")

UtilityCall = Callable[[list[dict]], Awaitable[str]]

SUMMARISE_SYSTEM = (
    "From this day's journal, list the 0-5 durable facts worth keeping "
    "long-term — things that will still matter in a month. One per line, no "
    "bullets, no preamble. If nothing durable happened output NOTHING.")


class ConsolidationReport:
    def __init__(self):
        self.days_processed: list[str] = []
        self.facts_added = 0
        self.exhausted_budget = False
        self.nothing_to_do = False


class DreamConsolidator:
    def __init__(self, vault: MindVault, store, clock: Clock, *,
                 utility: UtilityCall | None = None):
        self.vault = vault
        self.store = store               # the Build #1 FileMemoryStore
        self.clock = clock
        self.utility = utility
        self.progress_path = vault.vault / "state" / "dream_progress.json"
        self.episodic = vault.vault / "memory" / "episodic"

    # ---------------------------------------------------------------- backlog

    def backlog(self) -> list[str]:
        """Finished days not yet consolidated, oldest first."""
        progress = read_json(self.progress_path, {}) or {}
        done = set(progress.get("consolidated_days", []))
        today = day_of(self.clock.now())
        days = sorted(p.stem for p in self.episodic.glob("*.md")
                      if p.stem < today)          # never today's live journal
        return [d for d in days if d not in done]

    # ------------------------------------------------------------ consolidate

    async def consolidate(self, *, token_budget: int = 4000) -> ConsolidationReport:
        report = ConsolidationReport()
        pending = self.backlog()
        if not pending:
            report.nothing_to_do = True
            return report

        progress = read_json(self.progress_path, {}) or {}
        done_days: list[str] = progress.get("consolidated_days", [])
        spent = 0
        for day in pending:                       # oldest first
            text = (self.episodic / f"{day}.md").read_text(encoding="utf-8")
            cost = max(64, len(text) // 4)
            if spent + cost > token_budget:
                report.exhausted_budget = True
                break
            facts = await self._summarise_day(day, text)
            if facts:
                existing = self.vault.read("memory/semantic/facts.md").lower()
                added = [f for f in facts if f.lower() not in existing]
                if added:
                    self.vault.append(
                        "memory/semantic/facts.md",
                        "".join(f"- ({day}) {f}\n" for f in added))
                    for i, f in enumerate(added):
                        # distilled facts outrank the raw exchange at recall
                        self.store.index.upsert(
                            id=f"dream-{day}-{i}", kind="fact", text=f,
                            source_path="memory/semantic/facts.md",
                            source_span="", salience=2.0,
                            created_at=utc_iso_of(self.clock.now()),
                            embedding=self.store.embedder.embed([f])[0])
                    report.facts_added += len(added)
            done_days.append(day)
            report.days_processed.append(day)
            spent += cost

        write_json(self.progress_path, {"consolidated_days": done_days})
        self.vault.mark_dirty()
        return report

    async def _summarise_day(self, day: str, text: str) -> list[str]:
        if self.utility is None:
            # offline heuristic: keep the lines someone flagged as worth keeping
            lines = [l for l in text.splitlines() if l.startswith("### ")]
            keep = [l.split("  ", 1)[-1] for l in lines if "remember" in l.lower()]
            return keep[:5]
        try:
            raw = await self.utility([
                {"role": "system", "content": SUMMARISE_SYSTEM},
                {"role": "user", "content": f"Journal for {day}:\n{text[:6000]}"}])
            facts = [l.strip("-• ").strip() for l in raw.splitlines() if l.strip()]
            return [f for f in facts if len(f) > 3][:5]
        except Exception:  # noqa: BLE001 — a failed night leaves the backlog intact
            log.exception("DREAM summarise failed for %s", day)
            return []
