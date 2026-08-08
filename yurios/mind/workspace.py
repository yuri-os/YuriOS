"""The desk (SPEC §34) — a corner of the Vault she may write in without asking.

Every other write path in the mind is narrow on purpose. `memory/` is grown by
DREAM, `world/` by SENSE, `goals.md` by the goal store, `soul/` only through the
gated self-edit flow. That is the right shape for the things she *is*, and the
wrong shape for the things she is *doing*: a half-finished comparison of three
paddle-board brands, the notes behind a research question, a draft of something
she hasn't decided to say yet. Those need a surface with no schema and no
ceremony, or they don't get written down at all.

`vault/workspace/` is that surface, and `vault/skills/` is the same primitive
pointed at instructions instead of notes. Both live inside the Vault, so both
move when you copy her folder — but only one of them is versioned, and the split
is the point:

  * **The desk is not git-tracked.** Scratch churns. A note rewritten four times
    while she works through something is four commits of a file nobody will ever
    read a diff of, and the Vault's `git log` is supposed to be the diary of how
    she grew — putting working drafts in it buries the entries that matter under
    the ones that don't. `workspace/.gitignore` says so from inside the folder.
  * **Skills are.** A skill is a durable statement about how she does something.
    Changing one is exactly the kind of change worth being able to read, revert
    and carry, and they are written rarely enough that the history stays legible.

The sandbox is the whole design, and it is deliberately dull:

  * **Relative, inside, and no dotfiles.** `resolve()` refuses an absolute path,
    a `..` that climbs out, and any component beginning with `.` — the last one
    because `.git/`, `.gitignore` and `.env` all live within reach of a root
    that is otherwise hers, and none of them are notes.
  * **Symlinks are resolved before the check, not after.** A link planted inside
    the desk that points at `soul/CONSTITUTION.md` is caught by the same test as
    a `../` would be.
  * **Bounded.** One file, the whole tree, and the file count are capped. She is
    not adversarial, but a loop that appends every tick is not adversarial
    either and fills a disk just as well.

Nothing here executes, imports, or interprets what it stores — the desk holds
inert text, and the future code harness gets its own workspace outside the
Vault precisely so that "she can write here" and "this can run" never become the
same sentence (SPEC §28's workshop).

This module holds no `MindVault` and no `Clock`. It is imported by the tick loop
*and* by the tool server, which is a different process with no runtime at all,
so it must work knowing nothing but a path.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from yurios.app.vaultgit import atomic_write

log = logging.getLogger("mind.workspace")

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

#: Written *inside* `workspace/`, for the reason `KnowledgeStore.INDEX_GITIGNORE`
#: gives at length: a Vault's own `.gitignore` is written once at seed time and
#: never refreshed, so a line added there today protects nobody's existing vault.
#: A `.gitignore` inside the directory it describes needs no migration to arrive.
#:
#: `*` covers this file too, which is deliberate and is what the knowledge index
#: already does — git keeps honouring a `.gitignore` it is not tracking.
WORKSPACE_GITIGNORE = (
    "# Her desk is scratch, and scratch churns (§34.1). A draft rewritten four\n"
    "# times while she works something out is four commits of a diff nobody will\n"
    "# read, and the Vault's git log is meant to be the diary of how she grew.\n"
    "# The files are still here, still hers, and still travel when you copy the\n"
    "# folder — they are just not history. Skills next door ARE versioned.\n"
    "*\n")

#: Per-file and whole-tree ceilings. Generous for prose, small enough that a
#: runaway append is a caught error rather than a full disk.
MAX_FILE_BYTES = 256_000
MAX_TREE_BYTES = 32_000_000
MAX_FILES = 2_000

#: The tool names `world/tools/server.py` advertises for these two stores.
#:
#: They live here rather than there because three modules in two *processes*
#: have to agree on them and none of them can import the server: the host builds
#: the guard's rate buckets from these (world/main.py), and `_realise` decides
#: from them whether a call just changed the Vault (world/brain.py). A name that
#: drifts out of one of those lists fails silently — an unrationed hand, or a
#: write that never gets committed — so there is one list.
DESK_TOOLS = ("list_notes", "read_note", "write_note", "append_note",
              "delete_note")
SKILL_TOOLS = ("read_skill", "write_skill", "delete_skill")
#: The subset that changes files, and so dirties the Vault.
DESK_WRITE_TOOLS = ("write_note", "append_note", "delete_note",
                    "write_skill", "delete_skill")


class OutsideTheDesk(PermissionError):
    """A path that would land outside the workspace root."""

    def __init__(self, rel: str, reason: str):
        self.rel, self.reason = rel, reason
        super().__init__(f"{rel!r} is not a place on the desk: {reason}")


class DeskFull(OSError):
    """A write that would breach one of the ceilings above."""


@dataclass
class Entry:
    """One file on the desk, as the tools and the debug page report it."""
    path: str            # relative to the root, forward slashes, always
    bytes: int
    mtime: float
    is_dir: bool = False

    def as_dict(self) -> dict:
        return {"path": self.path, "bytes": self.bytes,
                "mtime": self.mtime, "dir": self.is_dir}


class Workspace:
    """A sandboxed directory tree, addressed by relative path.

    The root is created on construction — best-effort, like the knowledge
    shelf's: a Vault on a read-only mount is a strange configuration, not a
    reason to refuse to build the mind.
    """

    #: Dropped into the root on construction when set. The desk sets it (scratch
    #: is not history); `SkillStore` clears it, because a skill *is*.
    GITIGNORE: str | None = WORKSPACE_GITIGNORE

    def __init__(self, root: Path, *, max_file_bytes: int = MAX_FILE_BYTES,
                 max_tree_bytes: int = MAX_TREE_BYTES, max_files: int = MAX_FILES):
        self.root = Path(root)
        self.max_file_bytes = max_file_bytes
        self.max_tree_bytes = max_tree_bytes
        self.max_files = max_files
        self._ensure()

    def _ensure(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            ignore = self.root / ".gitignore"
            if self.GITIGNORE and not ignore.exists():
                ignore.write_text(self.GITIGNORE, encoding="utf-8")
        except OSError:
            log.warning("couldn't create the workspace at %s", self.root,
                        exc_info=True)

    # ------------------------------------------------------------------ paths

    def resolve(self, rel: str) -> Path:
        """The absolute path for a desk-relative one, or raise.

        Every read and write in this class goes through here, including the
        ones that only *list*, so there is exactly one place the sandbox is
        enforced and no caller can accidentally skip it.
        """
        raw = str(rel or "").strip().replace("\\", "/")
        if not raw:
            raise OutsideTheDesk(rel, "no path given")
        if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
            raise OutsideTheDesk(rel, "must be relative to the workspace")
        parts = [p for p in raw.split("/") if p not in ("", ".")]
        if not parts:
            raise OutsideTheDesk(rel, "no path given")
        for part in parts:
            if part == "..":
                raise OutsideTheDesk(rel, "no climbing out with '..'")
            if part.startswith("."):
                raise OutsideTheDesk(rel, f"no dotfiles or dotdirs ({part})")
        # resolve() follows symlinks, so a link planted inside the desk that
        # points out of it fails the containment test below rather than
        # sneaking past a purely textual check
        root = self.root.resolve()
        target = (root / "/".join(parts)).resolve()
        if target != root and root not in target.parents:
            raise OutsideTheDesk(rel, "resolves outside the workspace")
        return target

    def relative(self, path: Path) -> str:
        return Path(path).resolve().relative_to(self.root.resolve()).as_posix()

    # ------------------------------------------------------------------- read

    def exists(self, rel: str) -> bool:
        try:
            return self.resolve(rel).is_file()
        except OutsideTheDesk:
            return False

    def read(self, rel: str, default: str | None = None) -> str:
        path = self.resolve(rel)
        if not path.is_file():
            if default is not None:
                return default
            raise FileNotFoundError(f"nothing on the desk at {rel}")
        return path.read_text(encoding="utf-8", errors="replace")

    def list(self, sub: str = "", *, recursive: bool = True) -> list[Entry]:
        """Everything under `sub` (the whole desk by default), sorted by path.

        Dotfiles are skipped rather than refused: `resolve()` won't hand one
        out, so listing one would advertise a path no other method will open.
        """
        base = self.resolve(sub) if sub else self.root
        if not base.is_dir():
            return []
        walk = base.rglob("*") if recursive else base.iterdir()
        out: list[Entry] = []
        for path in walk:
            try:
                rel = self.relative(path)
            except ValueError:
                continue                    # a symlink out; not ours to report
            if any(part.startswith(".") for part in Path(rel).parts):
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            out.append(Entry(path=rel, bytes=0 if path.is_dir() else st.st_size,
                             mtime=st.st_mtime, is_dir=path.is_dir()))
        out.sort(key=lambda e: e.path)
        return out

    def usage(self) -> tuple[int, int]:
        """(files, bytes) on the desk right now — what the ceilings measure."""
        files = [e for e in self.list() if not e.is_dir]
        return len(files), sum(e.bytes for e in files)

    # ------------------------------------------------------------------ write

    def _check_room(self, path: Path, incoming: int) -> None:
        if incoming > self.max_file_bytes:
            raise DeskFull(f"that file is {incoming} bytes; the limit is "
                           f"{self.max_file_bytes}")
        files, total = self.usage()
        existing = path.stat().st_size if path.is_file() else 0
        if not path.is_file() and files + 1 > self.max_files:
            raise DeskFull(f"the workspace already holds {files} files "
                           f"(limit {self.max_files}) — clear something first")
        if total - existing + incoming > self.max_tree_bytes:
            raise DeskFull(f"the workspace is full ({total} bytes, limit "
                           f"{self.max_tree_bytes}) — clear something first")

    def write(self, rel: str, text: str) -> Entry:
        path = self.resolve(rel)
        data = text if text.endswith("\n") or not text else text + "\n"
        self._check_room(path, len(data.encode("utf-8")))
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, data)             # the Vault-wide write discipline
        st = path.stat()
        return Entry(path=self.relative(path), bytes=st.st_size, mtime=st.st_mtime)

    def append(self, rel: str, text: str) -> Entry:
        path = self.resolve(rel)
        prior = path.read_text(encoding="utf-8") if path.is_file() else ""
        if prior and not prior.endswith("\n"):
            prior += "\n"
        return self.write(rel, prior + text)

    def delete(self, rel: str) -> bool:
        """Remove one file. Directories are left alone — an empty folder costs
        nothing and "delete" that can recurse is a different, scarier verb."""
        path = self.resolve(rel)
        if not path.is_file():
            return False
        path.unlink()
        return True

    # --------------------------------------------------------------- for DREAM

    def digest(self, *, limit: int = 40) -> str:
        """A compact index of the desk for a prompt: path, size, last touched.

        Dream jobs and the prompt's workspace slot both want "what is on the
        desk" without any of the contents; this is that, cheap enough to build
        every time it's asked for.
        """
        entries = [e for e in self.list() if not e.is_dir]
        if not entries:
            return ""
        entries.sort(key=lambda e: e.mtime, reverse=True)
        lines = [f"- {e.path} ({e.bytes}b)" for e in entries[:limit]]
        if len(entries) > limit:
            lines.append(f"- …and {len(entries) - limit} more")
        return "\n".join(lines)

    def gather(self, paths: list[str], *, max_chars: int = 6000) -> str:
        """Several desk files concatenated under their names, capped whole.

        The cap is on the *result*, not per file, because the caller is filling
        one prompt slot and cares about that total; files are taken in the order
        given, and the first one that doesn't fit ends the gather.
        """
        out: list[str] = []
        spent = 0
        for rel in paths:
            try:
                text = self.read(rel)
            except (OutsideTheDesk, FileNotFoundError, OSError):
                continue
            block = f"--- {rel} ---\n{text.strip()}\n"
            if spent + len(block) > max_chars:
                break
            out.append(block)
            spent += len(block)
        return "\n".join(out)

    def tokens(self) -> int:
        """Rough size of the whole desk, for the budget's benefit — the same
        chars/4 estimate `estimate_tokens` makes, without building the string
        it would need to measure."""
        return max(0, self.usage()[1] // 4)


# --------------------------------------------------------------------- skills


@dataclass
class Skill:
    """One thing she knows how to do, as a folder with a `SKILL.md` in it."""
    name: str
    description: str
    body: str
    path: str                       # "<name>/SKILL.md", relative to skills/
    author: str = ""                # "you" | "her" | whatever the file says
    enabled: bool = True
    files: list[str] = None         # supporting files beside the SKILL.md

    def __post_init__(self):
        self.files = self.files or []

    def as_dict(self, *, body: bool = False) -> dict:
        out = {"name": self.name, "description": self.description,
               "path": self.path, "author": self.author,
               "enabled": self.enabled, "files": self.files}
        if body:
            out["body"] = self.body
        return out


SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,48}$")

SKILL_TEMPLATE = """---
name: {name}
description: {description}
author: {author}
enabled: true
---

