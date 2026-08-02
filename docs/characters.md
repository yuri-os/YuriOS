# Characters

YuriOS 0.2 runs a **house** of companions. The process you start is a *host*: it owns the
registry, the storage tree and the port, and starts one isolated *runtime* — a complete
companion, body and mind — per character. A runtime doesn't know it has neighbours.

Normative detail: [`SPEC.md` §29–§33](../SPEC.md).

## The switchboard

`http://localhost:8768/` is the character board. One tile per companion, showing her portrait,
name, state, model and voice. Select a card to enter her sanctuary at
`/characters/<id>/sanctuary/`; **leaving the room returns to the board without stopping her** —
her background life doesn't depend on being looked at.

States you'll see on a tile:

| State | Meaning |
|---|---|
| `engaged` / `awake` / `resting` / `dreaming` | her mind's activity state — she's running (see [The mind](mind.md#activity-states)) |
| `offline` | registered, no runtime up |
| `starting` / `ready` | the host is bringing her up |
| `attention` | imported and waiting for review — see [below](#importing-a-card) |
| `failed` | her runtime didn't start; the tile carries the error |

One broken companion is never a down house: the host stays up and everyone else keeps running.

## Where a character lives

`DATA_DIR` (default `./data`) is the host's storage root:

```
data/
├── characters.json              # the registry (atomic, schema-versioned)
├── connections.json             # named provider profiles
├── archives/                    # archived characters, kept
└── characters/<id>/
    ├── source-card.png          # the PNG she was imported from, verbatim
    ├── card.json                # her card fields, the interchange format
    ├── portrait.png             # her face on the board and on export
    ├── vault/                   # her SOUL, memory, knowledge, goals, state — a git repo
    ├── corpus/                  # the raw conversation log
    ├── traces/                  # ticks.jsonl, context.jsonl
    ├── tool-logs/               # calls.jsonl — one line per tool call
    └── selfies/                 # her photos + provenance sidecars
```

**A character is a directory.** Moving her is copying it. The host refuses to start if any two
characters' writable roots overlap — two minds sharing a Vault would interleave commits and
consolidate each other's memories, so that failure happens loudly at boot rather than at 3 a.m.
in DREAM.

Her id (`yuri`, `mia`, `yuri_v2`) is derived from her display name and is the same string
everywhere: the URL segment, the directory name, and the suffix on her environment variables.

## Importing a card

The board's import control accepts **SillyTavern V2/V3 character cards** — an ordinary PNG with
the card JSON in a `tEXt` chunk. The parser prefers the V3 (`ccv3`) chunk, accepts V2 (`chara`),
and bounds everything before decoding it: file size, chunk size, chunk count, metadata size,
image dimensions and pixel count. A card is a file from the internet, and it's treated as one.

Import is a transaction. Her whole root — source card, `card.json`, a re-encoded portrait, a
Vault seeded with SOUL files written from the card's fields, the empty corpus/traces/tool-logs/
selfies directories, and a `git init` of the Vault — is assembled in a staging directory and made
visible with a single rename. A failure at any point leaves nothing behind. The portrait is
re-encoded from the PNG's pixels rather than copied, so nothing from the uploaded file's chunks
survives into the served image.

**Cards edited in place carry their own history.** A card that was revised on a site and
re-uploaded often has two `chara` chunks: the current payload and whatever the editor appended
past. The parser reads the first one — that is the live one, and on both of the real cards this
was found on it is also the complete one — and then says so, in `NOTES.md` and in the log:

> this PNG carried more than one chara payload. Read the first one in the file and ignored 1
> later one that disagrees with it — check the imported fields against the card you expected.

A file that needed disambiguating never starts on its own, even if it declares itself a YuriOS
card: the block that vouches for it is one of the two payloads in question. More than eight
payloads is still a refusal.

### What happens to a foreign layout

A YuriOS character keeps who she is in four places — `Identity`, `History`, `Appearance` and
`Manner`. A card from a card site keeps all four in one `description` field, in whatever shape
its author felt like: bracketed blocks, bold headers, bullet lists, ASCII rules, emoji section
markers. There is no schema to parse, so the importer reads the card's own **section headers**
and routes each block to where it belongs (`characters/cardsplit.py`).

It is a router, not a rewriter. Every line lands in exactly one section, in the author's own
words, and a layout it can't read leaves everything under `Identity` rather than guessing. Across
the 28-card sample in `scripts/bench_cards.py`, it fills `Appearance` on 24 and `History` on 21
where the old importer filled neither.

What it can't do is notice that the `scenario` field is holding two pages of world-building, or
that `character_version` is holding a URL, or write the one-line personality register the format
wants. That's [Optimize with AI](#optimize-with-ai), one button away in the studio.

### The review state

A card that doesn't declare itself a YuriOS card arrives **disabled**, marked `review_required`:
her capabilities don't run and no mind wakes. The board shows her as needing attention.

Open her profile, look at what came in, and **save** — that save is the human act that accepts the
review, enables her, and starts her. It's one click, and it exists so that a card you downloaded
can't quietly get a mind, tools and a Telegram bot before you've read it.

## Editing a character

Her profile drawer edits name, description, personality, scenario, first message, chat model,
utility model, endpoint, API key variable, voice, connection profile, body backend and rig, and her
three loop switches. The model and connection fields are also on the gear panel inside her room,
and blank means "inherit the node's `.env`".

Saving writes `card.json` **and** rewrites the matching sections of her SOUL files in the Vault,
then commits the Vault:

| Card field | SOUL destination |
|---|---|
| `description`, `system_prompt`, `post_history_instructions` | `soul/CONSTITUTION.md` |
| `scenario` | `soul/SCENARIO.md` |
| `first_mes` | `soul/BOOTSTRAP.md` (the cold open), else `SCENARIO.md` |
| `personality` | `soul/PERSONA.md` frontmatter |
| `creator_notes` | `soul/NOTES.md` |
| `name` | `soul/soul.yaml` |

The SOUL files are authoritative — prompts are assembled from them, never from `card.json` — so
an edit that didn't reach the files would be an edit that didn't happen.

A running character is restarted only if the save moved something her runtime was **built** with —
her name, her voice, her body, or the utility/dream loops. A change to her model, endpoint, key
variable, model knobs, mind switch or card text reaches her while she runs: she changes model
mid-conversation and keeps the thread (the SOUL is re-read on every turn).

## The card studio

`/studio/` is where a character is written and where she leaves. Reach it from **Create
character** on the board, or **Edit in studio** in any character's drawer.

It edits card *fields* — name, identity, manner, scenario, greetings, examples, lore, notes —
and writes them back down through `soul.yaml` into the SOUL files, one commit per save. The
right-hand column shows the card that will actually ship: the face, the per-field prompt budget
against the ch. 07 guidance, and what stays on this machine.

Two things there are worth knowing about:

- **The constitution is locked by default.** `Identity`, `History`, `Voice law` and `Hard limits`
  live in `CONSTITUTION.md`, which [§23](mind.md#self-edits)'s gate forbids *her* from editing.
  It does not forbid you — but it takes a deliberate click, and the commit says
  `studio: edit constitution` so it stands out in `git log`.
- **Grown since she arrived** lists every part of herself she rewrote and you approved, with the
  date and her stated reason. It is read from the Vault's git log, so it is as trustworthy as
  `git log` is.

Creating a character opens on the shape of a working one (from `soul-src`) rather than eight
empty boxes. Nothing exists on disk until you press create; after that she is enabled and
autostarts, because you wrote her and there is nothing to review.

## Optimize with AI

The studio's second button. It sends the whole card to a model you choose and gets back the same
character, re-filed into this format — the backstory out of the identity blob, the world-building
out of the scenario and into lorebook entries, a personality register where the source left one
empty, the jailbreak preamble moved from her persona to the voice law, the source URL out of the
version field and into the notes.

It also takes an instruction, which is the other half of what it's for. Formatting gets fixed
either way; the box is for the character:

> She's too guarded — make her openly devoted and warm to {{user}} from the first line, but keep
> the sharp tongue and the raven-court backstory.

An instruction like that is carried through every field it touches, because a card whose `manner`
says devoted and whose `first_mes` still says wary reads as two different people.

**Nothing is saved until you say so.** The model proposes; the dialog shows you a field-by-field
diff of what moved, was filled in, or was emptied; **Apply** puts it in the form as an ordinary
unsaved edit. That's deliberate — a character card is a file from the internet, and the most a
hostile one can do here is propose an edit you then decline.

**Picking the model.** The dialog has the same provider dropdown as the settings panel — LM
Studio, Ollama, OpenRouter, or a raw LiteLLM id — with **browse** listing what that server is
actually serving. Leave the box empty and she's optimised by her own utility model. The choice is
remembered in your browser and never reaches her registry record: which model re-files one card
says nothing about which model she should think with.

### It runs in three passes

*Who she is* (identity, history, appearance, manner, personality), then *where she is* (scenario,
greetings, lorebook), then *how she's played* (voice law, hard limits, examples, notes, version,
tags). Each pass sends only the fields that inform its own decisions and asks for only its own
group, and each one sees the draft the previous one left behind.

That structure is not tidiness — it's the only way this works on a model that thinks before it
answers. A reasoning model spends its `<think>` tokens out of the same window as its reply, and
one call carrying a whole card leaves it nothing to answer with. Measured on gemma-4-12b: a
single-call attempt spent all 2,873 available tokens reasoning and returned an empty string. Split
into passes, with a 32k window, all three land.

If a pass fails, the ones that worked are still yours — the dialog shows the diff, and names the
failed passes underneath with the reason.

The dialog watches the run rather than sitting still: the route streams a line per pass, and you
see which of the three is outstanding, what each one moved when it lands, and a clock that keeps
ticking. A retry is named where you can see it, because it roughly doubles that pass and a timer
that runs twice as long with no explanation is what makes people close the tab. Closing it *does*
stop the run — there is no point burning tokens for a pane nobody is reading.

### If it comes back empty

Almost always the window, not the model. Local runners load a model with a per-model default
context that's usually far below what the model supports — LM Studio's is often 8k — and the
optimiser says so with the numbers:

> the model thought for 6,293 tokens and had none left to answer with. It stopped at 8,192 tokens,
> of which this card's prompt was 1,896 — that is the context window it was loaded with, not the
> model's limit.

Raise it (`CONTEXT_LENGTH` in the settings panel, or the model's own load config) and run again.
A cut-off answer is salvaged down to the fields the model finished rather than thrown away, and
the dialog tells you it was partial.

Expect a few minutes on a local model — three passes, each thinking first. A hosted model is
considerably faster.

### Benchmarking it

```
python scripts/bench_cards.py ~/cards                       # the importer alone
python scripts/bench_cards.py ~/cards --optimize --model openrouter/…
```

Runs a whole folder through the real import path and scores which fields came out filled, plus
the house-style faults that survive (a scenario carrying world-building, a personality that is a
paragraph, an identity that is still a wall). One card tells you nothing; thirty tell you that
`Appearance` is empty on four of them.

## Exporting a character

The studio's **Export PNG** button, or `GET /api/characters/<id>/export` for the defaults.
You get a card carrying both a V2 (`chara`) and a V3 (`ccv3`) chunk, so it opens anywhere cards
open, plus a `yurios` block so it re-imports here with full fidelity — see
[the card format](card-format.md).

**What travels:** her portrait or a selfie you pick, her identity, persona, scenario, greetings,
examples, lore and creator notes — including everything she has grown into since you met.
**What never travels:** `USER.md`, relationship memory, the episodic journal, consolidated facts,
her goals, her world model, the corpus, traces, the tool audit, and every credential.

Sharing her is sharing who she is, not who you are. A card starts the relationship at zero.

### When the export refuses

Two different refusals, and the studio renders them differently:

- **`leak`** — content from a surface that never leaves this machine is in the card, or a
  credential, or your own name. Not overridable. The message names the surface and quotes the
  passage; fix the soul file and export again.
- **`review_required`** — a passage in her soul files also appears in a private surface. For a
  companion who has grown that is *expected*: she learned something about you, proposed it into
  her persona, and you approved it at the gate. The machine cannot tell that apart from a fact
  pasted into the wrong file, so it stops, shows you the passages, and asks once.

The one-click route fails closed for both, which is deliberate: an unacknowledged overlap that
merely warned would ship silently.

### Options

| Option | Default | What it does |
|---|---|---|
| Card format | `V3 + V2` | `V2 only` drops V3-only fields and lorebook decorators |
| Carry her soul files | on | the verbatim SOUL in the `yurios` block — off makes a re-import lossy |
| Credit YuriOS in the notes | on | a paragraph in `creator_notes` and a `yurios` tag; never her persona |
| Include dates | on | off writes `0` for both timestamps — a modification date is a disclosure |
| Face | her portrait | or any selfie, or an upload; framed to 512×768 |

## Loop switches

Each character carries three switches, independent of the house defaults:

| Switch | Effect |
|---|---|
| `mind` | her always-on tick loop. Toggling it takes effect live, with no restart |
| `utility` | the off-hot-path model work: fact extraction, summarisation |
| `dream` | nightly consolidation |

One companion can be a fully autonomous mind while another stays reactive-only. `utility` and
`dream` are wired at construction, so changing them restarts her runtime.

## Connection profiles

`data/connections.json` holds named profiles — `{name, backend, endpoint, api_key_env}`. A
profile says *where* a model is reached and *which environment variable* holds the key; it never
contains a secret. Keys stay in the host `.env` and are read by name.

Each character binds to a profile by name. On a first run with no file, the host seeds `default`
and `legacy-default` from your existing `.env` route, so an upgraded single-companion install
already points at something that works.

Her record overrides the host default **field by field** — chat model, utility model, TTS/STT
backend, voice register, body backend, avatar model. A blank binding means *inherit*, which is
what lets one `.env` still configure a house. See [Models](models.md) for what those routes are.

## One outside account, one character

Telegram's `getUpdates` is exclusive per bot token: two characters on one bot would fight over
its updates and neither would be reachable. So each companion gets her own @BotFather bot, named
with her registry id upper-cased — `TELEGRAM_BOT_TOKEN_MIA`, `TELEGRAM_CHAT_ID_MIA`.

The gear in *her own room* edits exactly that pair and nobody else's, so pasting a token there
can never take over another companion's chat. Full detail in [Channels](channels.md#telegram).

## Archiving and purging

Two different acts, deliberately:

- **Archive** stops her runtime and renames her root to `data/archives/<id>-<timestamp>`. Her
  files survive; she leaves the board.
- **Purge** deletes her root, and requires a confirmation string matching her id or display name.

Nothing else in YuriOS deletes a character root.

## Migrating from 0.1

On the first 0.2 start, the legacy roots — `VAULT_DIR`, `corpus/`, `traces/`, `tool-logs/`,
`selfies/` — are assembled into a registered character under `DATA_DIR`, before any mind wakes.
The originals are **copied, never moved**, and stay untouched as your backup.

Run it explicitly if you'd rather watch:

```bash
python -m yurios.migrate --check      # validate and report only
python -m yurios.migrate --dry-run    # show the migration without writing
python -m yurios.migrate              # do it
python -m yurios.migrate --data-dir /somewhere/else
```

The migration assembles the character in a staging directory on the same filesystem, makes it
visible with one rename, and writes `layout.json` **last** — that marker is the durable record
that it finished, so an interrupted run is a no-op you can simply run again.

It refuses rather than risks. Symlinked or unreadable legacy trees, an invalid `soul.yaml`, an
unsupported `vault_format`, a registry rooted at a different `DATA_DIR`, a colliding destination,
or a Vault git repository it can't commit all abort with an explanation and no partial character.
A legacy Vault that is a git repository keeps its history and gains one migration commit.

A migrated Yuri who has no portrait gets the shipped default one — once, and only when the file
is absent. A portrait you replaced, or one the forge rendered, is hers and is never overwritten.

## Adding a second companion

1. Get her card (or export one from a companion you already have).
2. Import it on the board, and read her profile.
3. Save the profile to accept the review — she starts.
4. Optionally give her her own model (profile drawer), her own bot
   (`TELEGRAM_BOT_TOKEN_<ID>` in `.env`), and her own body.

She gets her own Vault, her own memory, her own goals and her own journal from the first tick.
Nothing is shared but the house.
