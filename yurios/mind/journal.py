"""The journal (SPEC §24.1) — the surface that converts autonomy from creepy
to an inner life.

Her autonomous acts write into the *same* episodic day files the conversation
does (`memory/episodic/YYYY-MM-DD.md`), as `[she]` lines — one journal, two
authors, which is what lets DREAM consolidate a day of talk and a day of her
own doing in one pass, and what makes "what did you do while I was gone?"
answerable by opening one file. Each line is also embedded into the memory
index (she can recall her own past acts) and published as a `journal` event on
the outbound bus, so the inner-life panel updates live.

The journal is the default destination of initiative: the interrupt model's
SILENT outcome doesn't discard the act — it journals it. The value of a quiet
companion lives here, not in notifications.
"""
from __future__ import annotations

import logging

from yurios.world.clock import Clock
from yurios.world.hub import EventHub

from .util import day_of, dt_of, iso_of, utc_iso_of
from .vaultio import MindVault

log = logging.getLogger("mind.journal")


class Journal:
    def __init__(self, vault: MindVault, clock: Clock, hub: EventHub, *,
                 store=None):
        self.vault = vault
        self.clock = clock
        self.hub = hub
        self.store = store               # the FileMemoryStore (index)

    def write(self, text: str, *, kind: str = "act") -> None:
        """One journal line: the day file, the memory index, the live event."""
        now = self.clock.now()
        day = day_of(now)
        rel = f"memory/episodic/{day}.md"
        if not (self.vault.vault / rel).exists():
            self.vault.append(rel, f"# Journal — {day}\n\n")
        line = f"### {dt_of(now).strftime('%H:%M')}  [she] {text}\n"
        self.vault.append(rel, line)
        if self.store is not None:
            try:
                self.store.index.upsert(
                    id=f"act-{iso_of(now)}-{abs(hash(text)) % 10 ** 6}",
                    kind="event", text=text, source_path=rel, source_span="",
                    embedding=self.store.embedder.embed([text])[0],
                    created_at=utc_iso_of(now), salience=1.0)
            except Exception:  # noqa: BLE001 — the file is truth; the index is a cache
                log.debug("journal index write skipped", exc_info=True)
        self.hub.publish("journal", {"text": text, "kind": kind,
                                     "ts": iso_of(now)})

    def day_entries(self, day: str) -> list[dict]:
        """Parsed entries for one day — the /api/journal shape."""
        out = []
        for line in self.vault.read(f"memory/episodic/{day}.md").splitlines():
            if not line.startswith("### "):
                continue
            body = line[4:]
            hhmm, _, rest = body.partition("  ")
            hers = rest.startswith("[she] ")
            out.append({"time": hhmm.strip(),
                        "hers": hers,
                        "text": rest[6:] if hers else rest})
        return out
