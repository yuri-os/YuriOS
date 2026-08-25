"""Vault write + git helpers (SPEC §2.1, B1 §6.5).

Every durable change to the mind reaches a commit — `git -C vault log` reads as
the diary of how she grew, one entry a day rather than one per turn (§2.1).
Vault writes are atomic (write-temp-then-rename) and land immediately, so a
crash leaves whole files, never a half-written one; what waits out the day is
the history entry, not the data (→ ch. 19, crash recovery). `memory/index/` is
gitignored and excluded.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

VAULT_GITIGNORE = """\
# derived, rebuildable — never committed (§4.1); scripts/reindex.py rebuilds it
memory/index/

# scheduler bookkeeping, not memory: a bus cursor and a heartbeat timestamp that
# change on every tick, and the activity ladder's position. Committing these
# turns `git log` into one entry per heartbeat and buries the diary in it.
# Everything else under state/ (sessions, budget, quarantine, dream progress)
# is durable and stays versioned.
state/engine.json
state/activity.json
state/activity.jsonl
"""


def atomic_write(path: Path, text: str) -> None:
    """Write-temp-then-rename in the same directory (rename is atomic on POSIX)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def atomic_append(path: Path, text: str) -> None:
    """Append via read + atomic rewrite — the journal and ledgers stay whole on crash."""
    path = Path(path)
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    atomic_write(path, current + text)


def _git_env(vault: Path) -> dict | None:
    """Git refuses to touch a repo owned by a different uid ("dubious
    ownership") — routine on NTFS/exFAT mounts where every file reads as
    root-owned. The Vault is a repo *we* create for the user, and it must stay
    a folder they can copy anywhere (§4.2), so instead of asking them to edit
    their global config per-path, shim a global config that (a) includes their
    real one and (b) marks this Vault safe. The shim lives under the user's
    XDG state dir — git also distrusts a config file that is itself
    foreign-owned, so it cannot live inside the Vault on such a mount."""
    if not (Path(vault) / ".git").is_dir():
        return None
    vault_abs = Path(vault).resolve()
    state = Path(os.environ.get("XDG_STATE_HOME",
                                Path.home() / ".local" / "state")) / "minimum-viable-waifu"
    state.mkdir(parents=True, exist_ok=True)
    tag = hashlib.md5(str(vault_abs).encode()).hexdigest()[:12]
    shim = state / f"gitconfig-{tag}"
    if not shim.exists():
        home = Path.home()
        xdg = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
        shim.write_text(
            "[include]\n"
            f"\tpath = {home / '.gitconfig'}\n"          # missing files are
            f"\tpath = {xdg / 'git' / 'config'}\n"       # silently skipped
            "[safe]\n"
            f"\tdirectory = {vault_abs}\n",
            encoding="utf-8")
    return os.environ | {"GIT_CONFIG_GLOBAL": str(shim)}


def _git(vault: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(vault), *args],
                          capture_output=True, text=True, env=_git_env(vault))


def ensure_repo(vault: Path) -> None:
    """`git init` the Vault if it isn't one yet (seed step, §4.1)."""
    if not (Path(vault) / ".git").exists():
        _git(vault, "init", "-q")
        # the Vault is the *user's* repo; give it an identity so commits work anywhere
        _git(vault, "config", "user.name", "yurios-vault")
        _git(vault, "config", "user.email", "vault@localhost")


#: How long the Vault waits between commits (SPEC §2.1). A commit per turn and
#: per dirty tick made `git log` unreadable as the thing it is for — the diary
#: of how she grew — because a day of ordinary conversation buried the
#: two entries that mattered under three hundred that did not. So the Vault
#: takes one snapshot a day: everything that changed since the last one, in one
#: commit, under the message of whatever finally tripped the window.
#:
#: Nothing is at risk in between. Vault writes are atomic and land immediately;
#: what waits is only the *history entry*, and `git add -A` on the far side
#: picks up every change since regardless of which write asked for it.
COMMIT_INTERVAL_S = 24 * 60 * 60


def head_at(vault: Path) -> tuple[str | None, int]:
    """HEAD's sha and its commit time (epoch seconds), or `(None, 0)` on a repo
    with no commits yet. One subprocess, because the throttle below runs on
    every turn and the answer is two fields of the same log line."""
    result = _git(vault, "log", "-1", "--format=%H %ct")
    if result.returncode != 0:
        return None, 0
    sha, _, when = result.stdout.strip().partition(" ")
    return (sha or None), (int(when) if when.isdigit() else 0)


