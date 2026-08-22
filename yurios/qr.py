"""A QR encoder, in-repo, because the payload is one URL and the cost is one file.

`yurios pair` prints a QR to a terminal and the settings panel draws one in the
browser (SPEC §11.1), both of them for the same short string: the pairing link
that carries this installation's owner token to a phone. That is byte-mode QR at
a low version — a bounded, frozen, forty-year-old format with no upstream to
track — so it is written here rather than added to the dependency list, where
every entry is argued for in `pyproject.toml`.

What it implements: byte mode, error-correction levels L and M, versions 1–20
(1,085 codewords, ~850 bytes at L — a pairing URL is around a hundred). Numeric
and alphanumeric modes are not implemented: they would shrink the symbol for
digit-only payloads and there are none. Kanji mode is not implemented either.
Beyond version 20 `encode` raises rather than guessing — see `_BLOCKS`.

The output is a matrix of booleans (True = dark), which `svg()` and `terminal()`
render. Nothing here reads a clock, a file or the network, so the whole module is
a pure function of its input and the tests can compare matrices literally.

Two notes for anyone changing it. The symbols are pinned in `tests/test_qr.py`,
which also reads its own output back out of the matrix and checks the
Reed-Solomon syndromes — the encoding is verifiable without a decoder. And the
mask is chosen by the standard's penalty score, evaluated on the finished symbol
including its format strip; a reader who scores the encoding region alone gets a
different mask for some payloads, and both symbols are valid and scan.
"""
from __future__ import annotations

from typing import Sequence

# --- the tables the standard does not let you derive ------------------------
#
# ISO/IEC 18004 table 9, for the two levels we offer. Each entry is
# (ec codewords per block, blocks in group 1, data codewords per block in group 1,
#  blocks in group 2, data codewords per block in group 2). Group 2's blocks
# carry exactly one more data codeword than group 1's; a version where the split
# is even has no group 2. Extending this to versions 21–40 (or to levels Q and H)
# is a matter of copying more rows of the same table — nothing else here is
# version-bounded.
_BLOCKS: dict[tuple[int, str], tuple[int, int, int, int, int]] = {
    (1, "L"): (7, 1, 19, 0, 0),      (1, "M"): (10, 1, 16, 0, 0),
    (2, "L"): (10, 1, 34, 0, 0),     (2, "M"): (16, 1, 28, 0, 0),
    (3, "L"): (15, 1, 55, 0, 0),     (3, "M"): (26, 1, 44, 0, 0),
    (4, "L"): (20, 1, 80, 0, 0),     (4, "M"): (18, 2, 32, 0, 0),
    (5, "L"): (26, 1, 108, 0, 0),    (5, "M"): (24, 2, 43, 0, 0),
    (6, "L"): (18, 2, 68, 0, 0),     (6, "M"): (16, 4, 27, 0, 0),
    (7, "L"): (20, 2, 78, 0, 0),     (7, "M"): (18, 4, 31, 0, 0),
    (8, "L"): (24, 2, 97, 0, 0),     (8, "M"): (22, 2, 38, 2, 39),
    (9, "L"): (30, 2, 116, 0, 0),    (9, "M"): (22, 3, 36, 2, 37),
    (10, "L"): (18, 2, 68, 2, 69),   (10, "M"): (26, 4, 43, 1, 44),
    (11, "L"): (20, 4, 81, 0, 0),    (11, "M"): (30, 1, 50, 4, 51),
    (12, "L"): (24, 2, 92, 2, 93),   (12, "M"): (22, 6, 36, 2, 37),
    (13, "L"): (26, 4, 107, 0, 0),   (13, "M"): (22, 8, 37, 1, 38),
    (14, "L"): (30, 3, 115, 1, 116), (14, "M"): (24, 4, 40, 5, 41),
    (15, "L"): (22, 5, 87, 1, 88),   (15, "M"): (24, 5, 41, 5, 42),
    (16, "L"): (24, 5, 98, 1, 99),   (16, "M"): (28, 7, 45, 3, 46),
    (17, "L"): (28, 1, 107, 5, 108), (17, "M"): (28, 10, 46, 1, 47),
    (18, "L"): (30, 5, 120, 1, 121), (18, "M"): (26, 9, 43, 4, 44),
    (19, "L"): (28, 3, 113, 4, 114), (19, "M"): (26, 3, 44, 11, 45),
    (20, "L"): (28, 3, 107, 5, 108), (20, "M"): (26, 3, 41, 13, 42),
}
MAX_VERSION = 20

