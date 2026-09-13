"""The SOUL's length teaching (Voice law + EXAMPLES.md).

The model copies demonstrated voice. A set that is ten scene-length replies
and one closer that says "not the short, kind version" teaches essays; a
Voice law with no pacing rule never contradicts them. These pin the teaching
set, not a live model's obedience — that needs the opt-in pacing battery.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from yurios.app.core.soul import SoulLoader

ROOT = Path(__file__).resolve().parents[1]
SOUL_SRC = ROOT / "soul-src"

# Everyday replies. Scene-length character beats (comfort, fear, fluster) may
# run longer; the closer must not, because recency is what she copies.
TERSE_WORDS = 25
LONG_WORDS = 80


def _char_side(block: str, name: str = "") -> str:
    match = re.search(r"\{\{\s*char\s*\}\}\s*:(.*)", block, re.IGNORECASE | re.DOTALL)
    if match:
        return match.group(1).strip()
    if name:
        match = re.search(rf"^{re.escape(name)}\s*:(.*)", block,
                          re.IGNORECASE | re.MULTILINE | re.DOTALL)
        if match:
            return match.group(1).strip()
    return block.strip()


@pytest.fixture(scope="module")
def soul():
    if not (SOUL_SRC / "soul.yaml").is_file():
        pytest.skip("soul-src missing")
    return SoulLoader(SOUL_SRC).load()


@pytest.fixture(scope="module")
def example_blocks():
    if not (SOUL_SRC / "EXAMPLES.md").is_file():
        pytest.skip("soul-src missing")
    reader_sections = []
    raw = (SOUL_SRC / "EXAMPLES.md").read_text(encoding="utf-8")
    chunks = re.split(r"\n## Example", raw)
    for chunk in chunks[1:]:
        title, _, body = chunk.partition("\n")
        reader_sections.append((title.strip(" -"), body.strip()))
    assert reader_sections, "EXAMPLES.md has no ## Example blocks"
    return reader_sections


def test_voice_law_does_not_carry_card_publishing_notes(soul):
    """'NSFW character cards load' and 'tasteful so it can be shared' are
    author notes. They were in Voice law, which never drops."""
    law = soul.voice_law.lower()
    assert "nsfw character cards" not in law
    assert "tasteful so" not in law
    assert "shared freely" not in law


def test_voice_law_teaches_reply_size_tracks_the_beat(soul):
    """Voice law is block 1 and never overflows (SPEC §2.1). The pacing rule
    has to live here or it vanishes the turn the example set is dropped."""
    law = soul.voice_law.lower()
    assert "tracks the beat" in law
    assert "fill the room" in law


def test_example_set_has_everyday_shorts_and_a_real_long_answer(example_blocks):
    words = [len(_char_side(body).split()) for _, body in example_blocks]
    terse = sum(1 for n in words if n <= TERSE_WORDS)
    long = sum(1 for n in words if n >= LONG_WORDS)
    assert terse >= 4, f"need everyday shorts, got {terse} ≤{TERSE_WORDS}w: {words}"
    assert long >= 1, f"need a real-question long answer, got {words}"


def test_the_long_answer_is_not_the_last_example(example_blocks):
    """Demonstrated recency is what she copies. Closing on the 139-word
    'not the short, kind version' beat made every check-in an essay."""
    words = [len(_char_side(body).split()) for _, body in example_blocks]
    assert max(words) >= LONG_WORDS
    assert words[-1] <= TERSE_WORDS, (
        f"last example is {words[-1]} words ({example_blocks[-1][0]!r}); "
        f"close on a short everyday reply")


def test_soul_src_does_not_cite_book_chapters():
    """Author-book 'ch. 09' citations are not for the model. USER.md is
    injected whole; EXAMPLES and PERSONA are one edit away from the same."""
    if not SOUL_SRC.is_dir():
        pytest.skip("soul-src missing")
    hits = []
    for path in sorted(SOUL_SRC.glob("*.md")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"(→\s*)?(book\s+)?ch\.\s*\d+", line, re.I):
                hits.append(f"{path.name}:{n}: {line.strip()}")
    assert hits == [], "chapter citations in soul-src:\n" + "\n".join(hits)


def test_loader_keeps_the_pacing_line_and_the_short_closer(soul):
    """Same facts through SoulLoader, which is what assemble() actually reads."""
    assert "tracks the beat" in soul.voice_law.lower()
    starts = [b.strip() for b in soul.examples.split("<START>") if b.strip()]
    assert starts
    last = _char_side(starts[-1], name=soul.name)
    assert len(last.split()) <= TERSE_WORDS