def commit(vault: Path, message: str) -> str | None:
    """`git add -A && git commit`, at most once per `COMMIT_INTERVAL_S` (§2.1).

    Returns the resulting HEAD sha, or None if there is nothing to return one
    for. Never raises on 'nothing to commit' — an uneventful turn is not an
    error, and neither is one that arrives inside the window.

    The window is measured against the Vault's own HEAD rather than a timer in
    this process, so it survives a restart, is shared by every writer of this
    Vault, and cannot be reset by bouncing the daemon. A Vault with no commits
    yet — a fresh seed, a freshly imported card — has no window and commits at
    once; that first entry is what starts the clock.
    """
    sha, when = head_at(vault)
    if sha is not None and (time.time() - when) < COMMIT_INTERVAL_S:
        return sha                      # inside the window: the writes stand, the
                                        # history entry waits for the next one
    _git(vault, "add", "-A")
    staged = _git(vault, "diff", "--cached", "--quiet")
    if staged.returncode == 0:  # nothing staged
        return sha
    result = _git(vault, "commit", "-q", "-m", message)
    if result.returncode != 0:
        raise RuntimeError(f"vault commit failed: {result.stderr.strip()}")
    return head(vault)


def head(vault: Path) -> str | None:
    """Current Vault HEAD sha (surfaced by /api/health as `vault_head`, §10)."""
    result = _git(vault, "rev-parse", "HEAD")
    return result.stdout.strip() if result.returncode == 0 else None


def mv(vault: Path, src: str, dst: str, *, force: bool = False) -> None:
    """Move a file inside the Vault (used to retire BOOTSTRAP.md, §5.4).

    `git mv` first, because that is the operation that reads best afterwards.
    With `force`, git's refusals are treated as bookkeeping rather than as a
    verdict on the move, and the rename happens anyway — the commit that follows
    stages everything (`add -A`), so history records it either way.

    Retirement passes `force`, because both refusals are reachable and neither
    means "don't move this": the destination is occupied when a bootstrap was
    restored to re-run onboarding (a supported move — nothing is lost, the older
    copy stays in `git log`), and the source is *not under version control* for
    the moments between restoring that file by hand and the next commit. Without
    this, either one leaves a greeting trying and failing to retire, on every
    arrival, for good.
    """
    source, target = Path(vault, src), Path(vault, dst)
    if not source.exists():
        raise RuntimeError(f"vault mv failed: {src} is not there to move")
    target.parent.mkdir(parents=True, exist_ok=True)
    result = _git(vault, "mv", *(["-f"] if force else []), src, dst)
    if result.returncode == 0:
        return
    if not force:
        raise RuntimeError(f"vault mv failed: {result.stderr.strip()}")
    source.replace(target)


def log(vault: Path, n: int = 20) -> list[str]:
    """Last n commit subjects — the diary of how she grew (§4.2)."""
    result = _git(vault, "log", f"-{n}", "--pretty=%h %s")
    return result.stdout.strip().splitlines() if result.returncode == 0 else []


# --- reading the diary back (the mind debug page, SPEC §24.3) -----------------
# One commit per dirty tick and one per turn means `git log` *is* the record of
# every durable change she ever made. These read it structurally so a page can
# show "what changed in USER.md, and when" instead of a list of subject lines.

#: A commit-ish that may be handed to git. Checked before it reaches argv, so a
#: value shaped like a flag can never be read as one.
SHA = re.compile(r"^[0-9a-fA-F]{4,40}$")

_RS, _US = "\x1e", "\x1f"           # record / field separators, absent from git output
_FORMAT = f"{_RS}%H{_US}%h{_US}%at{_US}%an{_US}%s"


def is_rev(value: str) -> bool:
    """Is this something we are willing to pass to git as a revision?"""
    return bool(value) and (value == "HEAD" or bool(SHA.match(value)))


def in_vault(vault: Path, rel: str) -> Path | None:
    """Resolve a caller-supplied path inside the Vault, or None if it escapes.
    The Vault is the jail; `..` and absolute paths are refusals, not surprises
    (the same rule MindVault._check enforces on the write side)."""
    root = Path(vault).resolve()
    try:
        target = (root / (rel or "")).resolve()
    except OSError:
        return None
    if target != root and root not in target.parents:
        return None
    if ".git" in target.relative_to(root).parts:
        return None                      # the plumbing is not part of her mind
    return target


