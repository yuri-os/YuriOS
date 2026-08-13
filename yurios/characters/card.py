"""Strict parser for PNG-embedded SillyTavern character cards."""

from __future__ import annotations

import base64
import binascii
import json
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_KNOWN_CRITICAL = {b"IHDR", b"PLTE", b"IDAT", b"IEND"}
_BIT_DEPTHS = {
    0: {1, 2, 4, 8, 16},
    2: {8, 16},
    3: {1, 2, 4, 8},
    4: {8, 16},
    6: {8, 16},
}


class CardParseError(ValueError):
    """The input is not a bounded, structurally valid PNG character card."""


@dataclass(frozen=True, slots=True)
class CardLimits:
    max_file_bytes: int = 32 * 1024 * 1024
    max_chunk_bytes: int = 16 * 1024 * 1024
    # High enough to admit an honest card, low enough to bound the parse loop.
    # A real 20 MB portrait off a card site arrives as ~5,000 small IDATs — the
    # encoder's choice, not a signal of anything — so a tight ceiling here
    # rejects perfectly good cards for how their PNG was written. The bound that
    # actually matters is `max_file_bytes`; this one only stops a pathological
    # file of empty chunks from spinning the loop.
    max_chunks: int = 16384
    # A card edited in place can end up carrying several `chara` payloads — the
    # editor appends a new tEXt chunk and leaves the old one behind. The first is
    # the live one; the rest are history. We read the first and say so (§30.1),
    # but we will not sift an unbounded pile of them.
    max_card_chunks: int = 8
    max_metadata_bytes: int = 4 * 1024 * 1024
    max_width: int = 8192
    max_height: int = 8192
    max_pixels: int = 16_777_216

    def __post_init__(self) -> None:
        if any(value <= 0 for value in (
            self.max_file_bytes,
            self.max_chunk_bytes,
            self.max_chunks,
            self.max_card_chunks,
            self.max_metadata_bytes,
            self.max_width,
            self.max_height,
            self.max_pixels,
        )):
            raise ValueError("all card parser limits must be positive")


@dataclass(frozen=True, slots=True)
class ParsedCard:
    data: dict[str, Any]
    keyword: str
    width: int
    height: int
    #: Things about the file the importer must not swallow — currently only
    #: "this PNG held more than one card payload and here is the one I read".
    warnings: tuple[str, ...] = ()

    @property
    def full_data(self) -> dict[str, Any]:
        return self.data


@dataclass(frozen=True, slots=True)
class ImageHeader:
    format: str
    width: int
    height: int


_JPEG_SOF = {
    0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
    0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
}


def preflight_image(
    source: bytes | bytearray | memoryview,
    *,
    limits: CardLimits | None = None,
    formats: tuple[str, ...] = ("PNG", "JPEG"),
) -> ImageHeader:
    """Read image headers and enforce dimensions before Pillow is involved."""
    limits = limits or CardLimits()
    data = memoryview(source)
    if len(data) > limits.max_file_bytes:
        raise CardParseError("image exceeds file size limit")

    image_format = ""
    width = height = 0
    if len(data) >= 24 and data[:8] == PNG_SIGNATURE:
        if bytes(data[12:16]) != b"IHDR" or struct.unpack(">I", data[8:12])[0] != 13:
            raise CardParseError("invalid PNG header")
        width, height = struct.unpack(">II", data[16:24])
        image_format = "PNG"
    elif len(data) >= 4 and bytes(data[:2]) == b"\xff\xd8":
        image_format = "JPEG"
        offset = 2
        while offset < len(data):
            if data[offset] != 0xFF:
                raise CardParseError("invalid JPEG marker stream")
            while offset < len(data) and data[offset] == 0xFF:
                offset += 1
            if offset >= len(data):
                break
            marker = data[offset]
            offset += 1
            if marker == 0x01 or 0xD0 <= marker <= 0xD8:
                continue
            if marker in (0xD9, 0xDA) or offset + 2 > len(data):
                break
            length = struct.unpack(">H", data[offset:offset + 2])[0]
            if length < 2 or offset + length > len(data):
                raise CardParseError("truncated JPEG segment")
            if marker in _JPEG_SOF:
                if length < 7:
                    raise CardParseError("invalid JPEG dimensions")
                height, width = struct.unpack(">HH", data[offset + 3:offset + 7])
                break
            offset += length

    if image_format not in formats:
        raise CardParseError(f"image must be {' or '.join(formats)}")
    if not width or not height:
        raise CardParseError(f"invalid {image_format} dimensions")
    if (width > limits.max_width or height > limits.max_height or
            width * height > limits.max_pixels):
        raise CardParseError("image dimensions exceed limits")
    return ImageHeader(image_format, width, height)


