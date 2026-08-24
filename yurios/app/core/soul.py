"""SoulLoader (SPEC §5) — read the persona the way the runtime does.

The SOUL is a folder of `.md` files plus a `soul.yaml` manifest that says which
source feeds which prompt section ("she reads herself into being", → ch. 19).
Build #1 does NOT consume a flattened card; it resolves `soul.yaml` against the
`.md` files in `vault/soul/` on every turn.

The resolver is `yurios/characters/soulfiles.py` (§5.1), promoted out of this
module once the card exporter needed it too — same reference syntax:

    FILE.md#Heading   → the prose under that "## Heading"
    FILE.md@key       → a key from the file's YAML frontmatter
    FILE.md           → the whole body (after frontmatter)

A list of sources concatenates in order. `WORLD.md` (lorebook) and
`EXAMPLES.md` (<START> blocks) get structured parsers, same as build_card.py.
A missing file or section fails loudly, never silently (§13.3).

`parse_md`, `split_sections` and `_Reader` are re-exported here because half the
repo imports them from this module; there is one implementation, next door.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from yurios.characters.soulfiles import (  # noqa: F401 (re-exported)
    FRONTMATTER_RE,
    H2_RE,
    SoulReader as _Reader,
    parse_md,
    split_sections,
)


def apply_macros(text: str, char_name: str, user_name: str) -> str:
    """{{char}} → soul name, {{user}} → USER_NAME; case-insensitive on the macro (§5.3)."""
    text = re.sub(r"\{\{\s*char\s*\}\}", char_name, text, flags=re.IGNORECASE)
    text = re.sub(r"\{\{\s*user\s*\}\}", user_name, text, flags=re.IGNORECASE)
    return text


@dataclass
class LoreEntry:
    """One WORLD.md entry: static, keyword-triggered card-native flavor (§5.3) —
    not the deferred document knowledge store (§12)."""
    name: str
    keys: list[str]
    content: str
    insertion_order: int


@dataclass
class Soul:
    """The resolved persona, mapped to the §7.1 prompt blocks (per §5.2)."""
    name: str
    card_version: str          # "<name lowercased>-v<major>@<canon>" (§5.2)
    voice_law: str             # CONSTITUTION.md#Voice law
    backbone: str              # identity · history · appearance · manner
    personality: str           # PERSONA.md@personality
    scenario: str              # SCENARIO.md#Scenario
    return_greetings: list[str]  # SCENARIO.md alternate greetings (continuity fallback)
    hard_limits: str           # CONSTITUTION.md#Hard limits — post-history (§7.1)
    examples: str              # EXAMPLES.md, <START>-joined
    drives: list[str] = field(default_factory=list)  # durable motives, not tasks
    lorebook: list[LoreEntry] = field(default_factory=list)
    bootstrap: str | None = None  # BOOTSTRAP.md#Cold open, if the file is present (§5.4)

    def lorebook_hits(self, message: str) -> list[LoreEntry]:
        """Entries whose keys appear in the user message (case-insensitive
        substring), ordered by insertion_order (§5.3). Budget-capping is the
        assembler's job (§7.2)."""
        low = message.lower()
        hits = [e for e in self.lorebook if any(k.lower() in low for k in e.keys)]
        return sorted(hits, key=lambda e: e.insertion_order)


def _build_examples(reader: _Reader, fname: str) -> str:
    """Each '## Example ...' block → one <START> exchange, joined (§5.1)."""
    blocks = [content for heading, content in reader.sections(fname).items()
              if heading.lower().startswith("example")]
    return "\n".join(f"<START>\n{b.strip()}" for b in blocks)


def _build_lorebook(reader: _Reader, fname: str) -> list[LoreEntry]:
    """Each '## Entry' with a 'keys:' line → one keyword-triggered LoreEntry (§5.3)."""
    entries: list[LoreEntry] = []
    for order, (heading, content) in enumerate(reader.sections(fname).items(), start=1):
        lines = content.strip().splitlines()
        keys: list[str] = []
        rest = lines
        for i, line in enumerate(lines):
            if line.lower().startswith("keys:"):
                keys = [k.strip() for k in line.split(":", 1)[1].split(",") if k.strip()]
                rest = lines[:i] + lines[i + 1:]
                break
        entries.append(LoreEntry(name=heading,
                                 keys=keys or [heading],
                                 content="\n".join(rest).strip(),
                                 insertion_order=order))
    return entries


class SoulLoader:
    """Loads the SOUL from `vault/soul/` — called on every turn (§5), so the
    persona is always whatever the files say *right now*."""

    def __init__(self, soul_dir: Path, user_name: str = "you"):
        self.soul_dir = Path(soul_dir)
        self.user_name = user_name

    def load(self) -> Soul:
        manifest_path = self.soul_dir / "soul.yaml"
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        reader = _Reader(self.soul_dir)
        fields = manifest["fields"]

        name = str(manifest["name"])
        # card_version = "<name lowercased>-v<major>@<canon>" (§5.2), stamped on
        # every journal entry and corpus record.
        major = str(manifest["character_version"]).split(".")[0]
        card_version = f"{name.lower()}-v{major}@{manifest['canon']}"

        def mac(text: str) -> str:
            return apply_macros(text, name, self.user_name)

        # BOOTSTRAP.md is consumed-once: file-presence IS the
        # "has she met you yet?" flag (§5.4).
        bootstrap = None
        if (self.soul_dir / "BOOTSTRAP.md").exists():
            bootstrap = mac(reader.resolve(str(fields["first_mes"])))

        lorebook = [LoreEntry(e.name, e.keys, mac(e.content), e.insertion_order)
                    for e in _build_lorebook(reader, str(fields["character_book"]))]
        raw_drives = manifest.get("drives", [])
        drives = ([mac(str(value).strip()) for value in raw_drives
                   if str(value).strip()]
                  if isinstance(raw_drives, list) else [])

        return Soul(
            name=name,
            card_version=card_version,
            voice_law=mac(reader.resolve_field(fields["system_prompt"])),
            backbone=mac(reader.resolve_field(fields["description"])),
            personality=mac(reader.resolve_field(fields["personality"])),
            scenario=mac(reader.resolve_field(fields["scenario"])),
            return_greetings=[mac(g) for g in reader.resolve_list(fields["alternate_greetings"])],
            hard_limits=mac(reader.resolve_field(fields["post_history_instructions"])),
            examples=mac(_build_examples(reader, str(fields["mes_example"]))),
            drives=drives,
            lorebook=lorebook,
            bootstrap=bootstrap,
        )
