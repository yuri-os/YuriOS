# The YuriOS card format

A YuriOS character card is an ordinary [Character Card V3][ccv3] `.PNG`. Any client that reads
V3 — SillyTavern, Chub, CharaVault, anything descended from TavernAI — opens it and gets a
complete, working character with no knowledge of YuriOS at all.

What makes it a *YuriOS* card is one extra key: `data.extensions.yurios`. Other runtimes ignore
it; YuriOS reads it and reconstructs the companion's soul byte-for-byte instead of re-deriving
it from flattened prose. This page is the published contract for that block, so other runtimes
can read our cards and we can read theirs.

Normative detail: [`SPEC.md` §28](../SPEC.md). Behaviour is pinned by
`tests/test_card_roundtrip.py` and `tests/test_export_privacy.py`.

## The envelope

Exactly what V3 specifies, nothing invented:

- a `tEXt` chunk named **`ccv3`**, holding base64 of the UTF-8 JSON of the V3 object;
- a `tEXt` chunk named **`chara`**, holding the same character as a V2 object, so V2-era
  clients still work. V3-aware readers must prefer `ccv3`.

Both chunks are spliced in immediately after `IHDR`. They are written as `tEXt` **by hand** and
not through Pillow's `PngInfo`, because some Pillow versions emit `iTXt` for text metadata and
SillyTavern reads only `tEXt` — a card written the convenient way arrives at the far end as
"contains no character data". Every export re-reads its own bytes with both a strict parser and
a client-shaped one before returning them; if either cannot find her, no file is produced.

Selecting `V2 only` in the studio writes just `chara`, drops the V3-only keys
(`nickname`, `assets`, `source`, `group_only_greetings`, `creator_notes_multilingual`,
`creation_date`, `modification_date`) and strips `@@` lorebook decorators from entry content.

## `data.extensions.yurios`

```jsonc
{
  "schema_version": 1,
  "runtime": "YuriOS",
  "runtime_version": "0.2.0",
  "docs": "https://yurios.org",

  "soul": {
    "manifest": { /* soul.yaml, parsed, minus `runtime_only:` */ },
    "files": {
      "soul.yaml":        "…",
      "CONSTITUTION.md":  "…",
      "PERSONA.md":       "…",
      "SCENARIO.md":      "…",
      "BOOTSTRAP.md":     "…",
      "EXAMPLES.md":      "…",
      "WORLD.md":         "…",
      "NOTES.md":         "…"
    },
    "encoding": "utf-8",
    "sha256": { "PERSONA.md": "…", /* … */ }
  },

  "lineage": {
    "character_id": "yuri",
    "card_version":  "yuri-v2@canon-v2",
    "canon":         "canon-v2",
    "vault_head":    "a1b2c3d",
    "generation":    2,
    "grown_from":    "sha256:…"
  },

  "growth": {
    "days_lived":         94,
    "vault_commits":      1204,
    "self_edits_applied": 17,
    "soul_files_changed": ["PERSONA.md", "SCENARIO.md"]
  },

  "body":  { "backend": "vrm",    "model": "…" },
  "voice": { "tts_backend": "kokoro", "voice_id": "…" }
}
```

### `soul` — why the files travel

A card's prose is a *flattening*. `description` is four sections glued together; the immutable
`CONSTITUTION` and the editable `PERSONA` become one paragraph; frontmatter keys vanish. Import
that and you get a character who reads correctly and has lost the structure her runtime needs —
YuriOS's importer has to synthesise the missing pieces and writes `_(Not supplied by the card.)_`
where it cannot.

Carrying the files verbatim makes the trip lossless: a re-import writes `vault/soul/` exactly as
it was, and `git diff` between the two machines is empty. That is what "boots on someone else's
machine" means.

