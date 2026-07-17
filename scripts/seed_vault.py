#!/usr/bin/env python3
"""seed_vault.py — create the Vault from the sibling SOUL (SPEC §5.1, §4.1).

Seed ONCE: persona files + soul.yaml → vault/soul/. The two `runtime_only`
files route to their runtime homes, not soul-prose:

  * USER.md    → vault/soul/USER.md            (the partner model)
  * MEMORY.md  → the memory tier:
        "## What I know that matters"            → memory/semantic/facts.md
        "## Things {{user}} asked me to forget"  → memory/semantic/forgotten.md

Both start empty in a fresh Vault — a card handed to someone else begins the
relationship at zero (→ D-014). Then `git init` the Vault: from here on, every
durable change to her mind is a commit (§6.5).

Usage:  python scripts/seed_vault.py  [--soul ../yuri-soul]  [--vault ./vault]
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from yurios.app import vaultgit  # noqa: E402
from yurios.app.core.soul import parse_md, split_sections  # noqa: E402

VAULT_GITIGNORE = """\
# derived, rebuildable — never committed (§4.1); scripts/reindex.py rebuilds it
memory/index/
"""


def seed(soul_src: Path, vault: Path) -> None:
    soul_src, vault = Path(soul_src), Path(vault)
    manifest_path = soul_src / "soul.yaml"
    if not manifest_path.exists():
        sys.exit(f"no soul.yaml at {soul_src} — is SOUL_SRC right?")
    if (vault / "soul" / "soul.yaml").exists():
        sys.exit(f"{vault} is already seeded — the mind lives in the Vault now; "
                 "delete the folder only if you mean to start the relationship over.")

    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    runtime_only = set(manifest.get("runtime_only", []))

    # persona files + manifest → vault/soul/ (everything except runtime_only)
    soul_dir = vault / "soul"
    soul_dir.mkdir(parents=True, exist_ok=True)
    for md in sorted(soul_src.glob("*.md")):
        if md.name in runtime_only or md.name == "README.md":
            continue
        shutil.copy2(md, soul_dir / md.name)
    shutil.copy2(manifest_path, soul_dir / "soul.yaml")

    # USER.md → the partner model (starts as the empty-relationship template)
    if "USER.md" in runtime_only:
        shutil.copy2(soul_src / "USER.md", soul_dir / "USER.md")

    # MEMORY.md → the memory tier (§5.1): it is runtime memory, not persona
    # prose, so it never lands under soul/
    semantic = vault / "memory" / "semantic"
    semantic.mkdir(parents=True, exist_ok=True)
    facts_body, forgotten_body = "", ""
    if "MEMORY.md" in runtime_only:
        _, body = parse_md(soul_src / "MEMORY.md")
        sections = split_sections(body)
        facts_body = sections.get("What I know that matters", "").strip()
        forgotten_body = sections.get(
            "Things {{user}} asked me to forget", "").strip()
    vaultgit.atomic_write(semantic / "facts.md",
                          "# Facts — what she knows that matters\n\n"
                          f"{facts_body}\n")
    vaultgit.atomic_write(semantic / "forgotten.md",
                          "# The forget-ledger — supersede, not delete (§6.7)\n\n"
                          f"{forgotten_body}\n")

    # the rest of the §4.1 skeleton
    (vault / "memory" / "episodic").mkdir(parents=True, exist_ok=True)
    vaultgit.atomic_write(vault / "memory" / "summary.md", "")
    (vault / "state").mkdir(parents=True, exist_ok=True)
    vaultgit.atomic_write(vault / "state" / "sessions.json",
                          '{\n "sessions": {}\n}')
    vaultgit.atomic_write(vault / ".gitignore", VAULT_GITIGNORE)

    # ONE git repo — the mind (§4.1). Backed up by copying the folder.
    vaultgit.ensure_repo(vault)
    vaultgit.commit(vault, "seed: soul + empty memory (a relationship at zero)")
    print(f"seeded {vault} from {soul_src}")
    print(f"  git -C {vault} log --oneline   ← the diary of how she grows")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--soul", default="./soul-src")   # SOUL (Build #2 is standalone)
    ap.add_argument("--vault", default="./vault")
    args = ap.parse_args()
    seed(Path(args.soul), Path(args.vault))
