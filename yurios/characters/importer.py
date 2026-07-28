"""Transactional import of PNG cards into self-contained character roots."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping

from PIL import Image, ImageOps, UnidentifiedImageError

from .card import CardLimits, CardParseError, card_fields, parse_png_card
from .models import (
    BodyBinding,
    CharacterPaths,
    CharacterRecord,
    ConnectionBinding,
    DisplayMetadata,
    LifecycleFlags,
    LoopSwitches,
    ModelBinding,
    VoiceBinding,
    new_character_id,
)
from .registry import CharacterRegistry


class CharacterImportError(ValueError):
    pass


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _string_list(value: object) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


def _is_yurios_card(card: Mapping[str, Any], fields: Mapping[str, Any]) -> bool:
    candidates = [card.get("yurios"), fields.get("yurios")]
    for owner in (card, fields):
        extensions = owner.get("extensions")
        if isinstance(extensions, Mapping):
            candidates.append(extensions.get("yurios"))
    return any(value is True or isinstance(value, Mapping) for value in candidates)


def _world_markdown(book: object, name: str) -> str:
    entries: object = book
    if isinstance(book, Mapping):
        entries = book.get("entries", [])
    blocks: list[str] = []
    if isinstance(entries, list):
        for index, entry in enumerate(entries, start=1):
            if not isinstance(entry, Mapping):
                continue
            title = _text(entry.get("name")) or _text(entry.get("comment")) or f"Entry {index}"
            raw_keys = entry.get("keys", entry.get("key", []))
            if isinstance(raw_keys, str):
                keys = [raw_keys]
            else:
                keys = _string_list(raw_keys)
            content = _text(entry.get("content"))
            blocks.append(
                f"## {title}\n\nkeys: {', '.join(keys) if keys else title}\n{content}".rstrip()
            )
    if not blocks:
        blocks.append(f"## {name}\n\nkeys: {name}\n")
    return "---\nsoul: world\n---\n\n# World\n\n" + "\n\n".join(blocks)


def _examples_markdown(value: str) -> str:
    parts = [part.strip() for part in value.split("<START>") if part.strip()]
    if not parts:
        parts = ["_(No example dialogue was supplied.)_"]
    blocks = [f"## Example {index}\n\n{part}" for index, part in enumerate(parts, start=1)]
    return "---\nsoul: examples\n---\n\n# Example dialogues\n\n" + "\n\n".join(blocks)


def _create_soul(soul: Path, fields: Mapping[str, Any], name: str) -> None:
    description = _text(fields.get("description"))
    personality = _text(fields.get("personality"))
    scenario = _text(fields.get("scenario"))
    first_message = _text(fields.get("first_mes"))
    alternates = _string_list(fields.get("alternate_greetings"))
    system_prompt = _text(fields.get("system_prompt"))
    post_history = _text(fields.get("post_history_instructions"))
    creator_notes = _text(fields.get("creator_notes"))
    version = _text(fields.get("character_version")) or "1.0.0"
    if not alternates:
        alternates = [first_message or f"Hello, I am {name}."]
    alternate_refs = "\n".join(
        f"    - SCENARIO.md#Alternate greeting {index}"
        for index in range(1, len(alternates) + 1)
    )

    manifest = f"""name: {_yaml_string(name)}
creator: {_yaml_string(_text(fields.get('creator')))}
character_version: {_yaml_string(version)}
spec: v3
canon: imported
portrait: portrait.png
tags: {json.dumps(_string_list(fields.get('tags')), ensure_ascii=False)}
fields:
  description:
    - CONSTITUTION.md#Identity
    - CONSTITUTION.md#History
    - PERSONA.md#Appearance
    - PERSONA.md#Manner
  personality: PERSONA.md@personality
  scenario: SCENARIO.md#Scenario
  first_mes: BOOTSTRAP.md#Cold open
  alternate_greetings:
{alternate_refs}
  mes_example: EXAMPLES.md
  system_prompt: CONSTITUTION.md#Voice law
  post_history_instructions: CONSTITUTION.md#Hard limits
  creator_notes: NOTES.md
  character_book: WORLD.md
runtime_only:
  - MEMORY.md
  - USER.md
"""
    _write(soul / "soul.yaml", manifest)
    _write(
        soul / "CONSTITUTION.md",
        f"""---
soul: constitution
mutable: false
---

# {name} - Constitution

## Identity

{description or '_(Not supplied by the card.)_'}

## History

_(No separate history was supplied by the card.)_

