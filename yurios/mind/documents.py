"""A file a person hands her, as the text she keeps (SPEC §20.1, §34.6).

Two places take one. Her shelf (`knowledge/reference/`) is what conversation
searches; her desk's inbox (`workspace/inbox/`) is what she reads on purpose and
decides about. They differ in what happens next, not in what a document *is*,
so the rules for turning an upload into text live here once: the suffixes she
reads, the size limits, the name it is filed under, and a PDF converted to the
`.md` of its text (`pdftext`) — said out loud as a refusal with a reason, where
a folder copied into would just ignore the file.

The sentences leave out the file's name: the client that sent it knows which
one it was, and prefixes it.
"""
from __future__ import annotations

import re

from .knowledge import SOURCE_PREFIX, SUFFIXES, TOPIC_PREFIX
from .pdftext import PdfUnreadable, pdf_text

#: The largest text document a person can hand her. Past `LONG_DOC_CHARS` the
#: shelf reads a document for notes, so a big one costs sections rather than
#: size; this bounds what one request holds. Her desk has its own, smaller
#: per-file limit (`workspace.MAX_FILE_BYTES`), which it enforces itself.
MAX_TEXT_BYTES = 5_000_000
#: A PDF is mostly fonts, images and layout; what is kept is the text in it,
#: which is a small fraction of the file.
MAX_PDF_BYTES = 50_000_000
#: What an upload route reads before deciding which of the two limits applies.
MAX_UPLOAD_BYTES = max(MAX_TEXT_BYTES, MAX_PDF_BYTES)

#: Names on the shelf that belong to her own reading: a `web-` source is
#: archived and never retrieved, and a `research-` page is overwritten the next
#: time a job of that name runs. The desk has no such names.
SHELF_RESERVED = (SOURCE_PREFIX, TOPIC_PREFIX)


class DocumentRefused(ValueError):
    """A document a person handed over that won't be taken, and why.

    `status` is the HTTP answer it maps to, so the route and the CLI print the
    same sentence."""

    def __init__(self, message: str, status: int = 415):
        super().__init__(message)
        self.status = status


def read_document(filename: str, data: bytes, *,
                  reserved: tuple[str, ...] = ()) -> tuple[str, str]:
    """The name and text a handed file is kept as: `.md`/`.txt` as they are,
    `.pdf` as the `.md` of its text. Never a path — only the basename survives.

    PDF conversion is CPU-bound and takes seconds for a book, so an async caller
    runs this off the loop.
    """
    base = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    stem, dot, suffix = base.rpartition(".")
    suffix = f".{suffix.lower()}" if dot else ""
    pdf = suffix == ".pdf"
    if not pdf and suffix not in SUFFIXES:
        raise DocumentRefused("she reads .md, .txt and .pdf files — convert it to "
                              "text first")
    kept = ".md" if pdf else suffix
    stem = re.sub(r"[^\w.-]+", "_", stem).strip("._")[:80 - len(kept)]
    if not stem:
        raise DocumentRefused(f"needs a name before the {suffix}", status=400)
    doc = stem + kept
    if reserved and doc.startswith(reserved):
        raise DocumentRefused(f"names starting {' or '.join(reserved)} are kept "
                              "for her own research — rename it", status=409)
    if pdf:
        if len(data) > MAX_PDF_BYTES:
            raise DocumentRefused(f"over the {MAX_PDF_BYTES // 1_000_000} MB limit "
                                  "for a PDF", status=413)
        try:
            return doc, pdf_text(data, title=base[:-len(suffix)].strip())
        except PdfUnreadable as e:
            raise DocumentRefused(f"can't read this PDF: {e}") from None
    if len(data) > MAX_TEXT_BYTES:
        raise DocumentRefused(f"over the {MAX_TEXT_BYTES // 1_000_000} MB limit",
                              status=413)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise DocumentRefused("not UTF-8 text") from None
    if "\x00" in text:
        raise DocumentRefused("not text")
    if not text.strip():
        raise DocumentRefused("empty", status=400)
    return doc, text


def shelf_document(filename: str, data: bytes) -> tuple[str, str]:
    """`read_document`, with the shelf's reserved names refused (SPEC §20.1)."""
    return read_document(filename, data, reserved=SHELF_RESERVED)
