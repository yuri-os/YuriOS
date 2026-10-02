#!/usr/bin/env python3
"""export_corpus.py — her corpus with your ratings joined in (SPEC §37.4).

Every reply she makes is one record in `corpus/turns.jsonl`; every 👍/👎 you
press is one line in `corpus/ratings.jsonl`, keyed by that record's id. This
joins the two (the last rating on a turn wins; a taken-back one counts as
none) and writes JSONL — the training asset the corpus exists to be. The join
itself lives in `yurios/app/corpus.py`, so the format has one definition.

  raw   every turn record verbatim, with `rating` on the rated ones
  kto   rated turns only: prompt / completion / label, the conversational
        shape TRL's KTO trainer reads

Reads only; writes nothing under `corpus/`. What it prints is your own
conversation — personal data, the same as the folder it came from.

Usage:  python scripts/export_corpus.py CHARACTER_ID [-o out.jsonl]
        python scripts/export_corpus.py --corpus path/to/corpus --format kto
        python scripts/export_corpus.py --list
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from yurios.app.corpus import EXPORT_FORMATS, export  # noqa: E402
from yurios.characters.models import CharacterRecord  # noqa: E402


def _characters(data_dir: str) -> dict[str, CharacterRecord]:
    """The registry, parsed but never written. `CharacterRegistry` tidies
    retired fields by saving the file back, and a CLI must not write it while
    the daemon may be up (SPEC §36.1) — so read the records directly."""
    root = Path(data_dir).resolve()
    path = root / "characters.json"
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    records = (CharacterRecord.from_dict(entry, data_root=root, dropped=[])
               for entry in raw.get("characters") or [])
    return {record.id: record for record in records}


def _corpus_dir(args: argparse.Namespace) -> Path:
    if args.corpus:
        return Path(args.corpus)
    characters = _characters(args.data_dir)
    record = characters.get(args.character)
    if record is None:
        known = ", ".join(characters) or "none"
        raise SystemExit(f"no character {args.character!r} under {args.data_dir} "
                         f"(characters: {known})")
    return record.paths.corpus


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Export a character's corpus with its ratings joined in.")
    ap.add_argument("character", nargs="?", help="character id (see --list)")
    ap.add_argument("--corpus", help="a corpus/ directory, instead of a character id")
    ap.add_argument("--data-dir", default="./data",
                    help="where the character registry lives (default ./data)")
    ap.add_argument("--format", choices=EXPORT_FORMATS, default="raw")
    ap.add_argument("--rated-only", action="store_true",
                    help="raw: only the turns a rating stands on")
    ap.add_argument("-o", "--out", help="write here instead of stdout")
    ap.add_argument("--list", action="store_true", help="list character ids and exit")
    args = ap.parse_args(argv)

    if args.list:
        for character_id in _characters(args.data_dir):
            print(character_id)
        return 0
    if not args.character and not args.corpus:
        ap.error("name a character id or pass --corpus (see --list)")

    corpus = _corpus_dir(args)
    if not (corpus / "turns.jsonl").exists():
        raise SystemExit(f"no turns.jsonl under {corpus} — nothing to export")
    rows, stats = export(corpus, fmt=args.format, rated_only=args.rated_only)

    text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    print(f"{len(rows)} rows from {stats['turns']} turns "
          f"({stats['up']} 👍, {stats['down']} 👎"
          + (f", {stats['orphaned']} ratings on turns no longer in the log"
             if stats["orphaned"] else "") + ")",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
