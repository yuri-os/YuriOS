"""Her own VRM body — the file that makes a character a 3D character (SPEC §6.8).

Every VRM room used to wear the one bundled `web/models/avatar.vrm`, so a
character could choose a Live2D rig of her own but never a 3D one. This is the
other half: one `.vrm` per character, kept in her own folder
(`CharacterPaths.body_vrm`) so it travels when she is copied, cloned or
archived, and served back to her rooms by the host.

A `.vrm` is a binary glTF (GLB) whose JSON names a VRM extension, so the check
reads only what it has to — the 12-byte header and the JSON chunk behind it —
and never the megabytes of mesh and texture after them. That is enough to tell
a real VRM from a renamed `.glb`, a picture, or a truncated upload, and it is
cheap enough to run every time a room asks what she is wearing.
"""
from __future__ import annotations

import json
import os
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

#: The ceiling on one upload. VRoid exports are typically 10–40 MB; a high-poly
#: commissioned model with 4K textures can reach ~100.
MAX_VRM_BYTES = 128 * 1024 * 1024

#: The ceiling on the JSON chunk alone — a real one is well under a megabyte.
MAX_JSON_BYTES = 32 * 1024 * 1024

_MAGIC = b"glTF"
_CHUNK_JSON = 0x4E4F534A          # "JSON", little-endian
_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True, slots=True)
class VrmInfo:
    version: str                  # "0.x" (UniVRM's `VRM`) or "1.0" (`VRMC_vrm`)
    title: str                    # the model's own name, from its meta; may be ""
    size: int                     # bytes on disk


def inspect(stream: BinaryIO, size: int) -> VrmInfo:
    """What the VRM in `stream` (of `size` bytes) is; ValueError when it is not one."""
    head = stream.read(20)
    if len(head) < 20 or head[:4] != _MAGIC:
        raise ValueError("that file is not a VRM model (no glTF binary header)")
    version, length, chunk_len, chunk_type = struct.unpack("<IIII", head[4:20])
    if version != 2:
        raise ValueError(f"that is a glTF version {version} file; a VRM is glTF 2")
    if length != size:
        raise ValueError("that VRM file is truncated or padded "
                         f"({size} bytes on disk, {length} in its header)")
    if chunk_type != _CHUNK_JSON or chunk_len > min(MAX_JSON_BYTES, size - 20):
        raise ValueError("that VRM file's JSON chunk is missing or malformed")
    try:
        doc = json.loads(stream.read(chunk_len).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("that VRM file's JSON chunk does not parse") from exc
    extensions = doc.get("extensions") if isinstance(doc, dict) else None
    extensions = extensions if isinstance(extensions, dict) else {}
    if isinstance(extensions.get("VRMC_vrm"), dict):
        meta = extensions["VRMC_vrm"].get("meta")
        return VrmInfo("1.0", _text(meta, "name"), size)
    if isinstance(extensions.get("VRM"), dict):
        meta = extensions["VRM"].get("meta")
        return VrmInfo("0.x", _text(meta, "title"), size)
    raise ValueError("that is a glTF model but not a VRM — export it as .vrm "
                     "(VRoid Studio, or UniVRM in Unity)")


def _text(meta: object, key: str) -> str:
    value = meta.get(key) if isinstance(meta, dict) else None
    return value.strip()[:120] if isinstance(value, str) else ""


def read_info(path: Path) -> VrmInfo | None:
    """The VRM at `path`, or None when there is none (or it no longer reads as one)."""
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            return inspect(stream, size)
    except (OSError, ValueError):
        return None


def store(source: BinaryIO, path: Path) -> VrmInfo:
    """Copy an upload into `path`, checked first, replacing her old body atomically.

    Streamed through a temporary file beside the destination, so a 100 MB model
    is never held in memory and a refused or failed upload leaves the body she
    had untouched. Raises ValueError for a file over the ceiling or not a VRM.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".upload-", suffix=".vrm", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        size = 0
        with os.fdopen(fd, "wb") as out:
            while chunk := source.read(_CHUNK_SIZE):
                size += len(chunk)
                if size > MAX_VRM_BYTES:
                    raise ValueError(f"that VRM is over the {MAX_VRM_BYTES // (1024 * 1024)} MB "
                                     "a body may be")
                out.write(chunk)
        with tmp.open("rb") as check:
            info = inspect(check, size)
        os.replace(tmp, path)
        return info
    finally:
        tmp.unlink(missing_ok=True)
