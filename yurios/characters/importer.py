"""Transactional import of PNG cards into self-contained character roots."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping

import yaml
from PIL import Image, ImageOps, UnidentifiedImageError

from yurios.app import vaultgit
# The desk's ignore rule has one home (§34.1). `mind.workspace` holds no runtime
# and imports nothing from this package, so reaching for it here is a constant,
# not a dependency on the mind.
from yurios.mind.workspace import WORKSPACE_GITIGNORE

from .appearance import mechanical_identity, write_appearance
from .card import CardLimits, CardParseError, card_fields, parse_png_card
from .cardsplit import clean_version, split_description
from .privacy import PRIVATE_SOUL_FILES
from .setting import mechanical_place, opening_situation, write_setting
from .soulfiles import SoulReader
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


log = logging.getLogger(__name__)


class CharacterImportError(ValueError):
    pass


#: What a name in a card's soul payload may look like. Deliberately narrow: no
#: separators, no leading dot, one extension, and only the two the SOUL uses.
_SOUL_FILE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\.(?:md|yaml)")
MAX_SOUL_FILE_BYTES = 256 * 1024
MAX_SOUL_TOTAL_BYTES = 1024 * 1024


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


def _yurios_block(card: Mapping[str, Any], fields: Mapping[str, Any]) -> dict[str, Any]:
    for owner in (fields, card):
        extensions = owner.get("extensions")
        if isinstance(extensions, Mapping) and isinstance(extensions.get("yurios"), Mapping):
            return dict(extensions["yurios"])
    return {}


def _soul_payload(block: Mapping[str, Any]) -> dict[str, str] | None:
    """The verbatim soul files a YuriOS card carries, or ``None`` if unusable.

    This is the difference between a card that re-imports as *her* and one that
    re-imports as a flattened summary of her: `_create_soul` below can only
    synthesise `vault/soul/` back out of card prose, which loses the
    CONSTITUTION/PERSONA split, the appearance/manner separation and every
    frontmatter key. When the payload is present and sound, it is written as-is.

    Everything here is adversarial input — a `.PNG` from a stranger on the
    internet is the single least trustworthy thing this runtime touches — so the
    rules are narrow and any failure falls back to synthesis rather than raising:
    a card that cannot be trusted to carry a soul is still a perfectly good card.

      * plain basenames only, `[A-Za-z0-9._-]`, `.md` or `.yaml` — no separators,
        no `..`, no dotfiles, so nothing can be planted outside `vault/soul/`
        (a `.git/hooks/post-commit` would otherwise be remote code execution);
      * never a runtime-only file: a hostile card must not be able to seed a
        `USER.md` that the next partner-model merge treats as established fact;
      * bounded per file and in total, and valid UTF-8;
      * `soul.yaml` must be present and parse, and every `fields:` reference
        must resolve against the files provided — a manifest pointing at a file
        that is not in the payload would leave the runtime unable to load her.
    """
    payload = block.get("soul")
    if not isinstance(payload, Mapping):
        return None
    raw = payload.get("files")
    if not isinstance(raw, Mapping) or not raw:
        return None

    files: dict[str, str] = {}
    total = 0
    for name, text in raw.items():
        if not isinstance(name, str) or not isinstance(text, str):
            return None
        if name != Path(name).name or not _SOUL_FILE_RE.fullmatch(name):
            return None
        if name.casefold() in {p.casefold() for p in PRIVATE_SOUL_FILES}:
            return None
        encoded = len(text.encode("utf-8"))
        if encoded > MAX_SOUL_FILE_BYTES:
            return None
        total += encoded
        if total > MAX_SOUL_TOTAL_BYTES:
            return None
        files[name] = text

    digests = payload.get("sha256")
    if isinstance(digests, Mapping):
        for name, expected in digests.items():
            if not isinstance(name, str) or name not in files or not isinstance(expected, str):
                return None
            actual = hashlib.sha256(files[name].encode("utf-8")).hexdigest()
            if expected != actual:
                return None

    if "soul.yaml" not in files:
        return None
    try:
        manifest = yaml.safe_load(files["soul.yaml"])
    except yaml.YAMLError:
        return None
    if not isinstance(manifest, dict) or not isinstance(manifest.get("fields"), dict):
        return None
    referenced = SoulReader(Path(".")).referenced_files(manifest["fields"])
    missing = [name for name in referenced if name not in files]
    # BOOTSTRAP.md is consumed-once (§5.4): a card cut from a character who has
    # already met someone legitimately has none, and `_create_soul` writes a
    # fresh cold open from the card's `first_mes`.
    if [name for name in missing if name != "BOOTSTRAP.md"]:
        return None
    return files


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


#: Seeded into `vault/workspace/` and `vault/skills/` (SPEC §34). Both folders
#: are addressed by name in the docs and by her own tools, so both explain
#: themselves on arrival rather than turning up as two empty mysteries.
WORKSPACE_README = """# Workspace

