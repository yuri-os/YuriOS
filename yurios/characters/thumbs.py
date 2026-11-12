"""Small copies of her selfies, for pickers that show many at once (SPEC §28.1).

A selfie is a full-resolution PNG — two or three megabytes each — and the
studio's Face page shows a page of them at a time. The page asks for these
instead: a WebP a few kilobytes big, made on first ask and kept beside her
folder's other derived files (`<root>/cache/thumbs/`), remade whenever the
selfie it came from is newer. Nothing here is hers in any sense that matters —
deleting the folder only costs the next page a moment.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

#: The box a thumbnail fits inside — a portrait-shaped tile, at twice the
#: size the picker draws it, so it stays sharp on a high-density screen.
BOX = (240, 360)
QUALITY = 80


def thumbnail(source: Path, cache_dir: Path) -> Path:
    """The cached thumbnail of `source`, made (or remade) if it is missing or stale.

    Blocking — Pillow decode and encode — so call it off the event loop. Raises
    OSError for a source Pillow cannot read.
    """
    target = cache_dir / f"{source.stem}.webp"
    try:
        if target.stat().st_mtime_ns >= source.stat().st_mtime_ns:
            return target
    except FileNotFoundError:
        pass
    from PIL import Image                      # lazy: the host imports this module

    cache_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as opened:
        opened.thumbnail(BOX, Image.Resampling.LANCZOS)
        small = opened if opened.mode in ("RGB", "RGBA") else opened.convert("RGBA")
        fd, tmp_name = tempfile.mkstemp(prefix=".thumb-", suffix=".webp", dir=cache_dir)
        os.close(fd)
        try:
            small.save(tmp_name, "WEBP", quality=QUALITY, method=4)
            os.replace(tmp_name, target)
        finally:
            Path(tmp_name).unlink(missing_ok=True)
    return target
