"""The SOUL manifest resolver — one copy, with the privacy jail inside it.

`soul.yaml` says which markdown feeds which prompt section ("she reads herself
into being", SPEC §5). The reference syntax is tiny:

    FILE.md#Heading   → the prose under that "## Heading"
    FILE.md@key       → a key from the file's YAML frontmatter
    FILE.md           → the whole body (after frontmatter)

This module lives in `characters/` — the leaf storage core — rather than in
`app/core/soul.py`, where it grew up, because two callers now need it and they
sit on opposite sides of the tree: the runtime loader (`SoulLoader`, which
resolves the manifest into a prompt) and the card exporter (which resolves the
same manifest into bytes that leave the machine). A resolver that decides *which
bytes leave the machine* must not exist in three copies, so it exists here and
`app/core/soul.py` re-exports it.

The jail is the reason it is a constructor argument and not a caller's
responsibility. `SoulReader(folder, forbidden={"USER.md"})` cannot be persuaded
to open `USER.md` — not through a `fields:` reference in a hand-edited manifest,
not through `../`, not through a symlink out of the folder. A caller that forgets
to filter gets a `SoulPrivacyError`, never the file. The runtime loader passes an
empty set (it is building a prompt for the person the file is *about*); the
exporter passes the private surfaces (SPEC §2.1, and `soul.yaml`'s own
`runtime_only:`). Refusal raises — an empty string would be a silent leak of the
wrong kind: a card that quietly lost its personality.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml

log = logging.getLogger("characters.soulfiles")

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
H2_RE = re.compile(r"^##\s+(.*?)\s*$", re.MULTILINE)


class SoulPrivacyError(PermissionError):
    """A manifest reference pointed at a file this reader may not open."""

    def __init__(self, name: str, reason: str):
        self.name = name
        self.reason = reason
        super().__init__(f"soul file {name!r} is not readable here: {reason}")


def parse_md_text(text: str) -> tuple[dict, str]:
    """Return (frontmatter dict, body) for soul .md *content*.

    Split out from `parse_md` so a file that does not exist yet can be held to
    the same reading — a proposed rewrite is checked against the manifest before
    it is ever written (`mind/selfedit.py`), and it must be read by exactly the
    parser that will read it afterwards or the check proves nothing.
    """
    m = FRONTMATTER_RE.match(text)
    if m:
        front = yaml.safe_load(m.group(1)) or {}
        body = text[m.end():]
    else:
        front, body = {}, text
    return front, body


def parse_md(path: Path) -> tuple[dict, str]:
    """Return (frontmatter dict, body) for a soul .md file."""
    return parse_md_text(path.read_text(encoding="utf-8"))


def split_sections(body: str) -> dict[str, str]:
    """Map each '## Heading' to the prose beneath it (order preserved)."""
    sections: dict[str, str] = {}
    matches = list(H2_RE.finditer(body))
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        sections[m.group(1).strip()] = body[start:end].strip()
    return sections


def manifest_refs(soul_dir: Path, fname: str) -> tuple[list[str], list[str]]:
    """(headings, frontmatter keys) `soul.yaml` asks one file for.

    Read off the manifest rather than a hardcoded table, so a card with its own
    shape is held to its own shape. A manifest that cannot be read at all yields
    nothing: a check that failed *closed* here would refuse every soul edit on
    any vault whose manifest we merely failed to parse.
    """
    try:
        manifest = yaml.safe_load(
            (Path(soul_dir) / "soul.yaml").read_text(encoding="utf-8")) or {}
        fields = manifest.get("fields") or {}
        items = list(fields.values())
    except (OSError, yaml.YAMLError, AttributeError) as exc:
        log.warning("soul.yaml unreadable, skipping the shape check: %s", exc)
        return [], []
    headings: list[str] = []
    keys: list[str] = []
    while items:
        ref = items.pop()
        if isinstance(ref, (list, tuple)):
            items.extend(ref)
        elif isinstance(ref, str) and ref.startswith(fname):
            if "#" in ref:
                headings.append(ref.split("#", 1)[1].strip())
            elif "@" in ref:
                keys.append(ref.split("@", 1)[1].strip())
    return headings, keys


def shape_complaint(soul_dir: Path, fname: str, content: str) -> str:
    """What rewriting `fname` with `content` would break, or "" if nothing.

    `soul.yaml` points *into* these files by heading and by frontmatter key, and
    a reference that stops resolving raises when her prompt is next assembled.
    Hand a model "content is the whole new file" and it writes good prose with
    the headings sanded off — and the character then fails to **start**, which
    is the one outcome a self-edit must never be able to cause, because a
    bricked character cannot be talked to about the edit that bricked her.
    """
    headings, keys = manifest_refs(soul_dir, fname)
    if not headings and not keys:
        return ""
    front, body = parse_md_text(content)
    have = split_sections(body)
    missing = [f"## {h}" for h in headings if h not in have]
    missing += [f"`{k}:` in the frontmatter" for k in keys
                if not str((front or {}).get(k, "")).strip()]
    if not missing:
        return ""
    return (f"{fname} would lose {', '.join(missing)}, which soul.yaml points "
            f"at — read the file first and keep its headings and its "
            f"frontmatter, changing only the prose under them")


#: Where a consumed bootstrap goes to rest (§5.4). One canonical path, so the
#: readers below never have to guess which of several retirements was the last.
RETIRED_BOOTSTRAP = Path("onboarded") / "BOOTSTRAP.done.md"


def retired_cold_open(soul_dir: Path, heading: str = "Cold open") -> str:
    """The cold open of a bootstrap that has already been consumed, or "".

    Read by fixed path rather than through `SoulReader`, on purpose. The reader
    is jailed to plain names in the soul folder and that jail is load-bearing —
    it is what stops a hand-edited manifest walking into a subdirectory. This
    path is a constant in our own code that no card can steer, so it goes
    *around* the jail instead of widening it for everyone.

    She keeps her first message after she has used it: it is still the card's
    `first_mes`, still the thing a stranger who imports her should be greeted
    with, and still hers to edit. Only its role at *runtime* is over.
    """
    path = Path(soul_dir) / RETIRED_BOOTSTRAP
    if not path.is_file():
        return ""
    try:
        _front, body = parse_md(path)
    except OSError:
        return ""
    return split_sections(body).get(heading, "").strip()


class SoulReader:
    """Lazy reader/cache over a soul folder, jailed to it.

    `forbidden` names files this reader refuses by basename (case-folded), and is
    a floor: it is only ever added to, never consulted for permission to widen.
    """

    def __init__(self, folder: Path, *, forbidden: frozenset[str] = frozenset()):
        self.folder = Path(folder)
        self.forbidden = frozenset(name.strip().casefold() for name in forbidden)
        self._front: dict[str, dict] = {}
        self._sections: dict[str, dict[str, str]] = {}
        self._body: dict[str, str] = {}

    def _resolve_path(self, fname: str) -> Path:
        """The one place a name becomes a path. Every refusal happens here."""
        name = fname.strip()
        if not name:
            raise SoulPrivacyError(fname, "empty reference")
        if name != Path(name).name or name in (".", ".."):
            raise SoulPrivacyError(name, "only plain file names may be referenced")
        if name.casefold() in self.forbidden:
            raise SoulPrivacyError(name, "the file is runtime-only and never leaves the Vault")
        path = self.folder / name
        # A symlink out of the soul folder is the interesting attack: the name is
        # plain, the target is not. Compare resolved parents, so `USER.md ->
        # /etc/passwd` and `NOTES.md -> ../../../.env` both land here.
        try:
            resolved = path.resolve()
            base = self.folder.resolve()
        except OSError as exc:
            raise SoulPrivacyError(name, f"cannot resolve: {exc}") from exc
        if resolved.parent != base:
            raise SoulPrivacyError(name, "the file resolves outside the soul folder")
        return path

    def exists(self, fname: str) -> bool:
        """Whether a referenced file is present *and* readable here."""
        try:
            return self._resolve_path(fname).exists()
        except SoulPrivacyError:
            return False

    def _load(self, fname: str) -> None:
        if fname in self._front:
            return
        path = self._resolve_path(fname)
        if not path.exists():
            raise FileNotFoundError(f"soul references missing file: {fname}")
        front, body = parse_md(path)
        self._front[fname] = front
        self._body[fname] = body.strip()
        self._sections[fname] = split_sections(body)

    def front(self, fname: str) -> dict:
        self._load(fname); return self._front[fname]

    def body(self, fname: str) -> str:
        self._load(fname); return self._body[fname]

    def section(self, fname: str, heading: str) -> str:
        self._load(fname)
        secs = self._sections[fname]
        if heading not in secs:
            raise KeyError(f"{fname}: no '## {heading}' section "
                           f"(have: {', '.join(secs) or 'none'})")
        return secs[heading]

    def sections(self, fname: str) -> dict[str, str]:
        self._load(fname); return self._sections[fname]

    def resolve(self, ref: str) -> str:
        """Resolve a 'FILE#Heading' / 'FILE@key' / 'FILE' reference to text."""
        if "#" in ref:
            fname, heading = ref.split("#", 1)
            return self.section(fname.strip(), heading.strip())
        if "@" in ref:
            fname, key = ref.split("@", 1)
            val = self.front(fname.strip()).get(key.strip())
            if val is None:
                raise KeyError(f"{fname}: no frontmatter key '{key}'")
            return str(val)
        return self.body(ref.strip())

    def resolve_field(self, src) -> str:
        if isinstance(src, list):
            return "\n\n".join(self.resolve(r) for r in src)
        return self.resolve(src)

    def resolve_list(self, src) -> list[str]:
        srcs = src if isinstance(src, list) else [src]
        return [self.resolve(r) for r in srcs]

    def referenced_files(self, manifest_fields: dict) -> list[str]:
        """Every file name a `fields:` mapping points at, in manifest order.

        Used by the exporter to report which files fed which card field, and by
        the importer to check a card's soul payload actually satisfies its own
        manifest before writing anything.
        """
        names: list[str] = []
        for source in manifest_fields.values():
            for ref in (source if isinstance(source, list) else [source]):
                if not isinstance(ref, str):
                    continue
                name = re.split(r"[#@]", ref, maxsplit=1)[0].strip()
                if name and name not in names:
                    names.append(name)
        return names
