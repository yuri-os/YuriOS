"""Card-shaped test fixtures, shared by the importer, exporter and studio suites.

Promoted out of `test_characters_importer.py` once the export side needed the
same helpers. `st_reader` is deliberately *not* the repo's strict parser: it is a
sketch of what a real client does — walk chunks, take `tEXt`, prefer `ccv3`,
base64 → JSON — so the round-trip tests check the thing that actually has to work
at the far end rather than checking our own parser against itself.
"""
from __future__ import annotations

import base64
import io
import json
import struct
import zlib
from typing import Any

from PIL import Image


def chunk(kind: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)


def png_bytes(size: tuple[int, int] = (5, 4), colour=(20, 40, 60, 100),
              mode: str = "RGBA") -> bytes:
    output = io.BytesIO()
    Image.new(mode, size, colour).save(output, "PNG")
    return output.getvalue()


def png_card(card: dict[str, Any], *, keyword: str = "ccv3",
             size: tuple[int, int] = (5, 4)) -> bytes:
    """A PNG with `card` embedded the way SillyTavern writes one."""
    png = png_bytes(size)
    iend = png.rfind(b"\x00\x00\x00\x00IEND")
    payload = keyword.encode("latin-1") + b"\x00" + base64.b64encode(
        json.dumps(card, ensure_ascii=False).encode("utf-8"))
    return png[:iend] + chunk(b"tEXt", payload) + png[iend:]


def card_data(**overrides: Any) -> dict[str, Any]:
    """A complete, well-formed V3 card body. Override any field."""
    data: dict[str, Any] = {
        "name": "Card Person",
        "creator": "Offline Test",
        "character_version": "3.2.1",
        "description": "A complete identity from the card.",
        "personality": "dry, observant, kind",
        "scenario": "A rainlit library.",
        "first_mes": "You made it.",
        "alternate_greetings": ["Back again?", "Good morning."],
        "mes_example": "<START>\n{{user}}: Hello\n{{char}}: Hello yourself, {{user}}.",
        "system_prompt": "Speak plainly.",
        "post_history_instructions": "Never narrate for {{user}}.",
        "creator_notes": "Imported without flattening unknown fields.",
        "tags": ["original", "test"],
        "character_book": {
            "entries": [
                {"name": "Library", "keys": ["book", "library"],
                 "content": "It never closes."},
            ]
        },
    }
    data.update(overrides)
    return data


def wrapper(data: dict[str, Any] | None = None, *, native: bool = False,
            spec: str = "v3") -> dict[str, Any]:
    body = card_data() if data is None else data
    if native:
        extensions = dict(body.get("extensions") or {})
        extensions.setdefault("yurios", {"schema_version": 1})
        body = {**body, "extensions": extensions}
    if spec == "v3":
        return {"spec": "chara_card_v3", "spec_version": "3.0", "data": body}
    return {"spec": "chara_card_v2", "spec_version": "2.0", "data": body}


def st_reader(png: bytes) -> dict[str, dict[str, Any]]:
    """Read a card the permissive way a client does. Raises on anything unusable."""
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("not a PNG")
    found: dict[str, dict[str, Any]] = {}
    offset = 8
    while offset + 8 <= len(png):
        (length,) = struct.unpack(">I", png[offset:offset + 4])
        kind = png[offset + 4:offset + 8]
        body = png[offset + 8:offset + 8 + length]
        stored = struct.unpack(">I", png[offset + 8 + length:offset + 12 + length])[0]
        offset += 12 + length
        if kind == b"tEXt":
            if zlib.crc32(kind + body) & 0xFFFFFFFF != stored:
                raise ValueError("tEXt CRC mismatch — a client would reject this")
            keyword, _, text = body.partition(b"\x00")
            try:
                parsed = json.loads(base64.b64decode(text))
            except (ValueError, TypeError):
                continue
            if isinstance(parsed, dict):
                found[keyword.decode("latin-1")] = parsed
        elif kind == b"IEND":
            break
    if not found:
        raise ValueError("no character data — a client would say the card is empty")
    return found


def preferred(png: bytes) -> dict[str, Any]:
    """The `data` a V3-aware client would use: `ccv3` first, `chara` after."""
    chunks = st_reader(png)
    card = chunks.get("ccv3") or chunks["chara"]
    return card.get("data", card)
