#!/usr/bin/env python3
"""3-month dummy relationship against GLM-5.2 (SPEC §6.3 living USER.md).

Seeds a throwaway Vault from soul-src, plays scripted Mira/Sam conversations
through the real `remember()` extractor and weekly DREAM compact, then scores
USER.md. The live character vault is never touched.

    python scripts/relationship_bench/run.py
    python scripts/relationship_bench/run.py --weeks 2
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import shutil
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(HERE))

from yurios.app.config import Config                                          # noqa: E402
from yurios.app.main import build_utility_model                               # noqa: E402
from yurios.app.memory.store import FileMemoryStore, Record                   # noqa: E402
from yurios.kernel.clock import VirtualClock                                  # noqa: E402
from yurios.mind.dream import DreamConsolidator                               # noqa: E402
from yurios.mind.vaultio import MindVault                                     # noqa: E402
from yurios.app.memory import partner                                         # noqa: E402
import cards                                                                  # noqa: E402
import generic                                                                # noqa: E402
import scenarios                                                              # noqa: E402
from score import score_user_md                                               # noqa: E402
from seed_vault import seed                                                   # noqa: E402


OUT_ROOT = HERE / "out"
SOUL_SRC = ROOT / "soul-src"
OUT = OUT_ROOT   # set per-track in play()


class HashEmbedder:
    """Same contract as the suite's FakeEmbedder — recall is not under test."""
    dim = 32

    def embed(self, texts):
        import re
        import zlib
        out = []
        for t in texts:
            v = [0.0] * self.dim
            for w in re.findall(r"[a-z0-9']+", (t or "").lower()):
                v[zlib.crc32(w.encode()) % self.dim] += 1.0
            out.append(v)
        return out


def _dt(day, hour=20, minute=0) -> datetime.datetime:
    return datetime.datetime(day.year, day.month, day.day, hour, minute)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


async def _complete(utility, messages, *, retries=3) -> str:
    last = ""
    for attempt in range(1, retries + 1):
        try:
            return await utility.complete(messages)
        except Exception as exc:  # noqa: BLE001 — bench must survive a blip
            last = f"{type(exc).__name__}: {exc}"
            print(f"    utility failed ({attempt}/{retries}): {last}")
            await asyncio.sleep(min(2 ** attempt, 8))
    print(f"    giving up: {last}")
    return ""


class RetryingUtility:
    """LiteLLMUtilityModel with retries; DREAM wants a callable, remember() a .complete."""

    def __init__(self, inner):
        self.inner = inner

    async def complete(self, messages, **params) -> str:
        return await _complete(self.inner, messages)

    async def __call__(self, messages, **params) -> str:
        return await self.complete(messages, **params)


def _import_card(character: str, out: Path) -> Path:
    """Import the card for real, into a scratch registry under `out`.

    Never the live `data/characters/` copy: a bench turn calls `remember()`,
    which writes USER.md and commits. The card PNG is read-only input; the
    vault under test is one this run created.
    """
    from yurios.characters.importer import CharacterImporter
    from yurios.characters.registry import CharacterRegistry

    name, card_rel, _user = cards.CARDS[character]
    card = ROOT / card_rel
    if not card.is_file():
        raise SystemExit(f"no card at {card}")
    registry = CharacterRegistry(out / "registry")
    record = CharacterImporter(registry, initialize_git=True).import_card(
        card, character_id=character)
    print(f"imported {name} from {card.name} → {record.paths.vault}")
    return record.paths.vault


