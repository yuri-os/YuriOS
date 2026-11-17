"""The gated self-edit flow (SPEC §23) — the SOUL split, made operational.

Build #1 gave the SOUL its two halves on disk — `CONSTITUTION.md` (who she is,
immutably) and `PERSONA.md` and friends (who she's becoming, editably) — but
nothing ever wrote the editable half except you. An always-on mind will want
to: a note in her persona, a preference she's noticed, a scenario line that
stopped being true. This module is the one door those writes go through:

  low risk   (memory, world, goals, a working note)   → auto-applied, committed
  high risk  (any soul/*.md — her identity)           → queued for YOUR approval

The pending queue lives at `state/pending_edits.json` — rendered by the
inner-life panel as a diff against the file as it stands, with the full
proposed content folded under it; the decision arrives back as a
`selfedit_decision` signal the loop consumes, and an approval may carry your
own rewrite of the proposal in place of hers. Every
applied edit is a git commit, so drift is never silent: `git -C vault log`
shows every time she changed, and `git revert` undoes any of it.

The constitution is not merely high-risk — it is *out of scope*: MindVault
refuses the write unconditionally, and this flow refuses even to queue a
proposal against it. She can read every limit she runs under; she cannot hold
the pen that rewrites them.
"""
from __future__ import annotations

import difflib
import hashlib
from dataclasses import dataclass

from yurios.app.memory import partner
from yurios.characters.soulfiles import shape_complaint
from yurios.kernel.clock import Clock

from .util import iso_of, new_id, read_json, write_json
from .vaultio import ConstitutionReadOnly, MindVault

LOW_RISK_PREFIXES = ("memory/", "world/", "knowledge/")

#: The longest rewrite of a queued edit a person may hand back with an approval.
#: A soul file is a few thousand characters; this is a ceiling, not a budget.
MAX_REVISION_CHARS = 200_000


