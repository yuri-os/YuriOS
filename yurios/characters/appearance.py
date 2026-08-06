"""Deriving a character's *visual* identity from the card she was imported from.

A character card is written for a language model: prose about who she is, how
she talks, what she wants, with her looks scattered through it in whatever shape
the author felt like. The forge needs the other thing — a compact noun-phrase
description of a person, the kind an image model can actually render. This turns
the first into the second, once, at import, and writes it where the camera looks:
``data/characters/<id>/appearance.yaml``.

Two rules shape the output, both inherited from the shipped ``yuri.yaml``:

- **Identity is who she is, not what she wears.** Clothing belongs to the
  wardrobe slot and to whatever she asks for at call time, so an outfit baked in
  here would fight every selfie that wanted something else. The card's clothing
  prose is deliberately dropped.
- **Identity is visual only.** Age in years, occupation, temperament and history
  are all real facts about her and all noise to a renderer — "19" renders
  nothing, "a young woman" renders a face.

The file it writes carries only what is hers (``name``, ``identity``, and any
guard her look specifically needs). The register, the text guard and the
structural anatomy guard are inherited from ``forge/characters/_register.yaml``
— see ``Character.load(defaults=…)``. That inheritance is the point: it keeps a
derived file two fields long and lets the house improve the register later
without rewriting every character.

Derivation is best-effort by design. The utility model writes a better identity
than any parser, but a machine with no key, no network, or a model in a mood
still has to end up with a character who looks like herself, so
``mechanical_identity`` is always there underneath.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Mapping

import yaml

from .soulfiles import parse_md, split_sections

log = logging.getLogger("characters.appearance")

# The character shipped with the repo. Only reachable by a character who has no
# card at all — see `ensure_appearance`, case 3.
HOUSE_CHARACTER = (Path(__file__).resolve().parent.parent
                   / "forge" / "characters" / "yuri.yaml")

# Sections of a card's description that are about the body. Cards are wildly
# inconsistent, so this is a net, not a schema — it catches the common headers
# ("Appearance:", "Physical description", "Looks") and is happy to miss.
_VISUAL_HEADERS = re.compile(
    r"^\s*[*_#\-\s]*(appearance|looks|physical(\s+description)?|body|"
    r"description of (her|his|their) (looks|appearance)|features)\s*[:\-]",
    re.IGNORECASE)
_SECTION_BREAK = re.compile(r"^\s*[*_#\-\s]*([A-Z][A-Za-z /]{2,30})\s*[:\-]\s*$")
# Facts that are true about her and useless to a renderer.
_NON_VISUAL = re.compile(
    r"^\s*[*_#\-\s]*(age|birthday|gender|occupation|status|job|role|"
    r"personality|likes|dislikes|hobbies|goals|backstory|history|"
    r"relationship|sexuality|orientation|name|alias|clothing|outfit|"
    r"wardrobe|attire|dress)\s*[:\-]", re.IGNORECASE)

APPEARANCE_SYSTEM = """\
You extract a character's visual appearance for an image generator.

You are given a character card written for a chat model. Return ONLY what a \
renderer can draw: species, apparent age band, build, height impression, skin, \
hair, eyes, face, and any distinguishing marks, ears, horns, wings or tails.

Hard rules:
- Write ONE flowing noun phrase describing the person, present tense, no name, \
no pronoun subject. Start like "a tall young woman with ..." .
- NO clothing, outfit, uniform or accessories. Those are chosen per picture and \
anything you put here would fight them.
- NO age in years, occupation, personality, backstory, powers, relationships or \
mood. "19" and "night-shift nurse" draw nothing.
- NO scene, pose, camera, lighting or background. Only the person.
- Prefer what the card actually says. Invent only what is needed to make an \
under-described feature renderable, and keep any invention plain.
- If the card describes something a renderer commonly gets wrong (unusual skin \
tone, a precise number of a feature, a mark that must stay small), add a short \
negative phrasing it as what must NOT appear.

