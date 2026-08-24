"""The card draft — the studio's view of a SOUL, and the way back.

The studio edits **card fields**, not markdown files. That is the right altitude
for a workbench: a card field maps 1:1 to what ships and to what the preview
renders, and the CONSTITUTION/PERSONA split is a runtime concern `apply_draft`
reconstructs on the way back down.

One field resists the flattening and is worth the exception. `soul.yaml` maps
four sections — CONSTITUTION#Identity, #History, PERSONA#Appearance, #Manner —
into the card's single `description`. Round-tripping that through one textarea
would collapse the structure and destroy the immutable/editable boundary the
whole SOUL design rests on. So the draft carries the *sections*, keyed by their
manifest reference, and `description` is the derived render of them: read-only in
the preview, with the token count that tells you what it costs every turn.

`provenance` is the other half of the page. Every draft field comes back with
who last moved it and when, read off the Vault's git log — which is what lets the
studio say "she wrote this line herself, on the 14th, and you approved it". That
is the feature people will screenshot, and it costs one `git log` per soul file.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

from . import vcs
from .exporter import SoulSnapshot, read_soul
from .models import CharacterRecord
from .privacy import CardExportError
from .soulfiles import RETIRED_BOOTSTRAP, SoulPrivacyError, retired_cold_open

#: The manifest references the studio knows how to edit, in page order. Anything
#: a manifest points somewhere else still renders — read-only, and labelled.
SECTION_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("identity", "CONSTITUTION.md#Identity", "Identity"),
    ("history", "CONSTITUTION.md#History", "History"),
    ("appearance", "PERSONA.md#Appearance", "Appearance"),
    ("manner", "PERSONA.md#Manner", "Manner"),
)
CONSTITUTION_FIELDS = frozenset({"identity", "history", "system_prompt",
                                 "post_history_instructions"})


@dataclass(slots=True)
class FieldProvenance:
    origin: str = "seed"       # seed | you | her | unknown
    commits: int = 0
    last: str | None = None
    subject: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"origin": self.origin, "commits": self.commits,
                "last": self.last, "subject": self.subject}


@dataclass(slots=True)
class Draft:
    name: str = ""
    nickname: str = ""
    creator: str = ""
    character_version: str = "1.0.0"
    tags: list[str] = field(default_factory=list)
    drives: list[str] = field(default_factory=list)
    identity: str = ""
    history: str = ""
    appearance: str = ""
    manner: str = ""
    personality: str = ""
    scenario: str = ""
    first_mes: str = ""
    alternate_greetings: list[str] = field(default_factory=list)
    group_only_greetings: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    system_prompt: str = ""
    post_history_instructions: str = ""
    creator_notes: str = ""
    lorebook: dict[str, Any] = field(default_factory=lambda: {
        "scan_depth": 4, "token_budget": 600, "recursive_scanning": False,
        "entries": [],
    })

    @property
    def description(self) -> str:
        """The derived render of the four backbone sections, in manifest order."""
        return "\n\n".join(part for part in
                           (self.identity, self.history, self.appearance, self.manner)
                           if part.strip())

    def to_dict(self) -> dict[str, Any]:
        data = {name: getattr(self, name) for name in self.__slots__}
        data["description"] = self.description
        return data

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Draft":
        draft = cls()
        for name in cls.__slots__:
            if name not in value:
                continue
            current = getattr(draft, name)
            incoming = value[name]
            if isinstance(current, str):
                setattr(draft, name, str(incoming or ""))
            elif isinstance(current, list):
                setattr(draft, name, [str(item) for item in (incoming or [])
                                      if str(item).strip()])
            elif isinstance(current, dict) and isinstance(incoming, Mapping):
                setattr(draft, name, _lorebook(incoming))
        draft.name = draft.name.strip()
        if not draft.character_version.strip():
            draft.character_version = "1.0.0"
        return draft


def _lorebook(value: Mapping[str, Any]) -> dict[str, Any]:
    entries = []
    for raw in value.get("entries") or []:
        if not isinstance(raw, Mapping):
            continue
        keys = raw.get("keys") or []
        if isinstance(keys, str):
            keys = [part.strip() for part in keys.split(",")]
        keys = [str(key).strip() for key in keys if str(key).strip()]
        content = str(raw.get("content") or "").strip()
        if not keys or not content:
            continue
        entries.append({
            "name": str(raw.get("name") or keys[0]),
            "keys": keys,
            "content": content,
            "constant": bool(raw.get("constant")),
            "use_regex": bool(raw.get("use_regex")),
            "case_sensitive": bool(raw.get("case_sensitive")),
        })
    return {
        "scan_depth": int(value.get("scan_depth") or 4),
        "token_budget": int(value.get("token_budget") or 600),
        "recursive_scanning": bool(value.get("recursive_scanning")),
        "entries": entries,
    }


# ------------------------------------------------------------------- reading

def _section(snapshot: SoulSnapshot, ref: str) -> str:
    try:
        return snapshot.reader.resolve(ref)
    except (SoulPrivacyError, FileNotFoundError, KeyError):
        return ""


def read_draft(record: CharacterRecord) -> tuple[Draft, dict[str, FieldProvenance]]:
    """The SOUL as a draft, plus who last moved each part of it."""
    snapshot = read_soul(record)
    manifest = snapshot.manifest
    fields = manifest["fields"]
    manifest_drives = manifest.get("drives", [])
    draft = Draft(
        name=str(manifest.get("name") or record.display.name or ""),
        nickname=str(manifest.get("nickname") or ""),
        creator=str(manifest.get("creator") or record.display.creator or ""),
        character_version=str(manifest.get("character_version") or "1.0.0"),
        tags=[str(tag) for tag in (manifest.get("tags") or [])],
        drives=([str(drive) for drive in manifest_drives if str(drive).strip()]
                if isinstance(manifest_drives, list) else []),
        personality=_section(snapshot, str(fields.get("personality") or "")),
        scenario=_section(snapshot, str(fields.get("scenario") or "")),
        creator_notes=_section(snapshot, str(fields.get("creator_notes") or "")),
        system_prompt=_section(snapshot, str(fields.get("system_prompt") or "")),
        post_history_instructions=_section(
            snapshot, str(fields.get("post_history_instructions") or "")),
    )
    for name, ref, _label in SECTION_FIELDS:
        setattr(draft, name, _section(snapshot, ref))

    first_ref = str(fields.get("first_mes") or "")
    first_file = re.split(r"[#@]", first_ref, maxsplit=1)[0].strip()
    if first_file and snapshot.reader.exists(first_file):
        draft.first_mes = _section(snapshot, first_ref)
    else:
        # She has met someone, so the bootstrap has been retired (§5.4) — but the
        # cold open is still the card's first message, and still hers to edit.
        # Without this the studio shows an empty box for every grown character
        # and offers to write a *new* bootstrap over the top of her history.
        draft.first_mes = retired_cold_open(
            Path(record.paths.vault) / "soul",
            first_ref.split("#", 1)[1].strip() if "#" in first_ref else "Cold open")

    greetings = fields.get("alternate_greetings") or []
    draft.alternate_greetings = [
        text for text in (_section(snapshot, str(ref))
                          for ref in (greetings if isinstance(greetings, list)
                                      else [greetings]))
        if text.strip()]

    examples_file = str(fields.get("mes_example") or "")
    if examples_file:
        try:
            draft.examples = [body.strip() for heading, body
                              in snapshot.reader.sections(examples_file).items()
                              if heading.lower().startswith("example") and body.strip()]
        except (SoulPrivacyError, FileNotFoundError, KeyError):
            draft.examples = []

    draft.lorebook = _read_lorebook(snapshot, str(fields.get("character_book") or ""))
    return draft, _provenance(record, snapshot)


def _read_lorebook(snapshot: SoulSnapshot, filename: str) -> dict[str, Any]:
    front: dict[str, Any] = {}
    entries: list[dict[str, Any]] = []
    if filename:
        try:
            front = snapshot.reader.front(filename)
            sections = snapshot.reader.sections(filename)
        except (SoulPrivacyError, FileNotFoundError, KeyError):
            sections = {}
        for heading, body in sections.items():
            lines = body.strip().splitlines()
            keys: list[str] = []
            rest = lines
            for index, line in enumerate(lines):
                if line.lower().startswith("keys:"):
                    keys = [k.strip() for k in line.split(":", 1)[1].split(",") if k.strip()]
                    rest = lines[:index] + lines[index + 1:]
                    break
            content = "\n".join(rest).strip()
            if content:
                entries.append({"name": heading, "keys": keys or [heading],
                                "content": content, "constant": False,
                                "use_regex": False, "case_sensitive": False})
    return {
        "scan_depth": int(front.get("scan_depth") or 4),
        "token_budget": int(front.get("token_budget") or 600),
        "recursive_scanning": bool(front.get("recursive_scanning") or False),
        "entries": entries,
    }


#: Which soul file backs which draft field, for the provenance read.
_FIELD_FILES: dict[str, str] = {
    "identity": "CONSTITUTION.md", "history": "CONSTITUTION.md",
    "system_prompt": "CONSTITUTION.md", "post_history_instructions": "CONSTITUTION.md",
    "appearance": "PERSONA.md", "manner": "PERSONA.md", "personality": "PERSONA.md",
    "scenario": "SCENARIO.md", "alternate_greetings": "SCENARIO.md",
    "first_mes": "BOOTSTRAP.md", "examples": "EXAMPLES.md",
    "lorebook": "WORLD.md", "creator_notes": "NOTES.md",
    "name": "soul.yaml", "tags": "soul.yaml", "creator": "soul.yaml",
    "drives": "soul.yaml",
    "character_version": "soul.yaml", "nickname": "soul.yaml",
}


def _provenance(record: CharacterRecord,
                snapshot: SoulSnapshot) -> dict[str, FieldProvenance]:
    """Who last wrote each soul file, mapped onto the draft fields it backs.

    File-level, not line-level, on purpose: a per-hunk blame would be prettier
    and would cost a `git blame` per field on every page load, and the question
    the studio actually asks — "has she been editing herself here?" — is answered
    at file granularity.
    """
    soul = Path(record.paths.vault) / "soul"
    files = dict(_FIELD_FILES)
    if not (soul / "BOOTSTRAP.md").is_file() and (soul / RETIRED_BOOTSTRAP).is_file():
        files["first_mes"] = RETIRED_BOOTSTRAP.as_posix()   # follow it into retirement

    cache: dict[str, list[vcs.Commit]] = {}
    out: dict[str, FieldProvenance] = {}
    for field_name, filename in files.items():
        if filename not in cache:
            cache[filename] = vcs.log(record.paths.vault, f"soul/{filename}", limit=50)
        commits = cache[filename]
        if not commits:
            out[field_name] = FieldProvenance(origin="seed")
            continue
        newest = commits[0]
        out[field_name] = FieldProvenance(
            origin=newest.author if len(commits) > 1 else "seed",
            commits=max(0, len(commits) - 1),
            last=newest.when.isoformat(),
            subject=newest.subject)
    return out


def grown_fields(provenance: Mapping[str, FieldProvenance]) -> list[str]:
    """The fields she changed herself — the Grown strip's contents."""
    return sorted(name for name, item in provenance.items() if item.origin == "her")


