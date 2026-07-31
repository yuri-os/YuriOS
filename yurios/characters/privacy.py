"""The export scrub — what may leave a character root, and the proof it didn't.

Getting this wrong once makes YuriOS the project that leaked somebody's
relationship, so the boundary is defended four times over and no layer is
trusted alone (spec §7):

  1. the exporter never takes a path — it derives one soul folder from a record,
     and `corpus/`, `traces/`, `memory/`, `state/`, `.git/` are not "excluded",
     they are never named (`exporter.py`);
  2. the reader is jailed to that folder and refuses the runtime-only files by
     name, however the manifest asks (`soulfiles.SoulReader`);
  3. the card is *built* key by key from an allowlist, never copied from
     `card.json` — an export is authored, not forwarded (`exporter.to_card_data`);
  4. and then this module reads the private surfaces of *this* vault, harvests
     canaries from them, and refuses to let the bytes out if any of them turn up
     in the card. Layer 4 is a runtime check and not only a test, because its job
     is to catch the leak that code written next year introduces.

**This module is the only one in the export path allowed to read a secret.** It
reads `os.environ` to harvest credential canaries, and exposes no function that
returns an environment value — the only thing it can do with a key is refuse to
ship it. `exporter.py` correspondingly imports neither `os` nor any config, which
`test_export_privacy.py` asserts against the AST. The module that can see a
secret cannot emit one; the module that emits cannot see.

### On false positives, which is where the design gets interesting

A genuinely grown persona can legitimately contain a sentence that also appears
in `USER.md` — the mind proposed a `PERSONA.md` edit, you approved it at the
gate (§23), and now "she knows he takes his coffee black" is part of who she is.
Refusing to export that would make the feature useless for exactly the
characters it exists for. So the assay's authority is scoped:

  * content from surfaces that are **never** exportable (memory, corpus, traces,
    goals, world, state, USER.md, MEMORY.md) is a **hard block**;
  * prose that came out of `vault/soul/` and merely overlaps one of those is a
    **warning with the overlap quoted**, for the human review pane to resolve;
  * credentials and a distinctive user name are hard blocks everywhere, no
    override, at any length.

Machine-enforce the file boundary; human-review the prose.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

# ---------------------------------------------------------------- the boundary

#: Soul files that never leave the Vault, whatever `soul.yaml` says. A floor:
#: the manifest's own `runtime_only:` list is unioned in, never subtracted.
PRIVATE_SOUL_FILES = frozenset({"USER.md", "MEMORY.md"})

#: Everything under a character root that the export path must never read as
#: card content, relative to the root. Rendered to the user as "what stays".
PRIVATE_SURFACES: tuple[tuple[str, str], ...] = (
    ("vault/soul/USER.md", "the partner model — what she knows about you"),
    ("vault/soul/MEMORY.md", "relationship memory"),
    ("vault/memory", "episodic journal, consolidated facts, the rolling summary"),
    ("vault/world", "her world model and current situation"),
    ("vault/knowledge", "documents you dropped in for her to read"),
    ("vault/goals.md", "her intentions"),
    ("vault/state", "sessions, budget, pending edits, quarantine"),
    ("corpus", "raw conversation — the trainable log"),
    ("traces", "tick traces and context history"),
    ("tool-logs", "what she did with her hands, and on whose behalf"),
    ("source-card.png", "the card someone else handed you"),
)

#: Names in `extensions` that are rebuilt rather than forwarded.
EXTENSION_DENY = frozenset({"yurios"})

#: A user name this generic cannot be a canary: `USER_NAME` defaults to "you",
#: and a hard block on "you" would refuse every card ever authored.
GENERIC_NAMES = frozenset({
    "you", "user", "me", "i", "him", "her", "them", "they", "he", "she", "it",
    "friend", "dear", "love", "master", "mistress", "sir", "madam", "anon",
    "anonymous", "name", "someone", "person", "human", "operator", "player",
})

_CREDENTIAL_KEY = re.compile(
    r"(API_?KEY|_TOKEN|TOKEN_|^TOKEN$|SECRET|PASSWORD|PASSWD|WEBHOOK|_PAT$|CREDENTIAL)",
    re.IGNORECASE,
)
_MIN_CREDENTIAL_LEN = 8

# A canary must be distinctive enough that a collision means a leak and not
# ordinary English. Tuned so "she is warm and quiet in the evenings" (7 words)
# counts and "I like it" does not.
MIN_CANARY_CHARS = 24
MIN_CANARY_WORDS = 5
_WINDOW_WORDS = 8
_WINDOW_STEP = 4
_MAX_CANARIES = 4000
_MAX_LINES_PER_FILE = 400
_MAX_JSONL_ROWS = 200
_MAX_EPISODIC_FILES = 3

_PLACEHOLDER = re.compile(r"^[_*(\s]*\(?(no|not|unknown|to be|empty|none)\b.*$", re.IGNORECASE)
_HEADING = re.compile(r"^(#{1,6}\s|---\s*$|```)")


class CardExportError(ValueError):
    """The export was refused. Carries what to blame, and whether it is fixable.

    `code` separates the two refusals the studio must render differently:
    `"leak"` is the machine saying no (private content in the card, fix the soul
    files), while `"review_required"` is the machine saying *not yet* — prose
    that overlaps a private surface, which a grown character legitimately has,
    and which ships once a human has read it and re-submitted with
    `acknowledged=True`. Failing closed on the second one is deliberate: an
    unacknowledged overlap that merely warned would ship silently from the
    one-click route, which is the exact shape of the accident this whole module
    exists to prevent.
    """

    def __init__(self, message: str, *, surface: str = "", field_name: str = "",
                 code: str = "invalid", overlaps: "list[Overlap] | None" = None):
        self.surface = surface
        self.field_name = field_name
        self.code = code
        self.overlaps = overlaps or []
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        return {"detail": str(self), "code": self.code, "surface": self.surface,
                "field": self.field_name,
                "overlaps": [asdict(overlap) for overlap in self.overlaps]}


# ------------------------------------------------------------- normalisation

def normalise(text: str) -> str:
    """Case-folded, whitespace-collapsed — the space both sides are compared in.

    Reformatting is the commonest way a leak hides: a fact lifted out of
    `USER.md` into a persona line arrives with different wrapping and different
    capitalisation, and a naive `in` check misses it.
    """
    # `\s` is Unicode-aware in Python 3, so NBSP and friends collapse here too.
    return re.sub(r"\s+", " ", text).strip().casefold()


def _spans(line: str) -> Iterator[str]:
    """A normalised line, plus sliding word windows so a partial lift still hits."""
    flat = normalise(line)
    words = flat.split(" ")
    if len(flat) >= MIN_CANARY_CHARS and len(words) >= MIN_CANARY_WORDS:
        yield flat
    if len(words) > _WINDOW_WORDS:
        for start in range(0, len(words) - _WINDOW_WORDS + 1, _WINDOW_STEP):
            window = " ".join(words[start:start + _WINDOW_WORDS])
            if len(window) >= MIN_CANARY_CHARS:
                yield window


# ------------------------------------------------------------------ harvest

@dataclass(frozen=True, slots=True)
class Canary:
    text: str          # already normalised, except `exact` ones
    surface: str       # the file or surface it came from
    hard: bool = True  # hard block, vs. a soul-overlap warning
    exact: bool = False  # match verbatim and at any length (credentials)
    word_bounded: bool = False  # match only on word boundaries (names)


def _text_lines(path: Path, limit: int = _MAX_LINES_PER_FILE) -> list[str]:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip().lstrip("-*+ ").strip()
        if not stripped or _HEADING.match(line.strip()) or _PLACEHOLDER.match(stripped):
            continue
        out.append(stripped)
        if len(out) >= limit:
            break
    return out


def _json_strings(value: Any, out: list[str], depth: int = 0) -> None:
    if depth > 8 or len(out) >= _MAX_LINES_PER_FILE:
        return
    if isinstance(value, str):
        if value.strip():
            out.append(value.strip())
    elif isinstance(value, dict):
        for item in value.values():
            _json_strings(item, out, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _json_strings(item, out, depth + 1)


def _jsonl_strings(path: Path) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out: list[str] = []
    for line in lines[-_MAX_JSONL_ROWS:]:
        try:
            _json_strings(json.loads(line), out)
        except (ValueError, TypeError):
            continue
    return out


def _json_file_strings(path: Path) -> list[str]:
    out: list[str] = []
    try:
        _json_strings(json.loads(path.read_text(encoding="utf-8", errors="replace")), out)
    except (OSError, ValueError, TypeError):
        return []
    return out


def _credential_canaries() -> list[Canary]:
    """Every secret this process can see, so none of it can be shipped."""
    found: list[Canary] = []
    for key, value in os.environ.items():
        value = (value or "").strip()
        if len(value) < _MIN_CREDENTIAL_LEN or not _CREDENTIAL_KEY.search(key):
            continue
        found.append(Canary(text=value, surface=f"the {key} credential",
                            hard=True, exact=True))
    return found


def harvest(root: Path, *, user_name: str = "") -> list[Canary]:
    """Every string this character root must not ship, from this vault's own data.

    `root` is the character root (`data/characters/<id>`). Bounded work: newest
    rows only, a line cap per file, a global cap.
    """
    root = Path(root)
    vault = root / "vault"
    canaries: list[Canary] = []
    seen: set[str] = set()

    def add(lines: Iterable[str], surface: str, *, hard: bool = True) -> None:
        for line in lines:
            for span in _spans(line):
                if span in seen:
                    continue
                seen.add(span)
                canaries.append(Canary(text=span, surface=surface, hard=hard))
                if len(canaries) >= _MAX_CANARIES:
                    return

    # The two runtime-only soul files: the worst possible leak, and the likeliest.
    for name in sorted(PRIVATE_SOUL_FILES):
        add(_text_lines(vault / "soul" / name), f"vault/soul/{name}")

    memory = vault / "memory"
    add(_text_lines(memory / "summary.md"), "vault/memory/summary.md")
    for name in ("facts.md", "forgotten.md"):
        add(_text_lines(memory / "semantic" / name), f"vault/memory/semantic/{name}")
    episodic = memory / "episodic"
    if episodic.is_dir():
        for path in sorted(episodic.glob("*.md"), reverse=True)[:_MAX_EPISODIC_FILES]:
            add(_text_lines(path), f"vault/memory/episodic/{path.name}")

    add(_text_lines(vault / "goals.md"), "vault/goals.md")
    add(_text_lines(vault / "world" / "situation.md"), "vault/world/situation.md")
    add(_jsonl_strings(vault / "world" / "beliefs.jsonl"), "vault/world/beliefs.jsonl")

    knowledge = vault / "knowledge"
    if knowledge.is_dir():
        for path in sorted(knowledge.rglob("*.md"))[:_MAX_EPISODIC_FILES]:
            add(_text_lines(path), f"vault/knowledge/{path.name}")

    state = vault / "state"
    if state.is_dir():
        for path in sorted(state.glob("*.json")):
            add(_json_file_strings(path), f"vault/state/{path.name}")

    add(_jsonl_strings(root / "corpus" / "turns.jsonl"), "corpus/turns.jsonl")
    traces = root / "traces"
    if traces.is_dir():
        for path in sorted(traces.glob("*.jsonl")):
            add(_jsonl_strings(path), f"traces/{path.name}")

    canaries.extend(_credential_canaries())

    # The user's own name, when it is distinctive enough to mean anything. This
    # guard is load-bearing: USER_NAME defaults to "you".
    name = (user_name or "").strip()
    if len(name) >= 3 and name.casefold() not in GENERIC_NAMES:
        canaries.append(Canary(text=name, surface="the user's name", hard=True,
                               word_bounded=True))
    return canaries


# --------------------------------------------------------------------- assay

@dataclass(frozen=True, slots=True)
class Overlap:
    surface: str
    excerpt: str
    hard: bool

    def summary(self) -> str:
        return f"{self.surface}: …{self.excerpt[:120]}…"


@dataclass(slots=True)
class PrivacyReport:
    ships: list[dict[str, Any]] = field(default_factory=list)
    stays: list[dict[str, Any]] = field(default_factory=list)
    canaries: int = 0
    ran_on: list[str] = field(default_factory=list)
    hits: list[Overlap] = field(default_factory=list)
    soul_overlaps: list[Overlap] = field(default_factory=list)
    image: dict[str, Any] = field(default_factory=dict)
    head: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ships": self.ships,
            "stays": self.stays,
            "assay": {
                "canaries": self.canaries,
                "ran_on": self.ran_on,
                "hits": [asdict(hit) for hit in self.hits],
                "soul_overlaps": [asdict(hit) for hit in self.soul_overlaps],
            },
            "image": self.image,
            "head": self.head,
        }


def _matches(canary: Canary, haystack: str, raw_haystack: str) -> bool:
    if canary.exact:
        return canary.text in raw_haystack
    if canary.word_bounded:
        return re.search(rf"\b{re.escape(normalise(canary.text))}\b", haystack) is not None
    return canary.text in haystack


def string_leaves(value: Any, out: list[str] | None = None, depth: int = 0) -> list[str]:
    """Every string a card carries, as the reader will see it.

    Assaying `json.dumps(card)` instead looks equivalent and is not: `dumps`
    escapes a real newline to the two characters `\\` and `n`, which `\\s+`
    cannot collapse, so a leaked passage that was merely re-wrapped on its way
    into a soul file normalises differently on the two sides and slips through.
    Compare decoded values, always.
    """
    out = [] if out is None else out
    if depth > 12:
        return out
    if isinstance(value, str):
        if value.strip():
            out.append(value)
    elif isinstance(value, Mapping):
        for item in value.values():
            string_leaves(item, out, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            string_leaves(item, out, depth + 1)
    return out


def assay(payload: Any, canaries: Iterable[Canary], *, soul_text: str = "",
          raw: str | bytes = "") -> tuple[list[Overlap], list[Overlap]]:
    """Check *payload* against the canaries. Returns (hard hits, soul overlaps).

    `payload` may be a card dict, a string, or bytes; a dict is flattened to its
    decoded string leaves (see `string_leaves`). `raw` is the literal artefact —
    the PNG, or the serialized JSON — scanned verbatim for the canaries that
    match at any length, so a credential hidden in binary metadata is still
    found even though it never appears as a card value.

    `soul_text` is everything the exportable soul files contain. A canary that
    also appears there is prose the user authored or approved into the persona,
    not a leak of the private surface it happens to echo — it is downgraded to a
    reviewable overlap for a human to clear. Credentials and names are never
    downgraded.
    """
    if isinstance(payload, (dict, list, tuple)):
        text = "\n".join(string_leaves(payload))
    elif isinstance(payload, bytes):
        text = payload.decode("utf-8", errors="replace")
    else:
        text = str(payload)
    raw_text = raw.decode("latin-1", errors="replace") if isinstance(raw, bytes) else str(raw)
    haystack = normalise(text)
    soul = normalise(soul_text)
    hard: list[Overlap] = []
    soft: list[Overlap] = []
    for canary in canaries:
        if not _matches(canary, haystack, raw_text or text):
            continue
        overlap = Overlap(surface=canary.surface, excerpt=canary.text, hard=canary.hard)
        downgradable = not canary.exact and not canary.word_bounded
        if downgradable and canary.text in soul:
            soft.append(overlap)
        else:
            hard.append(overlap)
    return hard, soft


def stays_report(root: Path) -> list[dict[str, Any]]:
    """What is staying on this machine, counted — the honest reassurance.

    Rendered above the export button. Real numbers, because "214 memories and
    3,901 conversation turns stay here" is both the truth and the best possible
    argument for the runtime that kept them.
    """
    root = Path(root)
    out: list[dict[str, Any]] = []
    for rel, reason in PRIVATE_SURFACES:
        path = root / rel
        if not path.exists():
            continue
        entry: dict[str, Any] = {"surface": rel, "reason": reason}
        if path.is_dir():
            files = [p for p in path.rglob("*") if p.is_file()]
            entry["files"] = len(files)
            entry["bytes"] = sum(p.stat().st_size for p in files)
            if rel == "vault/memory":
                episodic = path / "episodic"
                entry["entries"] = sum(
                    len([ln for ln in p.read_text(encoding="utf-8", errors="replace")
                         .splitlines() if ln.startswith("### ")])
                    for p in episodic.glob("*.md")) if episodic.is_dir() else 0
            if rel == "corpus":
                turns = path / "turns.jsonl"
                entry["turns"] = (
                    len(turns.read_text(encoding="utf-8", errors="replace").splitlines())
                    if turns.is_file() else 0)
        else:
            entry["bytes"] = path.stat().st_size
        out.append(entry)
    out.append({"surface": "credentials", "reason": "API keys and bot tokens never leave the node"})
    return out
