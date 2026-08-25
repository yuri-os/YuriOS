"""The `SPEC §n` citations are an index, and an index has to be true.

`SPEC.md` is 187 KB. Nobody reads it whole, and nothing should have to: the
convention is that code cites the section that governs it, so a reader — or an
agent asked to change §21's behaviour — finds the normative text by following a
comment instead of grepping 45k tokens of prose. That only works while the
citations resolve, and section numbers are stable precisely so they can be cited
(AGENTS.md: "never renumbered — code, tests, and scripts cite them").

Nothing checked it until this file, and it had already rotted: nine sites cited
sections that do not exist, including a `SPEC §13.3` in the voice fakes pointing
into a section SPEC.md's own heading marks "superseded by §27". They were
predecessor-spec references — the per-build specs the unified SPEC.md replaced —
that had lost their `B1`/`B2`/`B4` prefix and so read as current. An index that
is quietly 1.4% wrong is worse than no index, because it is trusted.

The rule this file enforces: a bare `SPEC §n` means *this* spec and must resolve.
A citation to a predecessor build's spec carries its prefix (`B2 §4.2`), per
PROVENANCE.md's map of which package came from which build.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: `SPEC §12.3` / `SPEC §7`. Deliberately anchored on the word: a bare `(§9)` is
#: ambiguous — prose uses it for a cross-reference within the same file, and the
#: sanctuary's set dressing renders "UNDER §4.2 OF YOUR LICENSE" on a billboard.
CITATION = re.compile(r"\bSPEC\s*§\s*(\d+(?:\.\d+)*)")

#: Where the spec's own section numbers are declared: `## §21 — …`, `### §21.2 …`.
HEADING = re.compile(r"^#{2,4}\s*§(\d+(?:\.\d+)*)", re.M)

SEARCHED = (
    ("yurios", "*.py"),
    ("tests", "*.py"),
    ("scripts", "*.py"),
    ("web", "*.js"),
)
#: This file talks *about* citations — its prose quotes `SPEC §13.3` and
#: `SPEC §12.3` as examples — so scanning itself would fail on its own docstring.
SKIP = ("__pycache__", "node_modules", "/dist/", "/vendor/",
        "test_spec_citations.py")


def _spec_sections() -> set[str]:
    """Every section number SPEC.md declares, headings and inline alike.

    Inline as well as headings because subsections are not all promoted to a
    heading — §7.6 is cited 33 times and lives in §7's body — and a citation to
    one of those is a good citation.
    """
    text = (ROOT / "SPEC.md").read_text(encoding="utf-8")
    return set(HEADING.findall(text)) | set(re.findall(r"§(\d+(?:\.\d+)*)", text))


def _citations() -> dict[str, list[str]]:
    """section -> the places that cite it, as `path:line`."""
    found: dict[str, list[str]] = {}
    for sub, glob in SEARCHED:
        for path in sorted((ROOT / sub).rglob(glob)):
            if any(s in str(path) for s in SKIP):
                continue
            for n, line in enumerate(path.read_text(encoding="utf-8",
                                                    errors="ignore").splitlines(), 1):
                for section in CITATION.findall(line):
                    found.setdefault(section, []).append(
                        f"{path.relative_to(ROOT)}:{n}")
    return found


def test_every_spec_citation_resolves_to_a_real_section():
    sections, cited = _spec_sections(), _citations()
    dangling = {s: where for s, where in cited.items() if s not in sections}
    assert not dangling, (
        "these cite a section SPEC.md does not have — either the section was "
        "renumbered (it must not be), or this is a predecessor build's spec and "
        "wants its prefix (B1/B2/B4, see PROVENANCE.md):\n" +
        "\n".join(f"  SPEC §{s}: {', '.join(w)}" for s, w in sorted(dangling.items())))


#: `## §13 — Tests → superseded by §27`. SPEC.md keeps a superseded section as a
#: one-line stub pointing at its successor, because the numbers are never reused.
SUPERSEDED = re.compile(r"^##\s*§(\d+(?:\.\d+)*)\s*—.*?superseded by §(\d+(?:\.\d+)*)",
                        re.M | re.I)


def test_nothing_cites_a_section_that_has_been_superseded():
    """Resolving is not the same as being useful.

    §12, §13 and §14 still exist — as one-line stubs saying where their content
    went — so a `SPEC §13` citation passes the test above and still lands the
    reader on a signpost rather than the rule. Twelve sites did exactly that,
    mostly the offline-suite rule that moved §13 → §27. SPEC.md declares the
    mapping itself, so there is nothing to judge: follow the signpost once, in
    the comment, instead of every time anyone reads it."""
    spec = (ROOT / "SPEC.md").read_text(encoding="utf-8")
    moved = dict(SUPERSEDED.findall(spec))
    assert moved, "no superseded sections found — has SPEC.md's stub format changed?"
    stale = {s: (moved[s], where) for s, where in _citations().items() if s in moved}
    assert not stale, (
        "these cite a superseded section; cite its successor instead:\n" +
        "\n".join(f"  SPEC §{s} → §{to}: {', '.join(w)}"
                   for s, (to, w) in sorted(stale.items())))


def test_the_citation_convention_is_actually_in_use():
    """A guard on the guard: if the regex stops matching — someone reformats the
    comments, the scan roots move — the test above passes vacuously and the index
    rots unwatched. The real number is ~630 sites over ~105 sections."""
    cited = _citations()
    assert len(cited) > 80, f"only {len(cited)} sections cited — has the scan broken?"
    assert sum(len(w) for w in cited.values()) > 500
