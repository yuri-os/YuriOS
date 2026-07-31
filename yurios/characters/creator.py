"""Create a character from a draft — the switchboard's missing half.

Until now a character could only come into existence by importing somebody
else's `.PNG`. That is a strange shape for a runtime whose whole pitch is a
companion you own: the only door in was a card from the internet.

This is the importer with the parsing taken out. It reuses the same vault
skeleton (`_create_vault`), the same transactional temp-dir-then-`os.replace`
discipline, and the same git seeding — because those are already right, and a
second, weaker copy of a transactional filesystem write is exactly the kind of
thing that leaves half a character on disk after a full volume.

Two things differ from an import, both deliberate:

  * **Lifecycle.** An imported card is `review_required` until you have looked at
    it, because it is a stranger's prose that will be fed to a model. A character
    you authored in the studio has already been reviewed — you wrote it — so she
    is enabled and autostarts.
  * **No `source-card.png`.** Nothing was imported, so there is nothing to keep.
    `card.json` is written from the draft as the authored card.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .importer import (
    CharacterImportError,
    _create_vault,
    _initialize_git,
    _sanitize_portrait,
)
from .card import CardLimits
from .defaults import install_default_portrait
from .models import (
    CharacterPaths,
    CharacterRecord,
    DisplayMetadata,
    LifecycleFlags,
    LoopSwitches,
    new_character_id,
)
from .registry import CharacterRegistry
from .studio import Draft, _set_manifest, write_soul
from .soulfiles import parse_md, split_sections

#: Where a blank studio gets its starting shape. Not a hardcoded companion — the
#: repo's own canon, so "Create character" opens on something coherent that the
#: user overwrites rather than on eight empty textareas.
SOUL_SRC = Path(__file__).resolve().parents[2] / "soul-src"


def _section(folder: Path, filename: str, heading: str) -> str:
    path = folder / filename
    if not path.is_file():
        return ""
    try:
        _front, body = parse_md(path)
    except (OSError, ValueError):
        return ""
    return split_sections(body).get(heading, "")


def template_draft(soul_src: Path | None = None) -> Draft:
    """A starting draft from `soul-src`, or an empty one if it is not installed."""
    folder = Path(soul_src or SOUL_SRC)
    if not (folder / "soul.yaml").is_file():
        return Draft(name="", character_version="1.0.0")
    import yaml
    try:
        manifest = yaml.safe_load((folder / "soul.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return Draft(name="", character_version="1.0.0")

    persona_front: dict[str, Any] = {}
    if (folder / "PERSONA.md").is_file():
        persona_front, _body = parse_md(folder / "PERSONA.md")
    scenario_sections: dict[str, str] = {}
    if (folder / "SCENARIO.md").is_file():
        _front, body = parse_md(folder / "SCENARIO.md")
        scenario_sections = split_sections(body)
    examples: list[str] = []
    if (folder / "EXAMPLES.md").is_file():
        _front, body = parse_md(folder / "EXAMPLES.md")
        examples = [text for heading, text in split_sections(body).items()
                    if heading.lower().startswith("example")]
    lorebook = _template_lorebook(folder)

    draft = Draft(
        name=str(manifest.get("name") or ""),
        creator=str(manifest.get("creator") or ""),
        character_version=str(manifest.get("character_version") or "1.0.0"),
        tags=[str(tag) for tag in (manifest.get("tags") or [])],
        identity=_section(folder, "CONSTITUTION.md", "Identity"),
        history=_section(folder, "CONSTITUTION.md", "History"),
        appearance=_section(folder, "PERSONA.md", "Appearance"),
        manner=_section(folder, "PERSONA.md", "Manner"),
        personality=str(persona_front.get("personality") or ""),
        scenario=scenario_sections.get("Scenario", ""),
        first_mes=_section(folder, "BOOTSTRAP.md", "Cold open"),
        alternate_greetings=[text for heading, text in scenario_sections.items()
                             if heading.lower().startswith("alternate greeting")],
        examples=examples,
        system_prompt=_section(folder, "CONSTITUTION.md", "Voice law"),
        post_history_instructions=_section(folder, "CONSTITUTION.md", "Hard limits"),
        creator_notes="",
    )
    if lorebook["entries"]:
        draft.lorebook = lorebook
    return draft


def _template_lorebook(folder: Path) -> dict[str, Any]:
    """`WORLD.md` → draft lore entries.

    Easy to leave out, and the omission is silent: a character created from the
    template would simply have no lorebook, and the first sign of it would be
    her not knowing the name of the city she lives in.
    """
    path = folder / "WORLD.md"
    entries: list[dict[str, Any]] = []
    front: dict[str, Any] = {}
    if path.is_file():
        front, body = parse_md(path)
        for heading, content in split_sections(body).items():
            lines = content.strip().splitlines()
            keys: list[str] = []
            rest = lines
            for index, line in enumerate(lines):
                if line.lower().startswith("keys:"):
                    keys = [k.strip() for k in line.split(":", 1)[1].split(",") if k.strip()]
                    rest = lines[:index] + lines[index + 1:]
                    break
            text = "\n".join(rest).strip()
            if text:
                entries.append({"name": heading, "keys": keys or [heading],
                                "content": text, "constant": False,
                                "use_regex": False, "case_sensitive": False})
    return {
        "scan_depth": int(front.get("scan_depth") or 4),
        "token_budget": int(front.get("token_budget") or 600),
        "recursive_scanning": bool(front.get("recursive_scanning") or False),
        "entries": entries,
    }


def draft_to_fields(draft: Draft) -> dict[str, Any]:
    """The `fields` mapping `_create_vault` seeds a SOUL from."""
    return {
        "name": draft.name,
        "creator": draft.creator,
        "character_version": draft.character_version,
        "description": draft.description,
        "personality": draft.personality,
        "scenario": draft.scenario,
        "first_mes": draft.first_mes,
        "alternate_greetings": list(draft.alternate_greetings),
        "mes_example": "\n".join(f"<START>\n{block}" for block in draft.examples),
        "system_prompt": draft.system_prompt,
        "post_history_instructions": draft.post_history_instructions,
        "creator_notes": draft.creator_notes,
        "tags": list(draft.tags),
        "character_book": {
            "entries": [
                {"name": entry.get("name"), "keys": entry.get("keys"),
                 "content": entry.get("content")}
                for entry in draft.lorebook.get("entries") or []
            ]
        },
    }


def create_character(registry: CharacterRegistry, draft: Draft, *,
                     character_id: str | None = None,
                     portrait: bytes | None = None,
                     limits: CardLimits | None = None,
                     initialize_git: bool = True,
                     loops: LoopSwitches | None = None) -> CharacterRecord:
    """Author a new character root from a draft. Transactional: all or nothing."""
    limits = limits or CardLimits()
    name = draft.name.strip()
    if not name:
        raise CharacterImportError("a character needs a name")

    characters_dir = registry.data_root / "characters"
    if character_id is None:
        unavailable = {record.id for record in registry}
        if characters_dir.is_dir():
            unavailable.update(path.name for path in characters_dir.iterdir())
        character_id = new_character_id(name, unavailable)
    if registry.get(character_id) is not None:
        raise CharacterImportError(f"character already exists: {character_id}")
    final_root = characters_dir / character_id
    if final_root.exists():
        raise CharacterImportError(f"character root already exists: {final_root}")

    fields = draft_to_fields(draft)
    card = {"spec": "chara_card_v3", "spec_version": "3.0",
            "data": {**fields, "extensions": {"yurios": {"schema_version": 1}}}}

    record = CharacterRecord(
        id=character_id,
        display=DisplayMetadata(name=name, creator=draft.creator,
                                description=draft.description,
                                tags=list(draft.tags)),
        paths=CharacterPaths.under(final_root),
        # You wrote her. There is nothing here to review.
        lifecycle=LifecycleFlags(enabled=True, autostart=True, review_required=False),
        loops=loops or LoopSwitches(),
    )

    characters_dir.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{character_id}.", dir=characters_dir))
    staged = CharacterPaths.under(temporary)
    moved = False
    try:
        staged.card_json.write_text(
            json.dumps(card, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
        if portrait:
            staged.portrait.write_bytes(_sanitize_portrait(portrait, limits))
        else:
            install_default_portrait(staged, name)
        _create_vault(staged.vault, fields, name)
        # The skeleton is the importer's; the persona is the studio's. Writing
        # it through the same writer the studio saves with is what makes
        # create → read → save an identity rather than a lossy pass through
        # `_create_soul`'s card-shaped flattening.
        write_soul(staged.vault / "soul", draft)
        # `_create_soul` stamps `canon: imported` because it normally runs behind
        # the importer. Nothing was imported here, and the canon travels into
        # every card version string and every journal entry (§5.2), so say so.
        _set_manifest(staged.vault / "soul" / "soul.yaml", {"canon": "original"})
        for directory in (staged.corpus, staged.traces, staged.tool_logs, staged.selfies):
            directory.mkdir(parents=True, exist_ok=True)
        if initialize_git:
            _initialize_git(staged.vault, message="vault: create character")
        os.replace(temporary, final_root)
        moved = True
        registry.add(record)
        return record
    except Exception:
        shutil.rmtree(final_root if moved else temporary, ignore_errors=True)
        raise


def create_from_dict(registry: CharacterRegistry, value: Mapping[str, Any],
                     **kwargs: Any) -> CharacterRecord:
    return create_character(registry, Draft.from_dict(value), **kwargs)
