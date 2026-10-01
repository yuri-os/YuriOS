"""Real PDFs for tests, built by hand: a text layer pypdf can read, with no
dependency that writes PDFs. One Type1 font, one content stream per page."""
from __future__ import annotations


def _escape(line: str) -> str:
    return line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(pages: list[list[str]], *, title: str | None = None) -> bytes:
    """A PDF whose page N shows `pages[N]`, one string per line."""
    n = len(pages)
    page_ids = [4 + 2 * i for i in range(n)]
    objs: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: (f"<< /Type /Pages /Count {n} /Kids ["
            + " ".join(f"{pid} 0 R" for pid in page_ids) + "] >>").encode(),
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    for pid, lines in zip(page_ids, pages):
        ops = "BT /F1 11 Tf 14 TL 72 760 Td " + " ".join(
            f"({_escape(line)}) Tj T*" for line in lines) + " ET"
        stream = ops.encode("latin-1")
        objs[pid] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                     f"/Resources << /Font << /F1 3 0 R >> >> "
                     f"/Contents {pid + 1} 0 R >>").encode()
        objs[pid + 1] = (f"<< /Length {len(stream)} >>\nstream\n".encode()
                         + stream + b"\nendstream")
    info = None
    if title is not None:
        info = max(objs) + 1
        objs[info] = f"<< /Title ({_escape(title)}) >>".encode("latin-1")
    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for num in sorted(objs):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode() + objs[num] + b"\nendobj\n"
    xref = len(out)
    size = max(objs) + 1
    out += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
    for num in range(1, size):
        out += f"{offsets[num]:010d} 00000 n \n".encode()
    trailer = f"<< /Size {size} /Root 1 0 R"
    if info:
        trailer += f" /Info {info} 0 R"
    out += f"trailer\n{trailer} >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)
