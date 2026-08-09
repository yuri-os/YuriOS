"""Deriving *where a character is* from the card she was imported from.

The situation block (SPEC §2.5) tells her, every single prompt, that her place is
"your small room above the Sprawl — the lamp, the window seat, the plant, the
rain on the window, the city burning beyond the glass". That sentence is true
about the companion this repo ships and false about everybody else: a nurse
imported from a card someone wrote in 2023 was being told, all day, that she
lives above a city she has never heard of. `vault/world/situation.md` had the
same shape of problem from the other end — the importer seeded it `_(Unknown.)_`
for every character, when the card in hand says perfectly plainly where she is.

So this module does for her *place* what `appearance.py` does for her face:

- **It reads the card, not the house.** The scenario field is where a card puts
  the present situation, and the description's `Setting:`/`Location:`/`Home:`
  sections are where the rest of it hides. Whatever comes out is hers.
- **It is prose, not a schema.** One to three sentences, second person, present
  tense — the register the situation block is already written in, because that
  is the block the text is dropped into.
- **The model improves it; the parser guarantees it.** `mechanical_place` runs
  synchronously at import with no network and no key, so a character always
  leaves the importer standing somewhere of her own. `derive_place` rewrites it
  into better prose when a utility model is reachable, and `refine_setting`
  refuses to touch a file a human has edited — the marker comment in the header
  is what tells the two apart.

What it deliberately leaves out is everything the rest of the block already
carries: the hour (the clock line), the weather and the music (the room's sticky
scene state), her body (the embodiment truth). A setting that mentions the time
of day would be a standing sentence arguing with a live one, every prompt.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Mapping

import yaml

from .soulfiles import parse_md, split_sections

log = logging.getLogger("characters.setting")

#: The line that says this file is still the machine's work. Editing past it —
#: which means deleting the comment, or any change at all through the studio —
#: makes the file yours, and `refine_setting` stops rewriting it.
DERIVED_MARK = "Derived from her character card at import"

#: Longest place we will keep. The setting rides in every prompt, so it is a
#: sentence or three, not a chapter: past this the mechanical fallback is
#: quietly cut and the model is told the budget outright.
MAX_PLACE_CHARS = 600

# Sections of a card's description that are about where she is. A net, not a
# schema (cards are wildly inconsistent), and happy to miss — the scenario field
# is the fallback and is usually the better source anyway.
_PLACE_HEADERS = re.compile(
    r"^\s*[*_#\-\s]*(setting|location|place|home|house|residence|room|"
    r"environment|surroundings|world|where(\s+(she|he|they)\s+lives?)?)\s*[:\-]",
    re.IGNORECASE)
_SECTION_BREAK = re.compile(r"^\s*[*_#\-\s]*([A-Z][A-Za-z /]{2,30})\s*[:\-]\s*$")
#: A body that is admitting to being empty, in either of the two shapes this
#: Vault writes: the italic-parenthetical placeholder, or a plain "unknown".
_PLACEHOLDER = re.compile(
    r"[_*]+\(.*\)[_*]+|[_*(\s]*(no|not|nothing|none|unknown|empty|to be)\b.*",
    re.IGNORECASE | re.DOTALL)
# Facts that are true about her and are not a place.
_NON_PLACE = re.compile(
    r"^\s*[*_#\-\s]*(age|birthday|gender|occupation|status|job|role|"
    r"personality|likes|dislikes|hobbies|goals|appearance|looks|body|"
    r"relationship|sexuality|orientation|name|alias|clothing|outfit|"
    r"wardrobe|attire|dress|speech|voice)\s*[:\-]", re.IGNORECASE)

SETTING_SYSTEM = """\
You write the standing setting for a character who runs as a live companion: \
the one or two sentences her prompt carries every turn to tell her where she is.

You are given her character card. Return ONLY the place.

