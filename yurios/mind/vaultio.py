"""The mind's write path into the Vault (SPEC §15.2, §23.1).

Every durable change the loop makes goes through this object, which enforces
the two rules that make a self-modifying agent shippable rather than terrifying:

  * **The constitution is read-only, even to her.** `soul/CONSTITUTION.md` is
    refused unconditionally — not even a queued proposal may target it. If the
    constraints are editable by the thing they constrain, they are not
    constraints.
  * **Identity surfaces route through the gate.** The other `soul/*.md` files
    are editable, but only with the `gate=True` token the self-edit flow
    (mind/selfedit.py) holds — a store or a stray ACT can't quietly become who
    she is.

Writes reuse the Build #1 atomic-write discipline, and the loop calls
`commit_if_dirty()` once per tick: exactly one commit per tick that changed
anything; an uneventful tick commits nothing, and that is not an error.

"Changed anything" is meant literally. A write whose content matches what is
already on disk is not a change — it is a glance — and it neither touches the
file nor sets the dirty flag. Without that, any caller that re-saves unchanged
state each tick (a world snapshot, a progress ledger) turns the loop into a
commit-per-heartbeat machine: git sees no diff in the file that was rewritten,
but the commit still fires and sweeps up whatever else happens to be in the
working tree. Callers should therefore route state files through `write_json()`
rather than saving them behind the vault's back and calling `mark_dirty()`.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from yurios.app import vaultgit

log = logging.getLogger("mind.vault")


class ConstitutionReadOnly(PermissionError):
    def __init__(self, rel: str):
        super().__init__(f"the constitution is read-only, even to her: {rel}")


class MindVault:
    """All the mind's Vault mutations; identity writes carry a gate token."""

    EDITABLE_SOUL = {"PERSONA.md", "SCENARIO.md", "EXAMPLES.md", "WORLD.md",
                     "NOTES.md", "USER.md", "MEMORY.md", "BOOTSTRAP.md"}

    def __init__(self, vault: Path):
        self.vault = Path(vault)
        self._dirty = False

    def _check(self, rel: str, *, gate: bool) -> Path:
        p = (self.vault / rel).resolve()
        if not str(p).startswith(str(self.vault.resolve())):
            raise PermissionError(f"path escapes the vault: {rel}")
        parts = p.relative_to(self.vault.resolve()).parts
        if parts and parts[0] == "soul":
            name = parts[-1] if len(parts) > 1 else ""
            if name == "CONSTITUTION.md":
                raise ConstitutionReadOnly(rel)
            if len(parts) == 2 and name in self.EDITABLE_SOUL and not gate:
                raise PermissionError(
                    f"identity surface {name} requires the gated self-edit flow")
        return p

    def write(self, rel: str, content: str, *, gate: bool = False) -> Path:
        p = self._check(rel, gate=gate)
        if p.exists() and p.read_text(encoding="utf-8") == content:
            return p                       # a glance, not a change
        vaultgit.atomic_write(p, content)
        self._dirty = True
        return p

    def write_json(self, rel: str, obj: Any, *, gate: bool = False) -> Path:
        """A state file, in the one JSON shape the Vault uses everywhere
        (mind/util.write_json). Change-detecting, like every other write."""
        return self.write(
            rel, json.dumps(obj, ensure_ascii=False, indent=2) + "\n", gate=gate)

    def append(self, rel: str, content: str, *, gate: bool = False) -> Path:
        p = self._check(rel, gate=gate)
        if not content:
            return p                       # appending nothing is not a change
        vaultgit.atomic_append(p, content)
        self._dirty = True
        return p

    def read(self, rel: str, default: str = "") -> str:
        p = self.vault / rel
        return p.read_text(encoding="utf-8") if p.exists() else default

    def mark_dirty(self) -> None:
        self._dirty = True

    def commit_if_dirty(self, message: str) -> None:
        """One commit per dirty tick (SPEC §15.1). Uses the Build #1
        git spine; a Vault that isn't a repo (bare tests) is tolerated."""
        if not self._dirty:
            return
        self._dirty = False
        try:
            vaultgit.commit(self.vault, message)
        except Exception:  # noqa: BLE001 — never let bookkeeping kill the loop
            log.debug("vault commit skipped (not a repo?)", exc_info=True)
