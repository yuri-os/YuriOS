# Troubleshooting

Two commands answer most questions:

```bash
python -m yurios.doctor            # what .env selects vs what's actually installed
curl localhost:8768/api/health     # what's actually running right now
```

The doctor reads the same `.env` the server reads, checks each selected backend against what's
importable, and prints the exact install command for anything missing — plus the `.env` change
that avoids the download altogether where one exists. `/api/health` reports the live truth:
voice, tools, mind, selfies, channels, viewers and context.

## She won't start

**`frontend not built; run npm run build in web`** — the frontend isn't compiled:

```bash
cd web && npm ci && npm run build
```

**`ModuleNotFoundError`** — you're probably outside the venv. `source .venv/bin/activate`. The
doctor names the venv you didn't activate.

**A resolver that hangs or drags in an ancient dependency** — check your Python version. YuriOS
requires **3.11–3.13**; the upper bound is real, and installing on 3.14 makes pip walk years of
litellm releases looking for one that accepts it.

**`character storage overlaps: … and …`** — two characters point at the same Vault, corpus,
traces, tool-log or selfie directory. The host refuses to start rather than let two minds
interleave commits in one Vault. Fix the paths in `data/characters.json`.

**`unsupported character registry schema`** — `characters.json` was written by a different version.
It's refused rather than guessed at.

## She's silent

Check `/api/health`'s `voice` block first.

- `"tts": "unloaded"`, `"listeners": 0` — she's at rest, not broken. Her voice loads when a
  client opens `/ws/voice` (enter one of her rooms) and is freed `VOICE_UNLOAD_AFTER_S` after
  the last one leaves, so a node hosting several characters keeps one voice resident instead of
  one per character. `VOICE_PRELOAD=1` warms it at boot instead.
- The first person into a cold room waits ~20 s for the models; the room captions it
  ("loading her voice…") rather than looking hung.
- `"tts": "fake"` — the voice extra isn't installed. `./install.sh` (without `--thin`), or
  `pip install -e ".[voice]"`.
- kokoro falls back to the fake — install `espeak-ng` as a **system** package. The pip wheel
  bundles a copy whose phoneme data loses espeak-ng's own path-resolution race, and its answer to
  data it can't read is to `exit(1)` the process; kokoro checks in a child process and falls back
  rather than taking the server down.
- You haven't clicked **start listening** (the mic button, bottom-left). Voice won't work until
  the page has your microphone.
- No greeting at all — you skipped the "enter the sanctuary" gate, which is what user-activates
  the `AudioContext`.

## She can't hear me

- Lower `VAD_THRESHOLD` (default `0.5`) for a quiet mic.
- Set `VAD_CONFIRM=false` if an over-strict VAD is dropping real speech; the transcript filter
  still catches punctuation-only hallucinations.
- She cuts off mid-sentence when you type — raise `VAD_BARGEIN_FRAMES`.
- She starts turns on keyboard noise — raise `VAD_ONSET_FRAMES`.

## Turns fail, or replies vanish

**"Context size has been exceeded"** — set `CONTEXT_LENGTH`. With `0`, LM Studio serves whatever
its per-model config defaults to, often far below what the model can do. Set it and the model is
pinned at that size *and* the masthead shows prompt tokens against it.

**Every turn is slow, with a reload each time** — `LMSTUDIO_PRELOAD=false`, or a non-LM-Studio
server. LM Studio's JIT loader unloads the last JIT-loaded model to serve the next request, so
without pinning, chat and embeddings evict each other every turn. Set `LMSTUDIO_PRELOAD=true`.

**She thinks out loud before answering** — `CHAT_THINKING=true` with a reasoning model. Set it to
`false`; the utility model keeps thinking on, off the hot path.

**An empty reply** — `MAX_REPLY_TOKENS` too small for a reasoning model: the `<think>` block eats
the budget and the reply comes back empty.

