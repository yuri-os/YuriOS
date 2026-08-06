"""Read-only Vault git, for the parts of `characters/` that need history.

`yurios/app/vaultgit.py` is the write side and belongs to the brain; it also
imports nothing from here, and this package must not import from `yurios.app`
(it is the leaf storage core). What the exporter and the studio need is only the
read side — the head that names the revision a card shipped from, the first
commit that dates a character's life, and the per-file log that lets the studio
say "she wrote this line herself, on the 14th".

Everything degrades to "no history" rather than raising: a Vault with no git
(`git` missing from PATH, an import that ran with `initialize_git=False`) is a
working Vault, and an export must never fail over a missing changelog.
"""
from __future__ import annotations

import datetime
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Commit:
    sha: str
    subject: str
    when: datetime.datetime

    @property
    def author(self) -> str:
        """Who moved the pen, read off the commit subject.

        The vault's writers all stamp a prefix: `vault:` for the seed and the
        memory tier, `selfedit:`/`mind:` for her own approved edits (§23),
        `studio:`/`user:` for yours. Anything else is history from before the
        convention and reads as unknown.
        """
        head = self.subject.split(":", 1)[0].strip().casefold() if ":" in self.subject else ""
        if head in ("selfedit", "mind", "dream"):
            return "her"
        if head in ("studio", "user"):
            return "you"
        if head == "vault":
            return "seed" if "import" in self.subject or "create" in self.subject else "runtime"
        return "unknown"


def _git(vault: Path, *args: str) -> str | None:
    """Run one read-only git command in *vault*; ``None`` on any failure."""
    binary = shutil.which("git")
    if binary is None or not (Path(vault) / ".git").exists():
        return None
    try:
        result = subprocess.run(
            [binary, "-c", f"safe.directory={Path(vault).resolve()}",
             "-C", str(vault), *args],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def head(vault: Path) -> str | None:
    """The short SHA a card is being cut from."""
    return _git(vault, "rev-parse", "--short", "HEAD")


def _parse(line: str) -> Commit | None:
    sha, _, rest = line.partition("\x1f")
    stamp, _, subject = rest.partition("\x1f")
    if not sha or not stamp:
        return None
    try:
        when = datetime.datetime.fromtimestamp(int(stamp), datetime.timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None
    return Commit(sha=sha, subject=subject, when=when)


def log(vault: Path, *paths: str, limit: int = 50) -> list[Commit]:
    """Commits touching *paths* (or the whole Vault), newest first."""
    args = ["log", f"-{max(1, limit)}", "--pretty=%h%x1f%at%x1f%s"]
    if paths:
        args += ["--follow"] if len(paths) == 1 else []
        args += ["--", *paths]
    out = _git(vault, *args)
    if not out:
        return []
    return [commit for commit in (_parse(line) for line in out.splitlines()) if commit]


def commit_counts(vault: Path, *paths: str, limit: int = 500) -> dict[str, int]:
    """How many of the last *limit* commits touched each path, in ONE git call.

    The batched answer to "which of these files has ever been edited". Asking it
    per file is a subprocess per soul file on every export *and* every preview of
    one, which is a dozen `git log` spawns to render a page.

    Paths come back exactly as git prints them — repo-relative — so callers
    compare against the same prefix they passed. No `--follow`: it is
    incompatible with multiple paths, and a rename shows up here as two names
    with a commit each, which answers the question just as well.
    """
    args = ["log", f"-{max(1, limit)}", "--name-only", "--pretty=format:%x1e"]
    if paths:
        args += ["--", *paths]
    out = _git(vault, *args)
    if not out:
        return {}
    counts: dict[str, int] = {}
    for chunk in out.split("\x1e"):
        for name in {line.strip() for line in chunk.splitlines() if line.strip()}:
            counts[name] = counts.get(name, 0) + 1
    return counts


def first_commit(vault: Path) -> Commit | None:
    """The commit the Vault begins at — a character's date of birth."""
    out = _git(vault, "log", "--reverse", "--pretty=%h%x1f%at%x1f%s")
    if not out:
        return None
    return _parse(out.splitlines()[0])


def count_commits(vault: Path) -> int:
    out = _git(vault, "rev-list", "--count", "HEAD")
    try:
        return int(out) if out else 0
    except ValueError:
        return 0
