#!/usr/bin/env python3
"""reindex.py — rebuild `vault/memory/index/` from the .md files alone (SPEC §4.3).

The markdown is authoritative; the index is a cache. Run this after hand-editing
journals, on a fresh clone of a Vault, or whenever the two disagree. (The runtime
also rebuilds automatically when the configured embedder no longer matches the one
that built the index — see app/main.py.) The rebuild logic lives in
app/memory/reindex.py so the runtime and this CLI share one implementation.

Usage:  python scripts/reindex.py  [--vault ./vault]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from yurios.app.memory.reindex import reindex  # noqa: E402

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", default="./vault")
    args = ap.parse_args()
    count = reindex(Path(args.vault))
    print(f"rebuilt index: {count} chunks from the .md files")
