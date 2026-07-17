"""Vault write + git helpers (SPEC §6.5).

Every durable change to the mind is a commit — `git -C vault log` reads as the
diary of how she grew (§4.2). Vault writes are atomic (write-temp-then-rename)
so a crash leaves the last *commit* intact, never a half-written file
(→ ch. 19, crash recovery). `memory/index/` is gitignored and excluded.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from pathlib import Path


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


def commit(vault: Path, message: str) -> str | None:
    """`git add -A && git commit` (§6.5). Returns the new HEAD sha, or None if
    nothing changed. Never raises on 'nothing to commit' — an uneventful turn
    is not an error."""
    _git(vault, "add", "-A")
    staged = _git(vault, "diff", "--cached", "--quiet")
    if staged.returncode == 0:  # nothing staged
        return head(vault)
    result = _git(vault, "commit", "-q", "-m", message)
    if result.returncode != 0:
        raise RuntimeError(f"vault commit failed: {result.stderr.strip()}")
    return head(vault)


def head(vault: Path) -> str | None:
    """Current Vault HEAD sha (surfaced by /api/health as `vault_head`, §10)."""
    result = _git(vault, "rev-parse", "HEAD")
    return result.stdout.strip() if result.returncode == 0 else None


def mv(vault: Path, src: str, dst: str) -> None:
    """`git mv` inside the Vault (used to retire BOOTSTRAP.md, §5.4)."""
    Path(vault, dst).parent.mkdir(parents=True, exist_ok=True)
    result = _git(vault, "mv", src, dst)
    if result.returncode != 0:
        raise RuntimeError(f"vault mv failed: {result.stderr.strip()}")


def log(vault: Path, n: int = 20) -> list[str]:
    """Last n commit subjects — the diary of how she grew (§4.2)."""
    result = _git(vault, "log", f"-{n}", "--pretty=%h %s")
    return result.stdout.strip().splitlines() if result.returncode == 0 else []
