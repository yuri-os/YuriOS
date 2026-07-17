"""The gated self-edit flow (SPEC §23) — the SOUL split, made operational.

Build #1 gave the SOUL its two halves on disk — `CONSTITUTION.md` (who she is,
immutably) and `PERSONA.md` and friends (who she's becoming, editably) — but
nothing ever wrote the editable half except you. An always-on mind will want
to: a note in her persona, a preference she's noticed, a scenario line that
stopped being true. This module is the one door those writes go through:

  low risk   (memory, world, goals, a working note)   → auto-applied, committed
  high risk  (any soul/*.md — her identity)           → queued for YOUR approval

The pending queue lives at `state/pending_edits.json` — rendered by the
inner-life panel with the full proposed content and a diff-shaped reason; the
decision arrives back as a `selfedit_decision` signal the loop consumes. Every
applied edit is a git commit, so drift is never silent: `git -C vault log`
shows every time she changed, and `git revert` undoes any of it.

The constitution is not merely high-risk — it is *out of scope*: MindVault
refuses the write unconditionally, and this flow refuses even to queue a
proposal against it. She can read every limit she runs under; she cannot hold
the pen that rewrites them.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from yurios.world.clock import Clock

from .util import iso_of, new_id, read_json, write_json
from .vaultio import ConstitutionReadOnly, MindVault

LOW_RISK_PREFIXES = ("memory/", "world/", "knowledge/")


@dataclass
class EditResult:
    id: str
    outcome: str          # applied | queued | rejected
    surface: str
    reason: str


class SelfEdit:
    def __init__(self, vault: MindVault, clock: Clock):
        self.vault = vault
        self.clock = clock
        self.pending_path = vault.vault / "state" / "pending_edits.json"

    # ---------------------------------------------------------------- propose

    def classify(self, surface: str) -> str:
        if surface.startswith(LOW_RISK_PREFIXES) or surface == "goals.md":
            return "low"
        return "high"     # soul/* and every unknown surface fail safe to the queue

    def propose(self, surface: str, content: str, *, reason: str) -> EditResult:
        surface = surface.replace("\\", "/")
        if surface.endswith("soul/CONSTITUTION.md"):
            raise ConstitutionReadOnly(surface)   # not even a queued proposal
        edit_id = new_id("e")
        if self.classify(surface) == "low":
            self.vault.write(surface, content, gate=True)
            return EditResult(edit_id, "applied", surface, reason)
        pending = read_json(self.pending_path, []) or []
        pending.append({"id": edit_id, "surface": surface, "content": content,
                        "reason": reason,
                        "proposed_at": iso_of(self.clock.now())})
        write_json(self.pending_path, pending)
        self.vault.mark_dirty()
        return EditResult(edit_id, "queued", surface, reason)

    # ----------------------------------------------------------------- decide

    def pending(self) -> list[dict]:
        return read_json(self.pending_path, []) or []

    def decide(self, edit_id: str, approve: bool) -> EditResult | None:
        """Consume one queued edit. Called from the loop when the user's
        `selfedit_decision` signal arrives (the /api/mind/edits route posts it)."""
        pending = self.pending()
        entry = next((p for p in pending if p["id"] == edit_id), None)
        if entry is None:
            return None
        write_json(self.pending_path, [p for p in pending if p["id"] != edit_id])
        self.vault.mark_dirty()
        if not approve:
            return EditResult(edit_id, "rejected", entry["surface"], entry["reason"])
        self.vault.write(entry["surface"], entry["content"], gate=True)
        return EditResult(edit_id, "applied", entry["surface"], entry["reason"])
