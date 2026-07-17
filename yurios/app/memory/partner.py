"""The partner model — `vault/soul/USER.md` (SPEC §6.3).

Her theory of *you*: durable, small, always injected whole (§7.1 block 5).
After each exchange the utility model extracts durable facts as ops against
the *current* USER.md (so it updates rather than duplicates), under a strict
JSON schema. Low-confidence claims are QUARANTINED — kept out of USER.md until
a second turn corroborates. Promotion, not capture, is the trust boundary
(→ ch. 15).

USER.md stays human-readable and human-editable markdown: the user can open
it, fix it, delete from it. It is their file (§4.2).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from yurios.app import vaultgit

SECTIONS = ("Stable", "Ongoing", "Don't forget")
QUARANTINE_CONFIDENCE = 0.6   # below this, an op waits for corroboration (§6.3)
UNSCORED_CONFIDENCE = 0.0     # a claim the model returned with NO confidence must
                              # be corroborated before it lands — "unsure" fails
                              # safe, matching the quarantine stance (§6.3). Do NOT
                              # default this high: a missing score is not certainty.
CORROBORATION_OVERLAP = 0.5   # token-overlap that counts as "the same claim again"

EXTRACT_SYSTEM = """\
You maintain a memory file about THE USER (the human), from a chat transcript.

The transcript has two speakers:
  - the user — lines beginning "you:"
  - the companion (the AI) — lines beginning with the companion's name (e.g. "yuri:")

Extract only DURABLE facts THE USER stated ABOUT THEMSELVES, worth remembering
across sessions: their identity/name, stable preferences, ongoing situations or
goals, explicit "remember this" items. Rules:
  - Use ONLY the user's ("you:") lines as the source of facts.
  - NEVER record the companion's self-description as a fact about the user. If the
    companion says "My name is Yuri", that is the COMPANION's name, not the user's.
  - If the user only asked a question or stated nothing durable about themselves,
    return {"ops": []}.
  - Ignore ephemeral chit-chat and roleplay stage directions.

Return JSON, nothing else:
{ "ops": [ { "section": "Stable"|"Ongoing"|"Don't forget", "text": string,
            "op": "add"|"update"|"remove", "confidence": 0..1 } ] }
Return {"ops": []} if nothing durable was stated."""


@dataclass
class Op:
    section: str
    text: str
    op: str = "add"            # add | update | remove
    confidence: float = UNSCORED_CONFIDENCE


def parse_ops(raw: str) -> list[Op]:
    """Tolerant parse of the utility model's reply (§6.2: malformed output is
    logged and dropped, never fatal to the turn)."""
    # a reasoning model (qwen3, r1, …) may prepend a <think>…</think> block; the
    # JSON we want is after it. Strip any think block before hunting for the object.
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL | re.IGNORECASE)
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < 0:
        return []
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return []
    ops = []
    for o in data.get("ops", []):
        if not isinstance(o, dict) or o.get("section") not in SECTIONS:
            continue
        text = str(o.get("text", "")).strip()
        if not text:
            continue
        ops.append(Op(section=o["section"], text=text,
                      op=o.get("op", "add"),
                      # a missing confidence is treated as UNSCORED (fails safe to
                      # the quarantine), never as certainty (§6.3).
                      confidence=float(o.get("confidence", UNSCORED_CONFIDENCE))))
    return ops


async def extract_ops(utility, user_md: str, exchange: str) -> tuple[str, list[Op]]:
    """One cheap utility-model call per exchange (§6.2 step 3). Returns the raw
    reply *and* the parsed ops so the caller can log both (the raw reply is the
    only faithful record of what the model actually proposed — §6.3)."""
    raw = await utility.complete([
        {"role": "system", "content": EXTRACT_SYSTEM},
        {"role": "user", "content":
            f"Current USER.md:\n\n{user_md}\n\n---\nLast exchange:\n\n{exchange}"},
    ])
    return raw, parse_ops(raw)


# --- merging ops into USER.md ------------------------------------------------

def _tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9']+", s.lower()))


def _overlap(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def apply_ops(user_md: str, ops: list[Op]) -> str:
    """Merge, don't blindly append (§6.3): `update` replaces the closest
    existing line, `remove` drops it, `add` skips near-duplicates. Sections
    are created on demand — the seeded USER.md's narrative sections are left
    untouched alongside them."""
    lines = user_md.splitlines()
    for op in ops:
        header = f"## {op.section}"
        if header not in lines:
            if lines and lines[-1].strip():
                lines.append("")
            lines.extend([header, ""])
        start = lines.index(header) + 1
        end = start
        while end < len(lines) and not lines[end].startswith("## "):
            end += 1
        bullet_idx = [i for i in range(start, end) if lines[i].lstrip().startswith("- ")]

        def best_match(threshold: float) -> int | None:
            scored = [(i, _overlap(lines[i].lstrip("- ").strip(), op.text))
                      for i in bullet_idx]
            scored = [(i, s) for i, s in scored if s >= threshold]
            return max(scored, key=lambda t: t[1])[0] if scored else None

        if op.op == "remove":
            i = best_match(CORROBORATION_OVERLAP)
            if i is not None:
                lines.pop(i)
        elif op.op == "update":
            i = best_match(0.3)
            if i is not None:
                lines[i] = f"- {op.text}"
            else:
                lines.insert(end, f"- {op.text}")
        else:  # add — skip if an equivalent line already exists
            if best_match(0.8) is None:
                lines.insert(end, f"- {op.text}")
    return "\n".join(lines).rstrip() + "\n"


# --- the quarantine (§6.3) ----------------------------------------------------

class Quarantine:
    """Low-confidence claims wait here (state/quarantine.json) until a second
    turn corroborates them; only then are they promoted into USER.md."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.items: list[dict] = []
        if self.path.exists():
            self.items = json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self) -> None:
        vaultgit.atomic_write(self.path, json.dumps(self.items, indent=2))

    def triage(self, ops: list[Op]) -> tuple[list[Op], list[Op]]:
        """Split ops into (apply_now, newly_quarantined). An op that matches a
        quarantined claim corroborates it → promote (apply now, clear entry)."""
        apply_now: list[Op] = []
        held: list[Op] = []
        for op in ops:
            if op.op == "remove":       # removals are always honored
                apply_now.append(op)
                continue
            match = next((q for q in self.items
                          if q["section"] == op.section
                          and _overlap(q["text"], op.text) >= CORROBORATION_OVERLAP),
                         None)
            if match is not None:       # second sighting — promote (§6.3)
                self.items.remove(match)
                apply_now.append(op)
            elif op.confidence < QUARANTINE_CONFIDENCE:
                self.items.append({"section": op.section, "text": op.text,
                                   "confidence": op.confidence})
                held.append(op)
            else:
                apply_now.append(op)
        self._save()
        return apply_now, held
