"""A PDF, as the text her shelf reads (SPEC §20.1).

The shelf is text on purpose: a chunk's span is a character range into the
file, the files tab shows the file, and the index is rebuildable from the files
alone. So a PDF is converted once, when it is handed over, into a `.md` that is
all of those things — and the PDF itself is not kept.

A PDF has no paragraphs, only lines placed on a page, and what `pypdf` hands
back is those lines joined by single newlines. The chunker splits on blank
lines, so unconverted, every page would become one chunk two or three times
the budget. `_reflow` puts the paragraphs back: a line that stops well short
of the page's usual width, on a sentence end, is the last line of one. It also
rejoins words hyphenated across a line, and marks each page so a passage can
be cited by page.

`pypdf` is imported lazily, so an install that predates it still runs; a PDF
is then refused with the reason rather than failing the request.
"""
from __future__ import annotations

import io
import logging
import re

log = logging.getLogger("mind.pdftext")

#: Characters that can end a paragraph's last line.
_ENDS = ".!?:;\"')]”’»"
#: A line this much shorter than the page's typical width, on one of _ENDS, ends
#: a paragraph. Below 1 so a justified line that happens to end a sentence
#: mid-paragraph does not split it.
_SHORT = 0.85


class PdfUnreadable(ValueError):
    """Why this PDF yields no text — the sentence the person is shown."""


def pdf_text(data: bytes, *, title: str = "") -> str:
    """The text of a PDF, reflowed into paragraphs, one `[page N]` per page."""
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError:
        raise PdfUnreadable("PDF support isn't installed here — run "
                            "`pip install pypdf`, or convert it to text first") from None
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise PdfUnreadable("it's password-protected")
        pages = [page.extract_text() or "" for page in reader.pages]
    except PdfUnreadable:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError, IndexError,
            NotImplementedError) as e:
        # A mangled file, or an encryption pypdf needs `cryptography` to undo.
        log.info("couldn't read a PDF: %s", e)
        raise PdfUnreadable("it isn't a PDF that can be read") from None
    if not any(p.strip() for p in pages):
        raise PdfUnreadable("it has no text layer (a scan?) — it needs OCR first")
    meta = getattr(reader, "metadata", None)
    heading = (getattr(meta, "title", None) or "").strip() or title
    out = [f"# {heading}"] if heading else []
    for n, page in enumerate(pages, 1):
        body = _reflow(page)
        if body:
            out.append(f"[page {n}]\n\n{body}")
    return "\n\n".join(out) + "\n"


def _reflow(page: str) -> str:
    lines = [re.sub(r"\s+", " ", line).strip() for line in page.splitlines()]
    widths = sorted(len(line) for line in lines if line)
    if not widths:
        return ""
    usual = widths[len(widths) * 3 // 4]
    paras: list[list[str]] = []
    buf: list[str] = []
    for line in lines:
        if not line:
            if buf:
                paras.append(buf)
                buf = []
            continue
        if buf and buf[-1].endswith("-") and line[:1].islower():
            buf[-1] = buf[-1][:-1] + line       # exam-/ple → example
        else:
            buf.append(line)
        if len(line) < usual * _SHORT and line[-1] in _ENDS:
            paras.append(buf)
            buf = []
    if buf:
        paras.append(buf)
    return "\n\n".join(" ".join(p) for p in paras)
