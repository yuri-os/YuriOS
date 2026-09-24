"""The timer board (SPEC §7.5) — the host schedules the wake, not the MCP server.

`set_timer`'s MCP call validates and returns the contract (`seconds`, `due`);
*this* object owns the actual countdown, on the injected clock, because only the
host owns her voice — when a timer lands it becomes a `timer` signal on the
SignalBus, and the mind announces it (SPEC §15.5): a timer is a promise, so the
announcement queues until it can actually be delivered.

**And a promise outlives the process.** The board used to hold its countdowns in
a list and nothing else, so every pending timer died with the server — a
restart, a crash, a `yurios configure`. That cost little while the ceiling was
three hours and is untenable at a day (`TIMER_MAX_MINUTES`): "wake me at seven"
set at midnight has a whole night to be restarted through. So the board is
**written to `<vault>/state/timers.json` on every change and read back at
construction**, which is the same bookkeeping-on-disk the inbox keeps, for the
same reason and with the same rules — untracked (it churns on every countdown
and what she *said* about a timer is already in the journal), best-effort (a
board that cannot be saved is still a board), and atomic (`vaultgit.atomic_write`
is plain file IO, not one of the git-shelling `BLOCKING` calls, so it is safe on
the event loop).

A restored timer needs no catching-up logic, because `due` is an absolute wall
epoch rather than a countdown: one that came due while she was down is simply
already due, and the first `poll` lands it like any other. What the signal adds
is `late_s`, so her announcement can say *"that was two hours ago"* instead of
"just finished" — a promise kept late is still kept, but it must not be
described as punctual.

`STALE_AFTER_S` is the one thing dropped on the way in: past it a timer is older
than the longest one anybody can set, so it is not a promise still owed but a
leftover from some earlier era of the process, and announcing a fortnight of
them at boot is noise rather than delivery.

**A landed timer stays on the file until it is delivered.** Landing is not
keeping: between `poll` and the announcement the promise crosses the `due`
queue, the SignalBus and the mind's announce queue, all in memory, and waits
there as long as a conversation is running or a line will not compose. The
board used to drop a timer from the file the moment it landed, so a restart
anywhere in that stretch kept it zero times. Now `poll` only marks it `landed`
— out of the public snapshot, off the next poll, still on disk — and the mind's
`ack` after delivery is what removes it. A boot restores it as already due, and
the first poll lands it again. A crash between delivering and `ack` announces it
twice, which is a small window and the better failure than never.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ...app import vaultgit
from ...kernel.clock import Clock

log = logging.getLogger("world.timers")

#: How late a restored timer may be and still be worth announcing. A day, which
#: is `TIMER_MAX_MINUTES`' own ceiling: anything further past due was already
#: overtaken by a timer you could have set from scratch since.
STALE_AFTER_S = 86400.0

GITIGNORE = (
    "# pending countdowns, not memory: scheduling state that changes every time\n"
    "# a timer is set or lands. What she *said* when one finished is already in\n"
    "# the journal and the corpus — committing this would put one entry per\n"
    "# countdown in the Vault's history.\n"
    "timers.json\n")


@dataclass
class Timer:
    id: str
    label: str
    due: float                       # clock seconds (wall epoch, so it survives)
    #: On the announcement queue, not yet delivered — kept on the file, left out
    #: of the snapshot. Never written: a restored one simply lands again.
    landed: bool = False


@dataclass
class TimerBoard:
    clock: Clock
    #: Where the board is kept between processes. None — the bare-runtime tests,
    #: a config with no Vault — is a working in-memory board, exactly as before.
    vault: Path | None = None
    _timers: list[Timer] = field(default_factory=list)
    # elapsed timers, drained by the mind's SENSE into `timer` signals (§15.5)
    due: asyncio.Queue = field(default_factory=asyncio.Queue)
    _wake: asyncio.Event = field(default_factory=asyncio.Event)
    on_change: Callable[[], None] | None = None

    def __post_init__(self) -> None:
        self.vault = Path(self.vault) if self.vault else None
        self._timers.extend(self._load())

    # ---- the countdown -------------------------------------------------------

    def add(self, *, id: str, label: str, seconds: float) -> Timer:
        t = Timer(id=id, label=label, due=self.clock.now() + seconds)
        self._timers.append(t)
        self._save()
        self._wake.set()             # re-plan the sleep: a nearer deadline may exist
        self._changed()
        return t

    def pending(self) -> list[Timer]:
        """The countdowns still running — a landed one is no longer pending."""
        return sorted((t for t in self._timers if not t.landed),
                      key=lambda t: t.due)

    def snapshot(self) -> dict:
        """The public, character-scoped countdown state (SPEC §7.5, §10)."""
        return {"timers": [{"id": t.id, "label": t.label, "due": t.due}
                           for t in self.pending()]}

    def poll(self) -> list[Timer]:
        """Move every elapsed timer onto the announcement queue. Deterministic —
        the sim-time tests drive this directly (SPEC §27)."""
        now = self.clock.now()
        landed = sorted((t for t in self._timers if not t.landed and t.due <= now),
                        key=lambda t: t.due)
        for t in landed:
            # Still on the file: only `ack` takes it off, after delivery. Off
            # the file at this point was the promise kept zero times by any
            # restart before the mind got to say it.
            t.landed = True
            self.due.put_nowait(t)
        if landed:
            self._changed()
        return landed

    def ack(self, id: str, due: float | None = None) -> bool:
        """The announcement was delivered: the promise is kept, off the board.

        `due` picks the one countdown when two share an id — a `set_timer`
        result without one lands here as the default `"t"` (world/brain.py).
        """
        for t in self._timers:
            if t.landed and t.id == id and (due is None or t.due == due):
                self._timers.remove(t)
                self._save()
                return True
        return False

    def _changed(self) -> None:
        if self.on_change is not None:
            self.on_change()

    async def run(self) -> None:
        """Production loop: sleep to the nearest deadline, wake early on add."""
        while True:
            self.poll()
            now = self.clock.now()
            wait = min((t.due - now for t in self.pending()), default=60.0)
            self._wake.clear()
            await self.clock.sleep(max(0.05, min(wait, 60.0)), wake=self._wake)

    # ---- the file ------------------------------------------------------------

    @property
    def path(self) -> Path | None:
        return self.vault / "state" / "timers.json" if self.vault else None

    def _load(self) -> list[Timer]:
        """What was still pending when the process went away, minus the stale.

        An unreadable board is an empty one, the inbox's rule: refusing to start
        her because a scheduling file was hand-edited or half-written would be
        the worse bug, and the next `add` rewrites it whole.
        """
        path = self.path
        if path is None or not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("timer board at %s is unreadable; starting empty",
                        path, exc_info=True)
            return []
        rows = data.get("timers") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            return []
        floor = self.clock.now() - STALE_AFTER_S
        out: list[Timer] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                due = float(row["due"])
            except (KeyError, TypeError, ValueError):
                continue
            if not math.isfinite(due):
                continue
            if due < floor:
                log.info("dropping a timer %.0fh past due: %r",
                         (self.clock.now() - due) / 3600.0, row.get("label"))
                continue
            out.append(Timer(id=str(row.get("id") or ""),
                             label=str(row.get("label") or ""), due=due))
        return out

    def _save(self) -> None:
        """Best-effort, like the inbox's: a board that cannot be written is
        still a board, and a timer you can't persist is better than a refused
        `set_timer`."""
        path = self.path
        if path is None:
            return
        try:
            state = path.parent
            state.mkdir(parents=True, exist_ok=True)
            ignore = state / ".gitignore"
            existing = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
            if "timers.json" not in existing:
                # Append rather than overwrite: `state/` already carries the
                # inbox's and the conversation's lines (world/inbox.py).
                vaultgit.atomic_write(ignore, existing + GITIGNORE)
            vaultgit.atomic_write(path, json.dumps(
                {"timers": [{"id": t.id, "label": t.label, "due": t.due}
                            for t in sorted(self._timers, key=lambda t: t.due)]},
                indent=1))
        except OSError:
            log.warning("couldn't save the timer board to %s", path, exc_info=True)
