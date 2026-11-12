"""The scripts every page loads by an unhashed name, stamped with their content (SPEC §32.1).

`/shared/settings.js`, `/shared/runtime.js` and their neighbours are served raw so
the Vite pages and the raw Live2D client run the same file. A raw name survives
every edit, and a browser that cached one without a `Cache-Control` header keeps
serving it on its own heuristic, for days, with no way for the server to reach
it. `no-cache` stops that for every copy fetched from now on; it cannot recall a
copy already held. A new URL can: each page is revalidated on every load, so
rewriting its `/shared/…` and `/js/…` references to `?v=<hash of the file>` means
an edit is a different address, and the stale copy is left at one nothing asks
for. Nobody has to know to hard-reload.
"""
from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

from fastapi.responses import Response

#: `src="/shared/settings.js"` / `href="/shared/settings.css"` — a reference that
#: already carries a query or a fragment is left as it was written
_RAW_REF = re.compile(r'((?:src|href)=")(/(?:shared|js)/[^"?#]+)(")')

# path → (mtime_ns, size, stamp): hashing on every page load would read every
# referenced file each time, and the stat says when the hash is stale
_stamps: dict[Path, tuple[int, int, str]] = {}


def _stamp(file: Path) -> str:
    try:
        stat = file.stat()
    except OSError:
        return ""
    known = _stamps.get(file)
    if known and known[:2] == (stat.st_mtime_ns, stat.st_size):
        return known[2]
    stamp = hashlib.sha256(file.read_bytes()).hexdigest()[:12]
    _stamps[file] = (stat.st_mtime_ns, stat.st_size, stamp)
    return stamp


def stamp_refs(html: str, web_dir: Path) -> str:
    """`html` with every raw `/shared/` and `/js/` reference stamped `?v=<hash>`.
    A reference to a file that is not there is left alone: it 404s either way."""
    def stamped(match: re.Match[str]) -> str:
        stamp = _stamp(web_dir / match.group(2).lstrip("/"))
        if not stamp:
            return match.group(0)
        return f"{match.group(1)}{match.group(2)}?v={stamp}{match.group(3)}"
    return _RAW_REF.sub(stamped, html)


def page(path: Path, web_dir: Path, *, frame_self: bool = False) -> Response:
    """One HTML page, its raw references stamped, revalidated on every load.

    `frame_self` lets this origin's own pages frame it — the Live2D body
    preview, which the studio shows through an iframe so the studio's own
    policy never needs Pixi's `unsafe-eval` (SPEC §6.8). Every other page
    refuses to be framed at all."""
    html = stamp_refs(path.read_text(encoding="utf-8"), web_dir)
    # SPEC §11.6: only the inline bootstraps in this local page may execute.
    inline = re.findall(r"<script\b[^>]*>(.*?)</script\s*>", html, re.I | re.S)
    hashes = sorted({"'sha256-" + base64.b64encode(hashlib.sha256(
        code.encode("utf-8")).digest()).decode("ascii") + "'" for code in inline if code.strip()})
    scripts = "script-src 'self' 'wasm-unsafe-eval' " + " ".join(hashes)
    if "live2d" in path.parts:
        scripts += " 'unsafe-eval'"       # Pixi 6 shader generation
    policy = "; ".join([
        "default-src 'self'", scripts, "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob: https:", "font-src 'self' data:",
        "connect-src 'self' blob: data: ws: wss: https:", "media-src 'self' data: blob:",
        "worker-src 'self' blob:", "object-src 'none'", "base-uri 'self'",
        "frame-ancestors 'self'" if frame_self else "frame-ancestors 'none'",
        "form-action 'self'",
    ])
    return Response(html, media_type="text/html", headers={
        "Cache-Control": "no-cache", "Content-Security-Policy": policy,
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "SAMEORIGIN" if frame_self else "DENY"})
