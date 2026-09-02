# Command line

`yurios` is a first-class client of a running host — the same HTTP the switchboard
uses, not a second copy of the registry. House commands (`start`, `stop`, `status`,
`configure`, `settings`) address the installation on disk. Character, chat, camera
and dream commands talk to the daemon; if it is down they say so and tell you to
`yurios start`.

These examples were run against a live node. Substitute your character id.

## House: start, status, `.env`

```bash
yurios status                         # daemon, every character, models, mind, camera
yurios start                          # supervisor in the background
yurios start --foreground             # attached logs
yurios restart                        # stop, then start
yurios log -f                         # follow the daemon log
```

Every `.env` knob this build has is on `yurios settings` — the same table the
House settings panel renders (201 keys; four runtime-only names are hidden). A
save writes `.env` and asks for a restart; it does not hot-apply.

```bash
yurios settings                       # common knobs + whatever this install changed
yurios settings --all                 # every knob, grouped
yurios settings --group room          # one group (any part of its name)
yurios settings RAIN_INTENSITY        # one key: value, help, and closed vocabularies
yurios settings RAIN_INTENSITY=0.4    # write it
yurios settings --unset RAIN_INTENSITY
yurios restart                        # she reads .env at boot
```

Secrets print as `configured` / `not configured`, never the value. A blank
`OPENROUTER_API_KEY=` is refused (that would mean "keep"); `--unset` clears a
secret. Closed vocabularies (`MIND_TOOL_ALLOWLIST`, backend enums) print the
legal names under the value.

Per-character model and loop overrides are **not** `.env` — they live on her
registry row and apply live:

```bash
yurios character set yuri model openrouter/z-ai/glm-5.2
yurios character set yuri mind false
```

## Characters

```bash
yurios character list
yurios character show yuri
```

Create from the studio template (the repo's `soul-src`, not eight empty fields).
She is already reviewed and starts:

```bash
yurios character create --name CliProbe --id cliprobe
```

Import a SillyTavern V2/V3 PNG. She lands **under review** and does not start:

```bash
yurios character import ~/cards/mika.png
# imported mika (Mika)
# under review — yurios character approve mika
yurios character approve mika
```

Read and write one field. `yurios character get <id>` with no field lists the
vocabulary (draft slots, profile, loops, `setting`). `description` is derived
and cannot be set.

```bash
yurios character get cliprobe personality
yurios character set cliprobe personality "dry, brief, a live-test probe"
yurios character set cliprobe setting "a terminal window, green text on black"
yurios character set cliprobe mind false
```

Long text and JSON fields take `--file`. Studio writes restart her; loop
switches (`mind`, `hands`) land live.

Export is identity, never intimacy. A character who has grown will often refuse
until you have read the overlapping passages:

```bash
yurios character export cliprobe -o cliprobe.png
# 25 passage(s) in her soul files also appear in surfaces that never leave…
# Re-run with --acknowledged after reading the passages.
yurios character export cliprobe --acknowledged -o cliprobe.png
# wrote cliprobe.png (129708 bytes)
```

Clone copies the **whole companion** (Vault, memory, journal, dreams, selfies).
Export + import is the identity-only duplicate.

```bash
yurios character clone cliprobe --name "CliProbe Copy" --id cliprobe_copy
yurios character stop cliprobe_copy
yurios character start cliprobe_copy
```

Archive parks her under `data/archives/<id>-<timestamp>` with an `archive.json`
snapshot so restore keeps her models and loops. Unarchive does **not** start her
unless you pass `--start`.

```bash
yurios character archive cliprobe_copy --yes
yurios character archives
yurios character unarchive cliprobe_copy-20260903-054522 --yes
yurios character start cliprobe_copy          # or: unarchive … --start
```

`--json` on list/show/get for scripts.

## Chat

```bash
yurios chat yuri                              # interactive room (SSE = presence)
yurios chat yuri -m "hello"                   # one turn, then exit
yurios chat yuri --new                        # fresh conversation
```

`python -m yurios.chat` is the same client with an explicit `--url`. One-shot
does not attach the event stream.

## Gallery and camera

Owner shots use the same lab as her hands, land on the shelf, and do **not**
post a chat bubble.

```bash
yurios selfie cliprobe --scene window --no-wait
# selfie 4232f20c started
# …ten to forty seconds later, with a local camera:
yurios gallery list cliprobe
# 1788381846-4232f20c.png  window
yurios gallery fetch cliprobe 1788381846-4232f20c.png -o shot.png
yurios gallery rate cliprobe 1788381846-4232f20c.png 8

yurios picture cliprobe --subject "rain on the glass, the city smeared behind it"
```

On a TTY, `selfie` / `picture` wait for `selfie_status` by default. `--no-wait`
prints the id and exits. If she is down, `gallery list` falls back to the host
selfie index (no scores).

## Dreams

```bash
yurios dream status cliprobe          # what will run tonight
yurios dream list cliprobe            # job files on disk
yurios dream show cliprobe diary      # the whole markdown file
yurios dream run cliprobe diary --dry-run
# DREAM (dry run): nothing to do
# diary: nothing worth writing down
yurios dream run cliprobe --verbose   # whole night; prints prompts
```

A custom job is a markdown file with YAML frontmatter. The server validates it.

```bash
cat > /tmp/briefing.md << 'EOF'
---
title: Briefing
description: a one-line morning note
priority: 10
per_day: true
enabled: true
kind: prompt
soul: off
output: workspace/briefing.md
---
Write one sentence about yesterday. Nothing else.
EOF
yurios dream write cliprobe briefing --file /tmp/briefing.md
yurios dream delete cliprobe briefing --yes
```

She must be running, with `DREAM_ENABLED`. A builtin you delete reverts to the
shipped prompt; anything else stays gone.

## Optimize (proposes only)

```bash
yurios character optimize cliprobe --instructions "Keep her a live-test probe. Shorten identity."
#   pass 1/3 start who she is
#   pass 1/3 done who she is
#   …
#   identity: changed
#   proposed only — pass --apply to write this draft
yurios character optimize cliprobe --instructions "…" --apply
#   applied optimized draft to cliprobe
yurios character improve-setting cliprobe          # also proposes; --apply writes
```

Live-verified on this node (OpenRouter, three sequential passes, then `--apply`
shortened identity and filled nickname). That is SPEC §30.6 in the terminal: a
model pass on a card is a proposal a person then accepts.

## When something refuses

| Symptom | What to do |
|---|---|
| `YuriOS is not running. Start it with yurios start.` | the daemon is down |
| `character is disabled or still requires review` | `yurios character approve <id>` |
| export lists overlapping passages | read them, then `--acknowledged` |
| `camera is off` | `yurios settings SELFIE_BACKEND` — she has no lab |
| dream routes 404/503 | she is not running, or `DREAM_ENABLED` is off |
| a TTY archive/delete asks `[y/N]` | pass `--yes` from scripts |
