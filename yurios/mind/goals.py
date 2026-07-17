"""Goals & intentions (SPEC §22) — `goals.md` is the store, human-readable.

This is what gives the background loop direction; without it an always-on
agent is a screensaver. Every goal carries **provenance** (who created it,
from what), a **commitment strategy** (how hard it defends itself against a
changing world), and a lifecycle: pending → active → waiting → done|abandoned.

Goal genesis is designed, not assumed — a store only the user writes to
starves the loop within weeks. The sources, stamped on every goal:
  * `user:*`     — explicit asks ("remind me to…").
  * `promise:*`  — REFLECT scans her own replies for commitments she made
    ("I'll look into that") and files each one; a companion who forgets her
    own promises is worse than one who forgets yours.
  * `maintenance:*` — DREAM backlog, knowledge drops.
The file itself is a markdown checklist: `cat vault/goals.md` reads as her
to-do list, because it is one.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from yurios.world.clock import Clock

from .util import iso_of, new_id, ts_of_iso
from .vaultio import MindVault

COMMITMENTS = ("blind", "single-minded", "open-minded")


@dataclass
class Goal:
    id: str
    text: str
    kind: str = "task"          # reach_out | task | maintenance
    priority: float = 0.5
    due: str | None = None      # ISO — when this becomes time-sensitive
    commitment: str = "single-minded"
    provenance: str = "user"    # source[:detail]
    state: str = "pending"      # pending | active | waiting | done | abandoned
    created: str = ""
    meta: dict = field(default_factory=dict)

    def is_due(self, clock: Clock, horizon_hours: float = 12.0) -> bool:
        if self.due is None:
            return False
        return (ts_of_iso(self.due) - clock.now()) / 3600 <= horizon_hours

    def is_stale(self, clock: Clock) -> bool:
        """Past due — whether it is defended or dropped is the commitment's call."""
        return self.due is not None and ts_of_iso(self.due) < clock.now()


LINE_RE = re.compile(r"^- \[(?P<done>[ x~])\] \((?P<id>[\w-]+)\) (?P<text>.*?)"
                     r"(?P<fields>(?: \| \w[\w-]*: [^|]*)*)$")


class GoalStore:
    def __init__(self, vault: MindVault, clock: Clock):
        self.vault = vault
        self.clock = clock

    # ------------------------------------------------------------------ parse

    def all(self) -> list[Goal]:
        goals = []
        for line in self.vault.read("goals.md").splitlines():
            m = LINE_RE.match(line.strip())
            if not m:
                continue
            fields = dict(re.findall(r"\| (\w[\w-]*): ([^|]*)", m.group("fields")))
            state = fields.get("state", "").strip()
            if not state:
                state = {"x": "done", "~": "abandoned"}.get(m.group("done"), "pending")
            goals.append(Goal(
                id=m.group("id"), text=m.group("text").strip(),
                kind=fields.get("kind", "task").strip(),
                priority=float(fields.get("priority", 0.5)),
                due=fields.get("due", "").strip() or None,
                commitment=fields.get("commit", "single-minded").strip(),
                provenance=fields.get("from", "user").strip(),
                state=state, created=fields.get("created", "").strip(),
                meta=_parse_meta(fields.get("meta", ""))))
        return goals

    def open_goals(self) -> list[Goal]:
        return [g for g in self.all() if g.state in ("pending", "active", "waiting")]

    def get(self, goal_id: str) -> Goal | None:
        return next((g for g in self.all() if g.id == goal_id), None)

    # ------------------------------------------------------------------ write

    def _render(self, goals: list[Goal]) -> str:
        lines = ["# Goals", ""]
        for g in goals:
            box = {"done": "x", "abandoned": "~"}.get(g.state, " ")
            parts = [f"- [{box}] ({g.id}) {g.text}",
                     f"kind: {g.kind}", f"priority: {g.priority}"]
            if g.due:
                parts.append(f"due: {g.due}")
            parts += [f"commit: {g.commitment}", f"from: {g.provenance}",
                      f"state: {g.state}", f"created: {g.created}"]
            if g.meta:
                parts.append(f"meta: {json.dumps(g.meta, ensure_ascii=False)}")
            lines.append(" | ".join(parts))
        return "\n".join(lines) + "\n"

    def _save(self, goals: list[Goal]) -> None:
        self.vault.write("goals.md", self._render(goals))

    def add(self, text: str, *, kind: str = "task", priority: float = 0.5,
            due: str | None = None, commitment: str = "single-minded",
            provenance: str = "user", meta: dict | None = None) -> Goal:
        goals = self.all()
        for existing in goals:                 # skip near-duplicates of open goals
            if (existing.state in ("pending", "active", "waiting")
                    and existing.text.lower() == text.lower()):
                return existing
        g = Goal(id=new_id("g"), text=text, kind=kind, priority=priority, due=due,
                 commitment=commitment, provenance=provenance,
                 created=iso_of(self.clock.now()), meta=meta or {})
        goals.append(g)
        self._save(goals)
        return g

    def set_state(self, goal_id: str, state: str) -> None:
        goals = self.all()
        for g in goals:
            if g.id == goal_id:
                g.state = state
        self._save(goals)

    def reconsider(self) -> list[Goal]:
        """Apply commitment strategies to stale goals (SPEC §22.2): blind is
        defended, open-minded drops the moment it stops being timely."""
        goals = self.all()
        changed = False
        for g in goals:
            if g.state not in ("pending", "waiting"):
                continue
            if g.is_stale(self.clock) and g.commitment == "open-minded":
                g.state = "abandoned"
                changed = True
        if changed:
            self._save(goals)
        return [g for g in goals if g.state == "abandoned"]


def _parse_meta(raw: str) -> dict:
    raw = raw.strip()
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except json.JSONDecodeError:
        return {}


# --- REFLECT's promise scan (SPEC §22.1) ---------------------------------------

PROMISE_RE = re.compile(
    r"\bI(?:'ll| will)\s+(?!never|not\b)(.{4,100}?)(?:[.!?\n]|$)", re.I)
REMIND_RE = re.compile(r"\bremind me to\s+(.{4,100}?)(?:[.!?\n]|$)", re.I)


def extract_promises(reply: str, user_msg: str) -> list[tuple[str, str]]:
    """Scan an exchange for commitments: hers, in her own words, and the
    user's explicit remind-me asks. Returns (text, provenance) pairs."""
    out = []
    for m in PROMISE_RE.finditer(reply or ""):
        out.append((m.group(1).strip().rstrip(","), "promise:her-own-words"))
    for m in REMIND_RE.finditer(user_msg or ""):
        out.append((m.group(1).strip().rstrip(","), "user:remind-me"))
    return out[:3]
