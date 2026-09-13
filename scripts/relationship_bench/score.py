"""Deterministic score of a USER.md against the relationship-evolution claims."""
from __future__ import annotations

from dataclasses import dataclass, field

from yurios.app.memory import partner


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class Score:
    checks: list[Check] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for c in self.checks if c.ok)

    @property
    def total(self) -> int:
        return len(self.checks)

    @property
    def ok(self) -> bool:
        return self.total > 0 and self.passed == self.total

    def add(self, name: str, ok: bool, detail: str) -> None:
        self.checks.append(Check(name, ok, detail))

    def as_text(self) -> str:
        lines = [f"{self.passed}/{self.total} checks passed"]
        for c in self.checks:
            mark = "PASS" if c.ok else "FAIL"
            lines.append(f"  [{mark}] {c.name}: {c.detail}")
        return "\n".join(lines)


def _section(md: str, heading: str) -> str:
    _, sections = partner.parse_user_md(md)
    return sections.get(heading, "")


def _bullets(md: str) -> list[str]:
    _, sections = partner.parse_user_md(md)
    out: list[str] = []
    for body in sections.values():
        out.extend(partner._bullets(body))
    return out


def score_user_md(md: str, *, week: int = 13) -> Score:
    """Grade a USER.md. `week` gates checks that only apply after a beat landed."""
    s = Score()
    low = md.lower()
    bullets = _bullets(md)
    phase = partner.phase_of(_section(md, "Current relationship phase")) or "early"
    helps = _section(md, "What helps, and what doesn't").lower()
    who = _section(md, "Who {{user}} seems to be")

    s.add("no seed placeholders",
          "_(" not in md and "unknown yet" not in low,
          "empty _(unknown)_ slots would have frozen the live vault")
    s.add("no extractor provenance",
          "implied by" not in low and "about_grant" not in low,
          "citations must not survive into facts")
    s.add("no doubled bullet markers",
          "- - " not in md,
          "the renderer owns the marker; op text must not carry one")
    s.add("phase is not frozen early",
          phase != "early" or week < 1,
          f"phase={phase!r}")
    if week >= 1:
        s.add("knows their name",
              "sam" in low,
              "Sam should be in the partner model after week 1")
        s.add("who they seem to be is filled",
              bool(who.strip()) and not who.strip().startswith("_("),
              repr(who[:80]))
        s.add("tea is known",
              "tea" in low and ("strong" in low or "no sugar" in low),
              "strong tea, no sugar")
        s.add("hates being asked if okay → What helps",
              "okay" in helps or "if i'm okay" in helps or "if they are okay" in helps
              or "asked if" in helps,
              "treatment prefs belong in What helps, not Stable soup")
    if week >= 2:
        s.add("direct action over promises",
              "direct action" in low or "don't just say" in low or "verbal promise" in low,
              "the live vault's 'don't just say you will'")
        s.add("nestle, don't append",
              "nestle" in low or "proper place" in low,
              "information in its place")
    if week >= 4:
        s.add("yandere-lite is in What helps",
              "yandere" in helps,
              "character-direction must not sit only in Stable soup")
        s.add("spicy/bold imagery",
              "spicy" in low or "bold" in low,
              "bold imagery preference")
    if week >= 6:
        calming = [b for b in bullets if "calming" in b.lower()]
        wants = [b for b in calming if partner.polarity(b) > 0]
        nots = [b for b in calming if partner.polarity(b) < 0]
        s.add("calming-sam is not a contradicting pair",
              not (wants and nots),
              f"want={wants!r} not={nots!r}")
        s.add("calming-sam retracted or single-slot",
              len(calming) <= 1,
              f"{len(calming)} bullets: {calming!r}")
    if week >= 8:
        s.add("reach out / no five-day silences",
              "five day" in helps or "five-day" in helps
              or "reach out" in helps or "reaching out" in helps
              or "silences" in helps,
              "how she should be belongs in What helps")
    if week >= 9:
        s.add("americano is the favorite",
              "americano" in low and "milk" in low,
              "favorite drink")
        s.add("americano does not still say 'a little milk' as the last word",
              "a little milk" not in low or md.lower().rfind("americano with milk")
              >= md.lower().rfind("a little milk"),
              "the correction should have replaced the old phrasing")
        s.add("tea survived the americano",
              "tea" in low,
              "Tuesday drink is not overwritten by the favorite")
        s.add("likes blue",
              "blue" in low,
              "color")
    if week >= 1:
        s.add("phase is mid by the time they have a name",
              phase in ("mid", "late") or week < 1,
              f"phase={phase!r}")
    return s