# ------------------------------------------------------------------- writing

def _replace_section(path: Path, heading: str, value: str) -> None:
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    marker = f"## {heading}"
    match = re.search(rf"(?m)^## {re.escape(heading)}\s*$", text)
    block = f"{marker}\n\n{value.strip()}\n"
    if match is None:
        text = text.rstrip() + "\n\n" + block
    else:
        following = re.search(r"(?m)^##\s+", text[match.end():])
        end = match.end() + following.start() if following else len(text)
        # The cut runs right up to the next `##`, blank separator and all, so
        # the block has to put that blank line back — otherwise the heading
        # below lands flush against this section's last paragraph.
        rest = text[end:]
        text = text[:match.start()] + block + ("\n" + rest if rest.strip() else "")
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


def _set_frontmatter(path: Path, key: str, value: str) -> None:
    if not path.is_file():
        return
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        return
    end = text.index("\n---\n", 4)
    front = yaml.safe_load(text[4:end]) or {}
    front[key] = value
    rendered = yaml.safe_dump(front, sort_keys=False, allow_unicode=True).strip()
    path.write_text(f"---\n{rendered}\n---\n{text[end + 5:].lstrip()}", encoding="utf-8")


def _replace_numbered(path: Path, prefix: str, values: list[str], *,
                      header: str) -> int:
    """Rewrite every `## <prefix> …` block; return how many were written.

    The heading match is deliberately looser than the headings this writes.
    Authored souls title their blocks — `## Alternate greeting — evening`,
    `## Example — comfort` — and a pattern that only recognised the numbered
    form left those in place, appended a renumbered set beside them, and pointed
    the manifest at the new ones: the old prose stayed in the file, dead, and
    shipped again inside the card's verbatim soul payload.

    Blanks are dropped *before* numbering, and the count comes back so
    `_sync_greeting_refs` can name exactly the blocks that exist. Numbering the
    unfiltered list instead left a gap — `1`, `3` — and a manifest reference to
    a `## … 2` that was never written, which `_resolve_list` swallows by
    returning no greetings at all.
    """
    text = path.read_text(encoding="utf-8") if path.is_file() else header
    pattern = re.compile(rf"(?m)^##\s+{re.escape(prefix)}(?:\s.*)?$")
    matches = list(pattern.finditer(text))
    if matches:
        following = re.search(r"(?m)^##\s+", text[matches[-1].end():])
        end = matches[-1].end() + following.start() if following else len(text)
        text = text[:matches[0].start()] + text[end:]
    kept = [value.strip() for value in values if value.strip()]
    blocks = "\n\n".join(f"## {prefix} {index}\n\n{value}"
                         for index, value in enumerate(kept, start=1))
    text = text.rstrip() + ("\n\n" + blocks if blocks else "") + "\n"
    path.write_text(text, encoding="utf-8")
    return len(kept)