## Hard limits

{post_history or '_(No post-history instructions were supplied.)_'}

## Voice law

{system_prompt or 'Stay in character and follow the card persona.'}
""",
    )
    _write(
        soul / "PERSONA.md",
        f"""---
soul: persona
mutable: true
personality: {_yaml_string(personality)}
---

# {name} - Persona

## Appearance

_(The source card does not separate appearance from description.)_

## Manner

{personality or '_(Not supplied by the card.)_'}
""",
    )
    greeting_blocks = []
    for index, greeting in enumerate(alternates, start=1):
        greeting_blocks.append(f"## Alternate greeting {index}\n\n{greeting}")
    _write(
        soul / "SCENARIO.md",
        "---\nsoul: scenario\n---\n\n# Scenario and greetings\n\n"
        f"## Scenario\n\n{scenario or '_(Not supplied by the card.)_'}\n\n"
        + "\n\n".join(greeting_blocks),
    )
    _write(
        soul / "BOOTSTRAP.md",
        "---\nsoul: bootstrap\nconsumed_once: true\n---\n\n"
        f"# Bootstrap\n\n## Cold open\n\n{first_message or alternates[0]}",
    )
    _write(soul / "EXAMPLES.md", _examples_markdown(_text(fields.get("mes_example"))))
    _write(soul / "WORLD.md", _world_markdown(fields.get("character_book"), name))
    _write(soul / "NOTES.md", creator_notes or "_(No creator notes were supplied.)_")
    _write(
        soul / "USER.md",
        """---
soul: user
runtime_only: true
---

# User model

## Who {{user}} seems to be

_(unknown)_

## What helps, and what does not