**A hosted model refuses to stay in character** — that's the model, not the runtime. See the note
about refusal-trained models in [Models](models.md#lm-studio-the-shipped-default).

## The room is black, or the body doesn't load

- `her body failed to load — check web/models/` — `web/models/avatar.vrm` is missing.
- The room renders but crawls — add `?fx=low`, or `?fx=phone` for the cheapest tier of all. Both
  turn on by themselves (a window under 900 px wide is `low`; a touch screen that small is
  `phone`), and the page drops render resolution on its own when frames slip, so a crawl on a
  desktop card usually means something else is holding the GPU. See
  [Bodies → Performance](bodies.md#performance). If this machine simply has no GPU to spare,
  open her [text room](bodies.md#the-text-room--no-body) instead — same conversation, no renderer.
- The Live2D page runs voice-only — the runtime isn't fetched: `python scripts/fetch_live2d.py`.
- A Live2D rig shows the default body — that key isn't installed. `GET /api/config` lists which
  ones are.

## The desktop window

- `--window` does nothing / import error — `./install.sh --desktop` (or `pip install -e
  ".[desktop]"`). The doctor reports this as advisory, since it only matters if you pass
  `--window`.
- She judders on Linux — `WINDOW_GUI=qt`. WebKitGTK caps `requestAnimationFrame` at ~30 fps.
- **On WSL** you get an opaque, titled Edge window — install the Electron shell from **Windows**
  (not from WSL): `cd desktop-shell && npm.cmd install`. See [Bodies → WSL](bodies.md#wsl).
- The mic doesn't work in the WSL window — the WSL VM's address isn't a secure context. The shell
  passes a per-run switch scoped to exactly that origin; the Edge fallback doesn't.

## Selfies come out as placeholder cards

`/api/health`'s `selfies` field says which case you're in:

| Value | Cause |
|---|---|
| `mock (no key — placeholder)` | `SELFIE_BACKEND=openrouter` with no `OPENROUTER_API_KEY` |
| `mock (diffusers unavailable — placeholder)` | missing `.[forge-local]`, or `SELFIE_LOCAL_MODEL` doesn't point at a checkpoint |
| `mock (krea2 unavailable — placeholder)` | missing `.[forge-krea2]`, or no Hugging Face access to the gated `krea/Krea-2-Raw` |

Every one of them also logged a WARNING at startup with the fix.

**Renders take ~70 s** — the pipeline is being CPU-offloaded because VRAM is full. Turn on
`SELFIE_LLM_PARK` (the default) to lend the LLM's VRAM for the render's duration; it drops to
~15 s and her brain is always re-pinned afterwards, even on a failed render.

**A render never starts while she's talking** — that's deliberate. A park mid-reply would kill the
streaming turn, so a render that needs one waits for a quiet moment.

## She never says anything on her own

That's the default, and it's the conservative half of the design: most ticks end in REST, and the
default gate-2 outcome is SILENT — do it quietly and journal it.

Check the journal and the trace before turning dials:

```bash
curl localhost:8768/api/mind/journal
tail -f data/characters/yuri/traces/ticks.jsonl
```

If you genuinely want more initiative, lower `MIND_INTERRUPT_THRESHOLD` (0.75) or raise
`MIND_MAX_INTERRUPTS_PER_DAY` (3). Remember quiet hours (~22:00–09:00) are a **hard gate**, not a
weight — no score gets through them.

## She talks too much

Raise `MIND_INTERRUPT_THRESHOLD`, lower `MIND_MAX_INTERRUPTS_PER_DAY`, or lengthen
`IDLE_TALK_MIN_S`/`IDLE_TALK_MAX_S` for the self-talk murmur. `MIND_ENABLED=false` turns the whole
autonomous half off and leaves the reactive companion intact.

## Telegram

- **Nothing happens** — she's in pairing mode. Message the bot; it replies with the
  `TELEGRAM_CHAT_ID` to set. Then restart.
- **`Conflict: terminated by other getUpdates request`** — two runtimes on one token. Give each
  companion her own bot (`TELEGRAM_BOT_TOKEN_<ID>`), or name the owner with `TELEGRAM_CHARACTER`.
- **`held by <her>`** — healthy, not failed: the shared unsuffixed bot went to whoever started
  first. Give this one her own.
- **A local chat is not copied to Telegram** — this is the safe default. Enable
  `TELEGRAM_SEND_NON_TELEGRAM` in settings if you intentionally want cross-chat forwarding.

## Memory and the index

- Recall looks wrong after changing embeddings — the auto-reindex should have run; force it with
  `python scripts/reindex.py`.
- `EMBED_DIM` must equal the model's vector width (bge-small = 384, nomic = 768). A mismatch fails
  at reindex.
- The markdown is authoritative and the index is a cache. Hand-edit the files freely, then
  reindex.

## Migration from 0.1

`python -m yurios.migrate --check` reports without touching anything. It refuses rather than
risks: symlinked or unreadable legacy trees, an invalid `soul.yaml`, an unsupported
`vault_format`, a registry rooted at a different `DATA_DIR`, or a Vault git repo it can't commit
all abort with an explanation and **no partial character**. Your legacy directories are copied,
never moved, so nothing is lost while you sort it out.

## Still stuck

- `git -C data/characters/<id>/vault log` — every change she made to herself, one commit per tick.
- `data/characters/<id>/tool-logs/calls.jsonl` — every tool call, allowed or denied.
- `data/characters/<id>/traces/ticks.jsonl` — why she did or didn't do a thing.
- `signals.jsonl` — what woke her.

The answer to "why did she…" is in the trace. That's what it's for.
