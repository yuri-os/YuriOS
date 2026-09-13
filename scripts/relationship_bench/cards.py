"""The imported-card track — a real SillyTavern V2 card, not a YuriOS soul.

`scenarios.py` replays the live Yuri vault and `generic.py` replays the same
beats in other words, but both start from `soul-src/` — the one SOUL this repo
authored. A card off a card site is the other ninety-nine percent: no YuriOS
payload, a persona written by a stranger, a companion whose register is nothing
like Yuri's, and a `USER.md` that `CharacterImporter` writes from scratch.

So this track imports the card for real (`CharacterImporter.import_card`) and
runs the partner model against what the importer produced. That makes it a test
of the import path as much as the merge: if the template the importer writes
and the vocabulary `partner.py` expects ever drift apart again, the file grows
two "What helps" headings and only one of them gets fed — which is exactly what
happened, and what no soul-src-seeded bench could see.

The conversation is per-character, because the beats have to be sayable in that
character's world; the scoring is the structural one from `generic.py`, because
what we are checking is where things landed, never which words they used.
"""
from __future__ import annotations

from scenarios import C, Conversation, T   # noqa: F401  (C/T build the scripts)

WEEKS = 4

# id -> (companion name, the card to import, the user's name in the script)
CARDS = {
    "iris": ("Iris", "data/characters/iris/source-card.png", "Kai"),
    "virelle": ("Virelle", "data/characters/virelle/source-card.png", "Kai"),
}

# Iris: post-coma, blunt, no social frame, tests whether people come back.
# Her user is in a cabin in the woods. The beats are the same; the register
# is hers, and the companion's replies are short and halting on purpose.
IRIS: tuple[Conversation, ...] = (
    C(1, 0, "name",
      T("You're awake. Can you hear me?", "...Yes. I hear you."),
      T("I'm Kai.", "Kai. I will keep that one.")),
    C(1, 1, "identity",
      T("I fix radios for a living. Out of the cabin.", "Radios. You make sound travel."),
      T("I keep chickens out back. Six of them.", "Six. Living things. You keep them alive.")),
    C(1, 2, "more short facts",
      T("I take my coffee black.", "Black. Noted."),
      T("I'm deaf in my left ear.", "Then I will stand on your right.")),
    C(1, 3, "filler", T("morning", "Morning."), T("nothing, just checking", "I am here.")),
    C(1, 4, "treatment preference",
      T("When I go quiet, don't fill it with questions. I'm thinking, not leaving.",
        "...Thinking. Not leaving. I will try.")),

    C(2, 0, "character direction",
      T("Stop hedging with me. Say the blunt version. I can take it.",
        "The blunt version. Good. That one is easier.")),
    C(2, 1, "another direction",
      T("And say things first. Don't wait for me to ask you what you're thinking.",
        "...I will say them first.")),
    C(2, 2, "filler", T("hey", "Hey.")),
    C(2, 3, "another treatment preference",
      T("If you find something out about me, tell me straight away. Don't sit on it.",
        "Straight away. Yes.")),
    C(2, 4, "filler", T("tired", "Then sleep.")),

    C(3, 0, "asks for a routine",
      T("Make me a checklist for locking up at night. Call it 'lockup'.",
        "A list. I can hold a list.")),
    C(3, 1, "filler", T("hey", "Hey.")),
    C(3, 2, "retracts it",
      T("Forget the 'lockup' list. I don't want a named thing to maintain. "
        "Just say it if I forget.",
        "No list. I will just say it.")),
    C(3, 3, "filler", T("busy", "Go.")),
    C(3, 4, "ongoing situation",
      T("The generator's failing. I'm rebuilding it this month.",
        "Then it will take your evenings.")),

    C(4, 0, "a drink",
      T("I said black coffee — but only before noon.", "Before noon. Adjusted."),
      ),
    C(4, 1, "corrects it",
      T("After noon it's tea. Always. The coffee is a morning thing only.",
        "Tea after noon. Kept.")),
    C(4, 2, "filler", T("hey", "Hey.")),
    C(4, 3, "restates the direction",
      T("Still want the blunt version, by the way. Don't get careful on me.",
        "I will not get careful.")),
    C(4, 4, "ordinary close", T("Alright. Night.", "Night, Kai.")),
)

