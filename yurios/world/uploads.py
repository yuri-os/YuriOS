"""What you sent her — the store behind a picture in the composer (SPEC §35).

Her camera writes into `selfie_dir`; this is the other direction, and it is a
separate shelf for a plain reason: one holds pictures *of* her that the studio
lists, exports and puts on cards, and the other holds a photo of your bike that
belongs to one sentence in one conversation. Mixing them would put your kitchen
in her gallery.

One picture takes two trips through here. `save()` accepts the bytes once, over
`POST /api/uploads`, and hands back an id; the turn that follows names that id
(`POST /api/chat`, or the voice socket's `text` frame) and `data_url()` turns it
into the base64 part the model actually reads. The upload is separate from the
turn on purpose — a 3 MB photo does not belong inside a JSON body on the voice
socket, and the split means the picture is already on disk, already checked and
already shrunk before anything asks a model to look at it.

Nothing that arrives is stored as it arrived. Every image is decoded, oriented
by its EXIF and re-encoded, which caps the long side to something a context
window can afford, drops the metadata (a holiday photo carries where you took
it), and means the bytes on disk and on the wire were written by Pillow here
rather than by whatever produced the file. The shelf is capped too: the newest
`upload_keep` survive a save and the rest are deleted, because a chat
attachment is not an archive and nobody is coming back to prune it by hand.
"""
from __future__ import annotations

import base64
import io
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

log = logging.getLogger("world.uploads")

#: Formats accepted at the door. Everything is re-encoded to PNG or JPEG from
#: here, so this list is only about what Pillow may be asked to decode — the
#: model is never handed a webp and left to have an opinion about it.
ACCEPTED = ("PNG", "JPEG", "WEBP", "GIF", "BMP")

#: A ceiling read from the *header*, before a single pixel is decoded: a 40 KB
#: PNG can declare 60000×60000 and cost gigabytes to load (Pillow's own
#: decompression-bomb guard sits far higher than a chat attachment needs).
MAX_SOURCE_PIXELS = 50_000_000

_SUFFIX = {"PNG": ".png", "JPEG": ".jpg"}
_MEDIA = {"PNG": "image/png", "JPEG": "image/jpeg"}


class UploadRejected(ValueError):
    """The bytes are not an image this can hand to a model. Carries the sentence
    the composer shows — every refusal here is one a person caused and can fix."""


@dataclass(frozen=True, slots=True)
class Attachment:
    id: str                 # the file name, which is the whole identity
    path: Path
    media_type: str         # "image/png" | "image/jpeg"
    width: int
    height: int
    bytes: int

    @property
    def url(self) -> str:
        """Where the chat renders it from. Under `/api/` so the host's per-
        character dispatcher scopes it for free (world/host.py) — the same
        reason every other runtime route lives there."""
        return f"/api/uploads/{self.id}"


class Uploads:
    """The `upload_dir` shelf. Cheap to construct: the directory is created on
    the first save, so a house whose model cannot see never grows one."""

    def __init__(self, directory: Path, *, max_px: int = 1024, keep: int = 200):
        self.dir = Path(directory)
        self.max_px = max(64, int(max_px))
        self.keep = max(1, int(keep))

    # ---- in ----------------------------------------------------------------
    def save(self, data: bytes) -> Attachment:
        """Decode, orient, shrink, re-encode, write. Raises `UploadRejected`
        with a sentence for the person who chose the file."""
        image, source_format = self._decode(data)
        with image:
            # EXIF first: a phone photo is stored sideways with a tag saying so,
            # and a model reads the pixels, not the tag. `exif_transpose` is
            # also the last thing that reads the metadata — everything from here
            # is a freshly built image with none.
            oriented = ImageOps.exif_transpose(image) or image
            has_alpha = (oriented.mode in ("RGBA", "LA")
                         or "transparency" in oriented.info)
            out_format = "PNG" if has_alpha else "JPEG"
            oriented = oriented.convert("RGBA" if has_alpha else "RGB")
            # `thumbnail` is a no-op when it already fits, so a screenshot that
            # is small enough is never re-sampled — only re-encoded.
            oriented.thumbnail((self.max_px, self.max_px),
                               Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            if out_format == "JPEG":
                oriented.save(buffer, format="JPEG", quality=88, optimize=True)
            else:
                oriented.save(buffer, format="PNG", optimize=True)
            width, height = oriented.size
        payload = buffer.getvalue()

        self.dir.mkdir(parents=True, exist_ok=True)
        name = f"{uuid.uuid4().hex[:16]}{_SUFFIX[out_format]}"
        (self.dir / name).write_bytes(payload)
        self._prune()
        log.info("upload: %s %d×%d (%s in, %s out, %d KB)", name, width, height,
                 source_format, out_format, len(payload) // 1024)
        return Attachment(id=name, path=self.dir / name,
                          media_type=_MEDIA[out_format],
                          width=width, height=height, bytes=len(payload))

    def _decode(self, data: bytes) -> tuple[Image.Image, str]:
        if not data:
            raise UploadRejected("that file is empty")
        try:
            image = Image.open(io.BytesIO(data))
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise UploadRejected("that doesn't look like an image") from exc
        # `open` has read the header and nothing else, so both checks below are
        # still ahead of the expensive part.
        if image.format not in ACCEPTED:
            image.close()
            raise UploadRejected(
                f"{image.format or 'that format'} isn't one she can be sent "
                f"({', '.join(ACCEPTED)})")
        if image.width * image.height > MAX_SOURCE_PIXELS:
            image.close()
            raise UploadRejected("that image is too large to open")
        return image, image.format or ""

    def _prune(self) -> None:
        """Keep the newest `keep`. Best-effort: a file that vanished under us
        (two saves racing, someone tidying the folder) is not a failed upload."""
        try:
            files = sorted(self.dir.iterdir(), key=lambda p: p.stat().st_mtime)
        except OSError:
            return
        for path in files[:-self.keep]:
            try:
                path.unlink()
            except OSError:
                pass

    # ---- out ---------------------------------------------------------------
    def get(self, name: str) -> Attachment | None:
        """One saved picture by id, or None. The name is pinned to the flat
        directory — `..` and absolute paths resolve outside it and are refused
        by the same comparison the selfie route uses."""
        if not name or "/" in name or "\\" in name:
            return None
        base = self.dir.resolve()
        path = (base / name).resolve()
        if path.parent != base or not path.is_file():
            return None
        media = _MEDIA["PNG"] if path.suffix == ".png" else _MEDIA["JPEG"]
        try:
            with Image.open(path) as image:
                size = image.size
            return Attachment(id=path.name, path=path, media_type=media,
                              width=size[0], height=size[1],
                              bytes=path.stat().st_size)
        except (UnidentifiedImageError, OSError, ValueError):
            return None

    def data_url(self, attachment: Attachment) -> str:
        """The `image_url` part a chat model reads: the bytes inline, base64.

        Inline rather than a link on purpose — the model is often a local server
        with no route back to this one, and a hosted one fetching a URL off your
        laptop is not a thing that can work. The size cap in `save()` is what
        keeps this affordable."""
        raw = attachment.path.read_bytes()
        return (f"data:{attachment.media_type};base64,"
                f"{base64.b64encode(raw).decode('ascii')}")