Hard rules:
- Address her as "you", present tense: "You are in a narrow flat over the \
laundromat, the fire escape outside your one window."
- ONE to THREE sentences, under 500 characters. It is in every prompt she ever \
reads; length here is rent.
- Only the standing place: the room, the building, what is outside it, what is \
in it that is always in it. Things a person standing there could point at.
- NO time of day, NO weather, NO music, NO season. The runtime injects the real \
clock and the real weather each turn, and a standing sentence about the rain \
would argue with the live one.
- NO plot, NO other people, NO what she is doing, NO her body or her looks. \
Where she is, not what happens there.
- Write "{user}" for the person she talks to, if you need to mention them at \
all. Prefer not to.
- Prefer what the card says. Invent only enough to make a bare card into a room, \
and keep any invention plain and small.

Return ONLY a JSON object, no prose around it:
{"place": "<the sentences>"}"""


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _macros(text: str, name: str) -> str:
    """Card macros, resolved the way the situation block can carry them.

    `{{char}}` is answered here because the importer knows her name and the
    renderer does not. `{{user}}` becomes the block's own `{user}` placeholder,
    which `render_situation` fills with the configured user name at prompt time
    — the same convention the embodiment truth has always used.
    """
    text = re.sub(r"\{\{\s*(char|character)\s*\}\}", name or "she", text,
                  flags=re.IGNORECASE)
    text = re.sub(r"\{\{\s*user\s*\}\}", "{user}", text, flags=re.IGNORECASE)
    return text


def place_excerpt(description: str) -> str:
    """The parts of a card's description that are plausibly about her place.

    Returns "" when the card has no recognisable setting section — the caller
    then falls back to the scenario, which is where most cards keep it.
    """
    kept: list[str] = []
    capturing = False
    for line in str(description or "").splitlines():
        if _PLACE_HEADERS.match(line):
            capturing = True
            tail = line.split(":", 1)[-1] if ":" in line else ""
            if _clean(tail):
                kept.append(_clean(tail))
            continue
        if capturing:
            if _NON_PLACE.match(line) or (_SECTION_BREAK.match(line)
                                          and not _PLACE_HEADERS.match(line)):
                capturing = False
                continue
            if _clean(line):
                kept.append(_clean(line))
    return " ".join(kept).strip()


def mechanical_place(name: str, *, scenario: str = "", description: str = "",
                     first_mes: str = "") -> str:
    """The no-model fallback: the card's own words about where she is.

    Worse prose than the model writes, and still unmistakably *her* room rather
    than ours — which is the whole job. The order is the order of reliability: a
    card's `scenario` field is defined to be the present situation, and a
    `Setting:` section in the description is the next best thing.

    Deliberately conservative past those two. A card that keeps its place in
    free prose under no heading at all gets nothing from this function, and the
    house sentence stays until `derive_place` — which is handed the whole
    description and can actually read it — has a model to run on. Pasting a
    paragraph of persona into "Your place is …" would be a worse room than the
    one it replaced.
    """
    excerpt = _clean(scenario) or place_excerpt(description)
    excerpt = _clean(_macros(excerpt, name))[:MAX_PLACE_CHARS].strip()
    if not excerpt:
        # Nothing in the card is about a place. Saying so is the honest answer:
        # it leaves the block with no place line at all, which is better than
        # lending her the house character's room.
        return ""
    return excerpt


def _parse(raw: str) -> str:
    """The model's answer, tolerantly: a JSON object is what we asked for, a
    fenced one is what we sometimes get, and bare prose is still a place."""
    text = str(raw or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, dict):
                return _clean(data.get("place", ""))
        except ValueError:
            pass
    return _clean(text)


async def derive_place(utility, *, name: str, scenario: str = "",
                       description: str = "", first_mes: str = "") -> str:
    """Ask the utility model where she lives. Never raises: a failure here must
    not fail an import, so the caller gets the mechanical answer and she still
    ends up standing somewhere of her own."""
    mechanical = mechanical_place(name, scenario=scenario, description=description,
                                  first_mes=first_mes)
    material = "\n\n".join(part for part in (
        f"Scenario:\n{_clean(scenario)}" if _clean(scenario) else "",
        f"Setting material from her description:\n{place_excerpt(description)}"
        if place_excerpt(description) else "",
        f"Her first message:\n{_clean(first_mes)}" if _clean(first_mes) else "",
    ) if part)
    if not material:
        material = _clean(description)
    if not material:
        return mechanical
    try:
        raw = await utility.complete([
            {"role": "system", "content": SETTING_SYSTEM},
            {"role": "user", "content":
                f"Character name: {_clean(name)}\n\n{material[:6000]}"},
        ])
    except Exception:
        log.exception("setting: the utility model couldn't place %s — falling "
                      "back to the card's own words", name)
        return mechanical
    place = _clean(_macros(_parse(raw), name))[:MAX_PLACE_CHARS].strip()
    if not place:
        log.warning("setting: empty place for %s — falling back to the card's "
                    "own words", name)
        return mechanical
    return place


# ------------------------------------------------------------------ the file

def render_markdown(name: str, place: str) -> str:
    """`vault/world/setting.md`, as text — a comment, a heading, the prose."""
    body = _clean(place) or "_(The card says nothing about where she is.)_"
    return (
        f"<!-- Where {_clean(name) or 'she'} is — the standing place her every "
        f"prompt puts her in (SPEC §2.5).\n"
        f"     {DERIVED_MARK}, and yours to edit. This is the sentence that "
        f"decides\n"
        "     whether she believes she is in her own room or in ours: it "
        "replaces the\n"
        "     house place in the embodiment truth, so write it as *where she "
        "is*, in\n"
        "     the second person and the present tense.\n"
        "\n"
        "     Leave the hour, the weather and the music out — the runtime "
        "injects the\n"
        "     real ones every turn, and a standing sentence about the rain "
        "would argue\n"
        "     with the live one. Write {user} for the person she talks to.\n"
        "\n"
        "     Empty this file and she gets no place line at all, which is "
        "honest;\n"
        "     delete it and the shipped character's room comes back, which is "
        "not. -->\n"
        "\n"
        "# Where she is\n"
        "\n"
        f"{body}\n")


def place_of(markdown: str) -> str:
    """The prose out of a `setting.md`, comment header and heading stripped.

    Tolerant on purpose — this file is meant to be opened in an editor, and a
    person who deletes the heading, or the comment, or both, has still written a
    perfectly good setting.
    """
    text = re.sub(r"<!--.*?-->", " ", str(markdown or ""), flags=re.DOTALL)
    lines = [line for line in text.splitlines()
             if not line.strip().startswith("#")]
    body = _clean("\n".join(lines))
    # `_(…)_` is this Vault's way of writing "nothing here yet" (the importer's
    # seeds, the empty goals file). A file saying so is a file with no place in
    # it, and reading it as prose would put the words "nothing written yet" in
    # every prompt she reads.
    if not body or _PLACEHOLDER.fullmatch(body):
        return ""
    return body[:MAX_PLACE_CHARS].strip()


def read_place(vault: str | Path) -> str:
    """Her standing place, read out of the Vault. "" when she has none — which
    is the signal to leave the house place where it is."""
    path = Path(vault) / "world" / "setting.md"
    try:
        return place_of(path.read_text(encoding="utf-8"))
    except OSError:
        return ""


def write_setting(path: str | Path, name: str, place: str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(name, place), encoding="utf-8")
    return path


def write_authored(path: str | Path, place: str) -> Path:
    """A setting a person wrote, saved without the derived marker — so no later
    background pass gets to improve it out from under them."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = _clean(place)
    path.write_text("# Where she is\n\n"
                    + (body if body else "_(Nothing written yet.)_") + "\n",
                    encoding="utf-8")
    return path


