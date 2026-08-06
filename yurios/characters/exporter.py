"""SOUL → a `.PNG` character card someone else can boot (SPEC §28).

The importer's mirror. It reads the persona the mind has been living in — and,
where you approved it at the gate (§23), editing — flattens it through
`soul.yaml` into a Character Card V3, and writes the bytes with a `yurios`
extension block that carries the soul files *verbatim* so a re-import on another
machine reconstructs `vault/soul/` exactly rather than re-deriving flattened
prose with holes in it.

What travels is who she is. What does not travel is who you are: `USER.md`, the
memory tier, the corpus, the traces, her goals, her world model and every
credential are unreachable from this module by construction, and `privacy.py`
refuses the bytes if a canary from any of them turns up in them anyway. A card
handed to someone else begins the relationship at zero (`soul-src`, D-014).

Three details here are load-bearing and look like tidying if you don't know:

  * **`tEXt`, spliced by hand after IHDR.** Some Pillow versions write `iTXt`
    for `PngInfo` text and SillyTavern reads only `tEXt`, so a Pillow-written
    card arrives at the far end as "no character data". Base64 is ASCII and
    therefore always latin-1 safe. Do not "clean this up".
  * **Macros are preserved, never expanded.** `SoulLoader` substitutes
    `{{user}}` because it is building a prompt for the person the file is
    about; an export must not, or the exporting user's name is baked into a
    stranger's card. This is the easiest possible way to leak a name.
  * **`BOOTSTRAP.md` is consumed-once** (§5.4) — file-presence *is* the "has she
    met you yet?" flag — so every character you have actually spoken to has no
    cold open, and `soul.yaml` maps `first_mes` straight at it. A naive export
    raises `FileNotFoundError` on exactly the grown characters this feature
    exists for. `_first_message` is that fallback.
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import io
import json
import re
import struct
import zlib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml
from PIL import Image, ImageOps, UnidentifiedImageError

from . import vcs
from .card import CardLimits
from .models import CharacterRecord
from .privacy import (
    EXTENSION_DENY,
    PRIVATE_SOUL_FILES,
    CardExportError,
    PrivacyReport,
    assay,
    harvest,
    stays_report,
    string_leaves,
)
from .soulfiles import SoulPrivacyError, SoulReader, retired_cold_open

#: Every key a V3 card may carry. Emitting anything outside this set is a bug,
#: asserted at build time — the allowlist *is* scrub layer 3.
V3_FIELDS = frozenset({
    "name", "nickname", "description", "personality", "scenario", "first_mes",
    "mes_example", "system_prompt", "post_history_instructions",
    "alternate_greetings", "group_only_greetings", "creator_notes",
    "creator_notes_multilingual", "tags", "creator", "character_version",
    "character_book", "assets", "source", "creation_date", "modification_date",
    "extensions",
})
#: V3-only keys, dropped from the V2 `chara` chunk.
_V3_ONLY = frozenset({
    "nickname", "assets", "source", "group_only_greetings",
    "creator_notes_multilingual", "creation_date", "modification_date",
})

SCHEMA_VERSION = 1
YURIOS_URL = "https://yurios.org"
DEFAULT_CANVAS = (512, 768)

# Four caps, spaced so each one can actually fire. A typical soul is 8–40 KB of
# markdown, so none of them is reachable without something pathological — but
# they have to degrade in the right order when it is: lose one file, then the
# payload, then refuse. (Set the payload total at or above the soft card cap and
# the soft cap becomes dead code, because the payload always sheds first.)
MAX_EXTENSION_BYTES = 64 * 1024        # per third-party extensions key
MAX_SOUL_FILE_BYTES = 256 * 1024       # per soul file — over it, that file is dropped
MAX_SOUL_TOTAL_BYTES = 512 * 1024      # the payload as a whole
SOFT_CARD_BYTES = 1024 * 1024          # a card this big sheds its payload
HARD_CARD_BYTES = 3 * 1024 * 1024      # over this, no card at all

#: Sentinels `importer._create_soul` writes when a card had nothing to say. They
#: are honest placeholders in a Vault and embarrassing in a shipped card.
_PLACEHOLDER_RE = re.compile(r"_\((?:no|not|the source card|to be|unknown)[^)]*\)_", re.IGNORECASE)

# The ch. 07 soft budgets, in approximate tokens (chars/4). Advisory, never fatal.
BUDGETS: dict[str, tuple[int, int]] = {
    "description": (150, 300),
    "personality": (40, 80),
    "scenario": (30, 60),
    "first_mes": (80, 200),
}
_REPORTED = ("description", "personality", "scenario", "first_mes", "mes_example",
             "system_prompt", "post_history_instructions")


# ------------------------------------------------------------------- options

@dataclass(frozen=True, slots=True)
class ExportOptions:
    spec: str = "v3"                    # "v3" writes ccv3 + chara; "v2" writes chara
    include_soul: bool = True           # the verbatim soul payload
    image: str = "portrait"             # "portrait" | "selfie:<name>" | "upload"
    image_bytes: bytes | None = None
    fit: str = "contain"                # "contain" | "cover" | "none"
    attribution: bool = True            # the creator_notes footer — never the persona
    timestamps: bool = True             # False writes 0, per the V3 privacy allowance
    filename: str | None = None
    acknowledged: bool = False          # the user cleared the soul-overlap review

    def __post_init__(self) -> None:
        if self.spec not in ("v2", "v3"):
            raise CardExportError(f"unknown card spec: {self.spec!r} (v2|v3)")
        if self.fit not in ("contain", "cover", "none"):
            raise CardExportError(f"unknown image fit: {self.fit!r}")


@dataclass(frozen=True, slots=True)
class TokenRow:
    field: str
    tokens: int
    budget: str
    over: bool


@dataclass(frozen=True, slots=True)
class ExportWarning:
    code: str
    message: str
    field: str = ""


@dataclass(slots=True)
class ExportResult:
    png: bytes
    card: dict[str, Any]
    filename: str
    report: list[TokenRow] = field(default_factory=list)
    warnings: list[ExportWarning] = field(default_factory=list)
    privacy: PrivacyReport = field(default_factory=PrivacyReport)
    verified: dict[str, str] = field(default_factory=dict)

    def to_dict(self, *, with_card_json: bool = True) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "bytes": len(self.png),
            "card": self.card if with_card_json else None,
            "report": [asdict(row) for row in self.report],
            "warnings": [asdict(w) for w in self.warnings],
            "privacy": self.privacy.to_dict(),
            "verified": self.verified,
        }


# ------------------------------------------------------------------ the soul

@dataclass(slots=True)
class SoulSnapshot:
    """One consistent read of a soul folder, plus the revision it came from.

    Everything is read in a single pass and `head` captured alongside it: a
    running mind can commit between the read and the write, and the card must
    describe exactly the bytes it shipped, not whatever the Vault said later.
    """
    manifest: dict[str, Any]
    reader: SoulReader
    files: dict[str, str]
    head: str | None
    forbidden: frozenset[str]
    soul_dir: Path = Path(".")   # for the few reads that are ours, not the manifest's

    @property
    def text(self) -> str:
        """Everything exportable, concatenated — the soul-overlap haystack."""
        return "\n".join(self.files.values())


def read_soul(record: CharacterRecord) -> SoulSnapshot:
    soul_dir = Path(record.paths.vault) / "soul"
    manifest_path = soul_dir / "soul.yaml"
    if not manifest_path.is_file():
        raise CardExportError("this character has no soul.yaml to export from",
                              surface="vault/soul/soul.yaml")
    try:
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise CardExportError(f"soul.yaml is not valid YAML: {exc}",
                              surface="vault/soul/soul.yaml") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("fields"), dict):
        raise CardExportError("soul.yaml has no `fields:` mapping",
                              surface="vault/soul/soul.yaml")

    runtime_only = manifest.get("runtime_only") or []
    forbidden = PRIVATE_SOUL_FILES | {
        str(name).strip() for name in runtime_only if isinstance(name, str)}
    reader = SoulReader(soul_dir, forbidden=frozenset(forbidden))

    files: dict[str, str] = {"soul.yaml": manifest_path.read_text(encoding="utf-8")}
    for path in sorted(soul_dir.glob("*.md")):
        if path.name.casefold() in {n.casefold() for n in forbidden}:
            continue
        try:
            files[path.name] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return SoulSnapshot(manifest=manifest, reader=reader, files=files,
                        head=vcs.head(record.paths.vault),
                        forbidden=frozenset(forbidden), soul_dir=soul_dir)


def _resolve(snapshot: SoulSnapshot, ref: Any, *, field_name: str) -> str:
    """Resolve one manifest reference, turning a jail refusal into an export error."""
    try:
        return snapshot.reader.resolve_field(ref)
    except SoulPrivacyError as exc:
        raise CardExportError(
            f"soul.yaml points the card's `{field_name}` at {exc.name} — "
            f"{exc.reason}. Fix the manifest or remove the reference.",
            surface=f"vault/soul/{exc.name}", field_name=field_name,
            code="manifest") from exc
    except (FileNotFoundError, KeyError) as exc:
        raise CardExportError(
            f"soul.yaml's `{field_name}` does not resolve: {exc}",
            surface="vault/soul/soul.yaml", field_name=field_name,
            code="manifest") from exc


def _resolve_list(snapshot: SoulSnapshot, ref: Any, *, field_name: str) -> list[str]:
    if not ref:
        return []
    try:
        return [text for text in snapshot.reader.resolve_list(ref) if text.strip()]
    except SoulPrivacyError as exc:
        raise CardExportError(
            f"soul.yaml points the card's `{field_name}` at {exc.name} — {exc.reason}.",
            surface=f"vault/soul/{exc.name}", field_name=field_name) from exc
    except (FileNotFoundError, KeyError):
        return []


def _examples(snapshot: SoulSnapshot, fname: Any) -> str:
    """Each '## Example …' block → one <START> exchange (§5.1)."""
    if not isinstance(fname, str):
        return ""
    try:
        blocks = [body for heading, body in snapshot.reader.sections(fname).items()
                  if heading.lower().startswith("example")]
    except (SoulPrivacyError, FileNotFoundError, KeyError):
        return ""
    return "\n".join(f"<START>\n{block.strip()}" for block in blocks if block.strip())


def _character_book(snapshot: SoulSnapshot, fname: Any, name: str) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    front: dict[str, Any] = {}
    if isinstance(fname, str):
        try:
            front = snapshot.reader.front(fname)
            sections = snapshot.reader.sections(fname)
        except (SoulPrivacyError, FileNotFoundError, KeyError):
            sections = {}
        for order, (heading, body) in enumerate(sections.items(), start=1):
            lines = body.strip().splitlines()
            keys: list[str] = []
            rest = lines
            for index, line in enumerate(lines):
                if line.lower().startswith("keys:"):
                    keys = [k.strip() for k in line.split(":", 1)[1].split(",") if k.strip()]
                    rest = lines[:index] + lines[index + 1:]
                    break
            content = "\n".join(rest).strip()
            if not content:
                continue
            entries.append({
                "keys": keys or [heading],
                "content": content,
                "enabled": True,
                "insertion_order": order,
                "case_sensitive": False,
                "use_regex": False,
                "constant": False,
                "name": heading,
                "extensions": {},
            })
    return {
        "name": str(front.get("name") or f"{name} — lorebook"),
        "description": str(front.get("description") or ""),
        "scan_depth": int(front.get("scan_depth") or 4),
        "token_budget": int(front.get("token_budget") or 600),
        "recursive_scanning": bool(front.get("recursive_scanning") or False),
        "extensions": {},
        "entries": entries,
    }


def _first_message(snapshot: SoulSnapshot, fields: Mapping[str, Any],
                   greetings: list[str], warnings: list[ExportWarning]) -> str:
    """The cold open, or the honest fallback when it has already been consumed.

    Consumed is not the same as gone. Retirement moves the bootstrap to
    `soul/onboarded/` so that *this* runtime stops opening on it (§5.4) — it
    says nothing about the card, and a stranger importing her has never met her.
    So a retired cold open is still the card's first message; the return-greeting
    fallback below is for the case where there is genuinely no cold open left to
    ship.
    """
    ref = fields.get("first_mes")
    name = re.split(r"[#@]", str(ref), maxsplit=1)[0].strip() if ref else ""
    if name and snapshot.reader.exists(name):
        return _resolve(snapshot, ref, field_name="first_mes")
    heading = str(ref).split("#", 1)[1].strip() if "#" in str(ref) else "Cold open"
    retired = retired_cold_open(snapshot.soul_dir, heading)
    if retired:
        return retired
    if greetings:
        warnings.append(ExportWarning(
            code="bootstrap_consumed",
            field="first_mes",
            message="Her cold open was consumed when you met, so the card ships her "
                    "return greeting as the first message. Write a fresh cold open if "
                    "this card is for a stranger."))
        return greetings[0]
    raise CardExportError(
        "this character has no first message: her cold open was consumed when you "
        "met and she has no return greetings. Write one before exporting.",
        surface="vault/soul/SCENARIO.md", field_name="first_mes")


# ------------------------------------------------------------ the yurios block

def _growth(record: CharacterRecord, snapshot: SoulSnapshot,
            *, timestamps: bool) -> dict[str, Any]:
    """Counts, never content. '94 days lived, 17 approved self-edits' says
    everything about the runtime and discloses nothing about the relationship."""
    vault = Path(record.paths.vault)
    commits = vcs.log(vault, limit=500)
    # One `git log --name-only` for the whole folder rather than one per file:
    # a file with more than the commit that seeded it has been edited since.
    touched = vcs.commit_counts(vault, "soul", limit=500)
    changed = sorted(
        name for name in snapshot.files
        if name != "soul.yaml" and touched.get(f"soul/{name}", 0) > 1
    )
    growth: dict[str, Any] = {
        "vault_commits": vcs.count_commits(vault),
        "self_edits_applied": sum(1 for c in commits if c.author == "her"),
        "soul_files_changed": changed,
    }
    first = vcs.first_commit(vault)
    if timestamps and first is not None:
        growth["days_lived"] = max(
            0, (datetime.datetime.now(datetime.timezone.utc) - first.when).days)
    return growth


def _soul_payload(snapshot: SoulSnapshot,
                  warnings: list[ExportWarning]) -> dict[str, Any] | None:
    total = sum(len(text.encode("utf-8")) for text in snapshot.files.values())
    if total > MAX_SOUL_TOTAL_BYTES:
        warnings.append(ExportWarning(
            code="soul_payload_dropped",
            message=f"the soul files are {total // 1024} KB, over the "
                    f"{MAX_SOUL_TOTAL_BYTES // 1024} KB card budget — the card ships "
                    "flattened prose only, and a re-import will be lossy."))
        return None
    files = {name: text for name, text in snapshot.files.items()
             if len(text.encode("utf-8")) <= MAX_SOUL_FILE_BYTES}
    for name in snapshot.files.keys() - files.keys():
        warnings.append(ExportWarning(
            code="soul_file_too_large", field=name,
            message=f"{name} is over {MAX_SOUL_FILE_BYTES // 1024} KB and is not "
                    "carried in the card's soul payload."))
    manifest = {k: v for k, v in snapshot.manifest.items() if k != "runtime_only"}
    return {
        "manifest": manifest,
        "files": files,
        "encoding": "utf-8",
        "sha256": {name: hashlib.sha256(text.encode("utf-8")).hexdigest()
                   for name, text in files.items()},
    }


def _yurios_block(record: CharacterRecord, snapshot: SoulSnapshot,
                  options: ExportOptions, previous: Mapping[str, Any],
                  warnings: list[ExportWarning]) -> dict[str, Any]:
    manifest = snapshot.manifest
    generation = previous.get("lineage", {}).get("generation") if isinstance(
        previous.get("lineage"), Mapping) else None
    block: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "runtime": "YuriOS",
        "runtime_version": _runtime_version(),
        "docs": YURIOS_URL,
        "lineage": {
            "character_id": record.id,
            "card_version": _card_version(manifest),
            "canon": str(manifest.get("canon") or ""),
            "vault_head": snapshot.head,
            "generation": int(generation) + 1 if isinstance(generation, int) else 0,
            "grown_from": _source_digest(record),
        },
        "growth": _growth(record, snapshot, timestamps=options.timestamps),
    }
    if record.body.backend or record.body.model:
        block["body"] = {"backend": record.body.backend, "model": record.body.model}
    if record.voice.tts_backend or record.voice.voice_id:
        block["voice"] = {"tts_backend": record.voice.tts_backend,
                          "voice_id": record.voice.voice_id}
    if options.include_soul:
        payload = _soul_payload(snapshot, warnings)
        if payload is not None:
            block["soul"] = payload
        else:
            block["soul_omitted"] = "size"
    return block


def _runtime_version() -> str:
    """Which YuriOS grew her. A tree that was never pip-installed says so."""
    try:
        from importlib.metadata import version
        return version("yurios")
    except Exception:
        return "unknown"


def _card_version(manifest: Mapping[str, Any]) -> str:
    """The §5.2 stamp the corpus already carries: `<name>-v<major>@<canon>`."""
    name = str(manifest.get("name") or "character").lower()
    major = str(manifest.get("character_version") or "1.0.0").split(".")[0]
    return f"{name}-v{major}@{manifest.get('canon') or 'unknown'}"


def _source_digest(record: CharacterRecord) -> str | None:
    path = Path(record.paths.source_card)
    if not path.is_file():
        return None
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _passthrough_extensions(record: CharacterRecord,
                            warnings: list[ExportWarning]) -> dict[str, Any]:
    """Third-party card extensions that arrived with the character, filtered.

    `depth_prompt`, `talkativeness` and friends are worth keeping so a
    round-tripped card still behaves in the client it came from. Everything else
    about `card.json` is *not* a source — an export is authored, not forwarded.
    """
    path = Path(record.paths.card_json)
    if not path.is_file():
        return {}
    try:
        wrapper = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    data = wrapper.get("data") if isinstance(wrapper.get("data"), Mapping) else wrapper
    extensions = data.get("extensions") if isinstance(data, Mapping) else None
    if not isinstance(extensions, Mapping):
        return {}
    kept: dict[str, Any] = {}
    for key, value in extensions.items():
        if not isinstance(key, str) or key in EXTENSION_DENY:
            continue
        try:
            encoded = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            warnings.append(ExportWarning(
                code="extension_dropped", field=key,
                message=f"the `{key}` card extension is not JSON and was dropped."))
            continue
        if len(encoded.encode("utf-8")) > MAX_EXTENSION_BYTES:
            warnings.append(ExportWarning(
                code="extension_dropped", field=key,
                message=f"the `{key}` card extension is over "
                        f"{MAX_EXTENSION_BYTES // 1024} KB and was dropped."))
            continue
        kept[key] = value
    return kept


# ------------------------------------------------------------------- the card

ATTRIBUTION = (
    "Grown under YuriOS — an always-on, local-first companion runtime. This card "
    "carries her full soul: import it into YuriOS and she picks up the persona "
    "exactly as authored, with her own memory, journal and inner life starting "
    f"fresh. {YURIOS_URL}"
)


def to_card_data(record: CharacterRecord, snapshot: SoulSnapshot,
                 options: ExportOptions,
                 warnings: list[ExportWarning]) -> dict[str, Any]:
    """The card `data` dict, built key by key. Scrub layer 3 lives here."""
    manifest = snapshot.manifest
    fields = manifest["fields"]
    name = str(manifest.get("name") or record.display.name or "Companion").strip()

    greetings = _resolve_list(snapshot, fields.get("alternate_greetings"),
                              field_name="alternate_greetings")
    creator_notes = _resolve(snapshot, fields["creator_notes"], field_name="creator_notes") \
        if fields.get("creator_notes") else ""
    if options.attribution:
        creator_notes = (creator_notes.rstrip() + "\n\n" + ATTRIBUTION).strip()

    tags = [str(tag) for tag in (manifest.get("tags") or []) if str(tag).strip()]
    if options.attribution and "yurios" not in {t.casefold() for t in tags}:
        tags.append("yurios")

    previous = _previous_yurios(record)
    extensions = _passthrough_extensions(record, warnings)
    extensions["yurios"] = _yurios_block(record, snapshot, options, previous, warnings)

    now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    first = vcs.first_commit(record.paths.vault)
    created = int(first.when.timestamp()) if first is not None else now

    data: dict[str, Any] = {
        "name": name,
        "nickname": str(manifest.get("nickname") or ""),
        "description": _resolve(snapshot, fields["description"], field_name="description"),
        "personality": _resolve(snapshot, fields["personality"], field_name="personality"),
        "scenario": _resolve(snapshot, fields["scenario"], field_name="scenario"),
        "first_mes": _first_message(snapshot, fields, greetings, warnings),
        "mes_example": _examples(snapshot, fields.get("mes_example")),
        "system_prompt": _resolve(snapshot, fields["system_prompt"], field_name="system_prompt"),
        "post_history_instructions": _resolve(
            snapshot, fields["post_history_instructions"],
            field_name="post_history_instructions"),
        "alternate_greetings": greetings,
        "group_only_greetings": [],
        "creator_notes": creator_notes,
        "creator_notes_multilingual": {},
        "tags": tags,
        "creator": str(manifest.get("creator") or record.display.creator or ""),
        "character_version": str(manifest.get("character_version") or "1.0.0"),
        "character_book": _character_book(snapshot, fields.get("character_book"), name),
        "assets": [{"type": "icon", "uri": "ccdefault:", "name": "main", "ext": "png"}],
        "source": _source_list(previous),
        "creation_date": created if options.timestamps else 0,
        "modification_date": now if options.timestamps else 0,
        "extensions": extensions,
    }
    unknown = set(data) - V3_FIELDS
    assert not unknown, f"exporter emitted non-V3 card keys: {sorted(unknown)}"
    return data


def _previous_yurios(record: CharacterRecord) -> dict[str, Any]:
    """The `yurios` block the character arrived with, for lineage continuity only."""
    path = Path(record.paths.card_json)
    if not path.is_file():
        return {}
    try:
        wrapper = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    data = wrapper.get("data") if isinstance(wrapper.get("data"), Mapping) else wrapper
    if not isinstance(data, Mapping):
        return {}
    extensions = data.get("extensions")
    block = extensions.get("yurios") if isinstance(extensions, Mapping) else None
    return dict(block) if isinstance(block, Mapping) else {}


def _source_list(previous: Mapping[str, Any]) -> list[str]:
    """V3 `source` is append-only, and the right home for provenance."""
    existing = previous.get("source") if isinstance(previous.get("source"), list) else []
    sources = [str(item) for item in existing if isinstance(item, str)]
    if YURIOS_URL not in sources:
        sources.append(YURIOS_URL)
    return sources


def wrap_card(data: Mapping[str, Any], spec: str) -> dict[str, Any]:
    if spec == "v3":
        return {"spec": "chara_card_v3", "spec_version": "3.0", "data": dict(data)}
    v2 = {key: value for key, value in data.items() if key not in _V3_ONLY}
    book = v2.get("character_book")
    if isinstance(book, Mapping):
        v2["character_book"] = {
            **book,
            "entries": [{k: v for k, v in entry.items() if k != "use_regex"}
                        | {"content": _strip_decorators(str(entry.get("content", "")))}
                        for entry in book.get("entries", [])],
        }
    return {"spec": "chara_card_v2", "spec_version": "2.0", "data": v2}


def _strip_decorators(content: str) -> str:
    """V3 lorebook decorators (`@@depth`, `@@role`, …) mean nothing to a V2 reader."""
    return "\n".join(line for line in content.splitlines()
                     if not line.lstrip().startswith("@@")).strip()


# ------------------------------------------------------------------ the image

def _decode_image(png: bytes, limits: CardLimits) -> tuple[Image.Image, dict[str, str]]:
    """Decode to pixels, and hand back the text chunks separately.

    Separately, because Pillow drops `.text` the moment you `convert()` or
    transpose — which is exactly the metadata strip we want, but it also means a
    later `getattr(image, "text")` silently sees nothing. Read it off the opened
    file while it still exists, so the privacy report can say truthfully what was
    dropped.
    """
    try:
        with Image.open(io.BytesIO(png)) as image:
            image.load()
            image.seek(0)
            text = dict(getattr(image, "text", {}) or {})
            oriented = ImageOps.exif_transpose(image)
            if oriented.width * oriented.height > limits.max_pixels:
                raise CardExportError("card image dimensions exceed limits", surface="image")
            has_alpha = oriented.mode in ("RGBA", "LA") or "transparency" in oriented.info
            return oriented.convert("RGBA" if has_alpha else "RGB"), text
    except CardExportError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise CardExportError(f"cannot decode the card image: {exc}",
                              surface="image") from exc


def _select_image(record: CharacterRecord, options: ExportOptions,
                  limits: CardLimits) -> tuple[bytes, str]:
    if options.image.startswith("selfie:"):
        name = options.image.split(":", 1)[1]
        base = Path(record.paths.selfies).resolve()
        path = (base / name).resolve()
        if path.parent != base or not path.is_file():
            raise CardExportError(f"no such selfie: {name}", surface="selfies")
        return path.read_bytes(), f"selfies/{name}"
    if options.image == "upload":
        if not options.image_bytes:
            raise CardExportError("no image was uploaded", surface="image")
        if len(options.image_bytes) > limits.max_file_bytes:
            raise CardExportError("the uploaded image exceeds the size limit",
                                  surface="image")
        return options.image_bytes, "upload"
    path = Path(record.paths.portrait)
    if path.is_file():
        return path.read_bytes(), "portrait.png"
    canvas = Image.new("RGB", DEFAULT_CANVAS, (17, 24, 22))
    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG")
    return buffer.getvalue(), "placeholder"


def _fit(image: Image.Image, mode: str) -> Image.Image:
    if mode == "none":
        return image
    if mode == "cover":
        return ImageOps.fit(image, DEFAULT_CANVAS, method=Image.LANCZOS, centering=(0.5, 0.35))
    contained = ImageOps.contain(image, DEFAULT_CANVAS, method=Image.LANCZOS)
    if contained.size == DEFAULT_CANVAS:
        return contained
    backdrop = Image.new(contained.mode, DEFAULT_CANVAS, _edge_colour(contained))
    backdrop.paste(contained, ((DEFAULT_CANVAS[0] - contained.width) // 2,
                               (DEFAULT_CANVAS[1] - contained.height) // 2))
    return backdrop


def _edge_colour(image: Image.Image) -> tuple[int, ...]:
    sample = image.convert("RGB").resize((3, 3), Image.LANCZOS)
    pixels = [sample.getpixel((x, y)) for x in (0, 2) for y in (0, 2)]
    channels = tuple(sum(p[i] for p in pixels) // len(pixels) for i in range(3))
    return channels + ((255,) if image.mode == "RGBA" else ())


def prepare_image(record: CharacterRecord, options: ExportOptions,
                  limits: CardLimits) -> tuple[bytes, dict[str, Any]]:
    """Decode, strip every scrap of upstream metadata, fit, re-mark if honest.

    Step two is the privacy-relevant one: a re-encode through Pillow drops all
    text chunks, EXIF and generator metadata — the same posture
    `forge/provenance.apply(mode="strip")` takes. Only `content_credentials`
    comes back, because an AI-generated image should stay marked as one.
    """
    raw, source = _select_image(record, options, limits)
    image, text = _decode_image(raw, limits)
    stripped = sorted(key for key in text if key != "content_credentials")
    credentials = text.get("content_credentials")
    framed = _fit(image, options.fit)

    buffer = io.BytesIO()
    if credentials:
        from PIL import PngImagePlugin
        info = PngImagePlugin.PngInfo()
        info.add_text("content_credentials", credentials)
        framed.save(buffer, format="PNG", pnginfo=info)
    else:
        framed.save(buffer, format="PNG")
    return buffer.getvalue(), {
        "source": source,
        "size": list(framed.size),
        "fit": options.fit,
        "stripped": stripped,
        "kept": ["content_credentials"] if credentials else [],
    }


# ------------------------------------------------------------------ the bytes

def text_chunk(keyword: str, card: Mapping[str, Any]) -> bytes:
    """One PNG `tEXt` chunk: length + type + keyword\\0base64(json) + CRC32."""
    encoded = base64.b64encode(json.dumps(card, ensure_ascii=False).encode("utf-8"))
    body = keyword.encode("latin-1") + b"\x00" + encoded
    return (struct.pack(">I", len(body)) + b"tEXt" + body
            + struct.pack(">I", zlib.crc32(b"tEXt" + body) & 0xFFFFFFFF))


def embed(png: bytes, chunks: Mapping[str, Mapping[str, Any]]) -> bytes:
    ihdr_end = 8 + 4 + 4 + 13 + 4      # signature + (len + "IHDR" + 13 + CRC)
    payload = b"".join(text_chunk(keyword, card) for keyword, card in chunks.items())
    return png[:ihdr_end] + payload + png[ihdr_end:]


def read_card_chunks(png: bytes) -> dict[str, dict[str, Any]]:
    """Re-read a card the permissive way a client does: walk chunks, take `tEXt`.

    Deliberately *not* `card.parse_png_card` — that is the strict parser, and
    verification has to answer a different question: would a real reader, which
    checks far less, still find the card? Both run at build time (§5.3).
    """
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise CardExportError("the exporter wrote something that is not a PNG")
    found: dict[str, dict[str, Any]] = {}
    offset = 8
    while offset + 8 <= len(png):
        (length,) = struct.unpack(">I", png[offset:offset + 4])
        kind = png[offset + 4:offset + 8]
        body = png[offset + 8:offset + 8 + length]
        # Advance first. A `continue` before this line is an infinite loop, and
        # the trigger is ordinary: any non-card `tEXt` chunk — a caption, or the
        # `content_credentials` record a selfie carries — fails the JSON decode.
        offset += 12 + length
        if kind == b"tEXt":
            stored = struct.unpack(">I", png[offset - 4:offset])[0]
            if zlib.crc32(kind + body) & 0xFFFFFFFF != stored:
                raise CardExportError("tEXt CRC mismatch — a reader would reject this card")
            keyword, _, text = body.partition(b"\x00")
            try:
                card = json.loads(base64.b64decode(text))
            except (ValueError, TypeError):
                continue           # a plain caption, not a card chunk
            if isinstance(card, dict):
                found[keyword.decode("latin-1")] = card
        elif kind == b"IEND":
            break
    return found


def png_text_chunks(png: bytes) -> dict[str, str]:
    """Every `tEXt` value in a PNG, decoded but not interpreted.

    The image is the second way private content reaches a card — a selfie's
    generation prompt, an upload's caption — and none of it is visible to an
    assay that only reads the card JSON.
    """
    values: dict[str, str] = {}
    offset = 8
    while offset + 8 <= len(png):
        (length,) = struct.unpack(">I", png[offset:offset + 4])
        kind = png[offset + 4:offset + 8]
        body = png[offset + 8:offset + 8 + length]
        offset += 12 + length
        if kind == b"tEXt":
            keyword, _, text = body.partition(b"\x00")
            values[keyword.decode("latin-1", "replace")] = text.decode("utf-8", "replace")
        elif kind == b"IEND":
            break
    return values


def verify(png: bytes, limits: CardLimits) -> dict[str, str]:
    """Both readers must find her, or the card does not leave this machine.

    An export a reader cannot parse is worse than an error, because it fails at
    the far end, on someone else's machine, silently.
    """
    from .card import CardParseError, card_fields, parse_png_card
    try:
        strict = parse_png_card(png, limits=limits)
    except CardParseError as exc:
        raise CardExportError(f"the exported card failed strict re-parsing: {exc}") from exc
    permissive = read_card_chunks(png)
    if not permissive:
        raise CardExportError("no tEXt chunks in the exported card — a reader "
                              "would say it contains no character")
    names: dict[str, str] = {}
    for keyword, card in permissive.items():
        data = card.get("data") if isinstance(card.get("data"), Mapping) else card
        name = str(data.get("name") or "")
        if not name:
            raise CardExportError(f"the {keyword} chunk carries no character name")
        names[keyword] = name
    strict_name = str(card_fields(strict.data).get("name") or "")
    if strict_name not in names.values():
        raise CardExportError("the strict and permissive readers disagree about "
                              "who is on this card")
    return names


# ------------------------------------------------------------------- reports

def estimate_tokens(text: str) -> int:
    """The chars/4 estimate, rounding halves *up*.

    `round()` is banker's rounding in Python and rounds half to even, so a
    114-character field estimates as 28 here and as 29 in `web/studio/draft.js`,
    whose `Math.round` rounds half up. The number is advisory either way, but the
    studio's budget panel and the shipped report disagreeing over the same text
    is the kind of small lie that costs an afternoon. Pinned by the parity test.
    """
    return max(1, int(len(text) / 4 + 0.5)) if text else 0


def token_report(data: Mapping[str, Any]) -> list[TokenRow]:
    def tokens(text: str) -> int:
        return estimate_tokens(text)

    rows: list[TokenRow] = []
    for name in _REPORTED:
        count = tokens(str(data.get(name) or ""))
        budget = BUDGETS.get(name)
        if budget:
            rows.append(TokenRow(name, count, f"{budget[0]}–{budget[1]}", count > budget[1]))
        elif name == "mes_example":
            rows.append(TokenRow(name, count, "spend freely", False))
        else:
            rows.append(TokenRow(name, count, "minimal", False))
    entries = len(data.get("character_book", {}).get("entries", []))
    rows.append(TokenRow("lorebook entries", entries, "fires on keys", False))
    return rows


def content_warnings(data: Mapping[str, Any], snapshot: SoulSnapshot) -> list[ExportWarning]:
    """Advisory only. Everything here is a judgement call, not a rule."""
    found: list[ExportWarning] = []
    for name in ("description", "personality", "scenario", "first_mes", "creator_notes"):
        text = str(data.get(name) or "")
        if _PLACEHOLDER_RE.search(text):
            found.append(ExportWarning(
                code="placeholder", field=name,
                message=f"`{name}` still contains an importer placeholder — it would "
                        "ship as her personality. Write it before sharing."))
        if not text.strip() and name in ("description", "personality"):
            found.append(ExportWarning(code="empty", field=name,
                                       message=f"`{name}` is empty."))
    for row in token_report(data):
        if row.over:
            found.append(ExportWarning(
                code="over_budget", field=row.field,
                message=f"`{row.field}` is about {row.tokens} tokens, over the "
                        f"{row.budget} guidance — it costs this on every turn."))
    if not data.get("character_book", {}).get("entries"):
        found.append(ExportWarning(code="no_lore", field="character_book",
                                   message="no lorebook entries — nothing fires on keywords."))
    if not data.get("mes_example"):
        found.append(ExportWarning(code="no_examples", field="mes_example",
                                   message="no example dialogue — her voice has to be "
                                           "inferred from prose alone."))
    # The voice-law check, generalised: only complain when her own constitution does.
    law = str(data.get("system_prompt") or "") + str(data.get("post_history_instructions") or "")
    if re.search(r"(no|never|avoid|without)[^.]{0,40}exclamation", law, re.IGNORECASE):
        for name in ("first_mes", "mes_example"):
            if "!" in str(data.get(name) or ""):
                found.append(ExportWarning(
                    code="voice_law", field=name,
                    message=f"`{name}` contains '!', which her own voice law forbids."))
    return found


# --------------------------------------------------------------------- build

def _filename(record: CharacterRecord, data: Mapping[str, Any],
              options: ExportOptions) -> str:
    if options.filename:
        stem = Path(options.filename).stem
    else:
        stem = str(data.get("name") or record.display.name)
    slug = re.sub(r"[^a-z0-9_-]+", "-", stem.lower()).strip("-")
    return f"{slug or record.id}.png"


def _build(record: CharacterRecord, options: ExportOptions, *,
           limits: CardLimits, user_name: str, with_bytes: bool) -> ExportResult:
    warnings: list[ExportWarning] = []
    snapshot = read_soul(record)
    data = to_card_data(record, snapshot, options, warnings)
    warnings.extend(content_warnings(data, snapshot))

    card = wrap_card(data, options.spec)
    serialized = json.dumps(card, ensure_ascii=False)
    size = len(serialized.encode("utf-8"))
    if size > HARD_CARD_BYTES:
        raise CardExportError(
            f"the card is {size // 1024} KB, over the {HARD_CARD_BYTES // 1024} KB "
            "limit a reader will accept")
    if size > SOFT_CARD_BYTES and options.include_soul:
        warnings.append(ExportWarning(
            code="soul_payload_dropped",
            message=f"the card reached {size // 1024} KB, so the verbatim soul payload "
                    "was dropped to keep it readable; a re-import will be lossy."))
        data["extensions"]["yurios"].pop("soul", None)
        data["extensions"]["yurios"]["soul_omitted"] = "size"
        card = wrap_card(data, options.spec)
        serialized = json.dumps(card, ensure_ascii=False)

    # ---- scrub layer 4, on the JSON --------------------------------------
    canaries = harvest(record.paths.root, user_name=user_name)
    hits, overlaps = assay(card, canaries, soul_text=snapshot.text, raw=serialized)
    privacy = PrivacyReport(
        ships=[{"file": f"vault/soul/{name}", "bytes": len(text.encode('utf-8'))}
               for name, text in sorted(snapshot.files.items())],
        stays=stays_report(record.paths.root),
        canaries=len(canaries), ran_on=["json"], hits=hits, soul_overlaps=overlaps,
        head=snapshot.head)
    _refuse_on(hits)
    # The review gate binds the thing that writes bytes, not the pane that exists
    # to show you what needs reviewing — a preview that refused to render the
    # overlaps would leave no way to clear them.
    if with_bytes:
        _require_review(overlaps, acknowledged=options.acknowledged, warnings=warnings)
    elif overlaps:
        warnings.append(ExportWarning(
            code="review_required",
            message=f"{len(overlaps)} passage(s) in her soul also appear in private "
                    "surfaces. Read them below; the export asks you to confirm."))

    image_meta: dict[str, Any] = {}
    png = b""
    verified: dict[str, str] = {}
    if with_bytes:
        image, image_meta = prepare_image(record, options, limits)
        chunks: dict[str, Mapping[str, Any]] = {"chara": wrap_card(data, "v2")}
        if options.spec == "v3":
            chunks["ccv3"] = wrap_card(data, "v3")
        png = embed(image, chunks)
        # ---- scrub layer 4 again, on the bytes ---------------------------
        # The image is the second way in: a selfie's prompt metadata, an
        # upload's caption, a stray chunk. The card pass cannot see any of it,
        # and a raw scan of the PNG cannot see the card either — the card chunks
        # are base64. So assay what a reader would actually recover: every text
        # chunk's decoded value, with the raw bytes behind it for credentials.
        image_text = "\n".join(png_text_chunks(png).values())
        byte_hits, byte_overlaps = assay(
            [image_text, *string_leaves(card)], canaries,
            soul_text=snapshot.text, raw=png)
        privacy.ran_on.append("png")
        privacy.hits = byte_hits
        privacy.soul_overlaps = byte_overlaps
        _refuse_on(byte_hits)
        _require_review(byte_overlaps, acknowledged=options.acknowledged,
                        warnings=warnings)
        verified = verify(png, limits)
    privacy.image = image_meta

    return ExportResult(png=png, card=card, filename=_filename(record, data, options),
                        report=token_report(data), warnings=warnings,
                        privacy=privacy, verified=verified)


def _refuse_on(hits: list) -> None:
    """A private surface reached the card. Not overridable."""
    if not hits:
        return
    first = hits[0]
    raise CardExportError(
        f"export refused: content from {first.surface} appears in the card "
        f"(…{first.excerpt[:100]}…). {len(hits)} passage(s) matched. This is the "
        "privacy scrub doing its job — remove it from the soul files and export again.",
        surface=first.surface, code="leak", overlaps=list(hits))


def _require_review(overlaps: list, *, acknowledged: bool,
                    warnings: list[ExportWarning]) -> None:
    """Soul prose that echoes a private surface. Overridable, but only by a human.

    A grown character legitimately carries things she learned about you and you
    approved into her persona at the gate — the machine cannot tell that apart
    from a fact pasted into the wrong file, and pretending otherwise would either
    break every real export or wave through every real leak. So it stops, shows
    the passages, and asks. Once. `acknowledged` is that answer, and it is never
    the default.
    """
    if not overlaps:
        return
    if acknowledged:
        warnings.append(ExportWarning(
            code="soul_overlap_acknowledged",
            message=f"{len(overlaps)} passage(s) in her soul also appear in private "
                    "surfaces, and you chose to ship them."))
        return
    raise CardExportError(
        f"{len(overlaps)} passage(s) in her soul files also appear in surfaces that "
        "never leave this machine. For a character who has grown that is expected — "
        "she learned them and you approved them — but read them before you share "
        "her, then export again to confirm.",
        surface=overlaps[0].surface, code="review_required", overlaps=list(overlaps))


def build_export(record: CharacterRecord, options: ExportOptions | None = None, *,
                 limits: CardLimits | None = None, user_name: str = "") -> ExportResult:
    """The whole pipeline, bytes included. Raises `CardExportError` on refusal."""
    return _build(record, options or ExportOptions(), limits=limits or CardLimits(),
                  user_name=user_name, with_bytes=True)


def preview_export(record: CharacterRecord, options: ExportOptions | None = None, *,
                   limits: CardLimits | None = None, user_name: str = "") -> ExportResult:
    """Everything the review pane renders, and no file: `png` comes back empty."""
    return _build(record, options or ExportOptions(), limits=limits or CardLimits(),
                  user_name=user_name, with_bytes=False)
