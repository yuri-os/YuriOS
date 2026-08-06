"""Her *own* selfie library — the per-character override of the shipped book.

The shipped ``forge/templates/selfie.yaml`` describes one character's world: a
sanctuary above a rainy megacity, magenta/cyan/amber, a tail in half the scenes.
It is a fine starting point and a bad description of anybody else. An overlay
(``SELFIE_TEMPLATES_EXTRA``) can *add* to it, but it cannot take the tail back
out — and a house running four characters has one env var to share between them.

So a character may carry her own ``selfie.yaml`` beside her ``appearance.yaml``,
and when she does it **replaces** the shipped library outright rather than
merging over it (``world/selfies.py::build_forge``). Same file, same schema, same
loader — the only difference is which book the forge and the `take_selfie`
description are built from. No file means the shipped defaults, unchanged; the
env overlay still layers on top of whichever base wins, so a house-wide register
keeps working for characters who never opened the studio.

This module is the *editor's* half: the yaml as structured rows the card studio
can render, reorder and expand, and the way back to a file the loader accepts.
It deliberately owns the whole file — comments and key order are regenerated from
the rows on every save, exactly like ``appearance.py`` owns the appearance file —
because a round-trip that half-preserved a hand-written file is worse than one
that is honest about rewriting it.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Mapping

import yaml

log = logging.getLogger("characters.selfiebook")

#: The library every character starts from (→ forge/templates.py).
SHIPPED = (Path(__file__).resolve().parent.parent
           / "forge" / "templates" / "selfie.yaml")

#: The five slots a selfie is composed from, in the order the studio shows them.
#: `SelfieBook.compose` reads exactly these section names, so this tuple is also
#: the list of sections a book file may carry.
SLOTS: tuple[tuple[str, str, str], ...] = (
    ("scenes", "Scenes", "Where she is — the room, the window, the rooftop."),
    ("framings", "Framing", "How the camera is held, and how much fits in it."),
    ("lighting", "Lighting", "What the light is doing to her."),
    ("moods", "Moods", "What her face is doing."),
    ("wardrobe", "Wardrobe", "What she's wearing. A tier, never a gate — "
                             "whether it renders is the backend's call."),
)

SLOT_NAMES = tuple(name for name, _label, _hint in SLOTS)

HEADER = """\
# {name}'s selfie library — her camera's own vocabulary (SPEC §7.6).
#
# This file REPLACES the shipped library (yurios/forge/templates/selfie.yaml)
# for this character: a selfie is composed as scene + framing + wardrobe +
# lighting + mood, and these are the rows those slots may name. Naming a slot
# pins it; leaving it out leaves it out (an unprompted shot with nothing else
# to go on rotates one in, seeded).
#
# Written by the card studio and yours to edit by hand. The studio regenerates
# this file from its rows on save, so comments you add below will not survive a
# save from the page — keep notes in `tool_hint`, which she actually reads.
#
# An entry is a plain string, or a mapping for the two mechanics that make a
# tier real instead of decorative:
#   prompt:   the fragment composed into the picture
#   negative: what must NOT appear, for a look that fights the generator's prior
#   pinned:   true = named asks only, never rotated into an unprompted shot
#
# Delete this file to go back to the shipped library.