async def play(args) -> int:
    global OUT
    is_card = args.track == "card"
    # the tracks must not overwrite each other's output
    OUT = OUT_ROOT / (f"card-{args.character}" if is_card else args.track)
    track = {"generic": generic, "card": cards}.get(args.track, scenarios)
    CHAR = (cards.CARDS[args.character][0].lower() if is_card else track.CHAR)
    weeks = min(args.weeks, track.WEEKS)
    convos = (track.conversations(weeks=weeks, character=args.character)
              if is_card else track.conversations(weeks=weeks))
    if not convos:
        print("no conversations")
        return 2

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    vault_dir = OUT / "vault"
    if is_card:
        vault_dir = _import_card(args.character, OUT)
    else:
        seed(SOUL_SRC, vault_dir)

    cfg = Config(_env_file=ROOT / ".env")
    if args.model:
        cfg.utility_model = args.model
        cfg.utility_enabled = True
    if not cfg.utility_model or cfg.utility_model.upper() == "NONE":
        print("UTILITY_MODEL is unset. Set it in .env (openrouter/z-ai/glm-5.2).")
        return 2
    inner = build_utility_model(cfg)
    utility = RetryingUtility(inner)
    print(f"utility: {getattr(inner, 'model', cfg.utility_model)}")
    print(f"weeks: {weeks}  conversations: {len(convos)}  "
          f"turns: {sum(len(c.turns) for c in convos)}")

    store = FileMemoryStore(
        vault_dir, HashEmbedder(), utility,
        char_name=CHAR, user_name="you", embed_dim=HashEmbedder.dim)
    first = convos[0].day
    clock = VirtualClock(start=_dt(first, 9, 0).timestamp())
    dream = DreamConsolidator(MindVault(vault_dir), store, clock, utility=utility)

    history: list[dict] = []
    traces: list[dict] = []
    week_scores: list[dict] = []
    evictions: list[dict] = []
    prev_bullets: list[str] = []

    def _bullets_of(md: str) -> list[str]:
        _, sections = partner.parse_user_md(md)
        return partner.all_bullets(sections)

    def _evicted(before: list[str], after: list[str], tag: str) -> None:
        """A fact that was in the file and is now neither present nor merged
        into a surviving bullet. `_collapse` used to do this silently — the
        whole point of the matching fix — so the bench has to watch for it."""
        for gone in before:
            if gone in after:
                continue
            if any(partner.same_slot(gone, kept, loose=True) for kept in after):
                continue
            evictions.append({"tag": tag, "lost": gone})
            print(f"    !! EVICTED: {gone}")

    def scored_now(md: str, week: int):
        if is_card:
            return cards.score_card(
                md, partner.read_persona_delta(vault_dir),
                character=args.character, week=week)
        if track is generic:
            return generic.score_generic(
                md, partner.read_persona_delta(vault_dir), week=week)
        return score_user_md(md, week=week)

    def snap(tag: str, day, week: int) -> dict:
        nonlocal prev_bullets
        md = store.read_user_md()
        now_bullets = _bullets_of(md)
        _evicted(prev_bullets, now_bullets, tag)
        prev_bullets = now_bullets
        scored = scored_now(md, week)
        rec = {
            "tag": tag,
            "day": day.isoformat() if hasattr(day, "isoformat") else str(day),
            "week": week,
            "phase": scored.checks[-1].detail if scored.checks else "",
            "n_bullets": sum(1 for ln in md.splitlines() if ln.lstrip().startswith("- ")),
            "passed": scored.passed,
            "total": scored.total,
            "ok": scored.ok,
            "md": md,
            "score": [{"name": c.name, "ok": c.ok, "detail": c.detail}
                      for c in scored.checks],
        }
        history.append(rec)
        _write(OUT / "snapshots" / f"{tag}.md", md)
        return rec

    snap("00-seed", first, 0)

    current_week = 1
    last_day = None
    for i, convo in enumerate(convos, start=1):
        if last_day is not None and convo.day != last_day and convo.weekday == 0:
            # Monday morning: DREAM the finished week in one pass
            morning = _dt(convo.day, 3, 0)
            clock._now = morning.timestamp()
            report = await dream.consolidate(token_budget=80_000)
            print(f"  DREAM week→{convo.day}: "
                  f"days={report.days_processed} facts={report.facts_added} "
                  f"user_md={report.user_md_rewritten}")
        last_day = convo.day
        clock._now = _dt(convo.day, 20, 0).timestamp()
        print(f"W{convo.week} {convo.day} {convo.title} ({len(convo.turns)} turns)")
        for t, turn in enumerate(convo.turns):
            rec = Record(
                session_id=f"w{convo.week}",
                turn_index=t,
                user_msg=turn.user,
                reply=turn.reply,
                ts=_dt(convo.day, 20, t),
            )
            try:
                result = await store.remember(rec)
                traces.append({
                    "day": convo.day.isoformat(),
                    "title": convo.title,
                    "turn": t,
                    "user": turn.user,
                    "ops": result.user_md_ops,
                    "quarantined": result.quarantined,
                })
                print(f"    turn {t}: ops={result.user_md_ops} "
                      f"held={result.quarantined}")
            except Exception:
                traceback.print_exc()
                traces.append({
                    "day": convo.day.isoformat(),
                    "title": convo.title,
                    "turn": t,
                    "user": turn.user,
                    "error": True,
                })
        snap(f"{convo.day.isoformat()}-{convo.title.replace(' ', '-')}",
             convo.day, convo.week)
        if convo.week != current_week and convo.weekday == 0:
            current_week = convo.week
        if convo.weekday == 4:  # Friday: week checkpoint
            week_md = store.read_user_md()
            scored = scored_now(week_md, convo.week)
            week_scores.append({
                "week": convo.week,
                "passed": scored.passed,
                "total": scored.total,
                "ok": scored.ok,
                "text": scored.as_text(),
            })
            _write(OUT / "weeks" / f"week-{convo.week:02d}.md", week_md)
            print(f"  WEEK {convo.week} SCORE: {scored.passed}/{scored.total}")

    # final DREAM after the last conversation
    if last_day is not None:
        clock._now = _dt(last_day + datetime.timedelta(days=1), 3, 0).timestamp()
        report = await dream.consolidate(token_budget=80_000)
        print(f"  final DREAM: days={report.days_processed} "
              f"facts={report.facts_added} user_md={report.user_md_rewritten}")

    final_md = store.read_user_md()
    final = scored_now(final_md, weeks)
    _write(OUT / "USER.final.md", final_md)
    _write(OUT / "history.json", json.dumps(
        [{k: v for k, v in h.items() if k != "md"} for h in history], indent=2))
    _write(OUT / "traces.json", json.dumps(traces, indent=2))
    _write(OUT / "week_scores.json", json.dumps(week_scores, indent=2))
    _write(OUT / "evictions.json", json.dumps(evictions, indent=2))

    delta_path = vault_dir / "state" / "persona_delta.json"
    delta = delta_path.read_text(encoding="utf-8") if delta_path.is_file() else "{}"
    _write(OUT / "persona_delta.json", delta)

    report_md = _report(weeks, convos, week_scores, final, final_md, delta,
                        history, evictions, args.track)
    _write(OUT / "REPORT.md", report_md)
    print()
    print(report_md)
    return 0 if final.ok else 1