_(to be learned)_
""",
    )
    _write(soul / "MEMORY.md", "# Relationship memory\n\n_(No memories yet.)_")


def _create_vault(vault: Path, fields: Mapping[str, Any], name: str) -> None:
    soul = vault / "soul"
    for directory in (
        vault / "memory" / "episodic",
        vault / "memory" / "index",
        vault / "memory" / "semantic",
        vault / "state",
        vault / "world",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    _create_soul(soul, fields, name)
    _write(vault / ".gitignore", "memory/index/")
    _write(vault / "goals.md", "# Goals\n\n_(No goals yet.)_")
    _write(vault / "memory" / "summary.md", "# Conversation summary\n\n_(Empty.)_")
    _write(vault / "memory" / "semantic" / "facts.md", "# Facts")
    _write(vault / "memory" / "semantic" / "forgotten.md", "# Forgotten facts")
    _write(vault / "world" / "beliefs.jsonl", "")
    _write(vault / "world" / "situation.md", "# Current situation\n\n_(Unknown.)_")
    _write(vault / "world" / "state.json", "{}")
    for filename in (
        "activity.json",
        "budget.json",
        "dream_progress.json",
        "engine.json",
        "quarantine.json",
    ):
        _write(vault / "state" / filename, "{}")
    _write(vault / "state" / "sessions.json", '{"sessions": {}}')


def _sanitize_portrait(png: bytes, limits: CardLimits) -> bytes:
    try:
        with Image.open(io.BytesIO(png)) as image:
            if image.format != "PNG":
                raise CharacterImportError("source card is not a PNG image")
            image.load()
            image.seek(0)
            portrait = ImageOps.exif_transpose(image)
            has_alpha = portrait.mode in ("RGBA", "LA") or "transparency" in portrait.info
            portrait = portrait.convert("RGBA" if has_alpha else "RGB")
            if portrait.width * portrait.height > limits.max_pixels:
                raise CharacterImportError("portrait dimensions exceed limits")
            output = io.BytesIO()
            portrait.save(output, format="PNG")
            return output.getvalue()
    except CharacterImportError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise CharacterImportError(f"cannot decode PNG pixels: {exc}") from exc


def _initialize_git(vault: Path) -> bool:
    git = shutil.which("git")
    if git is None:
        return False
    result = subprocess.run(
        [git, "init", "-q", str(vault)], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise CharacterImportError(f"git init failed: {result.stderr.strip()}")
    git_at_vault = [git, "-c", f"safe.directory={vault.resolve()}", "-C", str(vault)]
    result = subprocess.run(
        [*git_at_vault, "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise CharacterImportError(f"git repository check failed: {result.stderr.strip()}")
    if Path(result.stdout.strip()).resolve() != vault.resolve():
        raise CharacterImportError("git repository check failed: Vault is not the repository root")
    for key, value in (("user.name", "yurios-vault"), ("user.email", "vault@localhost")):
        result = subprocess.run(
            [*git_at_vault, "config", "--local", key, value],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise CharacterImportError(f"git config failed: {result.stderr.strip()}")
    result = subprocess.run(
        [*git_at_vault, "add", "-A"], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise CharacterImportError(f"git add failed: {result.stderr.strip()}")
    result = subprocess.run(
        [*git_at_vault, "commit", "-q", "-m", "vault: import character card"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise CharacterImportError(f"git commit failed: {result.stderr.strip()}")
    return True


class CharacterImporter:
    def __init__(
        self,
        registry: CharacterRegistry,
        *,
        limits: CardLimits | None = None,
        initialize_git: bool = True,
    ):
        self.registry = registry
        self.limits = limits or CardLimits()
        self.initialize_git = initialize_git

    def import_card(
        self,
        source: str | Path | bytes | bytearray | memoryview,
        *,
        character_id: str | None = None,
        enabled: bool | None = None,
        autostart: bool = False,
        loops: LoopSwitches | None = None,
        connection: ConnectionBinding | None = None,
        models: ModelBinding | None = None,
        voice: VoiceBinding | None = None,
        body: BodyBinding | None = None,
    ) -> CharacterRecord:
        if isinstance(source, (str, Path)):
            try:
                with Path(source).open("rb") as stream:
                    png = stream.read(self.limits.max_file_bytes + 1)
            except OSError as exc:
                raise CharacterImportError(f"cannot read card: {exc}") from exc
        else:
            png = bytes(source)
        try:
            parsed = parse_png_card(png, limits=self.limits)
        except CardParseError as exc:
            raise CharacterImportError(str(exc)) from exc
        portrait = _sanitize_portrait(png, self.limits)
        fields = card_fields(parsed.data)
        name = _text(fields.get("name")).strip()
        if not name:
            raise CharacterImportError("card has no character name")

        characters_dir = self.registry.data_root / "characters"
        if character_id is None:
            unavailable = {record.id for record in self.registry}
            if characters_dir.is_dir():
                unavailable.update(path.name for path in characters_dir.iterdir())
            if any(record.display.name.casefold() == name.casefold() for record in self.registry):
                unavailable.add(new_character_id(name))
            character_id = new_character_id(name, unavailable)
        if self.registry.get(character_id) is not None:
            raise CharacterImportError(f"character already exists: {character_id}")
        final_root = self.registry.data_root / "characters" / character_id
        if final_root.exists():
            raise CharacterImportError(f"character root already exists: {final_root}")
        final_paths = CharacterPaths.under(final_root)
        native = _is_yurios_card(parsed.data, fields)
        lifecycle = LifecycleFlags(
            enabled=(native if enabled is None else bool(enabled)) if native else False,
            autostart=bool(autostart) if native else False,
            review_required=not native,
        )
        record = CharacterRecord(
            id=character_id,
            display=DisplayMetadata(
                name=name,
                creator=_text(fields.get("creator")),
                description=_text(fields.get("description")),
                tags=_string_list(fields.get("tags")),
            ),
            paths=final_paths,
            lifecycle=lifecycle,
            loops=loops or LoopSwitches(),
            connection=connection or ConnectionBinding(),
            models=models or ModelBinding(),
            voice=voice or VoiceBinding(),
            body=body or BodyBinding(),
        )

        characters_dir.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{character_id}.", dir=characters_dir))
        staged = CharacterPaths.under(temporary)
        moved = False
        try:
            staged.source_card.write_bytes(png)
            staged.card_json.write_text(
                json.dumps(parsed.data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            staged.portrait.write_bytes(portrait)
            _create_vault(staged.vault, fields, name)
            for directory in (staged.corpus, staged.traces, staged.tool_logs, staged.selfies):
                directory.mkdir(parents=True, exist_ok=True)
            if self.initialize_git:
                _initialize_git(staged.vault)
            os.replace(temporary, final_root)
            moved = True
            self.registry.add(record)
            return record
        except Exception:
            shutil.rmtree(final_root if moved else temporary, ignore_errors=True)
            raise


def import_character_card(
    source: str | Path | bytes | bytearray | memoryview,
    data_root: str | Path,
    **kwargs: Any,
) -> CharacterRecord:
    """Convenience entry point using the default registry at ``data_root``."""
    registry = CharacterRegistry(data_root)
    return CharacterImporter(registry).import_card(source, **kwargs)
