"""SoulLoader (SPEC §5) — read the persona the way the runtime does.

The SOUL is a folder of `.md` files plus a `soul.yaml` manifest that says which
source feeds which prompt section ("she reads herself into being", → ch. 19).
Build #1 does NOT consume a flattened card; it resolves `soul.yaml` against the
`.md` files in `vault/soul/` on every turn.

The resolver is from `../yuri-soul/build_card.py` (§5.1) — same
reference syntax:

    FILE.md#Heading   → the prose under that "## Heading"
    FILE.md@key       → a key from the file's YAML frontmatter
    FILE.md           → the whole body (after frontmatter)

A list of sources concatenates in order. `WORLD.md` (lorebook) and
`EXAMPLES.md` (<START> blocks) get structured parsers, same as build_card.py.
A missing file or section fails loudly, never silently (§13.3).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
H2_RE = re.compile(r"^##\s+(.*?)\s*$", re.MULTILINE)


def parse_md(path: Path) -> tuple[dict, str]:
    """Return (frontmatter dict, body) for a soul .md file."""
    text = path.read_text(encoding="utf-8")
    m = FRONTMATTER_RE.match(text)
    if m:
        front = yaml.safe_load(m.group(1)) or {}
        body = text[m.end():]
    else:
        front, body = {}, text
    return front, body


def split_sections(body: str) -> dict[str, str]:
    """Map each '## Heading' to the prose beneath it (order preserved)."""
    sections: dict[str, str] = {}
    matches = list(H2_RE.finditer(body))
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        sections[m.group(1).strip()] = body[start:end].strip()
    return sections


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
    lorebook: list[LoreEntry] = field(default_factory=list)
    bootstrap: str | None = None  # BOOTSTRAP.md#Cold open, if the file is present (§5.4)

    def lorebook_hits(self, message: str) -> list[LoreEntry]:
        """Entries whose keys appear in the user message (case-insensitive
        substring), ordered by insertion_order (§5.3). Budget-capping is the
        assembler's job (§7.2)."""
        low = message.lower()
        hits = [e for e in self.lorebook if any(k.lower() in low for k in e.keys)]
        return sorted(hits, key=lambda e: e.insertion_order)


class _Reader:
    """Lazy reader/cache over the soul folder (from build_card.py)."""

    def __init__(self, folder: Path):
        self.folder = folder
        self._front: dict[str, dict] = {}
        self._sections: dict[str, dict[str, str]] = {}
        self._body: dict[str, str] = {}

    def _load(self, fname: str):
        if fname not in self._front:
            path = self.folder / fname
            if not path.exists():
                raise FileNotFoundError(f"soul references missing file: {fname}")
            front, body = parse_md(path)
            self._front[fname] = front
            self._body[fname] = body.strip()
            self._sections[fname] = split_sections(body)

    def front(self, fname: str) -> dict:
        self._load(fname); return self._front[fname]

    def body(self, fname: str) -> str:
        self._load(fname); return self._body[fname]

    def section(self, fname: str, heading: str) -> str:
        self._load(fname)
        secs = self._sections[fname]
        if heading not in secs:
            raise KeyError(f"{fname}: no '## {heading}' section "
                           f"(have: {', '.join(secs) or 'none'})")
        return secs[heading]

    def sections(self, fname: str) -> dict[str, str]:
        self._load(fname); return self._sections[fname]

    def resolve(self, ref: str) -> str:
        """Resolve a 'FILE#Heading' / 'FILE@key' / 'FILE' reference to text."""
        if "#" in ref:
            fname, heading = ref.split("#", 1)
            return self.section(fname.strip(), heading.strip())
        if "@" in ref:
            fname, key = ref.split("@", 1)
            val = self.front(fname.strip()).get(key.strip())
            if val is None:
                raise KeyError(f"{fname}: no frontmatter key '{key}'")
            return str(val)
        return self.body(ref.strip())

    def resolve_field(self, src) -> str:
        if isinstance(src, list):
            return "\n\n".join(self.resolve(r) for r in src)
        return self.resolve(src)

    def resolve_list(self, src) -> list[str]:
        srcs = src if isinstance(src, list) else [src]
        return [self.resolve(r) for r in srcs]


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
            lorebook=lorebook,
            bootstrap=bootstrap,
        )