def is_derived(path: str | Path) -> bool:
    """Whether this file is still the machine's work."""
    try:
        return DERIVED_MARK in Path(path).read_text(encoding="utf-8")
    except OSError:
        return False


#: The opening `world/situation.md`, before a single turn has happened. The
#: placeholder it replaces (`_(Unknown.)_`) was a lie by omission: nothing had
#: happened yet, but the card in hand said exactly where she was standing while
#: it didn't.
def opening_situation(place: str) -> str:
    lines = ["# Current situation", ""]
    if _clean(place):
        lines += [_clean(place).replace("{user}", "the person she talks to"), ""]
    lines.append("Nothing has happened here yet — nobody has spoken to her, and "
                 "she has nothing running.")
    return "\n".join(lines) + "\n"


# -------------------------------------------------------- record-level entries

def card_material(record) -> tuple[str, str, str, str]:
    """(name, scenario, description, first_mes) for a character, card first.

    The card is the source of record. A character with no `card.json` — every
    install promoted from the pre-registry layout — still has her scenario in
    `SCENARIO.md` where it has always been, so there is no reason to guess.
    """
    card_path = Path(record.paths.card_json)
    if card_path.is_file():
        try:
            card = json.loads(card_path.read_text(encoding="utf-8"))
            data = card.get("data") if isinstance(card.get("data"), Mapping) else card
            if isinstance(data, Mapping):
                return (str(data.get("name") or record.display.name or ""),
                        str(data.get("scenario") or ""),
                        str(data.get("description") or ""),
                        str(data.get("first_mes") or ""))
        except (OSError, ValueError):
            log.exception("setting: couldn't read %s's card", record.id)
    soul = Path(record.paths.vault) / "soul"
    scenario = _soul_section(soul / "SCENARIO.md", "Scenario")
    description = "\n\n".join(part for part in (
        _soul_section(soul / "CONSTITUTION.md", "Identity"),
        _soul_section(soul / "PERSONA.md", "Appearance")) if part)
    return (record.display.name, scenario, description,
            _soul_section(soul / "BOOTSTRAP.md", "Cold open"))


