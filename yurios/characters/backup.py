"""A whole character as one file: snapshots kept here, backups taken away (SPEC §29.8).

Clone copies her root to a new id on this node. Export writes a card, which is
identity and nothing else (§30.5). This is the third thing: everything she is,
in a file. Her root (Vault with its git history, memory, corpus, traces, tool
audit, selfies, the pictures you sent her) plus the registry row that names her
models, voice, loops and lifecycle, plus any uploaded body from the house
library that she wears. Without those last two a restore brings back her
memories in a stranger's body, wired to the house default.

One format serves both acts. A **snapshot** is the file kept on this node, under
`DATA_DIR/snapshots/<id>/`. A **backup** is the same file handed to you. Either
one restores: as a new character, or in her own place.

What stays out: her `.env` keys (they are secrets and live in the house file),
the connection profile's contents (the row names it, the house owns it), and
`cache/`, which is derived from files that are in the archive.

Every function here is blocking file I/O. The host calls them off the loop.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import re
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import IO, TYPE_CHECKING, Any, BinaryIO, Mapping, cast

from . import archive as archive_model
from .models import CharacterRecord, LifecycleFlags, _validate_id
from .registry import CharacterRegistry

if TYPE_CHECKING:                                   # bodylib imports desktop config
    from .bodylib import BodyLibrary

log = logging.getLogger("characters.backup")

FORMAT = "yurios-character-backup"
FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"
TREE_PREFIX = "character/"
BODIES_PREFIX = "bodies/"
SNAPSHOTS_DIR = "snapshots"

#: Top-level folders of her root that are derived from files the archive does
#: carry. Regenerated the first time anything asks.
SKIPPED_TOP_LEVEL = frozenset({"cache"})
#: Already compressed: deflating them again costs CPU and saves nothing. Her
#: selfies are most of a lived-in character's bytes.
STORED_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".vrm", ".zip", ".gz",
    ".pack", ".mp3", ".ogg", ".opus", ".m4a", ".mp4", ".webm", ".moc3", ".pdf",
})

_MiB = 1024 * 1024
_GiB = 1024 * _MiB
_CHUNK = _MiB
_BODY_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
#: `<id>-YYYYMMDD-HHMMSS[-n].zip`. The id may contain hyphens, which is why the
#: stamp is matched from the right.
_SNAPSHOT_NAME = re.compile(r"^(?P<id>[a-z0-9][a-z0-9._-]{0,63})-(?P<stamp>\d{8}-\d{6})"
                            r"(?:-(?P<n>\d{1,3}))?\.zip$")


class BackupError(ValueError):
    """The backup could not be written, read or restored. Always safe to show a user."""


@dataclass(frozen=True, slots=True)
class BackupLimits:
    """Bounds on a file that may have come from anywhere (SPEC §29.8).

    Generous, because a companion who has lived somewhere for a year has a lot of
    selfies, but finite. The largest character this was measured on was 400 MB
    across 4,800 files.
    """
    archive_bytes: int = 8 * _GiB
    expanded_bytes: int = 16 * _GiB
    entries: int = 200_000
    manifest_bytes: int = 1 * _MiB
    path_chars: int = 1024


DEFAULT_LIMITS = BackupLimits()


@dataclass(frozen=True, slots=True)
class BodyRef:
    kind: str                   # "vrm" | "live2d"
    id: str
    title: str

    @property
    def member(self) -> str:
        suffix = ".vrm" if self.kind == "vrm" else ".zip"
        return f"{BODIES_PREFIX}{self.kind}/{self.id}{suffix}"


@dataclass(frozen=True, slots=True)
class Manifest:
    character_id: str
    name: str
    created_at: str
    kind: str                   # "snapshot" | "backup"
    record: dict[str, Any]
    bodies: tuple[BodyRef, ...] = ()
    files: int = 0
    bytes: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "format": FORMAT,
            "version": FORMAT_VERSION,
            "kind": self.kind,
            "created_at": self.created_at,
            "character": {"id": self.character_id, "name": self.name},
            "record": self.record,
            "bodies": [{"kind": b.kind, "id": b.id, "title": b.title} for b in self.bodies],
            "files": self.files,
            "bytes": self.bytes,
        }

    def summary(self) -> dict[str, Any]:
        """What the board shows about a file before anything is restored from it."""
        return {
            "id": self.character_id,
            "name": self.name,
            "created_at": self.created_at,
            "kind": self.kind,
            "files": self.files,
            "bytes": self.bytes,
            "bodies": [{"kind": b.kind, "title": b.title} for b in self.bodies],
        }


# ---- writing -----------------------------------------------------------------


def write_backup(
    record: CharacterRecord,
    data_root: Path,
    dest: Path,
    *,
    library: "BodyLibrary | None" = None,
    kind: str = "backup",
    now: datetime.datetime | None = None,
) -> Manifest:
    """Write ``record``'s whole character to ``dest`` as one zip.

    The zip is assembled beside ``dest`` and renamed into place, so a reader never
    sees half a backup. The caller makes sure the tree is quiet (her runtime
    stopped). A copy taken under a live mind can catch a Vault mid-commit.
    """
    root = record.paths.root
    if not root.is_dir():
        raise BackupError(f"character root is missing: {root}")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    created = (now or datetime.datetime.now(datetime.timezone.utc)).isoformat(
        timespec="seconds")
    fd, temporary = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".tmp",
                                     dir=dest.parent)
    os.close(fd)
    try:
        with zipfile.ZipFile(temporary, "w", allowZip64=True) as archive:
            files, size = _write_tree(archive, root)
            bodies = _write_bodies(archive, record, library)
            manifest = Manifest(
                character_id=record.id,
                name=record.display.name,
                created_at=created,
                kind=kind,
                record=record.to_dict(data_root=data_root),
                bodies=tuple(bodies),
                files=files,
                bytes=size,
            )
            archive.writestr(
                MANIFEST_NAME,
                json.dumps(manifest.to_json(), ensure_ascii=False, indent=2, sort_keys=True),
                compress_type=zipfile.ZIP_DEFLATED,
            )
        os.replace(temporary, dest)
        return manifest
    except BackupError:
        raise
    except Exception as exc:
        raise BackupError(f"could not write a backup of {record.id}: {exc}") from exc
    finally:
        Path(temporary).unlink(missing_ok=True)


def _write_tree(archive: zipfile.ZipFile, root: Path) -> tuple[int, int]:
    """Her root, under `character/`. Directories are written too, so an empty
    `corpus/` or a git `refs/tags/` survives. Links and special files are not
    followed: a symlink in her tree is not hers to carry."""
    files = size = 0
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(current)
        rel_dir = here.relative_to(root)
        if rel_dir == Path("."):
            dirnames[:] = [d for d in dirnames if d not in SKIPPED_TOP_LEVEL]
        dirnames[:] = sorted(d for d in dirnames if not (here / d).is_symlink())
        if rel_dir != Path("."):
            archive.writestr(zipfile.ZipInfo(f"{TREE_PREFIX}{rel_dir.as_posix()}/"), b"")
        for name in sorted(filenames):
            path = here / name
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                continue
            rel = (rel_dir / name).as_posix()
            compress = (zipfile.ZIP_STORED if path.suffix.lower() in STORED_SUFFIXES
                        else zipfile.ZIP_DEFLATED)
            archive.write(path, f"{TREE_PREFIX}{rel}", compress_type=compress)
            files += 1
            size += info.st_size
    return files, size


def _write_bodies(archive: zipfile.ZipFile, record: CharacterRecord,
                  library: "BodyLibrary | None") -> list[BodyRef]:
    """The uploaded bodies she wears. Builtins ship with every install and stay out."""
    if library is None:
        return []
    refs: list[BodyRef] = []
    for kind, body_id in (("vrm", record.body.vrm), ("live2d", record.body.model)):
        if not body_id:
            continue
        entry = library.get(kind, body_id)
        if entry is None or entry.builtin:
            continue
        ref = BodyRef(kind, entry.id, entry.title)
        if kind == "vrm":
            path = library.vrm_file(entry.id)
            if path is None:
                continue
            archive.write(path, ref.member, compress_type=zipfile.ZIP_STORED)
        else:
            rig = library.root / "live2d" / entry.id
            if not rig.is_dir():
                continue
            # A rig goes in as the zip the library takes, so a restore re-adds it
            # through the same checks an upload passes (`BodyLibrary.add_live2d`).
            with archive.open(ref.member, "w", force_zip64=True) as member, \
                    zipfile.ZipFile(member, "w", zipfile.ZIP_DEFLATED) as inner:
                for path in sorted(rig.rglob("*")):
                    if path.is_file() and not path.is_symlink():
                        inner.write(path, path.relative_to(rig).as_posix())
        refs.append(ref)
    return refs


# ---- reading -----------------------------------------------------------------


def read_manifest(path: Path, limits: BackupLimits = DEFAULT_LIMITS) -> Manifest:
    """Open ``path`` and check that it is a YuriOS backup this build can restore."""
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise BackupError(f"could not read the backup: {exc}") from exc
    if size > limits.archive_bytes:
        raise BackupError(f"that backup is over the {limits.archive_bytes // _GiB} GB "
                          "this node restores")
    try:
        with zipfile.ZipFile(path) as archive:
            return _manifest_from(archive, limits)
    except zipfile.BadZipFile:
        raise BackupError("that is not a YuriOS backup (not a zip file)") from None


def _manifest_from(archive: zipfile.ZipFile, limits: BackupLimits) -> Manifest:
    infos = archive.infolist()
    if len(infos) > limits.entries:
        raise BackupError(f"that backup holds over {limits.entries} entries")
    try:
        info = archive.getinfo(MANIFEST_NAME)
    except KeyError:
        raise BackupError("that is not a YuriOS backup (no manifest.json)") from None
    with archive.open(info) as stream:
        raw = stream.read(limits.manifest_bytes + 1)
    if len(raw) > limits.manifest_bytes:
        raise BackupError("that backup's manifest is too large")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise BackupError("that backup's manifest is not valid JSON") from None
    if not isinstance(data, Mapping) or data.get("format") != FORMAT:
        raise BackupError("that is not a YuriOS character backup")
    version = data.get("version")
    if version != FORMAT_VERSION:
        # Refused, not guessed at: the same rule as the registry (§29.2).
        raise BackupError(f"that backup is format version {version!r}; this build "
                          f"restores version {FORMAT_VERSION}")
    record = data.get("record")
    character = data.get("character")
    if not isinstance(record, Mapping) or not isinstance(character, Mapping):
        raise BackupError("that backup's manifest is missing her record")
    character_id = str(character.get("id") or record.get("id") or "")
    try:
        _validate_id(character_id)
    except ValueError as exc:
        raise BackupError(f"that backup names an invalid character id: {exc}") from None
    bodies: list[BodyRef] = []
    for raw_body in data.get("bodies") or []:
        if not isinstance(raw_body, Mapping):
            raise BackupError("that backup lists a body that is not an object")
        kind, body_id = str(raw_body.get("kind", "")), str(raw_body.get("id", ""))
        if kind not in ("vrm", "live2d") or not _BODY_ID.fullmatch(body_id):
            raise BackupError("that backup lists a body this node cannot name")
        bodies.append(BodyRef(kind, body_id, str(raw_body.get("title") or body_id)[:120]))
    name = str(character.get("name") or "").strip() or character_id
    return Manifest(
        character_id=character_id,
        name=name[:200],
        created_at=str(data.get("created_at") or ""),
        kind=str(data.get("kind") or "backup"),
        record=dict(record),
        bodies=tuple(bodies),
        files=_int(data.get("files")),
        bytes=_int(data.get("bytes")),
    )


def _int(value: object) -> int:
    try:
        return max(0, int(value))                      # type: ignore[call-overload]
    except (TypeError, ValueError):
        return 0


# ---- restoring ---------------------------------------------------------------


@dataclass(slots=True)
class StagedRestore:
    """A restored tree beside its final place, plus the row that will name it.

    Nothing is visible yet. `install` makes it so: one rename, then the registry
    row. `discard` takes everything back out, including any body it added to the
    library.
    """
    record: CharacterRecord
    staging: Path
    manifest: Manifest
    added_bodies: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def install(self, registry: CharacterRegistry) -> None:
        final = self.record.paths.root
        if final.exists():
            raise BackupError(f"character root already exists: {final.name}")
        os.replace(self.staging, final)
        try:
            registry.add(self.record)
        except Exception:
            os.replace(final, self.staging)
            raise

    def discard(self, library: "BodyLibrary | None" = None) -> None:
        shutil.rmtree(self.staging, ignore_errors=True)
        if library is None:
            return
        for kind, body_id in self.added_bodies:
            try:
                library.remove(kind, body_id)
            except (KeyError, ValueError, OSError):
                log.warning("could not take restored body %s/%s back out", kind, body_id)


def stage_restore(
    source: Path,
    registry: CharacterRegistry,
    *,
    character_id: str,
    name: str | None = None,
    trusted: bool,
    library: "BodyLibrary | None" = None,
    limits: BackupLimits = DEFAULT_LIMITS,
    allow_existing: bool = False,
) -> StagedRestore:
    """Unpack ``source`` into a staging folder for ``character_id``. Nothing is visible.

    ``trusted`` is whether this node wrote the file (a snapshot). An uploaded
    backup is a file from outside, so it lands under review exactly as an
    imported card does (§30.3), whatever lifecycle it recorded.

    ``allow_existing`` is for a restore in her place: she is still there while
    the file unpacks, and is archived only once it has. `install` still refuses
    to land on a root that exists.
    """
    _validate_id(character_id)
    characters_dir = registry.data_root / "characters"
    characters_dir.mkdir(parents=True, exist_ok=True)
    final_root = characters_dir / character_id
    if not allow_existing and (registry.get(character_id) is not None or final_root.exists()):
        raise BackupError(f"character already exists: {character_id}")
    try:
        archive = zipfile.ZipFile(source)
    except zipfile.BadZipFile:
        raise BackupError("that is not a YuriOS backup (not a zip file)") from None
    except OSError as exc:
        raise BackupError(f"could not read the backup: {exc}") from exc
    staging = Path(tempfile.mkdtemp(prefix=f".{character_id}.restore.", dir=characters_dir))
    staged: StagedRestore | None = None
    try:
        with archive:
            manifest = _manifest_from(archive, limits)
            _extract_tree(archive, staging, limits)
            try:
                record = archive_model.record_from_snapshot(
                    {"record": manifest.record}, character_id=character_id,
                    dest_root=final_root, data_root=registry.data_root)
            except ValueError as exc:
                raise BackupError(f"that backup's record is not valid: {exc}") from exc
            if name and name.strip():
                record.display.name = name.strip()[:80]
            if not trusted:
                record.lifecycle = LifecycleFlags(enabled=False, autostart=False,
                                                  review_required=True)
            staged = StagedRestore(record=record, staging=staging, manifest=manifest)
            _restore_bodies(archive, staged, library, limits)
        return staged
    except BackupError:
        if staged is not None:
            staged.discard(library)
        shutil.rmtree(staging, ignore_errors=True)
        raise
    except Exception as exc:
        if staged is not None:
            staged.discard(library)
        shutil.rmtree(staging, ignore_errors=True)
        raise BackupError(f"could not restore that backup: {exc}") from exc


def _member_path(name: str, limits: BackupLimits) -> PurePosixPath:
    """The relative path a tree member names, or a refusal. Never trust a name."""
    if len(name) > limits.path_chars or "\\" in name or "\x00" in name:
        raise BackupError("that backup holds an entry with an unsafe name")
    rel = name[len(TREE_PREFIX):].rstrip("/")
    path = PurePosixPath(rel)
    if not rel or path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise BackupError("that backup holds an entry with an unsafe name")
    return path


def _extract_tree(archive: zipfile.ZipFile, staging: Path, limits: BackupLimits) -> None:
    """Every `character/` member into ``staging``, counting bytes as they are read.

    The sizes in the zip's directory are claims. The budget counts what actually
    decompresses, and a member that runs over stops the restore there.
    """
    budget = limits.expanded_bytes
    base = staging.resolve()
    for info in archive.infolist():
        name = info.filename
        if name == MANIFEST_NAME or name.startswith(BODIES_PREFIX):
            continue
        if not name.startswith(TREE_PREFIX):
            raise BackupError(f"that backup holds an entry this format does not: {name[:80]}")
        if name == TREE_PREFIX:
            continue
        if info.flag_bits & 0x1:
            raise BackupError("that backup holds an encrypted entry")
        mode = info.external_attr >> 16
        if mode and stat.S_ISLNK(mode):
            raise BackupError("that backup holds a symbolic link")
        rel = _member_path(name, limits)
        target = staging.joinpath(*rel.parts)
        if not target.resolve().is_relative_to(base):
            raise BackupError("that backup holds an entry with an unsafe name")
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(info) as src, target.open("xb") as dst:
            while chunk := src.read(_CHUNK):
                budget -= len(chunk)
                if budget < 0:
                    raise BackupError(f"that backup expands past the "
                                      f"{limits.expanded_bytes // _GiB} GB this node restores")
                dst.write(chunk)


def _restore_bodies(archive: zipfile.ZipFile, staged: StagedRestore,
                    library: "BodyLibrary | None", limits: BackupLimits) -> None:
    """Put her bodies back on her. A body this library already has (same node, or
    the same upload) is worn as it is. One it lacks is re-added through the
    library's own checks and worn under the id that gives. A body that is refused
    costs her the body, never the restore: she wears the default, and the answer
    says so."""
    record = staged.record
    wanted = {"vrm": record.body.vrm, "live2d": record.body.model}
    for ref in staged.manifest.bodies:
        if wanted.get(ref.kind) != ref.id:
            continue                            # listed, but she no longer wears it
        if library is None:
            continue
        existing = library.get(ref.kind, ref.id)
        if existing is not None and not existing.builtin:
            continue
        try:
            info = archive.getinfo(ref.member)
        except KeyError:
            staged.warnings.append(f"her {ref.kind.upper()} body was not in the backup")
            _wear(record, ref.kind, "")
            continue
        try:
            with archive.open(info) as stream:
                # The library reads with `.read(n)` and nothing else.
                bounded = cast(BinaryIO, _Bounded(stream, limits.expanded_bytes))
                if ref.kind == "vrm":
                    entry = library.add_vrm(bounded, f"{ref.title}.vrm")
                else:
                    entry = library.add_live2d(bounded, f"{ref.title}.zip")
        except (ValueError, OSError) as exc:
            staged.warnings.append(f"her {ref.kind.upper()} body was refused: {exc}")
            _wear(record, ref.kind, "")
            continue
        staged.added_bodies.append((ref.kind, entry.id))
        _wear(record, ref.kind, entry.id)


def _wear(record: CharacterRecord, kind: str, body_id: str) -> None:
    if kind == "vrm":
        record.body.vrm = body_id
    else:
        record.body.model = body_id


class _Bounded:
    """A read-only stream that stops at ``limit`` bytes rather than trust a header."""

    def __init__(self, stream: IO[bytes], limit: int) -> None:
        self._stream = stream
        self._left = limit

    def read(self, size: int = -1) -> bytes:
        chunk = self._stream.read(size if size >= 0 else _CHUNK)
        self._left -= len(chunk)
        if self._left < 0:
            raise ValueError("that body expands past what a backup may hold")
        return chunk


# ---- the snapshot shelf ------------------------------------------------------


def snapshots_root(data_root: Path) -> Path:
    return Path(data_root) / SNAPSHOTS_DIR


def new_snapshot_path(data_root: Path, character_id: str,
                      now: datetime.datetime | None = None) -> Path:
    """Where the next snapshot of ``character_id`` goes. Never an existing file."""
    _validate_id(character_id)
    folder = snapshots_root(data_root) / character_id
    stamp = (now or datetime.datetime.now()).strftime("%Y%m%d-%H%M%S")
    candidate = folder / f"{character_id}-{stamp}.zip"
    n = 2
    while candidate.exists():
        candidate = folder / f"{character_id}-{stamp}-{n}.zip"
        n += 1
    return candidate


def snapshot_path(data_root: Path, character_id: str, name: str) -> Path | None:
    """The snapshot file ``name`` of ``character_id``, or None for anything else."""
    try:
        _validate_id(character_id)
    except ValueError:
        return None
    match = _SNAPSHOT_NAME.fullmatch(name or "")
    if match is None or match.group("id") != character_id:
        return None
    folder = (snapshots_root(data_root) / character_id).resolve()
    path = (folder / name).resolve()
    if path.parent != folder or not path.is_file():
        return None
    return path


def list_snapshots(data_root: Path, character_id: str | None = None) -> list[dict[str, Any]]:
    """Snapshots on this node, newest first. All characters' when no id is given,
    including characters no longer on the board: a purge does not reach this
    shelf, which is what makes it worth having."""
    root = snapshots_root(data_root)
    if not root.is_dir():
        return []
    folders = [root / character_id] if character_id else sorted(root.iterdir())
    rows: list[dict[str, Any]] = []
    for folder in folders:
        if not folder.is_dir():
            continue
        for path in folder.glob("*.zip"):
            match = _SNAPSHOT_NAME.fullmatch(path.name)
            if match is None or match.group("id") != folder.name:
                continue
            try:
                manifest = read_manifest(path)
                summary = manifest.summary()
            except BackupError as exc:
                summary = {"id": folder.name, "name": folder.name, "created_at": "",
                           "error": str(exc)}
            rows.append({
                **summary,
                "id": folder.name,
                "snapshot": path.name,
                "stamp": match.group("stamp"),
                "_order": (match.group("stamp"), int(match.group("n") or 1)),
                "size": path.stat().st_size,
            })
    rows.sort(key=lambda row: row["_order"], reverse=True)
    for row in rows:
        del row["_order"]
    return rows
