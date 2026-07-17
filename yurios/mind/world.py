"""WorldModelStore (SPEC §19) — the present tense, promoted from a rendering to
a store.

Build #4's situation block was a *rendering* of host state: honest, but with no
beliefs, no expectations, no memory of what was true when. This store is the
organ ch. 19 names: SENSE writes it (`observe`), APPRAISE scores salience
against it (surprise), DECIDE plans over it, and every prompt is built from its
`situation()` — which still *includes* the Build #4 host lines (the injected
clock, the embodiment truth, the room's sticky scene state, the pending timers)
via the same `render_situation` renderer, so the block's place in the prompt is
the seam that survived, exactly as promised.

Three disciplines:
  * **Beliefs, not facts.** Everything here is a time-stamped guess with a
    confidence; nothing hardens into durable memory without corroboration.
  * **Expectations score as surprise.** `expect()` stores a checkable belief
    about what comes next; a later `observe()` that meets it resolves quietly,
    one that finds it past due produces prediction-error — the cheapest good
    salience signal there is.
  * **The snapshot is a file.** `world/situation.md` in the Vault is what she
    believes is the case right now — `cat`-able, diffable, hers and yours.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from yurios.world.avatar.controller import VrmController
from yurios.world.clock import Clock
from yurios.world.situation import render_situation
from yurios.world.tools.timers import TimerBoard

from .signals import Signal
from .util import iso_of, jsonl_append, jsonl_read, read_json, ts_of_iso, write_json
from .vaultio import MindVault


@dataclass
class UpdateResult:
    surprises: list[dict] = field(default_factory=list)   # violated expectations
    resolved: list[str] = field(default_factory=list)     # expectations quietly met


@dataclass
class Fact:
    subject: str
    belief: str
    confidence: float
    ts: str
    kind: str = "belief"     # belief | expectation


class WorldModelStore:
    def __init__(self, vault: MindVault, clock: Clock, *,
                 controller: VrmController, timers: TimerBoard,
                 user_name: str = "you"):
        self.vault = vault
        self.clock = clock
        self.controller = controller
        self.timers = timers
        self.user_name = user_name
        self.state_path = vault.vault / "world" / "state.json"
        self.beliefs_path = vault.vault / "world" / "beliefs.jsonl"

    # ------------------------------------------------------------------ state

    def _state(self) -> dict:
        return read_json(self.state_path, None) or {
            "user_present": False, "last_user_message": None,
            "last_contact_out": None, "threads": [], "expectations": [],
        }

    def _save(self, st: dict) -> None:
        write_json(self.state_path, st)
        self.vault.mark_dirty()

    # ---------------------------------------------------------------- observe

    def observe(self, signal: Signal) -> UpdateResult:
        st = self._state()
        now_iso = iso_of(self.clock.now())
        res = UpdateResult()

        if signal.type in ("user_message", "turn_committed"):
            st["user_present"] = True
            st["last_user_message"] = now_iso
            text = signal.payload.get("text", "")
            if text:
                self._belief("user", f"said: {text[:120]}", 1.0)
        elif signal.type == "user_present":
            st["user_present"] = True
        elif signal.type == "user_absent":
            st["user_present"] = False
        elif signal.type == "task_completion":
            name = signal.payload.get("task", "a task")
            st["threads"] = [t for t in st["threads"] if t.get("task") != name]
            self._belief("work", f"finished: {name}", 1.0)
        elif signal.type == "suspend_gap":
            hours = signal.payload.get("hours", 0)
            self._belief("machine", f"the machine slept ~{hours:.1f}h", 1.0)

        # score open expectations against this observation (SPEC §19.3)
        still_open = []
        text_l = str(signal.payload.get("text", "")).lower()
        for exp in st["expectations"]:
            keys = [k.lower() for k in exp.get("keys", [])]
            if (signal.type in ("user_message", "turn_committed") and keys
                    and any(k in text_l for k in keys)):
                res.resolved.append(exp["text"])
                self._belief("world", f"expectation met: {exp['text']}", 0.9)
                continue
            if exp.get("due") and ts_of_iso(exp["due"]) < self.clock.now():
                res.surprises.append(exp)   # prediction-error = surprise = salience
                self._belief("world", f"expectation violated: {exp['text']}", 0.8)
                continue
            still_open.append(exp)
        st["expectations"] = still_open
        self._save(st)
        return res

    def _belief(self, subject: str, belief: str, confidence: float,
                kind: str = "belief") -> None:
        jsonl_append(self.beliefs_path, {
            "ts": iso_of(self.clock.now()), "subject": subject, "belief": belief,
            "confidence": confidence, "kind": kind})
        self.vault.mark_dirty()

    # -------------------------------------------------------------- situation

    def situation(self) -> str:
        """The live stage every prompt is built from (SPEC §19.2): the Build #4
        host lines first (time · embodiment · scene · timers — the seam that
        survived), then what only a store can know: presence, open threads,
        what she half-expects."""
        st = self._state()
        now = self.clock.now()
        lines = [render_situation(self.clock, controller=self.controller,
                                  timers=self.timers, user_name=self.user_name)]
        if st["user_present"]:
            lines.append(f"{self.user_name} is here right now.")
        elif st["last_user_message"]:
            gap_h = (now - ts_of_iso(st["last_user_message"])) / 3600
            if gap_h < 1:
                lines.append(f"{self.user_name} was here minutes ago.")
            elif gap_h < 24:
                lines.append(f"{self.user_name} has been away about "
                             f"{gap_h:.0f} hours.")
            else:
                lines.append(f"{self.user_name} has been away about "
                             f"{gap_h / 24:.0f} days.")
        else:
            lines.append(f"{self.user_name} hasn't spoken yet.")
        for t in st["threads"][:5]:
            lines.append(f"In progress: {t.get('text', t)}")
        for e in st["expectations"][:3]:
            lines.append(f"You half-expect: {e['text']}")
        text = "\n".join(lines) + "\n"
        # the snapshot file: written only when it changed (no commit-per-glance)
        old = self.vault.read("world/situation.md")
        if text != old:
            self.vault.write("world/situation.md", text)
        return text

    # ----------------------------------------------------------------- expect

    def expect(self, text: str, *, due_ts: float | None = None,
               keys: list[str] | None = None) -> None:
        st = self._state()
        st["expectations"].append({
            "text": text, "due": iso_of(due_ts) if due_ts else None,
            "keys": keys or [], "created": iso_of(self.clock.now())})
        self._belief("world", f"expects: {text}", 0.6, kind="expectation")
        self._save(st)

    def add_thread(self, text: str, task: str | None = None) -> None:
        st = self._state()
        st["threads"].append({"text": text, "task": task,
                              "since": iso_of(self.clock.now())})
        self._save(st)

    def note_contact_out(self) -> None:
        st = self._state()
        st["last_contact_out"] = iso_of(self.clock.now())
        self._save(st)

    def snapshot(self) -> dict:
        return self._state()

    # ------------------------------------------------------------------ query

    def query(self, q: str, *, at: str | None = None) -> list[Fact]:
        """Point-in-time: what was believed when. Snapshot stage = a filtered
        belief log; the temporal knowledge graph is the sanctioned later stage."""
        q_l = q.lower()
        cutoff = ts_of_iso(at) if at else None
        out = []
        for r in jsonl_read(self.beliefs_path):
            if cutoff and ts_of_iso(r["ts"]) > cutoff:
                continue
            if q_l and q_l not in r["belief"].lower() and q_l not in r["subject"].lower():
                continue
            out.append(Fact(subject=r["subject"], belief=r["belief"],
                            confidence=r["confidence"], ts=r["ts"],
                            kind=r.get("kind", "belief")))
        return out

    def inspect(self, selector: str = "") -> list[Fact]:
        return self.query(selector)
