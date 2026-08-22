"""The in-repo QR encoder (`yurios/qr.py`).

Nothing here needs a decoder, which is the point: a QR is a self-describing
structure, so the tests take the encoder apart with its own geometry and check
the two things a scanner would.

1. **The payload really is in there.** `_read_back` undoes the mask, walks the
   same zig-zag the placement uses, un-interleaves the blocks and pulls the bytes
   out of the bit stream. If the bit ordering, the block split or the module
   placement were wrong, the string would not come back.
2. **The parity is real Reed-Solomon.** A codeword sequence with valid parity has
   zero syndromes: evaluating it as a polynomial at 2^0…2^(n-1) over GF(256)
   gives zero for every one. That is the property a scanner's error correction
   relies on, and it is checked directly rather than by trusting the tables.

The symbols themselves were verified against `segno`, an independent encoder,
while this module was written: 1,920 matrices across versions 1-20, both error
levels and all eight masks, compared module by module. That comparison needed a
dependency this project does not carry, so what is pinned here instead is a
handful of golden matrices — enough that a change to the tables or the masking
has to explain itself.
"""
from __future__ import annotations

import hashlib

import pytest

from yurios import qr


def _read_back(matrix, version: int, ecc: str, mask: int) -> bytes:
    """Pull the payload out of a finished symbol, the way a scanner would."""
    size = len(matrix)
    grid = [list(row) for row in matrix]
    _, used = qr._function_modules(version)
    for r in range(size):
        for c in range(size):
            if not used[r][c] and qr._mask(r, c, mask):
                grid[r][c] = not grid[r][c]

    bits: list[int] = []
    col, upward = size - 1, True
    while col > 0:
        if col == 6:
            col -= 1
        for row in (range(size - 1, -1, -1) if upward else range(size)):
            for offset in (0, 1):
                if not used[row][col - offset]:
                    bits.append(1 if grid[row][col - offset] else 0)
        upward = not upward
        col -= 2

    words = [int("".join(str(b) for b in bits[i:i + 8]), 2)
             for i in range(0, len(bits) // 8 * 8, 8)]
    # de-interleave: the data half is read back column-wise across the blocks
    ec_per_block, g1, d1, g2, d2 = qr._BLOCKS[(version, ecc)]
    sizes = [d1] * g1 + [d2] * g2
    blocks: list[list[int]] = [[] for _ in sizes]
    index = 0
    for i in range(max(sizes)):
        for block, length in zip(blocks, sizes):
            if i < length:
                block.append(words[index])
                index += 1
    data = [word for block in blocks for word in block]

    stream = "".join(format(word, "08b") for word in data)
    count_bits = qr._count_bits(version)
    assert stream[:4] == "0100", "byte mode indicator"
    length = int(stream[4:4 + count_bits], 2)
    body = stream[4 + count_bits:4 + count_bits + length * 8]
    return bytes(int(body[i:i + 8], 2) for i in range(0, len(body), 8))


def _syndromes_zero(block: list[int], ec_count: int) -> bool:
    """A block plus its parity, evaluated at each root of the generator."""
    full = block + qr._ec_codewords(block, ec_count)
    for power in range(ec_count):
        acc = 0
        for coefficient in full:
            acc = qr._mul(acc, qr._EXP[power]) ^ coefficient
        if acc:
            return False
    return True


@pytest.mark.parametrize("ecc", ["L", "M"])
@pytest.mark.parametrize("length", [1, 7, 64, 180, 400])
def test_the_payload_reads_back_out_of_the_symbol(ecc, length):
    payload = ("https://192.168.1.20:8768/auth?token=" + "aZ0-_" * 200)[:length]
    version = qr._pick_version(len(payload.encode()), ecc)
    for mask in range(8):
        matrix = qr.encode(payload, ecc=ecc, version=version, mask=mask)
        assert len(matrix) == version * 4 + 17
        assert _read_back(matrix, version, ecc, mask) == payload.encode()


@pytest.mark.parametrize("version", [1, 6, 7, 13, 14, 20])
@pytest.mark.parametrize("ecc", ["L", "M"])
def test_every_block_carries_valid_reed_solomon_parity(version, ecc):
    ec_per_block, g1, d1, g2, d2 = qr._BLOCKS[(version, ecc)]
    for length in (d1, d2 or d1):
        block = [(i * 37 + 11) % 256 for i in range(length)]
        assert _syndromes_zero(block, ec_per_block)


def test_the_finder_patterns_and_the_dark_module_are_where_they_belong():
    matrix = qr.encode("pair me", ecc="M", version=2, mask=3)
    size = len(matrix)
    for top, left in ((0, 0), (0, size - 7), (size - 7, 0)):
        ring = [matrix[top + r][left + c] for r in range(7) for c in range(7)]
        assert sum(ring) == 33                       # 7x7 frame + 3x3 centre
        assert matrix[top + 3][left + 3] is True
    assert matrix[size - 8][8] is True               # the module that is always dark
    assert all(matrix[6][i] == (i % 2 == 0) for i in range(8, size - 8))


def test_the_version_is_the_smallest_that_fits_and_overflow_is_refused():
    assert len(qr.encode("x" * 10, ecc="M")) == 21                 # version 1
    assert len(qr.encode("x" * 20, ecc="M")) == 25                 # version 2
    # a pairing link is nowhere near the ceiling; well past it is an error, not
    # a silently truncated symbol
    assert len(qr.encode("https://192.168.1.20:8768/auth?token=" + "a" * 43)) == 37
    with pytest.raises(ValueError, match="does not fit"):
        qr.encode("x" * 5000)
    with pytest.raises(ValueError, match="ecc must be"):
        qr.encode("x", ecc="H")


def test_pinned_symbols():
    """Golden matrices. A change to the tables, the masking or the format strip
    moves these; if that is deliberate, re-verify against an independent encoder
    before repinning them."""
    def digest(**kwargs) -> str:
        rows = qr.encode(**kwargs)
        packed = "".join("".join("1" if v else "0" for v in row) for row in rows)
        return hashlib.sha256(packed.encode()).hexdigest()[:16]

    assert digest(data="YuriOS", ecc="M", version=1, mask=0) == "3b12fb5bf8d1fa55"
    assert digest(data="YuriOS", ecc="L", version=7, mask=5) == "9a7f398e2768c0ff"
    assert digest(data="x" * 300, ecc="M", version=14, mask=2) == "082553c7ccc163d7"


def test_both_renderers_reproduce_the_matrix_and_its_quiet_zone():
    matrix = qr.encode("https://example.invalid/auth?token=abc", ecc="M")
    size = len(matrix)

    svg = qr.svg(matrix, quiet=4)
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    assert f'viewBox="0 0 {size + 8} {size + 8}"' in svg
    # one path run per horizontal run of dark modules, and no <rect> per module
    runs = sum(1 for row in matrix
               for i, value in enumerate(row) if value and (i == 0 or not row[i - 1]))
    assert svg.count("M") == runs

    lines = qr.terminal(matrix, quiet=4, color=False).splitlines()
    assert len(lines) == (size + 8 + (size + 8) % 2) // 2
    # the quiet zone is real: the first two text rows are entirely light
    assert lines[0].strip() == "" and lines[1].strip() == ""
    coloured = qr.terminal(matrix, quiet=2)
    assert coloured.splitlines()[0].startswith("\x1b[30;47m")
