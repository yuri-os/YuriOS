"""Reading a stranger's card into the four sections a SOUL is made of.

A YuriOS character keeps who she is in four places — `CONSTITUTION.md#Identity`,
`#History`, `PERSONA.md#Appearance`, `#Manner` — because the immutable half and
the editable half have to be separable (§23). A character card from the internet
keeps all four in one `description` field, in whatever shape its author felt
like: bracketed blocks, bold headers, bullet attribute lists, ASCII rules, emoji
section markers, `{{char}} - Appearance ---`. There is no schema to parse.

So this is a **router, not a rewriter**. It finds the lines that open a labelled
block, decides which of the four sections that label belongs to, and moves the
author's own lines there verbatim. Nothing is reworded, nothing is dropped, and
a card it cannot read at all comes out exactly as it went in — everything in
`identity`, which is what the importer did before any of this existed.

That last property is what makes it safe to run on every import. The ceiling is
low by design: a router cannot turn a lore dump in the `scenario` field into
lorebook entries, or notice that `character_version` is holding a URL. That is
`optimize.py`'s job, and it costs a model call. This costs nothing and gets the
common shapes right, which is the difference between a studio page that opens
with one 5,000-character blob in Identity and one that opens with four sections
roughly where they belong.
"""
from __future__ import annotations

import re
from typing import Iterable

#: label → section. Only these switch the router; every other labelled block —
#: and there are thousands of them out there — continues whatever section is
#: open, which is what keeps a sentence containing a colon from being read as a
#: heading and tearing a paragraph in half.
_SECTION_WORDS: tuple[tuple[str, frozenset[str]], ...] = (
    ("appearance", frozenset({
        "appearance", "appearances", "looks", "physical", "physique", "body",
        "figure", "features", "attire", "clothing", "clothes", "outfit",
        "outfits", "wardrobe", "dress", "apparel", "aesthetic",
    })),
    ("history", frozenset({
        "backstory", "background", "history", "past", "origin", "origins",
        "biography", "bio", "upbringing", "childhood",
    })),
    ("manner", frozenset({
        "personality", "personalities", "traits", "trait", "behaviour",
        "behavior", "temperament", "demeanor", "demeanour", "disposition",
        "quirks", "quirk", "habits", "mannerisms", "speech", "voice", "tone",
        "likes", "dislikes", "hobbies", "interests", "kinks", "fetishes",
        "attitude", "psychology", "mindset",
    })),
)

#: Words that appear *beside* a section word and must not veto it: "Physical
#: Appearance", "Personality Traits", "Character Info - Appearance".
_LABEL_NOISE = frozenset({
    "and", "or", "of", "the", "a", "an", "her", "his", "their", "info",
    "information", "details", "description", "section", "notes", "char",
    "character", "general", "basic", "core", "key", "other", "misc",
})

#: `{{char}}` / `{{user}}` are placeholders, not words — stripped before a label
#: is measured so `{{char}} - Appearance ---` reads as "appearance".
_PLACEHOLDER = re.compile(r"\{\{[^}]*\}\}")
#: Markup a header wears on either side: hashes, stars, rules, quotes, brackets,
#: bullets, and the emoji plenty of cards use as section markers.
_EDGE = " \t#*_~=|>/\\[](){}·•—–-:：、,.\"'"


def _label(line: str) -> tuple[str, str] | None:
    """`(label, inline remainder)` if this line opens a labelled block.

    Two shapes count. `Appearance: she is tall` — a colon, with the block's first
    sentence possibly riding on the same line. And `**Appearance**` or
    `> 👀 Appearance` or `--- Appearance ---` — no colon, but wearing enough
    markup that it cannot be mistaken for prose. A bare `Appearance` on its own
    line is deliberately *not* a header: cards contain one-word paragraphs.
    """
    text = line.strip()
    if not text:
        return None
    head, colon, rest = text.partition(":")
    if not colon:
        head, rest = text, ""
    # Only the part *before* the colon is measured. Plenty of cards put a whole
    # two-thousand-character section on one line — `[Backstory: …]` — and a
    # length check against the line would miss every one of them.
    if len(head) > 120:
        return None
    core = _PLACEHOLDER.sub(" ", head).strip(_EDGE).strip()
    core = re.sub(r"^[^A-Za-z]+", "", core)
    core = re.sub(r"[^A-Za-z]+$", "", core)
    if not core or len(core) > 48:
        return None
    words = core.split()
    if not (1 <= len(words) <= 4):
        return None
    if not colon and head.strip() == core:
        return None                      # unmarked prose line, not a heading
    return core.lower(), rest.strip()


def _section_of(label: str) -> str | None:
    words = {word.strip("'’") for word in re.split(r"[^A-Za-z’']+", label) if word}
    meaningful = words - _LABEL_NOISE
    for section, vocabulary in _SECTION_WORDS:
        if meaningful & vocabulary:
            return section
    return None


def _tidy(block: Iterable[str]) -> str:
    """A routed block, with the bracket it lost on the way out put right.

    Cards wrap whole sections in `[Backstory: … ]`. Routing three of those into
    three different sections leaves each holding half a bracket pair, which reads
    as damage even though every word survived. Nothing else is touched.
    """
    text = "\n".join(block).strip("\n")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    for opener, closer in (("[", "]"), ("(", ")"), ("<", ">")):
        if text.startswith(opener) and text.count(opener) > text.count(closer):
            text = text[1:].lstrip()
        if text.endswith(closer) and text.count(closer) > text.count(opener):
            text = text[:-1].rstrip()
    return text.strip()


def split_description(description: str) -> dict[str, str]:
    """Route a card's `description` into `{identity, history, appearance, manner}`.

    Every line of the input lands in exactly one of them, in its original order
    and its original words. Unlabelled prose, and anything under a label this
    module does not recognise, stays with whatever section is open — which for
    the opening of a card means `identity`.
    """
    sections: dict[str, list[str]] = {name: [] for name in
                                      ("identity", "history", "appearance", "manner")}
    current = "identity"
    for line in str(description or "").splitlines():
        found = _label(line)
        if found is not None:
            section = _section_of(found[0])
            if section is not None:
                current = section
                if sections[section]:
                    sections[section].append("")   # keep two blocks apart
        sections[current].append(line)
    return {name: _tidy(lines) for name, lines in sections.items()}


#: What a version looks like when it is one: a short token carrying a digit.
#: Cards routinely use the field for a source URL, a "chat name", or a changelog
#: — all worth keeping, none of them a version.
_VERSION = re.compile(r"^[\w.+\- ]{1,32}$")


def clean_version(value: str) -> tuple[str, str]:
    """`(version, misfiled)` — the version to use, and whatever else was in there.

    The misfiled half is not thrown away; the importer appends it to the creator
    notes, where a source URL is genuinely useful to whoever opens the card next.
    """
    text = str(value or "").strip()
    if not text:
        return "1.0.0", ""
    first = text.splitlines()[0].strip()
    if len(text.splitlines()) == 1 and _VERSION.fullmatch(first) and any(
            character.isdigit() for character in first):
        return first, ""
    return "1.0.0", text