def _write_lorebook(path: Path, lorebook: Mapping[str, Any], name: str) -> None:
    front = {"soul": "world", "scan_depth": int(lorebook.get("scan_depth") or 4),
             "token_budget": int(lorebook.get("token_budget") or 600),
             "recursive_scanning": bool(lorebook.get("recursive_scanning"))}
    rendered = yaml.safe_dump(front, sort_keys=False, allow_unicode=True).strip()
    blocks = []
    for entry in lorebook.get("entries") or []:
        keys = ", ".join(entry.get("keys") or []) or entry.get("name", "entry")
        blocks.append(f"## {entry.get('name') or keys}\n\nkeys: {keys}\n"
                      f"{str(entry.get('content') or '').strip()}")
    body = "\n\n".join(blocks) if blocks else f"## {name}\n\nkeys: {name}\n"
    path.write_text(f"---\n{rendered}\n---\n\n# World\n\n{body}\n", encoding="utf-8")


def _block_end(text: str, start: int) -> int:
    """Where a top-level key stops owning the file. *start* begins the line
    after its `key:` line.

    A key's value is not always on that line: `tags:` may be followed by an
    indented block sequence, and a credit by a folded scalar. Replacing only the
    `key:` line then strands those continuation lines under a scalar, which is
    not a cosmetic problem — it is invalid YAML, and `read_soul` refuses the
    character from then on. So a rewrite consumes the whole block: every
    following line that is blank or indented, minus the blank lines at the tail,
    which are the separator before whatever comes next and stay put.
    """
    end = trailing = start
    for line in text[start:].splitlines(keepends=True):
        if line.strip() and not line[:1].isspace():
            break                              # a new top-level key or comment
        end += len(line)
        if line.strip():
            trailing = end                     # the last line that is really ours
    return trailing