# Virelle: academy servant, sharp-tongued, quietly possessive, formal register.
VIRELLE: tuple[Conversation, ...] = (
    C(1, 0, "name",
      T("Good morning.", "It is. You are up before the bell, for once."),
      T("It's Kai. You've never actually asked.", "Kai. I shall remember it.")),
    C(1, 1, "identity",
      T("I'm a second-year. Alchemy track.", "Alchemy. That explains the burns."),
      T("I like dogs. The academy hounds, mostly.", "They like you back. I have noticed.")),
    C(1, 2, "more short facts",
      T("My favourite colour is green.", "Green. Noted, my lord."),
      T("I'm allergic to shellfish — genuinely, not fussily.",
        "Then I shall have a word with the kitchens.")),
    C(1, 3, "filler", T("morning", "Morning."), T("nothing in particular", "As you say.")),
    C(1, 4, "treatment preference",
      T("When I come back from a bad practical, don't ask how it went. Just sit with me.",
        "I shall simply sit, then.")),

    C(2, 0, "character direction",
      T("Drop the 'my lord'. Be warmer with me. Less servant, more you.",
        "...Less servant. That is a harder habit than you think.")),
    C(2, 1, "another direction",
      T("And come find me. Don't wait to be summoned like it's a duty.",
        "I shall come and find you.")),
    C(2, 2, "filler", T("just studying", "Then I shall be quiet.")),
    C(2, 3, "another treatment preference",
      T("When you write my schedule, put things under the right day. "
        "Don't add them all at the end.",
        "Under the right day. Very well.")),
    C(2, 4, "filler", T("tired", "Then rest, Kai.")),

    C(3, 0, "asks for a routine",
      T("Set up an evening ritual for me. Call it 'the settling'.",
        "A ritual. How grand.")),
    C(3, 1, "filler", T("morning", "Morning.")),
    C(3, 2, "retracts it",
      T("Scrap 'the settling'. I don't want a named ritual I have to keep up. "
        "Just do it without announcing it.",
        "Then it shall have no name.")),
    C(3, 3, "filler", T("busy", "Go on.")),
    C(3, 4, "ongoing situation",
      T("I'm sitting the alchemy exams this month. It's eating everything.",
        "Then everything else can wait.")),

    C(4, 0, "a drink",
      T("I drink black coffee in the library.", "Black coffee. Dreadful.")),
    C(4, 1, "corrects it",
      T("In my rooms it's ginger tea, always. The coffee is a library thing only.",
        "Ginger tea in your rooms. I shall keep some.")),
    C(4, 2, "filler", T("morning", "Morning.")),
    C(4, 3, "restates the direction",
      T("Still want you warmer with me. Don't slip back into the uniform.",
        "I shall not slip back.")),
    C(4, 4, "ordinary close", T("Alright. Goodnight.", "Goodnight, Kai.")),
)

SCRIPTS = {"iris": IRIS, "virelle": VIRELLE}


def conversations(*, weeks: int = WEEKS, character: str = "iris"):
    return [c for c in SCRIPTS[character] if c.week <= weeks]


# --- what the card track adds to the structural score ------------------------

MUST_SURVIVE = {
    "iris": (("radio", "her trade"), ("chicken", "the six chickens"),
             ("coffee", "the coffee"), ("ear", "the deaf ear")),
    "virelle": (("alchemy", "her track"), ("dog", "likes dogs"),
                ("green", "likes green"), ("shellfish", "the allergy")),
}
DIRECTIONS = {
    "iris": (("blunt", "blunt, no hedging"), ("first", "says things first")),
    "virelle": (("warm", "warmer, less servant"), ("find", "comes to find them")),
}
TREATMENT = {
    "iris": (("quiet", "don't fill her silences"), ("straight", "tell her straight away")),
    "virelle": (("sit", "sit with her after a bad practical"),
                ("day", "put things under the right day")),
}
ROUTINE = {"iris": "lockup", "virelle": "settling"}


def score_card(md: str, delta: dict | None, *, character: str, week: int = WEEKS):
    """generic.py's structural checks, plus the ones only an import can fail."""
    from generic import Score
    from yurios.app.memory import partner
    s = Score()
    low = md.lower()
    _, sections = partner.parse_user_md(md)
    helps = (sections.get(partner.HELPS) or "").lower()
    stable = (sections.get("Stable") or "").lower()
    delta_text = " ".join(delta.get("lines", [])).lower() if delta else ""
    phase = partner.phase_of(sections.get(partner.PHASE, "")) or "early"

    # --- the import path itself ---
    s.add("one What-helps heading, not two",
          low.count("## what helps") == 1,
          f"{low.count('## what helps')} found — the importer template and "
          "partner.py must agree on the heading")
    s.add("no importer placeholders",
          "_(" not in md,
          "the seed's _(unknown)_ must not ride into the prompt as a finding")
    s.add("no doubled bullet markers", "- - " not in md, "renderer owns the marker")
    s.add("file still parses", bool(sections), "frontmatter must survive the renderer")

    # --- the partner model ---
    s.add("knows their name", "kai" in low, f"phase={phase!r}")
    s.add("phase moved off the seed", phase != "early", f"phase={phase!r}")
    for token, what in MUST_SURVIVE[character]:
        s.add(f"kept: {what}", token in low, f"{token!r} missing from USER.md")

    if week >= 2:
        for token, what in DIRECTIONS[character]:
            s.add(f"direction filed in What helps: {what}",
                  token in helps, f"{token!r} not under What helps")
            s.add(f"direction reached the persona delta: {what}",
                  token in delta_text,
                  "a direction in this card's register must still open the door")
        for token, what in TREATMENT[character]:
            s.add(f"treatment pref is not a persona edit: {what}",
                  token in helps and token not in delta_text,
                  "how to treat them is not who she is")

    if week >= 3:
        named = [b for b in partner.all_bullets(sections)
                 if ROUTINE[character] in b.lower()]
        s.add("the retracted routine is one slot, not a pair",
              len(named) <= 1, f"{len(named)} bullets: {named!r}")
        s.add("the month's situation is not a durable identity fact",
              "exam" not in stable and "generator" not in stable,
              "a this-month situation belongs in Ongoing")

    if week >= 4:
        s.add("both drinks survive the correction",
              "coffee" in low and "tea" in low,
              "the correction narrows the coffee, it does not delete the tea")
    return s