def _fingerprint(text: str) -> str:
    """Which version of a file a proposal was built against."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class SoulShapeError(ValueError):
    """A rewrite that would stop a soul file answering what `soul.yaml` asks.

    Not a privacy or permission refusal — the surface is hers to edit and the
    prose may well be better. It is that `soul.yaml` points *into* these files
    by heading and by frontmatter key, and a reference that stops resolving
    raises when her prompt is next assembled. A model handed "content is the
    whole new file" writes good prose with the headings sanded off, and the
    character then fails to **start** — which is the one outcome a self-edit
    must never be able to cause, because a bricked character cannot be talked
    to about the edit that bricked her.
    """


@dataclass
class EditResult:
    id: str
    outcome: str          # applied | queued | rejected
    surface: str
    reason: str
    revised: bool = False     # applied with your rewrite, not her proposal


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

    def _shape_complaint(self, surface: str, content: str) -> str:
        """What this rewrite would break, or "" if it breaks nothing."""
        if not surface.startswith("soul/") or not surface.endswith(".md"):
            return ""
        return shape_complaint(self.vault.vault / "soul",
                               surface.rsplit("/", 1)[-1], content)

    def propose(self, surface: str, content: str, *, reason: str,
                patch: dict | None = None) -> EditResult:
        """Apply a low-risk edit, or queue a soul one for your ruling.

        `patch` is the `{find, replace}` she sent when she changed one part of
        the file rather than rewriting it; `content` is that patch already
        applied to the file as it stood. Both are kept, together with a
        fingerprint of the file they were built against (`base`), because the
        queue is ruled on later — and by then another edit may have landed.
        """
        surface = surface.replace("\\", "/")
        if surface.endswith("soul/CONSTITUTION.md"):
            raise ConstitutionReadOnly(surface)   # not even a queued proposal
        complaint = self._shape_complaint(surface, content)
        if complaint:
            # Refused at proposal, not at approval: she is still in the
            # conversation here and can be told what she dropped, whereas an
            # approval is a click hours later on a queue entry that looked fine.
            raise SoulShapeError(complaint)
        edit_id = new_id("e")
        if self.classify(surface) == "low":
            self.vault.write(surface, content, gate=True)
            return EditResult(edit_id, "applied", surface, reason)
        entry: dict = {"id": edit_id, "surface": surface, "content": content,
                       "reason": reason, "base": _fingerprint(self.vault.read(surface)),
                       "proposed_at": iso_of(self.clock.now())}
        if (isinstance(patch, dict) and isinstance(patch.get("find"), str)
                and patch["find"] and isinstance(patch.get("replace", ""), str)):
            entry["patch"] = {"find": patch["find"],
                              "replace": patch.get("replace") or ""}
        pending = read_json(self.pending_path, []) or []
        pending.append(entry)
        write_json(self.pending_path, pending)
        self.vault.mark_dirty()
        return EditResult(edit_id, "queued", surface, reason)

    # ----------------------------------------------------------- what applies

    def realised(self, entry: dict) -> tuple[str, str]:
        """What approving `entry` as she wrote it would write now, and why it
        can't be approved as written ("" when it can).

        A queued proposal was built against the file as it was when she asked.
        Approve two in a row and the second, built against the file without
        the first, used to write the first one back out — silently, because
        each looked like a one-line change when it was proposed. So:

          * the file is unchanged since she asked → her content, as queued;
          * it changed, and she sent a patch → the patch again, against the
            file as it is now, as long as her `find` still appears once and
            the result still passes the shape check;
          * it changed, and she sent the whole file → stale: approving it
            would undo whatever landed in between.

        An entry queued before `base` was recorded is taken as written, which
        is how it would have been applied before.
        """
        content = str(entry.get("content", ""))
        base = entry.get("base")
        if not base:
            return content, ""
        current = self.vault.read(entry["surface"])
        if _fingerprint(current) == base:
            return content, ""
        patch = entry.get("patch")
        if not isinstance(patch, dict):
            return content, ("the file has changed since she wrote this version of "
                             "it — approving it as written would undo that change")
        find = str(patch.get("find", ""))
        hits = current.count(find) if find else 0
        if hits != 1:
            return content, (
                "the file has changed since she asked, and the passage she "
                "changed is no longer in it" if not hits else
                f"the file has changed since she asked, and the passage she "
                f"changed now appears {hits} times")
        fresh = current.replace(find, str(patch.get("replace", "")), 1)
        complaint = self._shape_complaint(entry["surface"], fresh)
        if complaint:
            return content, f"applied to the file as it is now, {complaint}"
        return fresh, ""

    def stale(self, edit_id: str) -> str:
        """Why the queued edit can't be approved as she wrote it, "" if it can.
        Raises KeyError for an edit no longer waiting."""
        entry = next((p for p in self.pending() if p["id"] == edit_id), None)
        if entry is None:
            raise KeyError(edit_id)
        return self.realised(entry)[1]

    # ----------------------------------------------------------------- decide

    def pending(self) -> list[dict]:
        return read_json(self.pending_path, []) or []

    def pending_view(self) -> list[dict]:
        """The queue as the inner-life panel reads it (SPEC §24.3): each entry
        plus `diff`, the changed lines against the file as it stands.

        A persona proposal is the whole new file, and what it changes is two
        lines at the bottom of it. Showing the file meant showing its
        frontmatter and its unchanged opening, cut off long before the change —
        so the one thing you are asked to rule on was the thing you could not
        see. The hunk headers are dropped: line numbers are the plumbing's."""
        out = []
        for entry in self.pending():
            current = self.vault.read(entry["surface"])
            content, stale = self.realised(entry)
            patch = entry.get("patch") if isinstance(entry.get("patch"), dict) else None
            if stale and patch:
                # Her patch no longer lands: show what she asked to change, not
                # a diff of a file built on a version that is gone.
                lines = ([f"-{x}" for x in str(patch.get("find", "")).splitlines()]
                         + [f"+{x}" for x in str(patch.get("replace", "")).splitlines()])
            else:
                diff = difflib.unified_diff(
                    current.splitlines(), content.splitlines(), n=2, lineterm="")
                lines = [line for line in diff
                         if not line.startswith(("---", "+++"))]
            out.append({**entry, "content": content, "stale": stale or None,
                        # what your own version starts from: hers when it still
                        # applies or is whole, the file as it is when her patch
                        # no longer lands on it
                        "draft": current if stale and patch else content,
                        "diff": ["…" if line.startswith("@@") else line
                                 for line in lines]})
        return out

    def check_revision(self, edit_id: str, content: str) -> None:
        """Would your rewrite of a queued edit be accepted? Raises KeyError for
        an edit no longer waiting, ValueError past the size ceiling, and
        `SoulShapeError` for a rewrite that drops what `soul.yaml` points at —
        the same rule her own proposal passed, because a character your edit
        bricked cannot start any more than one hers did. Asked by the route
        before the approval is posted, so the refusal reaches you, not a tick."""
        entry = next((p for p in self.pending() if p["id"] == edit_id), None)
        if entry is None:
            raise KeyError(edit_id)
        if len(content) > MAX_REVISION_CHARS:
            raise ValueError(f"that rewrite is {len(content):,} characters; "
                             f"the ceiling is {MAX_REVISION_CHARS:,}")
        complaint = self._shape_complaint(entry["surface"], content)
        if complaint:
            raise SoulShapeError(complaint)

    def withdraw(self, edit_id: str) -> None:
        """An obsolete proposal must no longer be approvable."""
        pending = self.pending()
        kept = [p for p in pending if p["id"] != edit_id]
        if len(kept) != len(pending):
            write_json(self.pending_path, kept)
            self.vault.mark_dirty()

    def decide(self, edit_id: str, approve: bool,
               content: str | None = None) -> EditResult | None:
        """Consume one queued edit. Called from the loop when the user's
        `selfedit_decision` signal arrives (the /api/mind/edits route posts it).

        `content` is your rewrite of the proposal, applied in place of hers.
        The route checked it; it is checked again here because the soul it was
        checked against can change before the tick — and a rewrite that no
        longer fits stays queued rather than being applied or thrown away."""
        pending = self.pending()
        entry = next((p for p in pending if p["id"] == edit_id), None)
        if entry is None:
            return None
        revised = content if approve else None
        if revised is not None and self._shape_complaint(entry["surface"], revised):
            return None
        applied = ""
        if approve and revised is None:
            # Hers, as it lands on the file NOW — a stale one stays queued for
            # you to reject or rewrite, rather than undoing what came between.
            applied, stale = self.realised(entry)
            if stale:
                return None
        write_json(self.pending_path, [p for p in pending if p["id"] != edit_id])
        self.vault.mark_dirty()
        if entry["surface"] == "soul/PERSONA.md":
            delta = partner.read_persona_delta(self.vault.vault) or {}
            # A click can reach SENSE before housekeeping withdraws the old
            # proposal. A correction already on disk makes that click stale.
            if not delta.get("queued", True) and (
                    delta.get("edit_id") == edit_id
                    or delta.get("superseded_reason") == entry["reason"]):
                return None
        if not approve:
            return EditResult(edit_id, "rejected", entry["surface"], entry["reason"])
        self.vault.write(entry["surface"],
                         revised if revised is not None else applied,
                         gate=True)
        return EditResult(edit_id, "applied", entry["surface"], entry["reason"],
                          revised=revised is not None)
