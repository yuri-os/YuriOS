from __future__ import annotations

import base64
import io
import json
import struct
import zlib

import pytest
from PIL import Image

from yurios.characters import CardLimits, CardParseError, parse_png_card


def _chunk(kind: bytes, data: bytes, *, crc: int | None = None) -> bytes:
    checksum = zlib.crc32(kind + data) & 0xFFFFFFFF if crc is None else crc
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", checksum)


def _card_png(*cards: tuple[str, object]) -> bytes:
    output = io.BytesIO()
    Image.new("RGBA", (3, 2), (12, 34, 56, 128)).save(output, "PNG")
    png = output.getvalue()
    iend = png.rfind(b"\x00\x00\x00\x00IEND")
    metadata = b"".join(
        _chunk(
            b"tEXt",
            keyword.encode("ascii")
            + b"\x00"
            + base64.b64encode(json.dumps(value).encode("utf-8")),
        )
        for keyword, value in cards
    )
    return png[:iend] + metadata + png[iend:]


def test_ccv3_is_preferred_and_unknown_card_fields_are_preserved():
    legacy = {"name": "Legacy"}
    v3 = {
        "spec": "chara_card_v3",
        "data": {"name": "V3", "future_field": {"untouched": [1, True]}},
        "unknown_envelope": "kept",
    }

    parsed = parse_png_card(_card_png(("chara", legacy), ("ccv3", v3)))

    assert parsed.keyword == "ccv3"
    assert parsed.data == v3
    assert parsed.width == 3
    assert parsed.height == 2


def test_crc_is_checked_for_every_chunk():
    png = _card_png(("chara", {"name": "CRC"}))
    marker = png.index(b"tEXt")
    length = struct.unpack(">I", png[marker - 4:marker])[0]
    crc_offset = marker + 4 + length
    corrupted = png[:crc_offset] + b"\x00\x00\x00\x00" + png[crc_offset + 4:]

    with pytest.raises(CardParseError, match="CRC mismatch in tEXt"):
        parse_png_card(corrupted)


def test_invalid_preferred_metadata_does_not_fall_back_to_chara():
    png = _card_png(("chara", {"name": "Legacy"}), ("ccv3", {"name": "V3"}))
    marker = png.index(b"ccv3\x00")
    broken = bytearray(png)
    broken[marker + len(b"ccv3\x00")] = ord("!")
    chunk_start = png.rfind(b"tEXt", 0, marker)
    length = struct.unpack(">I", broken[chunk_start - 4:chunk_start])[0]
    data_end = chunk_start + 4 + length
    crc = zlib.crc32(bytes(broken[chunk_start:data_end])) & 0xFFFFFFFF
    broken[data_end:data_end + 4] = struct.pack(">I", crc)

    with pytest.raises(CardParseError, match="valid base64"):
        parse_png_card(broken)


def test_file_chunk_and_metadata_limits_are_enforced():
    png = _card_png(("chara", {"name": "Bounded"}))

    with pytest.raises(CardParseError, match="file size"):
        parse_png_card(png, limits=CardLimits(max_file_bytes=len(png) - 1))
    with pytest.raises(CardParseError, match="chunk exceeds"):
        parse_png_card(png, limits=CardLimits(max_chunk_bytes=10))
    with pytest.raises(CardParseError, match="metadata exceeds"):
        parse_png_card(png, limits=CardLimits(max_metadata_bytes=4))


def test_a_card_edited_in_place_reads_the_live_payload_and_says_so():
    """Real cards off the sites carry the editor's leftovers.

    Two `chara` chunks used to be a refusal — there was no way to tell which one
    a reviewer would read. The file itself answers that: the first is the live
    one and the rest are revisions the editor appended past. Choosing it is fine;
    choosing it *quietly* is what §30.1 forbids, so the choice is reported."""
    live = {"name": "Reiky", "first_mes": "The guild hall roars."}
    stale = {"name": "Reiky", "first_mes": "later"}

    parsed = parse_png_card(_card_png(("chara", live), ("chara", stale)))

    assert parsed.data == live
    assert len(parsed.warnings) == 1
    assert "more than one chara payload" in parsed.warnings[0]
    assert "ignored 1 later one" in parsed.warnings[0]


def test_a_byte_identical_second_copy_is_not_worth_reporting():
    same = {"name": "Twice"}

    parsed = parse_png_card(_card_png(("chara", same), ("chara", same)))

    assert parsed.data == same
    assert parsed.warnings == ()


def test_a_pile_of_card_payloads_is_still_refused():
    png = _card_png(*(("chara", {"name": f"Copy {n}"}) for n in range(6)))

    with pytest.raises(CardParseError, match="more than 4 chara card payloads"):
        parse_png_card(png, limits=CardLimits(max_card_chunks=4))


def test_the_preferred_keyword_still_wins_over_a_repeated_legacy_one():
    """A file with two `chara` and one `ccv3` is read as v3 — and the count that
    is reported is the v3 one's, not the legacy noise beside it."""
    parsed = parse_png_card(_card_png(
        ("chara", {"name": "Old"}), ("chara", {"name": "Older"}),
        ("ccv3", {"spec": "chara_card_v3", "data": {"name": "Current"}})))

    assert parsed.keyword == "ccv3"
    assert parsed.data["data"]["name"] == "Current"
    assert parsed.warnings == ()
