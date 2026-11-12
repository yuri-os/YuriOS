"""The house's body library: every VRM and Live2D body a character can wear (SPEC §6.8).

One folder per node, `DATA_DIR/bodies/`, beside the characters rather than inside
any one of them, because a body is a thing you choose *for* a character — two of
them can wear the same one, and taking a body out of the library is not the same
act as editing her:

    bodies/vrm/<id>.vrm          the model, checked by `vrmbody` before it is kept
    bodies/vrm/<id>.json         what the library says about it
    bodies/live2d/<id>/…         a Cubism 3+ rig, unpacked from the zip it came in
    bodies/live2d/<id>.json

Beside what was uploaded, the library lists what ships: the bundled VRM
(`web/models/avatar.vrm`) and every vendor Live2D rig that is actually fetched
(`desktop/avatar_models.py`). Those are `builtin` — listed, wearable, never
removable — so the picker shows one list and a page never has to know which
bodies live where.

A character names her bodies by id (`BodyBinding.vrm`, `BodyBinding.model`); an
id the library does not know resolves to the default, so a removed body or a
card from another house degrades to *a* body, never an empty room.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import secrets
import shutil
import tempfile
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from yurios.characters import vrmbody
from yurios.desktop.avatar_models import DEFAULT as LIVE2D_DEFAULT
from yurios.desktop.avatar_models import MODELS as LIVE2D_VENDOR

KINDS = ("vrm", "live2d")

#: The bundled VRM's id, and the URL the character app serves it at. The `?v`
#: is web/js/main.js's cache-buster for the bundled file — bump both together.
BUNDLED_VRM = "bundled"
BUNDLED_VRM_URL = "/models/avatar.vrm?v=2"

#: A Live2D rig arrives as one zip. These bound what unpacking it may cost: the
#: zip itself, what it says it unpacks to, and how many files that is.
MAX_LIVE2D_ZIP_BYTES = 256 * 1024 * 1024
MAX_LIVE2D_UNPACKED_BYTES = 512 * 1024 * 1024
MAX_LIVE2D_FILES = 4000

_CHUNK = 1024 * 1024
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


@dataclass(frozen=True, slots=True)
class BodyEntry:
    """One body in the library, as the studio lists it and a room loads it."""
    kind: str               # "vrm" | "live2d"
    id: str
    title: str
    detail: str             # "VRM 1.0", "VRM 0.x", "Cubism 3+"
    bytes: int
    builtin: bool
    url: str                # what a room loads: the .vrm, or the rig's model3.json
    added: str = ""
    source: str = ""        # the filename it was uploaded as

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _slug(name: str) -> str:
    stem = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].strip("-")
    return stem or "body"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


class BodyLibrary:
    """The bodies on this node. Every method is blocking file I/O — call it off the loop."""

    def __init__(self, root: Path, *, live2d_vendor_dir: Path, vrm_bundled: Path) -> None:
        self.root = root
        self._vendor_dir = live2d_vendor_dir      # web/live2d — the vendor rigs' base
        self._bundled = vrm_bundled               # web/models/avatar.vrm
        self._bundled_entry: BodyEntry | None = None

    # ---- reading --------------------------------------------------------------

    def entries(self, kind: str) -> list[BodyEntry]:
        """Builtins first (the default leading), then uploads, newest first."""
        if kind == "vrm":
            builtin = [self._bundled_vrm()]
        elif kind == "live2d":
            builtin = self._vendor_rigs()
        else:
            raise ValueError(f"no such kind of body: {kind!r}")
        uploads = [entry for meta in sorted((self.root / kind).glob("*.json"))
                   if (entry := self._read(kind, meta.stem)) is not None]
        uploads.sort(key=lambda e: e.added, reverse=True)
        return builtin + uploads

    def get(self, kind: str, body_id: str) -> BodyEntry | None:
        body_id = (body_id or "").strip().lower()
        if kind == "vrm":
            if body_id in ("", BUNDLED_VRM):
                return self._bundled_vrm()
            return self._read("vrm", body_id) if _ID.match(body_id) else None
        if kind == "live2d":
            for rig in self._vendor_rigs():
                if rig.id == body_id:
                    return rig
            return self._read("live2d", body_id) if _ID.match(body_id) else None
        raise ValueError(f"no such kind of body: {kind!r}")

    def resolve(self, kind: str, body_id: str) -> BodyEntry:
        """What she actually wears: her choice, else the default for that kind."""
        found = self.get(kind, body_id)
        if found is not None:
            return found
        if kind == "vrm":
            return self._bundled_vrm()
        rigs = self._vendor_rigs()
        default = next((r for r in rigs if r.id == LIVE2D_DEFAULT), None)
        if default is not None:
            return default
        uploads = self.entries("live2d")
        if uploads:
            return uploads[0]
        # Nothing fetched and nothing uploaded: the client's own fallback URL.
        return BodyEntry("live2d", LIVE2D_DEFAULT, LIVE2D_DEFAULT.title(), "Cubism 3+", 0,
                         True, "/live2d/" + LIVE2D_VENDOR[LIVE2D_DEFAULT])

    def vrm_file(self, body_id: str) -> Path | None:
        if not _ID.match(body_id or ""):
            return None
        path = self.root / "vrm" / f"{body_id}.vrm"
        return path if path.is_file() else None

    def live2d_file(self, body_id: str, relative: str) -> Path | None:
        """A file inside an uploaded rig, or None for anything outside it."""
        if not _ID.match(body_id or ""):
            return None
        base = (self.root / "live2d" / body_id).resolve()
        path = (base / relative).resolve()
        if not path.is_relative_to(base) or not path.is_file():
            return None
        return path

    # ---- adding -----------------------------------------------------------------

    def add_vrm(self, source: BinaryIO, filename: str) -> BodyEntry:
        """Check and keep a `.vrm`. Raises ValueError with the reason it was refused."""
        folder = self.root / "vrm"
        body_id = self._new_id("vrm", Path(filename).stem)
        info = vrmbody.store(source, folder / f"{body_id}.vrm")
        return self._write("vrm", body_id, {
            "title": info.title or Path(filename).stem, "detail": f"VRM {info.version}",
            "bytes": info.size, "added": _now(), "source": Path(filename).name})

    def adopt_vrm(self, path: Path, title_hint: str = "") -> BodyEntry:
        """File a `.vrm` already on disk into the library, moving it (the migration)."""
        with path.open("rb") as handle:
            entry = self.add_vrm(handle, f"{title_hint or path.stem}.vrm")
        path.unlink(missing_ok=True)
        return entry

    def add_live2d(self, source: BinaryIO, filename: str) -> BodyEntry:
        """Unpack a zipped Cubism 3+ rig. Raises ValueError with the reason it was refused.

        Streamed to a temporary zip, checked entry by entry (no absolute paths, no
        `..`, no links, inside the size and count bounds), and unpacked beside its
        final folder, which it replaces in one rename — a refused zip leaves
        nothing behind.
        """
        folder = self.root / "live2d"
        folder.mkdir(parents=True, exist_ok=True)
        fd, zip_name = tempfile.mkstemp(prefix=".upload-", suffix=".zip", dir=folder)
        staging = Path(tempfile.mkdtemp(prefix=".unpack-", dir=folder))
        try:
            size = 0
            with os.fdopen(fd, "wb") as out:
                while chunk := source.read(_CHUNK):
                    size += len(chunk)
                    if size > MAX_LIVE2D_ZIP_BYTES:
                        raise ValueError(
                            f"that zip is over the {MAX_LIVE2D_ZIP_BYTES // (1024 * 1024)} MB "
                            "a Live2D body may be")
                    out.write(chunk)
            try:
                archive = zipfile.ZipFile(zip_name)
            except zipfile.BadZipFile:
                raise ValueError("that is not a zip — a Live2D body is uploaded as the rig's "
                                 "folder, zipped (the .model3.json, the .moc3 and its "
                                 "textures)") from None
            with archive:
                entry_path, unpacked = _check_rig(archive)
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    target = staging / PurePosixPath(info.filename)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as src, target.open("wb") as dst:
                        shutil.copyfileobj(src, dst, _CHUNK)
            stem = PurePosixPath(entry_path).name.removesuffix(".model3.json")
            body_id = self._new_id("live2d", stem or Path(filename).stem)
            os.replace(staging, folder / body_id)
            return self._write("live2d", body_id, {
                "title": stem or Path(filename).stem, "detail": "Cubism 3+",
                "entry": entry_path, "bytes": unpacked, "added": _now(),
                "source": Path(filename).name})
        finally:
            Path(zip_name).unlink(missing_ok=True)
            shutil.rmtree(staging, ignore_errors=True)

    # ---- removing ----------------------------------------------------------------

    def remove(self, kind: str, body_id: str) -> None:
        entry = self.get(kind, body_id)
        if entry is None:
            raise KeyError(body_id)
        if entry.builtin:
            raise ValueError(f"{entry.title} ships with YuriOS and cannot be removed")
        folder = self.root / kind
        (folder / f"{entry.id}.json").unlink(missing_ok=True)
        if kind == "vrm":
            (folder / f"{entry.id}.vrm").unlink(missing_ok=True)
        else:
            shutil.rmtree(folder / entry.id, ignore_errors=True)

    # ---- internals ----------------------------------------------------------------

    def _new_id(self, kind: str, name: str) -> str:
        reserved = {BUNDLED_VRM, *LIVE2D_VENDOR}
        while True:
            candidate = f"{_slug(name)}-{secrets.token_hex(3)}"
            if candidate not in reserved and not (self.root / kind / f"{candidate}.json").exists():
                return candidate

    def _write(self, kind: str, body_id: str, meta: dict[str, object]) -> BodyEntry:
        path = self.root / kind / f"{body_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        entry = self._read(kind, body_id)
        assert entry is not None
        return entry

    def _read(self, kind: str, body_id: str) -> BodyEntry | None:
        meta_path = self.root / kind / f"{body_id}.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(meta, dict):
            return None
        if kind == "vrm":
            model = self.root / "vrm" / f"{body_id}.vrm"
            if not model.is_file():
                return None
            # The URL changes with the bytes, so a body replaced under the same
            # id is never answered from yesterday's cache.
            url = f"/api/bodies/vrm/{body_id}.vrm?v={model.stat().st_mtime_ns}"
        else:
            entry = str(meta.get("entry", ""))
            if not entry or self.live2d_file(body_id, entry) is None:
                return None
            url = f"/api/bodies/live2d/{body_id}/files/{entry}"
        return BodyEntry(kind, body_id, str(meta.get("title") or body_id),
                         str(meta.get("detail", "")), int(meta.get("bytes", 0) or 0),
                         False, url, str(meta.get("added", "")), str(meta.get("source", "")))

    def _bundled_vrm(self) -> BodyEntry:
        if self._bundled_entry is not None:
            return self._bundled_entry
        info = vrmbody.read_info(self._bundled)
        # Read once: it ships with the code, so it only changes with a restart.
        self._bundled_entry = BodyEntry(
            "vrm", BUNDLED_VRM, (info.title if info else "") or "Bundled body",
            f"VRM {info.version}" if info else "VRM", info.size if info else 0,
            True, BUNDLED_VRM_URL)
        return self._bundled_entry

    def _vendor_rigs(self) -> list[BodyEntry]:
        rigs = []
        for key, rel in LIVE2D_VENDOR.items():
            if (self._vendor_dir / rel).is_file():
                rigs.append(BodyEntry("live2d", key, key.title(), "Cubism 3+ · ships with YuriOS",
                                      0, True, "/live2d/" + rel))
        rigs.sort(key=lambda r: r.id != LIVE2D_DEFAULT)
        return rigs


def _check_rig(archive: zipfile.ZipFile) -> tuple[str, int]:
    """(the rig's model3.json path inside the zip, bytes unpacked) — or ValueError."""
    infos = archive.infolist()
    if len(infos) > MAX_LIVE2D_FILES:
        raise ValueError(f"that zip holds over {MAX_LIVE2D_FILES} files — is it one rig?")
    unpacked = 0
    names: set[str] = set()
    for info in infos:
        name = info.filename
        posix = PurePosixPath(name)
        if name.startswith(("/", "\\")) or ".." in posix.parts or ":" in name or "\\" in name:
            raise ValueError(f"that zip has an entry that points outside itself: {name}")
        # A symlink's mode lives in the high half of external_attr on unix zips.
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise ValueError(f"that zip has a link in it ({name}); a rig is plain files")
        unpacked += info.file_size
        if unpacked > MAX_LIVE2D_UNPACKED_BYTES:
            raise ValueError(f"that zip unpacks to over "
                             f"{MAX_LIVE2D_UNPACKED_BYTES // (1024 * 1024)} MB")
        if not info.is_dir():
            names.add(str(posix))
    entries = sorted((n for n in names if n.lower().endswith(".model3.json")
                      and not PurePosixPath(n).name.startswith(".")
                      and "__MACOSX" not in PurePosixPath(n).parts),
                     key=lambda n: (len(PurePosixPath(n).parts), n))
    if not entries:
        if any(n.lower().endswith(".model.json") for n in names):
            raise ValueError("that is a Cubism 2 rig (.model.json); the Live2D room plays "
                             "Cubism 3 and later — re-export it as .model3.json")
        if any(n.lower().endswith((".vrm", ".glb")) for n in names):
            raise ValueError("that zip holds a 3D model, not a Live2D rig — upload a .vrm "
                             "on the VRM side")
        raise ValueError("no .model3.json in that zip — a Live2D body is the rig's folder, "
                         "zipped: the .model3.json, the .moc3 and its textures")
    entry = entries[0]
    try:
        model = json.loads(archive.read(entry))
        refs = model["FileReferences"]
        moc = str(refs["Moc"])
        textures = [str(t) for t in refs.get("Textures", [])]
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"{entry} is not a readable Live2D model3.json") from None
    folder = PurePosixPath(entry).parent
    for ref in (moc, *textures):
        wanted = str(folder / PurePosixPath(ref))
        if wanted not in names:
            raise ValueError(f"{entry} names {ref}, and it is not in the zip")
    return entry, unpacked