def _json_object(raw: bytes) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CardParseError("card JSON is not UTF-8") from exc

    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise CardParseError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def constant(value: str) -> None:
        raise CardParseError(f"invalid JSON number: {value}")

    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except CardParseError:
        raise
    except json.JSONDecodeError as exc:
        raise CardParseError(f"invalid card JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise CardParseError("card JSON must be an object")
    if "data" in value and not isinstance(value["data"], dict):
        raise CardParseError("card JSON 'data' must be an object")
    return value


def _decode_card(encoded: bytes, limits: CardLimits) -> dict[str, Any]:
    if not encoded:
        raise CardParseError("card metadata is empty")
    max_encoded = ((limits.max_metadata_bytes + 2) // 3) * 4
    if len(encoded) > max_encoded:
        raise CardParseError("card metadata exceeds decoded size limit")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CardParseError("card metadata is not valid base64") from exc
    if len(decoded) > limits.max_metadata_bytes:
        raise CardParseError("card metadata exceeds decoded size limit")
    return _json_object(decoded)


def parse_png_card(
    source: str | Path | bytes | bytearray | memoryview,
    *,
    limits: CardLimits | None = None,
) -> ParsedCard:
    """Parse a card, preferring ``ccv3`` and never falling back from an invalid V3."""
    limits = limits or CardLimits()
    if isinstance(source, (str, Path)):
        path = Path(source)
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise CardParseError(f"cannot read card: {exc}") from exc
        if size > limits.max_file_bytes:
            raise CardParseError("PNG exceeds file size limit")
        try:
            png = path.read_bytes()
        except OSError as exc:
            raise CardParseError(f"cannot read card: {exc}") from exc
    else:
        png = bytes(source)
    if len(png) > limits.max_file_bytes:
        raise CardParseError("PNG exceeds file size limit")
    if not png.startswith(PNG_SIGNATURE):
        raise CardParseError("invalid PNG signature")

    offset = len(PNG_SIGNATURE)
    chunks = 0
    width = height = 0
    seen_ihdr = seen_plte = seen_idat = seen_iend = False
    idat_finished = False
    metadata: dict[str, bytes] = {}
    copies: dict[str, int] = {}
    superseded: dict[str, int] = {}

    while offset < len(png):
        chunks += 1
        if chunks > limits.max_chunks:
            raise CardParseError("PNG exceeds chunk count limit")
        if len(png) - offset < 12:
            raise CardParseError("truncated PNG chunk")
        length = struct.unpack(">I", png[offset:offset + 4])[0]
        chunk_type = png[offset + 4:offset + 8]
        if length > limits.max_chunk_bytes:
            raise CardParseError("PNG chunk exceeds size limit")
        end = offset + 12 + length
        if end > len(png):
            raise CardParseError("truncated PNG chunk data")
        if not all(65 <= byte <= 90 or 97 <= byte <= 122 for byte in chunk_type):
            raise CardParseError("invalid PNG chunk type")
        if 97 <= chunk_type[2] <= 122:
            raise CardParseError("invalid PNG reserved chunk bit")
        data = png[offset + 8:offset + 8 + length]
        stored_crc = struct.unpack(">I", png[offset + 8 + length:end])[0]
        actual_crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
        if stored_crc != actual_crc:
            name = chunk_type.decode("ascii", errors="replace")
            raise CardParseError(f"CRC mismatch in {name} chunk")

        if chunks == 1 and chunk_type != b"IHDR":
            raise CardParseError("IHDR must be the first PNG chunk")
        if chunk_type not in _KNOWN_CRITICAL and 65 <= chunk_type[0] <= 90:
            raise CardParseError("unknown critical PNG chunk")
        if seen_iend:
            raise CardParseError("PNG data follows IEND")

        if chunk_type == b"IHDR":
            if seen_ihdr or length != 13:
                raise CardParseError("invalid or duplicate IHDR")
            seen_ihdr = True
            width, height, depth, color, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", data
            )
            if (
                not width
                or not height
                or width > limits.max_width
                or height > limits.max_height
                or width * height > limits.max_pixels
            ):
                raise CardParseError("PNG dimensions exceed limits")
            if color not in _BIT_DEPTHS or depth not in _BIT_DEPTHS[color]:
                raise CardParseError("invalid PNG color type/bit depth")
            if compression != 0 or filtering != 0 or interlace not in (0, 1):
                raise CardParseError("unsupported PNG encoding parameters")
        elif chunk_type == b"PLTE":
            if seen_plte or seen_idat or length == 0 or length > 768 or length % 3:
                raise CardParseError("invalid PLTE chunk")
            if color == 3 and length // 3 > 2**depth:
                raise CardParseError("PLTE has too many entries for indexed bit depth")
            seen_plte = True
        elif chunk_type == b"IDAT":
            if idat_finished:
                raise CardParseError("IDAT chunks must be consecutive")
            seen_idat = True
        elif seen_idat:
            idat_finished = True

        if chunk_type == b"tEXt":
            if b"\x00" not in data:
                raise CardParseError("malformed tEXt chunk")
            keyword, text = data.split(b"\x00", 1)
            if not 1 <= len(keyword) <= 79:
                raise CardParseError("invalid PNG text keyword")
            try:
                key = keyword.decode("latin-1")
            except UnicodeDecodeError as exc:
                raise CardParseError("invalid PNG text keyword") from exc
            if key in ("ccv3", "chara"):
                copies[key] = copies.get(key, 0) + 1
                if copies[key] > limits.max_card_chunks:
                    raise CardParseError(
                        f"PNG carries more than {limits.max_card_chunks} "
                        f"{key} card payloads")
                if key not in metadata:
                    metadata[key] = text
                elif text != metadata[key]:   # a byte-identical copy says nothing
                    superseded[key] = superseded.get(key, 0) + 1
        elif chunk_type == b"IEND":
            if length or not seen_idat:
                raise CardParseError("invalid IEND chunk")
            seen_iend = True
            offset = end
            break

        offset = end

    if not seen_ihdr or not seen_idat or not seen_iend:
        raise CardParseError("incomplete PNG")
    if offset != len(png):
        raise CardParseError("PNG data follows IEND")
    keyword = "ccv3" if "ccv3" in metadata else "chara" if "chara" in metadata else ""
    if not keyword:
        raise CardParseError("PNG contains no ccv3 or chara card metadata")
    stale = superseded.get(keyword, 0)
    return ParsedCard(
        data=_decode_card(metadata[keyword], limits),
        keyword=keyword,
        width=width,
        height=height,
        warnings=((
            f"this PNG carried more than one {keyword} payload. Read the first "
            f"one in the file and ignored {stale} later "
            f"{'one' if stale == 1 else 'ones'} that disagree with it — check "
            "the imported fields against the card you expected.",
        ) if stale else ()),
    )


class PNGCardParser:
    def __init__(self, limits: CardLimits | None = None):
        self.limits = limits or CardLimits()

    def parse(self, source: str | Path | bytes | bytearray | memoryview) -> ParsedCard:
        return parse_png_card(source, limits=self.limits)


def card_fields(card: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the field-bearing object without copying or discarding card data."""
    data = card.get("data")
    return data if isinstance(data, Mapping) else card
