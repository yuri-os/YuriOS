# Relationship-evolution bench

Drives the real `remember()` + DREAM partner-model path against GLM-5.2 over a
scripted dummy relationship, then scores `USER.md`. Two tracks, and the second
one is the point.

## `--track sam` — the live-vault replay (13 weeks, default)

Conversations scripted from the live Yuri vault's actual beats (name, tea,
"don't ask if I'm okay", nestle-don't-append, yandere-lite, calming-skill wanted
then not, five-day silences, americano vs tea) with a dummy companion **Mira**
and dummy user **Sam**, so the live vault is never touched.

## `--track generic` — the generalization check (4 weeks)

The same *beats* — name, identity, a treatment preference, a character
direction, a retraction, a correction, filler that must stay out — with a
different person (**Ada** / **Noor**) and no word in common with the Sam script.
Its scorer asserts **where things landed**, never which words they used.

This track exists because the first version of the filing was a list of
substrings lifted from the live vault (`"yandere"`, `"americano"`, `"hates being
asked"`). It scored 19/19 on the Sam track and filed *nothing* for anyone else:
a user who asked to be flirted with and texted first produced no persona delta
at all. Scorer, implementation and tests shared one couple's vocabulary, so the
bench could only confirm itself. The filing is the model's call now, and this is
what checks it. **A change that passes `sam` and fails `generic` has been fitted
to Sam again.**

## `--track card` — a real SillyTavern import (4 weeks)

Imports an actual third-party V2 card (`CharacterImporter.import_card`) into a
scratch registry and runs the partner model against the `USER.md` the importer
wrote. Nothing else in the bench exercises that file's *origin*: `sam` and
`generic` both seed from `soul-src/`, the one SOUL this repo authored, and a
card off a card site is the other ninety-nine percent.

It caught its own reason for existing. The importer's template had drifted from
`soul-src/USER.md` — "What helps, and what does **not**" against the runtime's
"...doesn't" — and headings were matched by exact string, so every imported
character grew a second What-helps heading, fed that one, and left the one the
template actually wrote sitting there with its `_(unknown)_` placeholder, in a
file injected whole on every turn.

```bash
python scripts/relationship_bench/run.py --track card --character iris
python scripts/relationship_bench/run.py --track card --character virelle
```

```bash
# full 13 weeks, 5 conversations/week, weekly DREAM
python scripts/relationship_bench/run.py

# the generalization track
python scripts/relationship_bench/run.py --track generic

# smoke: first two weeks only
python scripts/relationship_bench/run.py --weeks 2
```

Needs `OPENROUTER_API_KEY` and `UTILITY_MODEL=openrouter/z-ai/glm-5.2` in `.env`.
Writes `scripts/relationship_bench/out/<track>/` (gitignored), including
`evictions.json` — every bullet that left the file without a successor. That
file should stay empty; `_collapse` used to eat short bullets silently, and
counting them is cheaper than reading 65 snapshots.
