"""Portable vault seeding must survive Git's own config parser."""
from pathlib import Path, PureWindowsPath
import subprocess

from yurios.app import vaultgit


def test_windows_config_path_uses_git_compatible_separators():
    assert vaultgit._config_path(PureWindowsPath(r"C:\Users\a b\vault#1")) == (
        '"C:/Users/a b/vault#1"')


def test_git_parses_paths_and_repairs_an_old_shim(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config # home"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "owner # home"))
    vault = tmp_path / "vault # one"
    vault.mkdir()
    vaultgit.ensure_repo(vault)
    env = vaultgit._git_env(vault)
    shim = Path(env["GIT_CONFIG_GLOBAL"])
    shim.write_text('[include]\npath = C:\\Users\\broken\n', encoding="utf-8")
    env = vaultgit._git_env(vault)
    parsed = subprocess.run(
        ["git", "config", "--file", str(shim), "--get", "safe.directory"],
        capture_output=True, text=True, check=True, env=env)
    assert parsed.stdout.strip() == vault.resolve().as_posix()
    vaultgit.atomic_write(vault / "memory.md", "A durable shared event.\n")
    assert vaultgit.commit(vault, "seed")
    assert vaultgit.read_at(vault, "memory.md", rev="HEAD")["text"] == (
        "A durable shared event.\n")