def _parse_log(out: str) -> list[dict]:
    records = []
    for chunk in out.split(_RS):
        head, _, body = chunk.partition("\n")
        if not head.strip():
            continue
        parts = head.split(_US, 4)
        if len(parts) != 5:
            continue
        sha, short, at, author, subject = parts
        files, insertions, deletions = [], 0, 0
        for line in body.splitlines():
            cols = line.split("\t")
            if len(cols) != 3:
                continue
            added, removed, path = cols
            # a binary file reports "-" for both, which is a fact worth showing
            a = int(added) if added.isdigit() else None
            d = int(removed) if removed.isdigit() else None
            insertions += a or 0
            deletions += d or 0
            files.append({"path": path, "insertions": a, "deletions": d,
                          "binary": a is None})
        records.append({"sha": sha, "short": short, "at": int(at or 0),
                        "author": author, "subject": subject, "files": files,
                        "insertions": insertions, "deletions": deletions})
    return records


def log_records(vault: Path, *, skip: int = 0, limit: int = 25,
                rev: str | None = None, path: str | None = None) -> list[dict]:
    """Structured commits, newest first, with per-file line counts."""
    args = ["log", f"--skip={max(0, skip)}", f"-{max(1, limit)}",
            "--no-color", f"--pretty=format:{_FORMAT}", "--numstat"]
    if rev:
        args.append(rev)
    if path:
        args += ["--follow", "--", path]
    result = _git(vault, *args)
    return _parse_log(result.stdout) if result.returncode == 0 else []


def count_commits(vault: Path, *, path: str | None = None) -> int:
    args = ["rev-list", "--count", "HEAD"]
    if path:
        args += ["--", path]
    result = _git(vault, *args)
    try:
        return int(result.stdout.strip())
    except ValueError:
        return 0


def show(vault: Path, sha: str, *, max_bytes: int = 400_000) -> dict | None:
    """One commit: its metadata, its per-file counts, and its patch.

    The patch is capped because a knowledge drop commits the extracted text of
    whatever you handed her, and a page should not have to render a megabyte of
    it to tell you that is what happened. The counts come back either way, so
    the caller can say how much it is not showing."""
    if not is_rev(sha):
        return None
    records = log_records(vault, limit=1, rev=sha)
    if not records:
        return None
    patch = _git(vault, "show", "--no-color", "-M", "--format=", "--patch", sha, "--")
    diff = patch.stdout if patch.returncode == 0 else ""
    truncated = len(diff) > max_bytes
    return {**records[0], "diff": diff[:max_bytes], "truncated": truncated}


def read_at(vault: Path, rel: str, *, rev: str | None = None,
            max_bytes: int = 512_000) -> dict | None:
    """One file, as it is now (`rev=None`) or as it was at a commit.

    Reads the working tree rather than the index when no revision is asked for,
    because the interesting files — `state/*.json`, the recall index — are
    gitignored on purpose and would otherwise be invisible."""
    if rev is not None and not is_rev(rev):
        return None
    if rev is None:
        target = in_vault(vault, rel)
        if target is None or not target.is_file():
            return None
        raw = target.read_text(encoding="utf-8", errors="replace")
    else:
        if in_vault(vault, rel) is None:
            return None
        result = _git(vault, "show", f"{rev}:{rel}")
        if result.returncode != 0:
            return None
        raw = result.stdout
    return {"path": rel, "rev": rev, "text": raw[:max_bytes],
            "truncated": len(raw) > max_bytes, "bytes": len(raw)}


def tree(vault: Path, rel: str = "") -> list[dict] | None:
    """One directory of the Vault. Walks the filesystem, not the index, so the
    gitignored working state (`state/activity.json`, the chunk db) is listed —
    on a debug page those are exactly what you came to look at."""
    target = in_vault(vault, rel)
    if target is None or not target.is_dir():
        return None
    root = Path(vault).resolve()
    out = []
    for child in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name)):
        if child.name == ".git":
            continue
        stat = child.stat()
        out.append({"name": child.name,
                    "path": str(child.relative_to(root)),
                    "dir": child.is_dir(),
                    "bytes": stat.st_size if child.is_file() else None,
                    "mtime": stat.st_mtime})
    return out