`soul.files` is **optional**. Omit it (the studio's *Carry her soul files* toggle) and the card
is a perfectly ordinary V3 card that still imports — just through the lossy path every non-YuriOS
card uses.

**Caps, and how they degrade.** Over 256 KB, one file is dropped from the payload. Over 512 KB
total, the payload is dropped whole and `"soul_omitted": "size"` is set. Over 1 MB of serialized
card JSON, the same. Over 3 MB of serialized card JSON, the export refuses. These thresholds do
not include the rendered portrait PNG bytes. A typical soul is 8–40 KB, so none of this fires in
practice; it exists so that when it does, the card still opens.

**Reading a payload from a stranger.** It is a map of filename → contents, which is a file-write
primitive handed to you by an untrusted `.PNG`. YuriOS validates before writing anything: plain
basenames only (`[A-Za-z0-9._-]`, `.md` or `.yaml`, no separators, no leading dot), never
`USER.md` or `MEMORY.md`, ≤ 256 KB each and ≤ 1 MB total, valid UTF-8, digests must match where
given, and `soul.yaml` must parse *and* every one of its `fields:` references must resolve
against the files provided. Any failure falls back to synthesis rather than raising — a card that
cannot be trusted to carry a soul is still a perfectly good card.

### `lineage`

`generation` counts export → import hops: 0 for a card cut from a character who was never
imported, +1 each time round. `grown_from` is the SHA-256 of the card she was imported from, if
any. `vault_head` names the Vault revision the card was cut from, captured in the same read as
the files, so the card describes exactly the bytes it shipped even if her mind commits a second
later. `card_version` is the `§5.2` stamp her journal and corpus already carry.

### `growth`

**Counts, never content.** "94 days lived, 1,204 commits, 17 approved self-edits" says
everything about the runtime and discloses nothing about the relationship. `days_lived` is
omitted when timestamps are switched off.

## What is never on the card

Not "excluded" — unreachable. The exporter derives one soul folder from a character record and
takes no path from any caller, so `corpus/`, `traces/`, `tool-logs/`, `memory/`, `world/`,
`knowledge/`, `goals.md`, `state/` and `.git/` are never named by it. On top of that:

- the SOUL reader refuses `USER.md`, `MEMORY.md` and anything in the manifest's `runtime_only:`
  list, however a `fields:` reference asks — including through a symlink;
- the card is built key by key from a fixed V3 allowlist, never copied from `card.json`;
- and before any bytes are returned, the exporter harvests canaries from every private surface
  of *that* vault and refuses if one appears in the card or in the image's metadata. Credentials
  and a distinctive `USER_NAME` are hard blocks at any length.

A passage that appears both in her soul files and in a private surface is the interesting case:
a grown companion legitimately has things she learned about you and you approved into her
persona at the self-edit gate. The machine cannot tell that apart from a fact pasted into the
wrong file, so it stops, shows you the passages, and asks — once. See
[Characters → exporting](characters.md#exporting-a-character).

Her memory of you never travels. **A card starts the relationship at zero** — `USER.md` arrives
empty, `memory/` is empty (which is where `MEMORY.md`'s two halves live in a Vault:
`memory/semantic/facts.md` and `forgotten.md`, seeded blank), `goals.md` is empty, and the new
Vault's git history begins at one commit.

## Attribution

Default on, and confined to fields no client puts in the prompt:

- a paragraph appended to `creator_notes`;
- `"yurios"` appended to `tags`;
- `https://yurios.org` appended to V3's append-only `source` array.

It never touches `description`, `personality`, `scenario`, `first_mes`, `system_prompt` or
`post_history_instructions`. Writing marketing copy into someone's companion's persona — prose
that defines who she is and costs tokens on every turn in every client — would be the runtime
editing the character. The attribution toggle removes the `creator_notes` paragraph and the
`yurios` tag. The V3 `source` array always retains `https://yurios.org`, as V3 treats it as
append-only provenance.

## Timestamps

`creation_date` is her first Vault commit; `modification_date` is the export. Switching
*Include dates* off writes `0` for both, which V3 explicitly allows — a modification date is a
disclosure of when you were last at the keyboard.

## Reading a YuriOS card from another runtime

Ignore `extensions.yurios` entirely and you have a valid V3 card. If you want fidelity:

1. prefer the `ccv3` chunk; fall back to `chara`;
2. if `data.extensions.yurios.soul.files` is present, validate it as above and write the files
   as your persona source rather than parsing the flattened fields;
3. treat anything under `growth` as display-only, and `lineage.generation` as advisory;
4. and if you re-export, leave `source` intact — V3 says it is append-only.

[ccv3]: https://github.com/kwaroran/character-card-spec-v3