# Table E.1: the row/column centres of the alignment patterns. Version 1 has none.
_ALIGN: dict[int, tuple[int, ...]] = {
    1: (), 2: (6, 18), 3: (6, 22), 4: (6, 26), 5: (6, 30), 6: (6, 34),
    7: (6, 22, 38), 8: (6, 24, 42), 9: (6, 26, 46), 10: (6, 28, 50),
    11: (6, 30, 54), 12: (6, 32, 58), 13: (6, 34, 62), 14: (6, 26, 46, 66),
    15: (6, 26, 48, 70), 16: (6, 26, 50, 74), 17: (6, 30, 54, 78),
    18: (6, 30, 56, 82), 19: (6, 30, 58, 86), 20: (6, 34, 62, 90),
}

# Total codewords (data + ec) per version — table 1, column 3.
_TOTAL_CODEWORDS = (0, 26, 44, 70, 100, 134, 172, 196, 242, 292, 346, 404, 466,
                    532, 581, 655, 733, 815, 901, 991, 1085)

# Bits left over after the codewords are placed; they are written as zeros.
def _remainder_bits(version: int) -> int:
    if version == 1:
        return 0
    if 2 <= version <= 6:
        return 7
    if 7 <= version <= 13:
        return 0
    return 3


# --- GF(256), the field the Reed-Solomon parity lives in --------------------
# Primitive polynomial x^8 + x^4 + x^3 + x^2 + 1 (0x11d), generator 2.
_EXP = [0] * 512
_LOG = [0] * 256