"""


def _entry(value: Any) -> dict[str, Any]:
    """One row, from either shape the loader accepts."""
    if isinstance(value, Mapping):
        return {"prompt": str(value.get("prompt", "") or ""),
                "negative": str(value.get("negative", "") or ""),
                "pinned": bool(value.get("pinned"))}
    return {"prompt": str(value or ""), "negative": "", "pinned": False}


def parse(data: Mapping[str, Any] | None) -> dict[str, Any]:
    """A loaded yaml mapping as the studio's row model.

    Rows are a *list*, not a mapping, because the page lets you rename a key
    while typing and a dict keyed on a half-typed name loses the row you are
    editing. Order is the file's order, which is the order she reads them in.
    """
    data = data if isinstance(data, Mapping) else {}
    slots: dict[str, list[dict[str, Any]]] = {}
    for name in SLOT_NAMES:
        section = data.get(name)
        rows = []
        if isinstance(section, Mapping):
            for key, value in section.items():
                rows.append({"key": str(key), **_entry(value)})
        slots[name] = rows
    return {"tool_hint": str(data.get("tool_hint", "") or ""), "slots": slots}


def read(path: str | Path) -> dict[str, Any]:
    """Parse a book file. Raises ValueError on anything the loader would choke
    on later — a broken library should be refused by the page that can fix it,
    not by her camera an hour after you saved it."""
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"could not read the selfie library: {exc}") from exc
    if data is not None and not isinstance(data, Mapping):
        raise ValueError("a selfie library must be a mapping of slots")
    return parse(data)


def normalise(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """The studio's payload, cleaned: blank keys and blank prompts dropped, a
    duplicate key keeping its last row (which is what the file would have done
    anyway — the yaml mapping has one entry per key)."""
    value = value if isinstance(value, Mapping) else {}
    incoming = value.get("slots") if isinstance(value.get("slots"), Mapping) else value
    slots: dict[str, list[dict[str, Any]]] = {}
    for name in SLOT_NAMES:
        rows: dict[str, dict[str, Any]] = {}
        for raw in (incoming or {}).get(name) or []:
            if not isinstance(raw, Mapping):
                continue
            key = str(raw.get("key", "") or "").strip()
            prompt = str(raw.get("prompt", "") or "").strip()
            if not key or not prompt:
                continue                       # a half-typed row is not a tier
            rows[key] = {"key": key, "prompt": prompt,
                         "negative": str(raw.get("negative", "") or "").strip(),
                         "pinned": bool(raw.get("pinned"))}
        slots[name] = list(rows.values())
    return {"tool_hint": str(value.get("tool_hint", "") or "").strip(),
            "slots": slots}


def to_yaml_data(book: Mapping[str, Any]) -> dict[str, Any]:
    """The row model as the mapping `SelfieBook.load` reads. Plain strings for
    plain tiers — a file full of one-key mappings is unreadable, and the loader
    accepts both."""
    data: dict[str, Any] = {}
    if book.get("tool_hint"):
        data["tool_hint"] = book["tool_hint"]
    slots = book.get("slots") or {}
    for name in SLOT_NAMES:
        section: dict[str, Any] = {}
        for row in slots.get(name) or []:
            if row.get("negative") or row.get("pinned"):
                entry: dict[str, Any] = {"prompt": row["prompt"]}
                if row.get("negative"):
                    entry["negative"] = row["negative"]
                if row.get("pinned"):
                    entry["pinned"] = True
                section[row["key"]] = entry
            else:
                section[row["key"]] = row["prompt"]
        if section:
            data[name] = section
    return data


def render_yaml(book: Mapping[str, Any], name: str = "Her") -> str:
    return HEADER.format(name=name.strip() or "Her") + yaml.safe_dump(
        to_yaml_data(book), allow_unicode=True, sort_keys=False,
        default_flow_style=False, width=88)


def write(path: str | Path, book: Mapping[str, Any], name: str = "Her") -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_yaml(book, name), encoding="utf-8")
    return path


def shipped() -> dict[str, Any]:
    """The house library as rows — what the studio shows a character who has no
    file of her own, and what "reset" puts back."""
    try:
        return read(SHIPPED)
    except ValueError:
        log.exception("selfiebook: the shipped library is unreadable")
        return parse({})


def read_for(record) -> tuple[dict[str, Any], str]:
    """(book, source) for a character: her own file if she has one, otherwise
    the shipped library. `source` is what the studio's badge says, and it is the
    difference between "editing her library" and "editing a copy of ours"."""
    path = Path(record.paths.selfie_templates)
    if path.is_file():
        try:
            return read(path), "character"
        except ValueError:
            log.exception("selfiebook: %s has an unreadable selfie.yaml — "
                          "showing the shipped library instead", record.id)
    return shipped(), "shipped"
