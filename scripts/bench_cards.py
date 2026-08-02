#!/usr/bin/env python3
"""bench_cards.py — how well a folder of real cards imports (SPEC §30.6).

Every card site lays a character out differently, so "does the importer map
foreign cards onto our fields" is not a question one card can answer. This runs
a whole folder through the real import path and scores what came out, which
turns a vague impression into a number that moves when the router improves.

    python scripts/bench_cards.py ~/Documents/roleplay/YuriOS_test_cards
    python scripts/bench_cards.py CARDS --optimize --model lm_studio/some-model

Two grades, because there are two mechanisms:

  **import** — `cardsplit.py` alone: no model, no network, what every user gets.
  **optimize** — plus one `optimize.py` pass per card, with whichever model you
  name. Slow and it costs tokens, so it is opt-in.

The score is deliberately shallow: which of the fields a good card fills are
non-empty, and whether the ones with a house style (a `personality` register, a
`scenario` that is a situation rather than a lore dump) look like it. It cannot
tell you the prose is good. It tells you, over thirty cards, that Appearance is
empty on eleven of them — which is the thing you actually want to know.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from yurios.characters import CharacterImporter, CharacterRegistry     # noqa: E402
from yurios.characters.card import CardParseError                      # noqa: E402
from yurios.characters.optimize import CardOptimizeError, optimize_draft  # noqa: E402
from yurios.characters.studio import Draft, read_draft                 # noqa: E402

#: The fields a card that imported well has something in. `nickname` and
#: `group_only_greetings` are legitimately empty on most cards and are not scored.
SCORED: tuple[str, ...] = (
    "name", "identity", "history", "appearance", "manner", "personality",
    "scenario", "first_mes", "examples", "creator_notes", "lorebook",
)


#: `_create_soul` writes an italic parenthetical where a card gave it nothing —
#: "_(Not supplied by the card.)_". Scoring that as content would report a
#: perfect 28/28 on fields no card actually filled, which is the one result this
#: script must never produce.
def _is_placeholder(text: str) -> bool:
    stripped = text.strip()
    return stripped.startswith("_(") and stripped.endswith(")_")


def _filled(draft: Draft, field: str) -> bool:
    value = getattr(draft, field)
    if field == "lorebook":
        return bool(value.get("entries"))
    if isinstance(value, list):
        return any(item.strip() and not _is_placeholder(item) for item in value)
    return bool(str(value).strip()) and not _is_placeholder(str(value))


def _quality(draft: Draft) -> list[str]:
    """House-style problems a filled field can still have."""
    faults: list[str] = []
    if len(draft.personality) > 200:
        faults.append("personality is a paragraph, not a register")
    if len(draft.scenario) > 1500:
        faults.append("scenario is carrying world-building")
    if len(draft.identity) > 2500:
        faults.append("identity is still a wall")
    if not _filled(draft, "appearance"):
        faults.append("no appearance — her selfies will guess")
    return faults


def _score(draft: Draft) -> dict:
    filled = [field for field in SCORED if _filled(draft, field)]
    return {"filled": filled,
            "empty": [field for field in SCORED if field not in filled],
            "faults": _quality(draft)}


async def _run(paths: list[Path], *, optimize: bool, model: str,
               instructions: str) -> list[dict]:
    utility = None
    if optimize:
        from yurios.app.main import model_api_base
        from yurios.app.providers.openrouter import LiteLLMUtilityModel
        from yurios.world.config import Config
        cfg = Config()
        chosen = model or cfg.utility_model
        utility = LiteLLMUtilityModel(chosen, cfg.openrouter_api_key,
                                      thinking=cfg.utility_thinking,
                                      api_base=model_api_base(cfg, chosen))

    rows: list[dict] = []
    for path in paths:
        row: dict = {"card": path.name}
        with tempfile.TemporaryDirectory() as workspace:
            registry = CharacterRegistry(Path(workspace))
            importer = CharacterImporter(registry, initialize_git=False)
            try:
                record = importer.import_card(path)
            except (CardParseError, ValueError) as exc:
                row["error"] = str(exc)
                rows.append(row)
                print(f"  ✗ {path.name[:56]:58} {exc}")
                continue
            draft, _provenance = read_draft(record)
            row["import"] = _score(draft)
            mark = f"{len(row['import']['filled'])}/{len(SCORED)}"
            if optimize:
                try:
                    result = await optimize_draft(utility, draft, model=model,
                                                  instructions=instructions)
                    row["optimize"] = _score(result.draft)
                    row["changed"] = [item["field"] for item in result.changes]
                    mark += f" → {len(row['optimize']['filled'])}/{len(SCORED)}"
                except CardOptimizeError as exc:
                    row["optimize_error"] = str(exc)
                    mark += f" → failed: {str(exc)[:60]}"
            print(f"  · {path.name[:56]:58} {mark}")
        rows.append(row)
    return rows


def _summary(rows: list[dict], grade: str) -> None:
    scored = [row[grade] for row in rows if grade in row]
    if not scored:
        return
    print(f"\n{grade}: {len(scored)} cards")
    for field in SCORED:
        hits = sum(1 for score in scored if field in score["filled"])
        bar = "█" * round(hits / len(scored) * 24)
        print(f"  {field:<22} {hits:3}/{len(scored)}  {bar}")
    faults: dict[str, int] = {}
    for score in scored:
        for fault in score["faults"]:
            faults[fault] = faults.get(fault, 0) + 1
    if faults:
        print("  faults")
        for fault, count in sorted(faults.items(), key=lambda item: -item[1]):
            print(f"    {count:3} × {fault}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("folder", type=Path, help="a folder of .png character cards")
    parser.add_argument("--optimize", action="store_true",
                        help="also run each card through the AI optimiser")
    parser.add_argument("--model", default="",
                        help="LiteLLM id to optimise with (default: UTILITY_MODEL)")
    parser.add_argument("--instructions", default="",
                        help="the user instruction to pass to every optimisation")
    parser.add_argument("--json", type=Path, default=None,
                        help="write the full per-card result here")
    args = parser.parse_args()

    paths = sorted(p for p in args.folder.glob("*.png") if p.is_file())
    if not paths:
        print(f"no .png cards in {args.folder}", file=sys.stderr)
        return 1
    print(f"{len(paths)} cards in {args.folder}\n")

    rows = asyncio.run(_run(paths, optimize=args.optimize, model=args.model,
                            instructions=args.instructions))
    failed = [row for row in rows if "error" in row]
    _summary(rows, "import")
    if args.optimize:
        _summary(rows, "optimize")
    if failed:
        print(f"\n{len(failed)} cards would not import at all:")
        for row in failed:
            print(f"  {row['card']}\n    {row['error']}")
    if args.json:
        args.json.write_text(json.dumps(rows, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