Her desk. She may read, write and delete anything in here through her own tools,
without asking and without a gate — drafts, research notes, working scratch, the
middle of a thought.

You can drop files in too; they are ordinary files. This folder is inside the
Vault and travels when you copy it, but it is **not** version-controlled —
scratch churns, and the Vault's `git log` is the diary of how she grew, not of
every draft she rewrote. Her skills, next door, are versioned.

Not in here: anything that runs. The code harness gets its own workspace outside
the Vault. And no dotfiles — her tools refuse them.
"""

SKILLS_README = """# Skills

One folder per skill, each with a `SKILL.md`:

    skills/
    └── tea-timer/
        ├── SKILL.md
        └── (any supporting files)

`SKILL.md` is YAML frontmatter plus instructions:

    ---
    name: tea-timer
    description: How she likes to run a tea steep — when to reach for this
    author: you
    enabled: true
    ---

    Ask which tea first, then set the timer for...

The `description` is the load-bearing field: every turn carries a one-line
catalog of names and descriptions, and the body is only loaded once she has
decided this is the skill the moment calls for. Write it as *when to reach for
this*, not as a title.

Drop skills in by hand, or let her write her own. Unlike the workspace next
door, this folder IS version-controlled: what she knows how to do is worth being
able to read back and revert.
"""

DOCUMENT_EDITING_SKILL = """---
name: document-editing
description: when asked to create, revise, reorder, or remove content from a workspace document
author: YuriOS
enabled: true
---

# Document Editing

Treat the document on disk as the source of truth. Do not claim an edit happened until its tool call succeeds.

## Choose the right tool

- Create or deliberately replace the entire note: `write_note`.
- Add text only when it belongs at the end of the note: `append_note`.
- Change, move, or remove part of an existing note: `edit_note`.
- Remove the whole note: `delete_note`.

Never use `append_note` to add material to a named section in the middle of a document.

## Safe edit order

1. Call `read_note` before changing an existing document. It returns line numbers and a total line count.
2. For a long document, call `read_note` again with `start_line` and `end_line` around the target. Line numbers are 1-based and inclusive.
3. Make the smallest edit that achieves the request. Use exact text when the target occurs once. Use a line range when the same text or section appears more than once.
4. When making more than one independent edit, work from the bottom of the document upward so earlier line numbers do not shift.
5. After an important or destructive edit, read the affected lines again before saying it is complete.

## Duplicates

Keep the first/canonical section unless the user says otherwise. Read both copies and decide exactly which one to remove.

- If the duplicate block is unique, call `edit_note` with that exact block as `old_text` and `new_text` as an empty string.
- If both copies are identical, call `edit_note` with the repeated block as `old_text` and `new_text` empty. It preserves the first copy and removes the later one.
- Include the duplicate heading and its body in the deletion range, but do not remove the next section's heading.
- Do not rewrite the whole document or append a replacement just to fix a duplicate.

