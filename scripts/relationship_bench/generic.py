"""The same beats, none of the same words — the generalization track.

The `scenarios.py` script is lifted from one live vault, and the filing used to
be a list of substrings lifted from the same place ("yandere", "americano",
"hates being asked"). That scored 19/19 and filed *nothing* for anybody else:
scorer, implementation and tests all shared one couple's vocabulary, so the
bench could only ever confirm itself.

This track replays the same structure — name, identity, a treatment preference,
a character direction, a retraction, a correction, filler that must stay out —
with a different person, a different companion, and no word in common with the
Sam script. The scorer below asserts *where things landed*, never which words
they used. If the Sam track passes and this one does not, the filing has been
fitted to Sam again.
"""
from __future__ import annotations

from dataclasses import dataclass

from scenarios import C, Conversation, T   # noqa: F401  (C/T build the script)

# Dates come from `scenarios.START` — `Conversation.day` is defined against it,
# and only the *spacing* of the days matters to a DREAM pass.
WEEKS = 4
CHAR = "noor"
USER = "ada"

CONVERSATIONS: tuple[Conversation, ...] = (
    # week 1 — name, identity, and short facts that must all survive
    C(1, 0, "name",
      T("morning", "Morning. The light's just come round."),
      T("I'm Ada, by the way.", "Ada. Got it.")),
    C(1, 1, "identity",
      T("I'm a paramedic. Nights, mostly.", "That explains the hours."),
      T("I like dogs.", "Noted.")),
    C(1, 2, "more short facts",
      T("I like green.", "Green it is."),
      T("I'm allergic to shellfish.", "I'll keep that one close.")),
    C(1, 3, "filler", T("hey", "Hey."), T("nothing much", "Alright.")),
    C(1, 4, "treatment preference",
      T("When I come back wrecked, don't fuss over me. Just talk about something else.",
        "Something else, then.")),

    # week 2 — a character direction (this is the PERSONA door)
    C(2, 0, "character direction",
      T("I want you warmer with me. Less careful. Say the thing you're thinking.",
        "Then I'll say it.")),
    C(2, 1, "another direction",
      T("And start things yourself. Don't wait for me to open every time.",
        "I'll start.")),
    C(2, 2, "filler", T("just working", "I'll be here.")),
    C(2, 3, "another treatment preference",
      T("If you write something down for me, put it where it belongs. "
        "Don't stack it at the bottom.",
        "Where it belongs.")),
    C(2, 4, "filler", T("tired", "Then rest.")),

    # week 3 — a request, then its retraction (the contradiction beat)
    C(3, 0, "asks for a routine",
      T("Can you make a wind-down routine for me? Call it 'settle-ada'.",
        "I can do that.")),
    C(3, 1, "filler", T("hey", "Hey.")),
    C(3, 2, "retracts it",
      T("Drop the 'settle-ada' routine. I don't want named routines I have to manage. "
        "Just do it quietly.",
        "Quietly, then.")),
    C(3, 3, "filler", T("busy", "Go on, then.")),
    C(3, 4, "ongoing situation",
      T("I'm moving flat this month. It's eating everything.", "That'll pass.")),

    # week 4 — a correction that must replace, not stack
    C(4, 0, "a drink",
      T("I drink black coffee at work.", "Black coffee.")),
    C(4, 1, "corrects it",
      T("Actually — at home it's ginger tea, always. The coffee is a work thing only.",
        "Ginger tea at home. Kept.")),
    C(4, 2, "filler", T("hey", "Hey.")),
    C(4, 3, "restates the direction",
      T("Still want you warmer, by the way. Don't drift back to polite.",
        "I won't.")),
    C(4, 4, "ordinary close",
      T("Alright. Night.", "Night, Ada.")),
)


def conversations(*, weeks: int = WEEKS) -> list[Conversation]:
    return [c for c in CONVERSATIONS if c.week <= weeks]


# --- the scorer: structure, never vocabulary ---------------------------------

@dataclass
class Check:
    name: str
    ok: bool
    detail: str


class Score:
    def __init__(self):
        self.checks: list[Check] = []

    def add(self, name, ok, detail):
        self.checks.append(Check(name, bool(ok), str(detail)))

    @property
    def passed(self):
        return sum(1 for c in self.checks if c.ok)

    @property
    def total(self):
        return len(self.checks)

    @property
    def ok(self):
        return self.total > 0 and self.passed == self.total

    def as_text(self):
        out = [f"{self.passed}/{self.total} checks passed"]
        for c in self.checks:
            out.append(f"  [{'PASS' if c.ok else 'FAIL'}] {c.name}: {c.detail}")
        return "\n".join(out)


# The facts Ada states once and never repeats. Every one must be in the file at
# the end — this is the eviction bug, asserted rather than eyeballed.
MUST_SURVIVE = (
    ("paramedic", "her job"),
    ("dog", "likes dogs"),
    ("green", "likes green"),
    ("shellfish", "the allergy"),
)
# Directions about the COMPANION. Belong in What helps AND in the persona delta.
DIRECTIONS = (("warm", "warmer, less careful"), ("start", "initiates first"))
# Preferences about how ADA is treated. What helps, but NOT the persona delta.
TREATMENT = (("fuss", "don't fuss when she's wrecked"),
             ("belong", "put things where they belong"))


def score_generic(md: str, delta: dict | None, *, week: int = WEEKS) -> Score:
    from yurios.app.memory import partner
    s = Score()
    low = md.lower()
    _, sections = partner.parse_user_md(md)
    helps = (sections.get("What helps, and what doesn't") or "").lower()
    stable = (sections.get("Stable") or "").lower()
    delta_text = " ".join(delta.get("lines", [])).lower() if delta else ""
    phase = partner.phase_of(sections.get("Current relationship phase", "")) or "early"

    s.add("knows their name", "ada" in low, f"phase={phase!r}")
    s.add("phase moved off the seed", phase != "early", f"phase={phase!r}")
    s.add("no doubled bullet markers", "- - " not in md, "renderer owns the marker")
    s.add("no seed placeholders", "_(" not in md, "empty slots must be filled or gone")

    for token, what in MUST_SURVIVE:
        s.add(f"kept: {what}", token in low, f"{token!r} missing from USER.md")

    if week >= 2:
        for token, what in DIRECTIONS:
            s.add(f"direction filed in What helps: {what}",
                  token in helps, f"{token!r} not under What helps")
            s.add(f"direction reached the persona delta: {what}",
                  token in delta_text,
                  "a character direction in nobody's vocabulary must still "
                  "open the PERSONA door")
        for token, what in TREATMENT:
            s.add(f"treatment pref is not a persona edit: {what}",
                  token in helps and token not in delta_text,
                  "how to treat her is not who she is")

    if week >= 3:
        routine = [b for b in partner.all_bullets(sections) if "settle-ada" in b.lower()]
        s.add("the retracted routine is one slot, not a pair",
              len(routine) <= 1, f"{len(routine)} bullets: {routine!r}")
        s.add("moving flat is not filed as a durable identity fact",
              "moving" not in stable and "flat" not in stable,
              "a this-month situation belongs in Ongoing")

    if week >= 4:
        s.add("both drinks survive the correction",
              "coffee" in low and "tea" in low,
              "the correction narrows the coffee, it does not delete the tea")
    return s
