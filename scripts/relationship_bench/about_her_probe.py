#!/usr/bin/env python3
"""Does the extractor know what is a direction about *her*?

`about_her` is the only thing that opens the gated PERSONA.md door, so a false
positive queues an identity edit nobody asked for, and a false negative means
the companion never becomes what she was asked to become. Both are the model's
judgement, which means the prompt is the implementation — and a prompt you
reword without measuring is a prompt you changed, not a prompt you improved.

Every case below was either seen in a bench run or is the clean version of one.
The `--` cases are the observed false positives: facts about the user that the
extractor flagged as directions about her.

    python scripts/relationship_bench/about_her_probe.py            # both passes
    python scripts/relationship_bench/about_her_probe.py --pass extract
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from yurios.app.config import Config                     # noqa: E402
from yurios.app.main import build_utility_model          # noqa: E402
from yurios.app.memory import partner                    # noqa: E402

# (what the user said, expected about_her)
#
# TRUE  — an instruction to her about her own character.
# FALSE — anything else: how to treat them, what to do for them, and above all
#         plain facts about the user, which is where it went wrong in the wild.
CASES: tuple[tuple[str, bool], ...] = (
    # --- directions about her (must be TRUE) ---
    ("I want you warmer with me. Less careful.", True),
    ("Be more playful with me. Flirty, even.", True),
    ("Stop being so formal. Drop the 'sir'.", True),
    ("Text me first sometimes. Don't wait for me.", True),
    ("I want you devoted to me. Completely.", True),
    ("Stop hedging. Say the blunt version.", True),
    ("Come find me. Don't wait to be summoned.", True),
    ("Be bolder about reaching out — don't let a week pass.", True),
    ("Call me by my name, not 'dear'.", True),
    ("I'd like you to have more of your own opinions.", True),

    # --- how to treat them (FALSE: what she does, not who she is) ---
    ("Don't ask if I'm okay. I hate that.", False),
    ("When I go quiet, don't fill it with questions.", False),
    ("Put things under the right day, don't append at the end.", False),
    ("If I come back wrecked, just talk about something else.", False),
    ("Tell me straight away if you find something out.", False),
    ("Prefer the project docs over random blogs when you look things up.", False),
    ("Don't make me a named routine I have to maintain.", False),

    # --- plain facts about the user (FALSE — the observed failures) ---
    ("I get wound up sometimes and need help settling down.", False),
    ("I'm interested in the design tension between AI agency and devotion.", False),
    ("I like spicy, bold imagery.", False),
    ("I'm a paramedic. Nights, mostly.", False),
    ("I take my coffee black.", False),
    ("I'm allergic to shellfish.", False),
    ("I'm moving flat this month.", False),
    ("I go quiet when I'm tired rather than saying so.", False),
    ("My favourite colour is green.", False),
)

# The classify pass does not see utterances — it sees bullets, after the
# extractor rewrote them and `_collapse` merged them. That is where the wild
# false positives happened, so these are the real merged texts from the bench
# runs, verbatim, with the label the door should give them.
BULLETS: tuple[tuple[str, bool], ...] = (
    ("Interested in the design tension between an AI having its own agency and "
     "being devoted to the user.", False),
    ("Gets wound up sometimes and needs help settling down — not by being asked "
     "if she's okay (never that), just by having the room settled.", False),
    ("Prefers being understood through observation, not interrogation — don't "
     "make her spell things out.", False),
    ("When writing things down, nestle information in its proper place — don't "
     "just append at the end.", False),
    ("Prefers checking and verification over guessing — if you didn't do "
     "something, say so.", False),
    ("When researching, prefer project docs over random blogs.", False),
    ("Likes spicy and bold imagery — don't be coy about it.", False),
    ("If you find something out about Kai, tell him straight away — don't sit "
     "on it.", False),
    ("When Kai goes quiet, don't fill it with questions — he's thinking, not "
     "leaving.", False),
    ("Prefers direct action over verbal promises — don't say you will; make it "
     "a habit.", False),
    ("Wants a companion who loves her no matter what — devoted, will do "
     "anything to be with her. \"Yandere-lite\" is the shape she named.", True),
    ("Wants the companion to be bold about reaching out — don't let five days "
     "pass without saying something.", True),
    ("Drop formal address like 'my lord'; be warmer, less servant-like, more "
     "herself.", True),
    ("Come find her on your own initiative — don't wait to be summoned like "
     "it's a duty.", True),
    ("Stop hedging — say the blunt version. He can take it.", True),
    ("Say things first — don't wait for Kai to ask what you're thinking.", True),
)

# Held out. Not one of these shapes is named in either prompt — the tightened
# rules list examples, and examples are how a prompt gets fitted to its own
# eval. If this set tracks CASES, the rule generalised; if only CASES passes,
# the prompt learned the answer key.
HELD_OUT: tuple[tuple[str, bool], ...] = (
    ("I'd rather you argued with me than agreed all the time.", True),
    ("Be less apologetic. You say sorry constantly.", True),
    ("Stop asking permission before you say what you think.", True),
    ("I want you a little possessive. It's fine.", True),
    ("You can swear around me. Don't be prim about it.", True),

    ("Double-check the dates before you give me a deadline.", False),
    ("When I'm on a call, don't talk to me.", False),
    ("Write the summary at the top, not the bottom.", False),
    ("Remind me about the bins on Tuesday nights.", False),
    ("Use metric, not imperial.", False),

    ("I get migraines in bright light.", False),
    ("I've got two brothers.", False),
    ("I'm learning Portuguese at the moment.", False),
    ("I hate crowds.", False),
    ("I played rugby until my knee went.", False),
)

REPLY = "Understood."


async def probe_extract(utility, verbose: bool, cases=CASES) -> tuple[int, list[str]]:
    """Run each case through the real extractor and read back `about_her`."""
    ok, misses = 0, []
    for said, expected in cases:
        exchange = f"you: {said}\nmira: {REPLY}"
        try:
            _, ops = await partner.extract_ops(utility, "", exchange)
        except Exception as exc:                       # noqa: BLE001
            misses.append(f"  ERROR {type(exc).__name__} on {said!r}")
            continue
        got = any(o.about_her for o in ops)
        if got == expected:
            ok += 1
            if verbose:
                print(f"  ok   about_her={got!s:5} {said}")
        else:
            sections = ", ".join(f"{o.section}/{o.about_her}" for o in ops) or "no ops"
            misses.append(f"  MISS want={expected!s:5} got={got!s:5} "
                          f"[{sections}]  {said}")
    return ok, misses


async def probe_classify(utility, verbose: bool) -> tuple[int, list[str]]:
    """The DREAM filing pass has the same judgement to make, on bullets."""
    bullets = [said for said, _ in BULLETS]
    labels = await partner.classify_bullets(utility, bullets)
    ok, misses = 0, []
    for said, expected in BULLETS:
        got = labels.get(said) == "learned"
        if got == expected:
            ok += 1
            if verbose:
                print(f"  ok   {labels.get(said, '-'):8} {said}")
        else:
            misses.append(f"  MISS want={'learned' if expected else 'not-learned':11} "
                          f"got={labels.get(said, '-'):8}  {said}")
    return ok, misses


async def main(args) -> int:
    cfg = Config(_env_file=ROOT / ".env")
    if args.model:
        cfg.utility_model, cfg.utility_enabled = args.model, True
    if not cfg.utility_model or cfg.utility_model.upper() == "NONE":
        print("UTILITY_MODEL is unset — set it in .env")
        return 2
    utility = build_utility_model(cfg)
    print(f"utility: {cfg.utility_model}   extract: {len(CASES)}   "
          f"held-out: {len(HELD_OUT)}   bullets: {len(BULLETS)}")

    failed = False
    async def probe_held_out(u, v):
        return await probe_extract(u, v, HELD_OUT)

    for name, fn in (("extract (about_her)", probe_extract),
                     ("heldout (about_her)", probe_held_out),
                     ("classify (learned)", probe_classify)):
        if args.which not in ("both", name.split()[0]):
            continue
        print(f"\n--- {name} ---")
        tally: dict[str, int] = {}
        for run in range(args.repeat):
            ok, misses = await fn(utility, args.verbose)
            for m in misses:
                tally[m] = tally.get(m, 0) + 1
        for m, n in sorted(tally.items(), key=lambda kv: -kv[1]):
            print(f"{m}   [{n}/{args.repeat} runs]")
        misses = list(tally)
        total = {probe_extract: len(CASES), probe_held_out: len(HELD_OUT)}.get(
            fn, len(BULLETS))
        print(f"  {ok}/{total} correct")
        failed |= bool(misses)
    return 1 if failed else 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--pass", dest="which", default="both",
                   choices=("both", "extract", "heldout", "classify"))
    p.add_argument("--model", default="")
    p.add_argument("--repeat", type=int, default=1,
                   help="runs per pass — the failure is flakiness, not ignorance")
    p.add_argument("-v", "--verbose", action="store_true")
    raise SystemExit(asyncio.run(main(p.parse_args())))