## Failure handling

- If an exact edit says the text is missing or ambiguous, stop guessing. Read the relevant lines and retry with the current text or a line range.
- If `read_note` says `truncated: true`, narrow the line range before editing.
- A failed call did not change the document. Do not say that it did.
"""


def _write_partner_model(soul: Path) -> None:
    """`USER.md`, empty. The relationship starts at zero.

    Not configurable, and written on every path — synthesised or verbatim. A
    card handed to someone else carries who she is, never who you were to her
    (`soul-src`, D-014).

    The manifest's other `runtime_only:` file, `MEMORY.md`, deliberately has no
    counterpart here. It is runtime memory rather than persona prose, so it
    never lands under `soul/` at all: `scripts/seed_vault.py` splits it into
    `memory/semantic/facts.md` and `forgotten.md`, which is where every reader
    looks, and `_create_vault` below seeds those two empty. A `soul/MEMORY.md`
    written beside them would be a second, inert copy — read by nothing, yet
    offered to her gated self-edit flow as somewhere to put a memory
    (`mind/vaultio.py`'s `EDITABLE_SOUL`), which is a place for one to go and
    never come back.
    """
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


def _restore_soul(soul: Path, files: Mapping[str, str], fields: Mapping[str, Any],
                  name: str) -> None:
    """Write a card's verbatim soul payload, so the round trip is byte-exact."""
    soul.mkdir(parents=True, exist_ok=True)
    for filename, text in files.items():
        (soul / filename).write_text(text, encoding="utf-8")
    if "BOOTSTRAP.md" not in files:
        # Her cold open was consumed before the card was cut, but the person
        # importing her has not met her yet — so she gets one, from the card.
        first_message = _text(fields.get("first_mes")) or f"Hello, I am {name}."
        _write(
            soul / "BOOTSTRAP.md",
            "---\nsoul: bootstrap\nconsumed_once: true\n---\n\n"
            f"# Bootstrap\n\n## Cold open\n\n{first_message}",
        )
    _write_partner_model(soul)


def _create_soul(soul: Path, fields: Mapping[str, Any], name: str,
                 warnings: tuple[str, ...] = ()) -> None:
    description = _text(fields.get("description"))
    personality = _text(fields.get("personality"))
    scenario = _text(fields.get("scenario"))
    first_message = _text(fields.get("first_mes"))
    alternates = _string_list(fields.get("alternate_greetings"))
    system_prompt = _text(fields.get("system_prompt"))
    post_history = _text(fields.get("post_history_instructions"))
    creator_notes = _text(fields.get("creator_notes"))
    # A foreign card keeps all four backbone sections in one `description`, so
    # the split has to be recovered rather than read (`cardsplit`). It is a
    # router, not a rewriter: a card whose layout it cannot read comes out with
    # everything under Identity, which is what this function always did.
    sections = split_description(description)
    version, misfiled_version = clean_version(_text(fields.get("character_version")))
    if misfiled_version:
        # A source URL or a "chat name" in `character_version` is worth keeping
        # and is not a version — the notes are where whoever opens the card next
        # actually looks for it.
        creator_notes = (f"{creator_notes}\n\n" if creator_notes else "") + \
            f"From the source card's version field:\n\n{misfiled_version}"
    for warning in warnings:
        # The parser resolved an ambiguity in the file. Resolving it quietly is
        # what §30.1 forbids, so it lands where whoever reviews this import reads.
        creator_notes = (f"{creator_notes}\n\n" if creator_notes else "") + \
            f"**On import:** {warning}"
    if not alternates:
        alternates = [first_message or f"Hello, I am {name}."]
    version = version or "1.0.0"
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

{sections['identity'] or '_(Not supplied by the card.)_'}

## History

{sections['history'] or '_(No separate history was supplied by the card.)_'}

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

{sections['appearance'] or '_(The source card does not separate appearance from description.)_'}

## Manner

{sections['manner'] or personality or '_(Not supplied by the card.)_'}
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
    _write_partner_model(soul)


def _create_vault(vault: Path, fields: Mapping[str, Any], name: str,
                  soul_files: Mapping[str, str] | None = None,
                  warnings: tuple[str, ...] = ()) -> None:
    soul = vault / "soul"
    for directory in (
        vault / "knowledge" / "reference",
        vault / "memory" / "episodic",
        vault / "memory" / "index",
        vault / "memory" / "semantic",
        vault / "skills",
        vault / "state",
        vault / "workspace",
        vault / "world",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    if soul_files:
        _restore_soul(soul, soul_files, fields, name)
    else:
        _create_soul(soul, fields, name, warnings)
    _write(vault / ".gitignore", vaultgit.VAULT_GITIGNORE)
    _write(vault / "goals.md", "# Goals\n\n_(No goals yet.)_")
    _write(vault / "memory" / "summary.md", "# Conversation summary\n\n_(Empty.)_")
    _write(vault / "memory" / "semantic" / "facts.md", "# Facts")
    _write(vault / "memory" / "semantic" / "forgotten.md", "# Forgotten facts")
    # Both are directories a *human* is told to put files in, so both ship with
    # the note that says so. The desk's ignore file goes down at seed time too,
    # not just when `Workspace` first constructs: otherwise this README is
    # committed by the import commit below and stays tracked forever afterwards,
    # since .gitignore has no effect on a path git already knows.
    _write(vault / "workspace" / ".gitignore", WORKSPACE_GITIGNORE)
    _write(vault / "workspace" / "README.md", WORKSPACE_README)
    _write(vault / "skills" / "README.md", SKILLS_README)
    _write(vault / "skills" / "document-editing" / "SKILL.md", DOCUMENT_EDITING_SKILL)
    _write(vault / "world" / "beliefs.jsonl", "")
    # Where she is, from her own card (characters/setting.py). Mechanical and
    # synchronous, so an import with no network and no key still leaves her
    # standing somewhere of her own; `refine_setting` rewrites it into better
    # prose when a utility model is reachable. This used to be `_(Unknown.)_`
    # for every character, which was a lie by omission — nothing had happened
    # yet, but the card in hand said perfectly plainly where she was.
    place = mechanical_place(name, scenario=_text(fields.get("scenario")),
                             description=_text(fields.get("description")),
                             first_mes=_text(fields.get("first_mes")))
    if place:
        write_setting(vault / "world" / "setting.md", name, place)
    _write(vault / "world" / "situation.md", opening_situation(place))
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


def _initialize_git(vault: Path, *, message: str = "vault: import character card") -> bool:
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
        [*git_at_vault, "commit", "-q", "-m", message],
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
        for warning in parsed.warnings:
            log.warning("import: %s", warning)
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
        block = _yurios_block(parsed.data, fields) if native else {}
        soul_files = _soul_payload(block) if native else None
        # A file the parser had to disambiguate is never trusted enough to start
        # on its own, however native it claims to be — a human reads it first.
        trusted = native and not parsed.warnings
        lifecycle = LifecycleFlags(
            enabled=(trusted if enabled is None else bool(enabled)) if trusted else False,
            autostart=bool(autostart) if trusted else False,
            review_required=not trusted,
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
            # Her face, before anything can go wrong with it (§7.6). Written
            # from the card's own words, synchronously, so a character always
            # leaves the importer with a likeness of her own — the utility model
            # rewrites this into better prose when one is reachable
            # (`refine_appearance`), but an import that never gets that far must
            # not leave her borrowing whoever is shipped in the repo.
            write_appearance(staged.appearance, name,
                             mechanical_identity(name, _text(fields.get("description"))))
            _create_vault(staged.vault, fields, name, soul_files, parsed.warnings)
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