Return ONLY a JSON object, no prose around it:
{"identity": "<the noun phrase>", "negative": "<what must not appear, or \"\">"}"""


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def visual_excerpt(description: str) -> str:
    """The parts of a card's description that are plausibly about her body.

    Used to focus the model (a 4000-word card buries the two useful lines) and
    as the raw material for the mechanical fallback. Returns "" when the card
    has no recognisable appearance section — the caller then falls back to the
    whole description, which is noisier but never empty.
    """
    lines = str(description or "").splitlines()
    kept: list[str] = []
    capturing = False
    for line in lines:
        if _VISUAL_HEADERS.match(line):
            capturing = True
            tail = line.split(":", 1)[-1] if ":" in line else ""
            if _clean(tail):
                kept.append(_clean(tail))
            continue
        if capturing:
            # A new non-visual header ends the run; a blank line does not, since
            # plenty of cards double-space inside a section.
            if _NON_VISUAL.match(line) or (_SECTION_BREAK.match(line)
                                           and not _VISUAL_HEADERS.match(line)):
                capturing = False
                continue
            if _clean(line):
                kept.append(_clean(line))
    return " ".join(kept).strip()


def mechanical_identity(name: str, description: str) -> str:
    """The no-model fallback: her appearance section, stripped of the lines that
    are plainly not about her body. Worse prose than the model writes, and still
    unmistakably *her* rather than somebody else — which is the whole job."""
    excerpt = visual_excerpt(description)
    if not excerpt:
        # No appearance section: keep only description lines that aren't
        # obviously non-visual, and cap it — a whole card as an identity block
        # drowns the render, but it beats a stranger's face.
        excerpt = " ".join(_clean(l) for l in str(description or "").splitlines()
                           if _clean(l) and not _NON_VISUAL.match(l))
    excerpt = _clean(excerpt)[:1200]
    return excerpt or f"a person named {_clean(name) or 'her'}, appearance unspecified"


def _parse(raw: str) -> tuple[str, str]:
    """Pull (identity, negative) out of the model's answer, tolerantly — a JSON
    object is what we asked for, a fenced one is what we sometimes get, and bare
    prose is still usable as the identity."""
    text = str(raw or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, dict):
                return (_clean(data.get("identity", "")),
                        _clean(data.get("negative", "")))
        except ValueError:
            pass
    return _clean(text), ""


async def derive_identity(utility, *, name: str, description: str,
                          personality: str = "") -> tuple[str, str]:
    """Ask the utility model for (identity, negative). Never raises: a failure
    here must not fail an import, so the caller gets the mechanical answer and
    the character still ends up looking like herself."""
    excerpt = visual_excerpt(description) or _clean(description)
    if not excerpt and not _clean(personality):
        return mechanical_identity(name, description), ""
    try:
        raw = await utility.complete([
            {"role": "system", "content": APPEARANCE_SYSTEM},
            {"role": "user", "content":
                f"Character name: {_clean(name)}\n\n"
                f"Appearance material from her card:\n\n{excerpt[:6000]}"},
        ])
    except Exception:
        log.exception("appearance: the utility model couldn't describe %s — "
                      "falling back to the card's own words", name)
        return mechanical_identity(name, description), ""
    identity, negative = _parse(raw)
    if not identity:
        log.warning("appearance: empty identity for %s — falling back to the "
                    "card's own words", name)
        return mechanical_identity(name, description), ""
    return identity, negative


def render_yaml(name: str, identity: str, negative: str = "") -> str:
    """The appearance file, as text. Only her fields — the register, the text
    guard and the structural anatomy guard are inherited, not copied, so this
    file stays readable and the house can improve the rest underneath her."""
    body: dict[str, Any] = {"name": _clean(name), "identity": _clean(identity)}
    if _clean(negative):
        body["character_negative_extra"] = _clean(negative)
    header = (
        "# Her visual identity — who the camera renders (SPEC §7.6).\n"
        "#\n"
        "# Derived from her character card at import and yours to edit: this is\n"
        "# the file that decides whether a selfie looks like her. Identity is\n"
        "# *body only* — face, build, hair, species, marks — and deliberately no\n"
        "# clothing, because wardrobe is chosen per picture and an outfit baked\n"
        "# in here would fight every shot that wanted another.\n"
        "#\n"
        "# The quality register, the no-text guard and the structural anatomy\n"
        "# guard are inherited from forge/characters/_register.yaml. Add to the\n"
        "# guard with `character_negative_extra`; override the register itself\n"
        "# only if her look genuinely isn't the house style.\n\n")
    return header + yaml.safe_dump(body, allow_unicode=True, sort_keys=False,
                                   default_flow_style=False, width=78)


def write_appearance(path: str | Path, name: str, identity: str,
                     negative: str = "") -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_yaml(name, identity, negative), encoding="utf-8")
    return path


def card_fields(card: Mapping[str, Any]) -> tuple[str, str, str]:
    """(name, description, personality) out of a v2/v3 card, either shape."""
    data = card.get("data") if isinstance(card.get("data"), Mapping) else card
    return (str(data.get("name", "") or ""),
            str(data.get("description", "") or ""),
            str(data.get("personality", "") or ""))


def soul_material(record) -> tuple[str, str]:
    """(name, appearance prose) read straight out of her SOUL folder.

    The fallback for a character with no `card.json` — which is every install
    promoted from the pre-registry layout, because `yurios/migrate.py` moves the
    Vault and never writes a card. Her looks are in `PERSONA.md#Appearance`
    where they have always been, so there is no reason to guess at them.
    """
    soul = Path(record.paths.vault) / "soul"
    name = ""
    manifest = soul / "soul.yaml"
    if manifest.is_file():
        try:
            loaded = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
            name = str((loaded or {}).get("name") or "") if isinstance(loaded, Mapping) else ""
        except (OSError, yaml.YAMLError):
            name = ""
    parts: list[str] = []
    for filename, headings in (("PERSONA.md", ("Appearance",)),
                               ("CONSTITUTION.md", ("Identity",))):
        path = soul / filename
        if not path.is_file():
            continue
        try:
            _front, body = parse_md(path)
        except (OSError, ValueError, yaml.YAMLError):
            continue
        sections = split_sections(body)
        parts.extend(sections[h] for h in headings if sections.get(h))
    return name, "\n\n".join(parts)


def _shipped_house_character(name: str) -> bool:
    """Whether *name* is the character the shipped appearance file describes.

    The one honest reason to hand somebody else's file to a character: she has
    no card and no soul because she predates all of this, and she is literally
    the companion this repo ships. Anyone else must not inherit that face —
    `world/selfies.py::_identity` says why, and it is the exact bug (a renamed
    legacy companion rendering with Yuri's cat ears and Yuri's name in the
    provenance sidecar) that skipping this check reintroduced.
    """
    if not name.strip() or not HOUSE_CHARACTER.is_file():
        return False
    try:
        shipped = yaml.safe_load(HOUSE_CHARACTER.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return False
    return _clean(name).casefold() == _clean(
        (shipped or {}).get("name", "") if isinstance(shipped, Mapping) else "").casefold()


def _borrowed_house_face(record, path: Path) -> bool:
    """Whether this file is the shipped character's, worn by somebody else.

    A repair, not a rule. An earlier `ensure_appearance` copied the shipped
    appearance file to *any* character with no card, so installs already exist
    where a renamed legacy companion renders as Yuri — and because that copy
    carries no `render_yaml` marker, `refine_appearance` reads it as
    hand-edited and will never replace it. Detecting the verbatim copy is what
    lets one restart put her own face back.
    """
    try:
        if path.read_text(encoding="utf-8") != HOUSE_CHARACTER.read_text(encoding="utf-8"):
            return False
    except OSError:
        return False
    soul_name, _prose = soul_material(record)
    if _shipped_house_character(soul_name or record.display.name):
        return False                           # it really is her file
    log.warning("appearance: %s was wearing the shipped character's appearance "
                "file — deriving her own instead", record.id)
    return True


def ensure_appearance(record) -> Path | None:
    """Make sure this character has a face of her *own* before her camera is
    built. Cheap, synchronous, and safe to call on every start.

    Four cases, in order:

    1. She already has an appearance file — hers, untouched.
    2. She has a card — derive from it mechanically, right now. Every character
       who arrived through import or the creator has one, so this is the path
       almost everyone takes; the utility model improves on it later.
    3. No card, but a SOUL — derive from `PERSONA.md#Appearance`. This is the
       migrated pre-registry install, and her looks are already written down.
    4. Neither, and she is the companion this repo ships — the shipped file is
       genuinely hers, verbatim, register and wardrobe tiers included.

    A character who is none of those gets None, and the caller renders the
    neutral stand-in: a photo of no one beats a photo of the wrong person.
    """
    path = Path(record.paths.appearance)
    display = record.display.name
    if path.is_file() and not _borrowed_house_face(record, path):
        return path
    card_path = Path(record.paths.card_json)
    if card_path.is_file():
        try:
            card = json.loads(card_path.read_text(encoding="utf-8"))
            name, description, _ = card_fields(card)
            return write_appearance(path, name or display,
                                    mechanical_identity(name or display, description))
        except (OSError, ValueError):
            log.exception("appearance: couldn't read %s's card", record.id)
            return None
    soul_name, prose = soul_material(record)
    name = soul_name or display
    if prose.strip():
        log.info("appearance: %s has no card — deriving her likeness from her "
                 "own PERSONA.md", record.id)
        return write_appearance(path, name, mechanical_identity(name, prose))
    if _shipped_house_character(name):
        log.info("appearance: %s predates the character registry — seeding her "
                 "appearance from the shipped %s", record.id, HOUSE_CHARACTER.name)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(HOUSE_CHARACTER.read_text(encoding="utf-8"),
                            encoding="utf-8")
            return path
        except OSError:
            log.exception("appearance: couldn't seed %s's appearance", record.id)
            return None
    log.warning("appearance: %s has no card and no appearance prose in her SOUL "
                "— leaving her without a likeness rather than lending her "
                "another character's. Write vault/soul/PERSONA.md#Appearance, "
                "or her appearance.yaml directly.", record.id)
    return None


async def refine_appearance(record, utility, *, force: bool = False) -> bool:
    """Rewrite a character's appearance file with the utility model's version.

    The importer already left a mechanical one behind, so this is an
    improvement pass, not a repair: it runs after import, and again on demand
    for characters imported before any of this existed. Returns whether the
    file changed.

    `force=False` still rewrites — the mechanical file is exactly what this
    replaces. What it won't do is touch a file a human has edited, which is the
    point of the marker line: once you have written her face yourself, no
    background pass gets to overwrite it.
    """
    path = Path(record.paths.appearance)
    if not force and path.is_file() and not _is_derived(path):
        log.info("appearance: %s has a hand-edited appearance file — leaving it",
                 record.id)
        return False
    card_path = Path(record.paths.card_json)
    if not card_path.is_file():
        log.warning("appearance: %s has no card.json to read a likeness from",
                    record.id)
        return False
    try:
        card = json.loads(card_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.exception("appearance: %s has an unreadable card.json", record.id)
        return False
    before = path.read_text(encoding="utf-8") if path.is_file() else ""
    await derive_for_card(utility, card, path)
    return path.read_text(encoding="utf-8") != before


def _is_derived(path: Path) -> bool:
    """Whether this file is still the machine's work. The header `render_yaml`
    writes is the marker — remove or edit past it and the file is yours."""
    try:
        return "Derived from her character card at import" in path.read_text(
            encoding="utf-8")
    except OSError:
        return False


async def derive_for_card(utility, card: Mapping[str, Any],
                          path: str | Path) -> Path:
    """The import-time entry point: card in, appearance.yaml out. With no
    utility model (tests, a keyless machine) this still writes a file — the
    mechanical one — because the alternative is a character with no face of her
    own, and that is the bug this whole module exists to close."""
    name, description, personality = card_fields(card)
    if utility is None:
        return write_appearance(path, name, mechanical_identity(name, description))
    identity, negative = await derive_identity(
        utility, name=name, description=description, personality=personality)
    return write_appearance(path, name, identity, negative)