{body}
"""


class SkillStore(Workspace):
    """`vault/skills/` — instructions she can be handed, or write herself.

    A skill is a folder with a `SKILL.md`: YAML frontmatter naming it and, more
    importantly, *describing when to reach for it*, then a body of instructions.
    The two halves are loaded at different times and that is the whole point of
    the format — every turn carries the catalog (name + description, a line
    each, cheap enough to always send), and the body is pulled only once she has
    decided this is the skill the moment calls for. A store of twenty skills
    costs twenty lines of context until one of them is actually used.

    Supporting files sit beside the `SKILL.md` in the same folder and are hers
    to read through the desk tools; nothing here loads them implicitly.

    Unlike the desk, skills are **versioned**: a skill is a durable statement
    about how she does something, changing one is exactly the kind of change
    worth being able to read back and revert, and they are written rarely enough
    that the history stays legible.
    """

    GITIGNORE = None

    # ------------------------------------------------------------------- read

    def names(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(
            p.name for p in self.root.iterdir()
            if p.is_dir() and not p.name.startswith(".")
            and (p / "SKILL.md").is_file())

    def get(self, name: str) -> Skill | None:
        rel = f"{name}/SKILL.md"
        try:
            text = self.read(rel)
        except (OutsideTheDesk, FileNotFoundError, OSError):
            return None
        front, body = self._split(text)
        folder = self.resolve(name)
        files = sorted(p.name for p in folder.iterdir()
                       if p.is_file() and p.name != "SKILL.md"
                       and not p.name.startswith("."))
        return Skill(
            name=str(front.get("name") or name),
            description=str(front.get("description") or "").strip(),
            body=body.strip(), path=rel,
            author=str(front.get("author") or ""),
            enabled=front.get("enabled", True) is not False,
            files=files)

    def all(self) -> list[Skill]:
        found = (self.get(n) for n in self.names())
        return [s for s in found if s is not None]

    @staticmethod
    def _split(text: str) -> tuple[dict, str]:
        m = FRONTMATTER_RE.match(text)
        if not m:
            return {}, text
        try:
            front = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            log.warning("a SKILL.md has unreadable frontmatter; treating it as "
                        "bodyless prose")
            return {}, text
        return (front if isinstance(front, dict) else {}), text[m.end():]

    # ---------------------------------------------------------------- catalog

    def catalog(self, *, limit: int = 40) -> str:
        """The always-on half: one line per enabled skill, for the prompt.

        Returns "" when there are none, so the assembler can drop the whole
        block rather than send a heading with nothing under it.
        """
        lines = [f"- {s.name} — {s.description}" for s in self.all()
                 if s.enabled and s.description][:limit]
        return "\n".join(lines)

    # ------------------------------------------------------------------ write

    def save(self, name: str, *, description: str, body: str,
             author: str = "her") -> Skill:
        """Create or replace a skill. The name is the folder, so it is checked
        the way a folder must be — the desk sandbox would catch a `../` but not
        a name with a space in it, and a skill she cannot address by name later
        is worse than a refused write now."""
        name = (name or "").strip().lower().replace(" ", "-")
        if not SKILL_NAME_RE.match(name):
            raise ValueError(
                "a skill name is lowercase letters, digits and hyphens "
                f"(1-49 chars); {name!r} isn't one")
        if not (description or "").strip():
            raise ValueError("a skill needs a description — it is the only "
                             "part she reads before deciding to open it")
        existing = self.get(name)
        text = SKILL_TEMPLATE.format(
            name=name, description=" ".join(description.split()),
            author=author or (existing.author if existing else "her"),
            body=body.strip())
        self.write(f"{name}/SKILL.md", text)
        return self.get(name)

    def remove(self, name: str) -> bool:
        """Delete a skill folder and everything in it. Unlike `Workspace.delete`
        this one recurses, because a skill *is* its folder and leaving the
        supporting files behind would leave a half-skill nothing can name."""
        try:
            folder = self.resolve(name)
        except OutsideTheDesk:
            return False
        if not folder.is_dir():
            return False
        for path in sorted(folder.rglob("*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
        folder.rmdir()
        return True