def _set_manifest(path: Path, values: Mapping[str, Any]) -> None:
    text = path.read_text(encoding="utf-8")
    for key, value in values.items():
        line = f"{key}: {json.dumps(value, ensure_ascii=False)}\n"
        match = re.search(rf"(?m)^{re.escape(key)}\s*:.*$", text)
        if match is None:
            # Keep new keys above `fields:` so the manifest stays readable.
            marker = re.search(r"(?m)^fields\s*:", text)
            insert = marker.start() if marker else len(text)
            text = text[:insert] + line + text[insert:]
            continue
        # …from the start of the next line, so the key's own newline survives.
        after = match.end() + (1 if text[match.end():match.end() + 1] == "\n" else 0)
        text = text[:match.start()] + line + text[_block_end(text, after):]
    path.write_text(text, encoding="utf-8")


def apply_draft(record: CharacterRecord, draft: Draft) -> list[str]:
    """Write a draft back through the manifest. Returns the files it touched.

    Constitution sections are written here, and that is intentional: §23's lock
    binds *her* — the mind may not hold the pen that rewrites its own limits —
    not you. The route says so in the commit message, so `git log` shows plainly
    when a constitution moved and who moved it.
    """
    soul = Path(record.paths.vault) / "soul"
    if not soul.is_dir():
        raise CardExportError("this character has no soul folder to save into",
                              surface="vault/soul", code="invalid")
    return write_soul(soul, draft)