def _init_tables() -> None:
    x = 1
    for i in range(255):
        _EXP[i] = x
        _LOG[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        _EXP[i] = _EXP[i - 255]


_init_tables()


def _mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _generator(degree: int) -> list[int]:
    """The RS generator polynomial (x-2^0)(x-2^1)…(x-2^(degree-1)), coefficients
    high-order first."""
    poly = [1]
    for i in range(degree):
        nxt = [0] * (len(poly) + 1)
        for j, coeff in enumerate(poly):
            nxt[j] ^= coeff                      # × x
            nxt[j + 1] ^= _mul(coeff, _EXP[i])   # × 2^i
        poly = nxt
    return poly


def _ec_codewords(block: Sequence[int], count: int) -> list[int]:
    """Polynomial long division; the remainder is the parity for this block."""
    gen = _generator(count)
    rem = list(block) + [0] * count
    for i in range(len(block)):
        lead = rem[i]
        if lead == 0:
            continue
        for j, coeff in enumerate(gen):
            rem[i + j] ^= _mul(coeff, lead)
    return rem[len(block):]


# --- the bit stream ---------------------------------------------------------

class _Bits:
    def __init__(self) -> None:
        self.bits: list[int] = []

    def put(self, value: int, length: int) -> None:
        for i in range(length - 1, -1, -1):
            self.bits.append((value >> i) & 1)

    def __len__(self) -> int:
        return len(self.bits)


def _capacity_bits(version: int, ecc: str) -> int:
    ec_per_block, g1, d1, g2, d2 = _BLOCKS[(version, ecc)]
    return (g1 * d1 + g2 * d2) * 8


def _count_bits(version: int) -> int:
    """Byte mode's character-count field: 8 bits below version 10, 16 above."""
    return 8 if version < 10 else 16


def _pick_version(length: int, ecc: str) -> int:
    for version in range(1, MAX_VERSION + 1):
        needed = 4 + _count_bits(version) + length * 8
        if needed <= _capacity_bits(version, ecc):
            return version
    raise ValueError(
        f"{length} bytes does not fit a version-{MAX_VERSION} QR at level {ecc}; "
        "shorten the payload (a pairing URL is around a hundred bytes)")


def _codewords(data: bytes, version: int, ecc: str) -> list[int]:
    capacity = _capacity_bits(version, ecc)
    stream = _Bits()
    stream.put(0b0100, 4)                       # byte mode
    stream.put(len(data), _count_bits(version))
    for byte in data:
        stream.put(byte, 8)
    stream.put(0, min(4, capacity - len(stream)))            # terminator
    while len(stream) % 8:                                    # to a byte boundary
        stream.put(0, 1)
    words = [int("".join(str(b) for b in stream.bits[i:i + 8]), 2)
             for i in range(0, len(stream), 8)]
    # …and the standard's filler, alternating from 0xEC, until the data capacity
    # is full.
    pad = (0xEC, 0x11)
    written = len(words)
    while len(words) < capacity // 8:
        words.append(pad[(len(words) - written) % 2])
    return words


def _interleave(words: Sequence[int], version: int, ecc: str) -> list[int]:
    """Split into blocks, take the parity of each, then read them column-wise.

    Interleaving is what makes the error correction worth having: a scratch
    across the symbol lands as a few bytes in each block rather than as a burst
    that overruns one block's capacity.
    """
    ec_per_block, g1, d1, g2, d2 = _BLOCKS[(version, ecc)]
    blocks: list[list[int]] = []
    offset = 0
    for _ in range(g1):
        blocks.append(list(words[offset:offset + d1]))
        offset += d1
    for _ in range(g2):
        blocks.append(list(words[offset:offset + d2]))
        offset += d2
    parity = [_ec_codewords(block, ec_per_block) for block in blocks]

    out: list[int] = []
    for i in range(max(len(b) for b in blocks)):
        for block in blocks:
            if i < len(block):
                out.append(block[i])
    for i in range(ec_per_block):
        for block_parity in parity:
            out.append(block_parity[i])
    return out


# --- the symbol -------------------------------------------------------------
#
# The matrix is built twice over: `_function_modules` paints everything whose
# position the standard fixes (finders, separators, timing, alignment, the dark
# module, and the areas reserved for format and version information) and records
# which cells that claimed, then the data snake fills what is left. Keeping the
# reservation map separate from the pixels is what lets the snake ask "is this
# cell mine?" without having to re-derive the geometry.

def _size(version: int) -> int:
    return version * 4 + 17


def _function_modules(version: int) -> tuple[list[list[bool]], list[list[bool]]]:
    n = _size(version)
    grid = [[False] * n for _ in range(n)]
    used = [[False] * n for _ in range(n)]

    def rect(top: int, left: int, height: int, width: int, dark: bool) -> None:
        for r in range(top, top + height):
            for c in range(left, left + width):
                if 0 <= r < n and 0 <= c < n:
                    grid[r][c] = dark
                    used[r][c] = True

    # the three finders, each with its one-module separator
    for top, left in ((0, 0), (0, n - 7), (n - 7, 0)):
        rect(top - 1, left - 1, 9, 9, False)
        rect(top, left, 7, 7, True)
        rect(top + 1, left + 1, 5, 5, False)
        rect(top + 2, left + 2, 3, 3, True)

    # the timing patterns: the alternating line joining the finders, in the gap
    # the separators above left already claimed either end of
    for i in range(n):
        if not used[6][i]:
            grid[6][i], used[6][i] = i % 2 == 0, True
        if not used[i][6]:
            grid[i][6], used[i][6] = i % 2 == 0, True

    # the alignment patterns, at every pair of centres except the three corners
    # a finder already occupies. The test is on the corner, not on "is this cell
    # spoken for": a centre sitting in the timing row is a real alignment pattern
    # (version 7's at row 6 is the first), and it overwrites the timing modules
    # with values that happen to agree with them.
    centres = _ALIGN[version]
    skip = {(centres[0], centres[0]), (centres[0], centres[-1]),
            (centres[-1], centres[0])} if centres else set()
    for row in centres:
        for col in centres:
            if (row, col) in skip:
                continue
            rect(row - 2, col - 2, 5, 5, True)
            rect(row - 1, col - 1, 3, 3, False)
            rect(row, col, 1, 1, True)

    # the format-information strip, reserved now and written after masking
    for i in range(9):
        used[8][i] = used[i][8] = True
    for i in range(8):
        used[8][n - 1 - i] = used[n - 1 - i][8] = True
    grid[n - 8][8] = True            # the dark module, always set, always here
    used[n - 8][8] = True

    if version >= 7:                 # the version strip, two copies
        for i in range(18):
            row, col = i // 3, i % 3
            used[n - 11 + col][row] = True
            used[row][n - 11 + col] = True

    return grid, used


def _place_data(grid: list[list[bool]], used: list[list[bool]],
                codewords: Sequence[int], version: int) -> None:
    """The zig-zag: two-module columns walked right to left, alternating up and
    down, skipping the vertical timing pattern's column."""
    n = len(grid)
    bits = [(word >> i) & 1 for word in codewords for i in range(7, -1, -1)]
    bits += [0] * _remainder_bits(version)
    index = 0
    col = n - 1
    upward = True
    while col > 0:
        if col == 6:                 # the timing column is not a data column
            col -= 1
        rows = range(n - 1, -1, -1) if upward else range(n)
        for row in rows:
            for offset in (0, 1):
                c = col - offset
                if used[row][c]:
                    continue
                grid[row][c] = bool(bits[index]) if index < len(bits) else False
                index += 1
        upward = not upward
        col -= 2


def _mask(row: int, col: int, pattern: int) -> bool:
    if pattern == 0:
        return (row + col) % 2 == 0
    if pattern == 1:
        return row % 2 == 0
    if pattern == 2:
        return col % 3 == 0
    if pattern == 3:
        return (row + col) % 3 == 0
    if pattern == 4:
        return (row // 2 + col // 3) % 2 == 0
    if pattern == 5:
        return (row * col) % 2 + (row * col) % 3 == 0
    if pattern == 6:
        return ((row * col) % 2 + (row * col) % 3) % 2 == 0
    return ((row + col) % 2 + (row * col) % 3) % 2 == 0


_FORMAT_MASK = 0b101010000010010


def _format_bits(ecc: str, pattern: int) -> int:
    """15 bits: 5 of data (the level, then the mask) and a BCH(15,5) remainder,
    XORed with the standard's constant so an all-zero format never reads blank."""
    level = {"L": 0b01, "M": 0b00, "Q": 0b11, "H": 0b10}[ecc]
    data = (level << 3) | pattern
    rem = data << 10
    while rem.bit_length() >= 11:
        rem ^= 0b10100110111 << (rem.bit_length() - 11)
    return ((data << 10) | rem) ^ _FORMAT_MASK


def _version_bits(version: int) -> int:
    """18 bits: the version and a BCH(18,6) remainder. Versions 7 and up only."""
    rem = version << 12
    while rem.bit_length() >= 13:
        rem ^= 0b1111100100101 << (rem.bit_length() - 13)
    return (version << 12) | rem


def _write_format(grid: list[list[bool]], ecc: str, pattern: int) -> None:
    """Both copies of the format strip, most significant bit first."""
    n = len(grid)
    bits = _format_bits(ecc, pattern)
    for i in range(15):
        bit = bool((bits >> (14 - i)) & 1)
        # copy one, wrapped around the top-left finder
        if i < 6:
            grid[8][i] = bit
        elif i == 6:
            grid[8][7] = bit
        elif i == 7:
            grid[8][8] = bit
        elif i == 8:
            grid[7][8] = bit
        else:
            grid[14 - i][8] = bit
        # copy two, split between the other two finders: seven modules climbing
        # the bottom-left, then eight running out along the top-right
        if i < 7:
            grid[n - 1 - i][8] = bit
        else:
            grid[8][n - 8 + i - 7] = bit


def _write_version(grid: list[list[bool]], version: int) -> None:
    if version < 7:
        return
    n = len(grid)
    bits = _version_bits(version)
    for i in range(18):
        bit = bool((bits >> i) & 1)
        row, col = i // 3, i % 3
        grid[n - 11 + col][row] = bit
        grid[row][n - 11 + col] = bit


def _penalty(grid: list[list[bool]]) -> int:
    """The standard's four rules, added up. A lower score is a symbol a scanner
    finds easier: long same-colour runs, 2x2 blocks of one colour, the finder
    pattern turning up inside the data, and an unbalanced dark/light ratio."""
    n = len(grid)
    lines = [list(row) for row in grid]
    lines += [[grid[r][c] for r in range(n)] for c in range(n)]
    score = 0

    for line in lines:                                        # rule 1: runs of 5+
        run, colour = 1, line[0]
        for value in line[1:]:
            if value == colour:
                run += 1
            else:
                if run >= 5:
                    score += 3 + (run - 5)
                run, colour = 1, value
        if run >= 5:
            score += 3 + (run - 5)

    for r in range(n - 1):                                    # rule 2: 2x2 blocks
        for c in range(n - 1):
            block = (grid[r][c], grid[r][c + 1], grid[r + 1][c], grid[r + 1][c + 1])
            if all(block) or not any(block):
                score += 3

    # rule 3: the finder's 1:1:3:1:1 signature with four light modules on either
    # side of it, anywhere in a row or column, is what a scanner would mistake
    # for a finder. Running off the edge of the symbol counts as light — the
    # quiet zone is out there.
    finder = [True, False, True, True, True, False, True]
    for line in lines:
        for i in range(n - 6):
            if line[i:i + 7] != finder:
                continue
            if not any(line[max(0, i - 4):i]) or not any(line[i + 7:i + 11]):
                score += 40

    # rule 4: how far the dark proportion strays from half, in 5% steps
    dark = sum(1 for row in grid for value in row if value)
    lower = (dark * 100 // (5 * n * n)) * 5
    score += min(abs(lower - 50), abs(lower + 5 - 50)) // 5 * 10
    return score


def encode(data: str | bytes, *, ecc: str = "M",
           version: int | None = None, mask: int | None = None) -> list[list[bool]]:
    """The QR matrix for `data` — a list of rows of booleans, True where dark.

    `version` and `mask` exist for the tests, which pin both so a symbol can be
    compared literally against a reference encoder. Left alone, the version is
    the smallest that fits and the mask is the one the standard's penalty score
    likes best.
    """
    if ecc not in ("L", "M"):
        raise ValueError("ecc must be 'L' or 'M'")
    payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    if version is None:
        version = _pick_version(len(payload), ecc)
    elif not 1 <= version <= MAX_VERSION:
        raise ValueError(f"version must be 1–{MAX_VERSION}")
    elif len(payload) * 8 + 4 + _count_bits(version) > _capacity_bits(version, ecc):
        raise ValueError(f"{len(payload)} bytes do not fit version {version} at {ecc}")

    words = _interleave(_codewords(payload, version, ecc), version, ecc)
    base, used = _function_modules(version)
    _place_data(base, used, words, version)
    _write_version(base, version)

    best: list[list[bool]] | None = None
    best_score = -1
    for pattern in ([mask] if mask is not None else range(8)):
        grid = [row[:] for row in base]
        for r in range(len(grid)):
            for c in range(len(grid)):
                if not used[r][c] and _mask(r, c, pattern):
                    grid[r][c] = not grid[r][c]
        _write_format(grid, ecc, pattern)
        score = _penalty(grid)
        if best is None or score < best_score:
            best, best_score = grid, score
    assert best is not None
    return best


# --- renderers --------------------------------------------------------------
#
# Both take the matrix and add the quiet zone the standard requires (four
# modules of light on every side — a QR with no margin is the single most
# common reason a phone will not see one).

def svg(matrix: Sequence[Sequence[bool]], *, quiet: int = 4, module: int = 8,
        dark: str = "#000000", light: str = "#ffffff") -> str:
    """One <svg> element, no external anything, safe to inline into a page.

    The dark modules are emitted as a single <path> of move+horizontal-line runs
    rather than one <rect> per module: a version-6 symbol is 1,681 modules, and
    an element each would be a document the browser lays out for a hundredth of
    a second every time the panel opens.
    """
    n = len(matrix)
    span = n + quiet * 2
    runs: list[str] = []
    for r, row in enumerate(matrix):
        c = 0
        while c < n:
            if not row[c]:
                c += 1
                continue
            start = c
            while c < n and row[c]:
                c += 1
            runs.append(f"M{start + quiet} {r + quiet}h{c - start}v1h-{c - start}z")
    path = "".join(runs)
    # `stroke="none"` is not decoration: a page whose stylesheet paints every
    # <svg> as a line icon (`fill:none; stroke:currentColor; stroke-width:1.7`)
    # inherits that onto these two elements, and 1.7 *viewBox units* is nearly
    # two modules wide — the symbol comes out as a smear. Saying it here means
    # the code survives being dropped into a page that has never heard of it.
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {span} {span}" '
            f'width="{span * module}" height="{span * module}" '
            f'shape-rendering="crispEdges" role="img">'
            f'<rect width="{span}" height="{span}" fill="{light}" stroke="none"/>'
            f'<path d="{path}" fill="{dark}" stroke="none"/></svg>')


def terminal(matrix: Sequence[Sequence[bool]], *, quiet: int = 4,
             color: bool = True) -> str:
    """Two module rows per text row, using half-block characters.

    Colour is forced rather than inherited: dark-on-light is what a scanner
    expects, and a terminal with a dark theme would otherwise hand it a
    photographic negative. `color=False` is the fallback for a pipe or a terminal
    that would print the escapes literally — it draws with full blocks, which
    needs a light background to scan.
    """
    n = len(matrix)
    rows = [[False] * (n + quiet * 2) for _ in range(quiet)]
    rows += [[False] * quiet + list(row) + [False] * quiet for row in matrix]
    rows += [[False] * (n + quiet * 2) for _ in range(quiet)]
    if len(rows) % 2:
        rows.append([False] * len(rows[0]))

    lines: list[str] = []
    for i in range(0, len(rows), 2):
        top, bottom = rows[i], rows[i + 1]
        if color:
            line = "".join("\u2588" if t and b else "\u2580" if t else
                           "\u2584" if b else " " for t, b in zip(top, bottom))
            lines.append(f"\x1b[30;47m{line}\x1b[0m")
        else:
            lines.append("".join("\u2588\u2588" if t and b else "\u2580\u2580" if t
                                 else "\u2584\u2584" if b else "  "
                                 for t, b in zip(top, bottom)))
    return "\n".join(lines)