def _report(weeks, convos, week_scores, final, final_md, delta, history,
            evictions, track="sam") -> str:
    lines = [
        f"# Relationship evolution bench ({track}) — {weeks} weeks, GLM-5.2",
        "",
        f"{len(convos)} conversations, "
        f"{sum(len(c.turns) for c in convos)} turns.",
        "",
        "## Final score",
        "",
        "```",
        final.as_text(),
        "```",
        "",
        "## Week checkpoints",
        "",
    ]
    for w in week_scores:
        mark = "ok" if w["ok"] else "FAIL"
        lines.append(f"- week {w['week']}: {w['passed']}/{w['total']} {mark}")
    lines += [
        "",
        "## Facts lost",
        "",
        (f"{len(evictions)} bullet(s) disappeared without a successor:"
         if evictions else "None — every bullet survived or was merged."),
        "",
    ]
    for e in evictions:
        lines.append(f"- `{e['tag']}` — {e['lost']}")
    lines += [
        "",
        "## USER.md evolution (bullet count + phase)",
        "",
        "| tag | week | bullets | passed |",
        "|---|---:|---:|---:|",
    ]
    for h in history:
        if h["tag"].startswith("00-") or "week-" in h["tag"]:
            continue
        if h["tag"][4:5] != "-" and not h["tag"][:2].isdigit():
            pass
        # one row per Friday snapshot already in week_scores; list conversation snaps briefly
        if h["n_bullets"] or h["tag"] == "00-seed":
            lines.append(
                f"| `{h['tag'][:40]}` | {h['week']} | {h['n_bullets']} | "
                f"{h['passed']}/{h['total']} |")
    lines += [
        "",
        "## Final USER.md",
        "",
        "```markdown",
        final_md.rstrip(),
        "```",
        "",
        "## Persona delta (gated PERSONA.md proposal)",
        "",
        "```json",
        delta.strip() or "{}",
        "```",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--weeks", type=int, default=scenarios.WEEKS,
                   help="1–13. Default 13 (3 months, 5 convos/week).")
    p.add_argument("--character", default="iris", choices=tuple(cards.CARDS),
                   help="which imported card to run (--track card only)")
    p.add_argument("--track", default="sam", choices=("sam", "generic", "card"),
                   help="'sam' is the live-vault script; 'generic' replays the "
                        "same beats in a disjoint vocabulary and scores where "
                        "things landed, not which words they used; 'card' imports "
                        "a real SillyTavern V2 card and runs against what "
                        "CharacterImporter wrote.")
    p.add_argument("--model", default="",
                   help="Override UTILITY_MODEL (default: .env)")
    args = p.parse_args()
    args.weeks = max(1, args.weeks)
    return asyncio.run(play(args))


if __name__ == "__main__":
    raise SystemExit(main())