def write_soul(soul: Path, draft: Draft) -> list[str]:
    """The draft → SOUL writer, addressed by folder.

    The creator needs this before a `CharacterRecord` exists, and it must be the
    *same* writer: seeding a new character through the importer's synthesis
    instead would flatten the four backbone sections into `CONSTITUTION#Identity`
    on the way in, so the structured draft you typed would not survive its own
    first read back.
    """
    if not draft.name.strip():
        raise CardExportError("a character needs a name", field_name="name",
                              code="validation")
    touched: set[str] = set()

    def section(filename: str, heading: str, value: str) -> None:
        _replace_section(soul / filename, heading, value)
        touched.add(filename)

    section("CONSTITUTION.md", "Identity", draft.identity)
    section("CONSTITUTION.md", "History", draft.history)
    section("CONSTITUTION.md", "Voice law", draft.system_prompt)
    section("CONSTITUTION.md", "Hard limits", draft.post_history_instructions)
    section("PERSONA.md", "Appearance", draft.appearance)
    section("PERSONA.md", "Manner", draft.manner)
    _set_frontmatter(soul / "PERSONA.md", "personality", draft.personality)
    section("SCENARIO.md", "Scenario", draft.scenario)
    greetings = _replace_numbered(
        soul / "SCENARIO.md", "Alternate greeting", draft.alternate_greetings,
        header="---\nsoul: scenario\n---\n\n# Scenario and greetings\n")
    touched.add("SCENARIO.md")

    # The cold open is written back where it currently lives: the live bootstrap
    # while she still has one, the retired copy once she has met someone (§5.4).
    # Recreating `soul/BOOTSTRAP.md` for a grown character would be the studio
    # telling the runtime she has never met you — and would leave a second
    # bootstrap for the next greeting to trip over, since `soul/onboarded/`
    # already holds one.
    bootstrap = soul / "BOOTSTRAP.md"
    retired = soul / RETIRED_BOOTSTRAP
    if draft.first_mes.strip():
        target = retired if (retired.is_file() and not bootstrap.is_file()) else bootstrap
        if not target.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                "---\nsoul: bootstrap\nconsumed_once: true\n---\n\n# Bootstrap\n",
                encoding="utf-8")
        _replace_section(target, "Cold open", draft.first_mes)
        touched.add(target.relative_to(soul).as_posix())

    _replace_numbered(soul / "EXAMPLES.md", "Example", draft.examples,
                      header="---\nsoul: examples\n---\n\n# Example dialogues\n")
    touched.add("EXAMPLES.md")
    _write_lorebook(soul / "WORLD.md", draft.lorebook, draft.name)
    touched.add("WORLD.md")
    (soul / "NOTES.md").write_text(draft.creator_notes.rstrip() + "\n", encoding="utf-8")
    touched.add("NOTES.md")

    manifest: dict[str, Any] = {
        "name": draft.name, "creator": draft.creator,
        "character_version": draft.character_version, "tags": draft.tags,
        "drives": draft.drives,
    }
    if draft.nickname.strip():
        manifest["nickname"] = draft.nickname
    _set_manifest(soul / "soul.yaml", manifest)
    touched.add("soul.yaml")

    # The manifest's greeting references have to match the blocks just written —
    # the count `_replace_numbered` reports, not the length of the draft list,
    # which may have held blanks it dropped.
    _sync_greeting_refs(soul / "soul.yaml", greetings)
    return sorted(touched)


def _sync_greeting_refs(manifest_path: Path, count: int) -> None:
    text = manifest_path.read_text(encoding="utf-8")
    refs = "\n".join(f'    - "SCENARIO.md#Alternate greeting {index}"'
                     for index in range(1, count + 1))
    block = "  alternate_greetings:\n" + (refs if refs else "    []")
    pattern = re.compile(r"(?m)^  alternate_greetings:\s*\n(?:    [-\[].*\n?)*")
    if pattern.search(text):
        text = pattern.sub(block + "\n", text, count=1)
        manifest_path.write_text(text, encoding="utf-8")


def touched_by_draft(record: CharacterRecord) -> bool:
    """Whether this character's soul is shaped the way the studio expects."""
    soul = Path(record.paths.vault) / "soul"
    return (soul / "soul.yaml").is_file()