def _soul_section(path: Path, heading: str) -> str:
    if not path.is_file():
        return ""
    try:
        _front, body = parse_md(path)
    except (OSError, ValueError, yaml.YAMLError):
        return ""
    return split_sections(body).get(heading, "")


def ensure_setting(record) -> Path | None:
    """Make sure this character stands somewhere of her own. Cheap, synchronous,
    and safe to call on every start.

    Three cases, in order:

    1. She already has a `setting.md` — hers, untouched.
    2. She has a card, or a SOUL with a scenario — derive it mechanically, right
       now. That is everyone who arrived through the importer or the creator,
       including everyone imported before this existed.
    3. Neither says anything about a place — write nothing, and the house place
       stays. She is either the shipped companion (for whom it is true) or a
       character whose card never said, and inventing a room for her offline is
       worse than leaving the one sentence that at least admits it is a room.
    """
    path = Path(record.paths.setting)
    if path.is_file():
        return path
    name, scenario, description, first_mes = card_material(record)
    place = mechanical_place(name or record.display.name, scenario=scenario,
                             description=description, first_mes=first_mes)
    if not place:
        return None
    return write_setting(path, name or record.display.name, place)


async def refine_setting(record, utility, *, force: bool = False) -> bool:
    """Rewrite a character's setting with the utility model's version.

    The importer already left a mechanical one behind, so this is an improvement
    pass, not a repair: it runs after import, and again on demand for characters
    imported before any of this existed. Returns whether the file changed.

    What it will not do is touch a file a human has edited — that is the point
    of the marker line. Once you have written her room yourself, no background
    pass gets to redecorate it.
    """
    path = Path(record.paths.setting)
    if not force and path.is_file() and not is_derived(path):
        log.info("setting: %s has a hand-edited setting — leaving it", record.id)
        return False
    name, scenario, description, first_mes = card_material(record)
    if not any((scenario.strip(), description.strip(), first_mes.strip())):
        return False
    before = path.read_text(encoding="utf-8") if path.is_file() else ""
    place = await derive_place(utility, name=name or record.display.name,
                               scenario=scenario, description=description,
                               first_mes=first_mes)
    if not place:
        return False
    write_setting(path, name or record.display.name, place)
    return path.read_text(encoding="utf-8") != before


def derived_material(record) -> dict[str, Any]:
    """What the studio needs to show and re-derive the setting."""
    path = Path(record.paths.setting)
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    return {"setting": place_of(text) if text else "",
            "derived": bool(text) and DERIVED_MARK in text,
            "exists": bool(text)}
