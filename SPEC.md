# YuriOS — SPEC

Normative specification for YuriOS. Keywords **MUST**, **SHOULD**, **MAY** are used in
the RFC-2119 sense.

YuriOS is **one independent, self-contained project**: an always-on, local-first
companion who lives on the user's own machine — a 3D body you can see, a real-time voice
loop you can speak into, five tools she can reach for, one outbound event bus, and an
**always-on mind** that keeps running whether or not anyone is looking. Everything below
is this project's own code, run as **one process, one origin** (`python -m yurios.world`,
port **8768**). The Python packages are `yurios/app` (the brain), `yurios/desktop` (the
voice loop), `yurios/forge` (the image service), `yurios/world` (the body, the tools, the
bus, the server), `yurios/mind` (the autonomy engine) and `yurios/characters` (the
registry, the card reader, the connection profiles — Part III); the SOUL she is seeded from
lives in `soul-src/`, the browser frontend in `web/`. Where a subsystem has a history
worth recording, that history is documented in `PROVENANCE.md` — provenance, not a
dependency.

This document is organised in three parts. **Part I — the body (§1–§14)** specifies the
reactive companion: the brain and its file-Vault, the voice loop, the 3D body, the hands,
and the one bus. **Part II — the mind (§15–§25)** specifies the always-on autonomy engine
built on top of it. **Part III — the house (§29–§33)** specifies the 0.2 layer that runs
more than one of her: the host, the character registry, cards, connection profiles, the
switchboard, and the 0.1 → 0.2 migration. The cross-cutting omissions, tests, and growth
path are §26–§28, which sit between Parts II and III in the text because they were
written first — section numbers are stable, and in-source `SPEC §n` comments cite them,
so nothing is ever renumbered.

---

# Part I — the body (§1–§14)

## §1 — Goal and properties

A browser-based 3D companion — a VRM body in the canonical sanctuary, a chat transcript
beside her, a real-time voice loop, five tools over MCP, one event bus — driven by an
**always-on autonomy engine**. She runs continuously: pursues small goals when the user
is away, consolidates memory while they sleep, keeps her own promises, reads what lands on
her shelf, and reaches out *first* when — and only when — a salience model says it is
welcome.

The properties this build clears:

- **Identity** — a static SOUL (`soul/`) is the immovable backbone of every prompt, read
  on every turn (§2.1).
- **Honest memory** — durable facts and episodic recall persist as human-readable files
  in a git-backed Vault the user owns; she admits the edges of memory rather than
  confabulating (§2.1).
- **Embodiment** — a full 3D body *in a place*, with visemes, expressions, gaze, and a
  real-time voice (§3–§9). She is an AI but never bodiless (§2.5).
- **Hands** — reactive tool use: the capability half of owned agency (§7).
- **Initiative** — the always-on half of owned agency: a mind that decides, between turns,
  when to act and when to reach out (Part II).
- **One-on-one** — a single user, no audience, no engagement mechanics, no upsell in the
  loop.
- **Yours** — every dial, file, and log below is on the user's disk, under git; nothing
  phones home. Pulling the network cable **MUST NOT** change behaviour once the local
  models are installed.

Contracts that bind the whole build:

- The mind **MUST** be an always-running process state, not a callback: between turns it
  exists, ticks, and decides — the opposite default from a request/response chat loop.
- The reactive body **MUST** keep working with the mind disabled: with `MIND_ENABLED=false`
  the build degrades to exactly the reactive companion minus ambient life — the mind is
  additive, never load-bearing for conversation.
- The interrupt model is the make-or-break component and **MUST** ship conservative: a
  build that pings constantly has failed as surely as one that never speaks (§18).
- The build **MUST** be shaped around **one outbound event bus** (§10) carrying every
  host→frontend event as typed JSON, so a frontend is a thin view and any medium is a
  frontend (§10.5).

```
 python -m yurios.world — one process, one origin (:8768)
 ┌────────────────────────────────────────────────────────────────────────────────────┐
 │  the reactive body (§2–§10):                                                        │
 │  the brain · the voice loop · ToolBrain + MCP hands · SelfieLab ·                   │
 │  VrmController → EventHub → /api/events (SSE) · /ws/voice (audio only)              │
 │                          ▲ the same strings                                         │
 │                          │                                                          │
 │  yurios/mind — THE TICK LOOP (§15): SENSE → APPRAISE → DECIDE → ACT → REFLECT →      │
 │     ▲ SignalBus (§16): user turns · timers · presence · drops · decisions   REGULATE│
 │     ├ activity states ENGAGED/IDLE/DORMANT/DREAM + budget governor (§17)             │
 │     ├ gate 1 salience-to-act · gate 2 salience-to-interrupt (§18)                    │
 │     ├ WorldModelStore (§19) · KnowledgeStore (§20) · DREAM (§21) · goals (§22)       │
 │     ├ SOUL split + gated self-edits (§23)                                            │
 │     └ journal + tick trace → /api/mind + the inner-life panel (§24)                  │
 └────────────────────────────────────────────────────────────────────────────────────┘
```

The control model is: **the body is a puppet, the brain holds the strings.** All decisions
live in Python; the browser is a render-and-control client. The Python control surface
(`VrmController`, §4) is the seam the tick loop holds (§15.5).

## §2 — The brain, the voice, and the seam

The brain lives in `yurios/app`, the voice stack in `yurios/desktop`, the image service in
`yurios/forge` (§7.6), and the SOUL source in `soul-src/` — all first-party packages of
this project. `yurios/world/brain.py`'s `ToolBrain` subclasses the voice stack's
`BrainAdapter`, adding the tool loop (§2.3). Reuse across packages is ordinary internal
composition — subclass, call, extend — and each package is free to evolve on its own.

### §2.1 — The brain: a SOUL, a Vault, and a prompt

The mind is a **git-backed Vault of human-readable files** — the files *are* the database.
A derived, rebuildable local index does retrieval; it is a cache, never the source of
truth. Ownership, inspectability (`cat` / `git log`), and no-rug-pull all come from this.

**The Vault layout** (`vault/`, one git repo):

```
vault/
├── soul/                    # the persona — seeded from soul-src, read every turn
│   ├── CONSTITUTION.md       #   immutable — identity, voice law, hard limits
│   ├── PERSONA.md            #   appearance, manner, inner life, personality line
│   ├── SCENARIO.md           #   the place + the return greetings
│   ├── EXAMPLES.md           #   demonstrated voice (<START> blocks)
│   ├── WORLD.md              #   lorebook (keyword-triggered, sparse)
│   ├── USER.md               #   RUNTIME — the partner model: durable facts about the user
│   └── soul.yaml             #   manifest: which sources feed which prompt section
├── memory/
│   ├── episodic/             #   YYYY-MM-DD.md — append-only journal of exchanges
│   ├── semantic/
│   │   ├── facts.md          #   consolidated general facts (grows in DREAM, §21)
│   │   └── forgotten.md      #   the forget-ledger — supersede-not-delete tombstones
│   ├── summary.md            #   the rolling "what we've talked about"
│   └── index/                #   DERIVED: embeddings index — gitignored, rebuildable
├── world/                    # the world model (Part II, §19)
├── knowledge/                # the drop-folder knowledge layer (Part II, §20)
├── goals.md                  # her intentions (Part II, §22)
├── state/                    # sessions, activity, budget, engine cursor, pending edits
└── .gitignore                # memory/index/ (derived, never committed)
```

`USER.md` and everything under `memory/` (except the gitignored `index/`) are **committed**
— the mind's growth is a `git log` you can read and `git revert`. `soul/` is seeded once
from `soul-src` and then lives in the Vault; `CONSTITUTION.md` is never edited by the
reactive body (Part II gates who may edit it, §23).

**Prompt assembly** (`yurios/app/core/assemble.py`) composes the model input from the SOUL
(static) + the Vault (current) + a small raw window. The system prompt, top to bottom:

```
1. VOICE LAW              (CONSTITUTION#Voice law)
2. PERSONA BACKBONE       (CONSTITUTION#Identity/#History + PERSONA#Appearance/#Manner + @personality)
3. SCENARIO / PLACE       (SCENARIO#Scenario)
4. LORE (if fired)        (matched WORLD.md entries, keyword-triggered)
5. WHO YOU ARE TO HER     (vault/soul/USER.md, whole — it is small)
6. WHAT YOU'VE TALKED ABOUT (vault/memory/summary.md)
7. THINGS THAT MAY BE RELEVANT (recall(user_msg, k), each tagged with age)
8. WHAT YOU'VE READ       (knowledge.search(user_msg, k), each with its citation — §20.2)
9. THE HONESTY CONSTRAINT (§2.1, fixed text)
10. EXAMPLE VOICE (if budget allows)
```

followed by the last `RAW_WINDOW_TURNS` raw messages (default 6) and the new user message.
`CONSTITUTION.md#Hard limits` (post-history instructions) **MUST** be appended **after** the
history, so it is the last thing read before replying. The raw window **MUST** stay small
(long raw context degrades middle recall); the rolling summary carries older context
cheaply. On overflow, **drop the examples first, then knowledge, then recalled memories, then
the lorebook; never drop the voice law, persona, `USER.md`, or the honesty constraint.**
Knowledge goes before memory in that order because a chunk is a paragraph rather than a line
(so one buys back what a dozen memories would), because the shelf is on disk and the same
search runs again next turn, and because of the three it is the least *hers*.

Blocks 7 and 8 are the two retrieval slots and **MUST** stay separate (§20): memory cites a
conversation turn, knowledge cites a document + span. A page she read **MUST NOT** be
assembled as something the user told her.

**The honesty constraint** (property: honest memory) is a fixed block: *she remembers only
what is in the memory blocks and the current conversation; asked about something with no
record, she says so warmly and plainly and asks, rather than inventing a memory; and the
rule runs both ways — she never claims to "remember" details that are not actually present.*
This is verified by a golden-transcript test (§27).

**The memory contract** (`yurios/app/memory/store.py`, `MemoryStore`) is implemented
file-backed:

- `remember(record)` (post-turn, off the hot path): append the exchange to
  `memory/episodic/<today>.md`; embed and upsert one index chunk; and call the utility
  model to extract *durable* facts about the user and update `USER.md` (merge, don't
  duplicate — pass it the current `USER.md`). Externally-sourced or low-confidence claims
  are **quarantined** until a second turn corroborates. `remember` **MUST** tolerate a
  malformed utility response (log and drop, never fatal to the turn), and **MUST** attribute
  facts to the correct speaker — her own self-statements are never recorded as facts about
  the user.
- `recall(query, k)` (hot path): embed the query, ANN-search the index, rank by
  `similarity · salience · recency_decay(age)` (half-life default 30 days — old memories
  fade, never vanish), MMR-rerank to diversify, drop below `RETRIEVAL_MIN_SIM`, return top
  k. An empty Vault returns `[]`; assembly proceeds on SOUL + `USER.md` alone.
- `forget(selector)` is **supersede-not-delete**: remove the line from the working
  `USER.md`/`facts.md`, append a tombstone to `memory/semantic/forgotten.md`, and commit.
  The old value survives in `git log` but is gone from every future prompt — assembly never
  reads `forgotten.md`, and `recall` drops any chunk whose source text is tombstoned.
- `inspect(selector)` returns what she knows and why (source, confidence) — the file
  backend gets it almost free (`cat`, `git diff`). The debug view reads memory *through*
  `inspect()`, never around it.
- `consolidate()` is the DREAM pass — a stub in the reactive body, implemented by the mind
  (§21).

**The derived index** (`memory/index/`, `sqlite-vec` or a flat vector index) is a
rebuildable cache: one row per chunk with `source_path`/`source_span` back to the markdown,
gitignored, rebuilt by `yurios/app/memory/reindex.py`. It records an embedder fingerprint;
a same-dimension embedder swap that would silently poison recall triggers an **auto-rebuild
from the `.md` files** at boot. The markdown is authoritative — if the index disagrees,
rebuild it.

**The corpus log** (`yurios/app/corpus.py`) appends one faithful record per reply to an
append-only JSONL log (`corpus/turns.jsonl`) — the only place raw, trainable conversation
data is kept, the seed of a future distillation corpus. `corpus/` is **personal data, not
code**: gitignored, outside `vault/`, never committed, no phone-home. Each record carries
the full prompt as sent, the completion, the model, and a `collection_scope` that **MUST**
be one of `self` or `consented_hosted` (asserted in code) — a shipped card never logs a
stranger's conversation home. Ratings arrive later in a sidecar and merge at export.

### §2.2 — The world voice route

`yurios/world/routes/voice_ws.py` is the world server's voice route — it builds on the base
voice route (`yurios/desktop/routes/voice_ws.py`) and adds what a body in a room needs: the
ambient-speech seam (§9), the transcript tee to the chat and the mind (§2.6, §15.3), and the
expression re-route onto the bus (§10). It shares the base route's turn contract — one
`TurnController` per connection, barge-in-as-cancel, no-trace-on-failure — and is free to
diverge from it wherever the world's needs differ; no fork-tracking discipline is imposed.

### §2.3 — The tool loop wraps the brain

`ToolBrain` **MUST** subclass the voice stack's `BrainAdapter`, overriding reply streaming
(§7.4) and extending prompt assembly with exactly one appended block — the situation (§2.5);
`persist` and the greeting are inherited. The provider seam (text tokens in, text tokens
out) stays untouched: tools ride *in* the token stream, the same discipline as the emotion
tags (§9).

### §2.4 — Identity, models, and the reasoning switch

Her identity is the SOUL (§2.1); `VAULT_DIR` **MUST** allow pointing at an existing Vault to
continue that companion — copy the folder to move her.

All provider-facing model surfaces sit behind three Protocols (`yurios/app/providers/base.py`):
`ChatModel.stream`, `UtilityModel.complete`, `Embedder.embed` — nothing else imports an SDK.
The chat/utility model is routed by the model-id **prefix** through LiteLLM, so local and
hosted are a one-line change with no code change:

| Prefix / backend | Route | Auth / endpoint |
|---|---|---|
| `openrouter/<id>` (or a bare `<id>`) | hosted OpenRouter | `OPENROUTER_API_KEY` |
| `ollama/<model>` | local Ollama | — |
| `lm_studio/<model>` | local LM Studio (OpenAI-compatible) | `LMSTUDIO_BASE_URL` |

**Attribution.** Every *billed* OpenRouter request — the chat/utility models here, her camera
in §7.6 — **MUST** carry the app-attribution headers from `yurios/attribution.py`
(`HTTP-Referer`, `X-OpenRouter-Title`, and its legacy `X-Title` spelling, which overrides the
`liteLLM` default LiteLLM would otherwise send). OpenRouter keys the app page on the referer
URL, so all callers **MUST** send the same one or the usage splits in two; local routes send
none. Reads that cost nothing (the model listing behind the settings page) need no headers.
The same headers carry a composite `User-Agent` — `YuriOS/<version> <client>/<version>` —
which affects no attribution and exists so a provider's logs name her, not just her plumbing.
`yurios doctor` prints what a given `.env` actually puts on the wire.

The default stack is **local and needs no key**: an LM Studio model backs the mind, while the
in-process sentence-transformers embedder (`EMBED_BACKEND=sentence_tf`) owns memory without a
second server. An LM Studio embedder may instead reuse the chat server when explicitly selected.
Embeddings are always local and ownable. A failed backend **MUST** degrade gracefully (keep
talking, log the truth) rather than crash.

**Model residency.** Sharing one server has a cost that is not obvious: LM Studio JIT-loads
whatever model a request names, and by default unloads the previously JIT-loaded one to do it.
Every turn touches both models — the chat model streams the reply, the embedder recalls and
remembers — so left alone each turn evicts the other's model and pays a full reload (measured:
5.7 s for a 6.3 GB chat model, 1.9 s for an 84 MB embedder, *per turn*). At boot YuriOS
therefore **MUST** load every model it routes to LM Studio explicitly, through that server's
developer API (`POST /api/v1/models/load`), with no `ttl_seconds` — an explicit load is not a
JIT load, so the eviction rule never touches it and no idle timer unloads it. This changes
nothing in the user's LM Studio configuration: it is the Load button, pressed over HTTP, and
so **MUST** work against a stock install (`LMSTUDIO_PRELOAD=false` opts out). Like any backend
it degrades: an unreachable server, a model that is not downloaded, or one that will not fit
is logged and boots anyway, back to JIT loading — slow, not broken.

**The context window.** A local model is loaded into a *fixed* window, and the prompt grows
every turn (§7.1's blocks, the raw window, the situation, the tools directive). Left to the
provider's default — for LM Studio the per-model config its UI would load with, routinely a
fraction of what the model can do — a long enough conversation ends with the server refusing
the turn outright ("Context size has been exceeded") and the reply lost. So the window is a
knob and a readout, both. `CONTEXT_LENGTH` (0 = the provider's default) **MUST** be sent as
`context_length` on the explicit load above, so the number in `.env` is the window she
actually runs in, and a model already pinned in a *smaller* window **MUST** be reloaded to
honour it (a larger one is left alone — it already fits). The frontend **MUST** show how full
that window is: prompt tokens against the ceiling, published as a sticky `context` event on
the one bus (§10) so a page joining mid-conversation sees the last reading. The used side is
the server's own `prompt_tokens` where the route will report it (`stream_options`, allowlisted
per route — a rejected parameter costs the whole reply), else the §7.2 estimator, and which
one it is **MUST** be on the wire: an estimate is shown as approximate. Where the server
reports the window it actually loaded, that observation **MUST** outrank `CONTEXT_LENGTH` on
the gauge: a request that could not be honoured (a window too large for the machine) must
never be displayed as room she has. An unknown ceiling
(a hosted route, an LM Studio that will not say what it loaded) **MUST** read as unknown
rather than be guessed from the model's *maximum* window, which is a different number — often
30× larger — and would promise room that is not there.

**The reasoning switch.** A local model **MAY** be a reasoning model (a `<think>` pass before
the answer). Two knobs, one per role: `CHAT_THINKING` (the reply voice) and `UTILITY_THINKING`
(fact extraction / summarisation). For the **real-time voice loop, reply reasoning is OFF**
(`CHAT_THINKING=false`) — a `<think>` pass on the hot path would delay or, worse, empty the
spoken reply — while the **utility model keeps thinking** (`UTILITY_THINKING=true`): it runs
off the hot path where extraction quality matters and its latency is free. Thinking-on needs
token headroom so the pass *and* the answer both fit; thinking-off rides `reasoning_effort:
"none"` in the raw request body (`extra_body`) — passed as a top-level LiteLLM arg it is
rewritten and never applied — with a `/no_think` system-token fallback. Both are inert on a
non-reasoning model. The utility path **MUST** strip a leading `<think>…</think>` before
parsing its JSON, and **MUST** budget enough tokens (`UTILITY_MAX_TOKENS`) that a reasoning
pass does not truncate the answer to an empty string and silently lose the fact.

### §2.5 — The situation block: she knows when and where she is

Every assembled prompt — reply, greeting, and ambient speech alike — **MUST** carry one
appended `## THE SITUATION RIGHT NOW` block, rendered fresh per prompt from surfaces the host
owns: the current date and time from the **injected clock** (`yurios/world/clock.py`, never
the wall clock), the room's sticky scene state (rain, music), and the timer board's pending
timers with time remaining. The block **MUST** state the **embodiment truth**: her body is
rendered live — eyes that blink, a face that shows feeling, a mouth that moves with her voice
— in her room or on the desktop. She **MAY** know she runs as an AI; the block **MUST** direct
that this never means bodiless — she **MUST NOT** claim to have no body, no eyes, or no
surroundings, and asked about them she answers plainly (the no-narration rule forbids stage
directions, not the truth). The embodiment-truth text lives as one constant
(`yurios/world/situation.py`) so the test suite can assert it verbatim.

This block is the promised seam. In the reactive body it is a per-prompt rendering of host
state; with the mind running, the same `## THE SITUATION RIGHT NOW` slot is filled by
**`WorldModelStore.situation()`** (§19.2) — which still *contains* these host lines via the
same `situation.py` renderer, now extended with presence, open threads, and expectations.
The block's place in the prompt does not move; what fills it does. It is wired to the brain
via `ToolBrain.set_world`. Mindless, the brain **MUST** fall back to the bare host rendering.

### §2.6 — The chat surface: her words, visible, beside her

Both browser pages (`/` and `/live2d/`) **MUST** show a chat column next to the body: a
scrolling transcript with the user's turns (typed *and* spoken — the STT transcript joins the
chat), her committed replies, an accumulating **draft** while she speaks, a `proactive` tag on
lines she spoke unprompted (the greeting, ambient self-talk, a finished selfie, the mind's
initiative), and inline images when a message carries `image_url` (§7.6). The host owns the
transcript: an in-memory ring (~200 entries) appended by `post_message` and published as
`message` events on the bus (§10); `GET /api/history` backfills a fresh page.

The chat is the *visible* conversation, not her memory — the Vault stays the only durable
record, and the rules match: a barged-in turn drops its draft and commits nothing; ambient
lines appear in the chat but never persist (§9). Typing in the chat composer rides the shared
text-turn runner (§10.5), keeping full turn semantics. Desktop-pet windows (§6.5) hide the
chat column; the composer moves to the hover bar.

## §3 — The body: the VRM stage

- §3.1 **The frontend build.** three.js + `@pixiv/three-vrm` (+ `-animation`) are `npm`
  dependencies pinned in `web/package-lock.json` and bundled by **Vite** into `web/dist`
  (`web/vite.config.js`), so they receive upstream security updates via `npm audit` /
  `npm update`. The server serves `web/dist` at `/` and the large runtime binaries
  (`web/models`) at `/models` (`yurios/world/main.py`). Build step: `cd web && npm ci && npm
  run build`. The Live2D client under `web/live2d/` is its own raw-served app (§6.6).
- §3.2 **The per-frame update loop** **MUST** follow this manual order exactly and **MUST NOT**
  call `vrm.update()`: animation mixer → animated material uniforms → bone overrides →
  `humanoid.update()` → gaze → blink → emote → viseme → `expressionManager.update()` (commit)
  → constraints → **spring bones last**. Getting this order wrong is the classic failure mode
  (physics and expressions fight).
- §3.3 **Load passes:** `removeUnnecessaryVertices`, `combineSkeletons`, `frustumCulled =
  false` on every node, the look-at quaternion proxy, facing normalized to −Z, and `.vrma`
  clips re-anchored at the hips so she doesn't teleport to the animator's origin.
- §3.4 **The expression catalog.** The brain emits palette *names* (`neutral, happy, sad,
  surprised, shy, thinking, playful, tender`); the map from name → VRM expression weights
  **MUST** live in the frontend (`web/js/stage/EmoteController.js`), so the brain stays
  renderer-agnostic. Every palette name **MUST** resolve to a composite of the six VRM preset
  expressions (`happy, angry, sad, surprised, relaxed, neutral` — the only names guaranteed
  across VRM models), and the six preset names **MUST** also work directly (they are
  `VrmController.set_expression`'s catalog, §4). The emote blender **MUST NOT** stage `blink`
  or `aa` — those channels are owned by the blink controller and the viseme driver.
- §3.5 **Cheap aliveness:** procedural blink on a random timer, gaze that tracks the camera by
  default with idle saccades, and a looping idle `.vrma`. These run client-side,
  unconditionally — she is never a statue, even with the server gone.
- §3.6 **Degrade gracefully.** Without WebGL (or with the model missing) the page **MUST**
  still run the voice loop.

## §4 — The control channel (`avatar` events on the bus)

The Python-side **`VrmController` method surface is canonical** — the strings the tick loop
holds (§15.5). It **MUST** expose at least: `set_expression, set_expression_raw,
look_at_camera, look_forward, look_at, set_bone, reset_bone, set_mouth, set_material_color,
play_animation, load_model`, plus the scene channels `set_rain` and `music`. It runs in-process
on the app's event loop; every method publishes **one `avatar` event on the `EventHub`** (§10),
fanned out to every attached frontend over `/api/events`.

Command shapes ride the envelope `{"type":"avatar", "op": …}`:

```jsonc
{"type":"expression",     "name":"happy", "intensity":0.8}   // §3.4 catalog
{"type":"expression_raw", "values":{"blink":1.0}}
{"type":"look_at",        "mode":"camera"|"none"}
{"type":"look_at",        "target":{"x":0,"y":1.2,"z":-1}}
{"type":"bone",           "name":"rightUpperArm", "euler":{"x":0,"y":0,"z":-75}}
{"type":"bone_reset",     "name":"rightUpperArm"}            // name optional
{"type":"mouth",          "value":0.5}                       // manual override (§5)
{"type":"material_color", "material":"Tops_01_CLOTH", "color":"#223"}
{"type":"animation",      "url":"/models/idle.vrma", "loop":true, "fadeIn":0.3}
{"type":"load_model",     "url":"/models/avatar.vrm"}
{"type":"rain",           "intensity":0.6}                   // scene channel (§6)
{"type":"music",          "action":"play"|"stop", "track":"warm_pad", "volume":0.4}
```

Turn expressions ride this channel too (§10): the voice route realises an expression as
`set_expression(name, reset_ms=0)` (hold semantics), so one lane carries the face for every
body and every open page, scripted or spoken. The frontend **MUST** auto-reconnect. Events
arriving before the model loads **MAY** be dropped, except persistent appearance state
(material colors, rain intensity, music), which the hub keeps **sticky** and **MUST** replay
to every new subscriber before its first live event. Malformed JSON is logged and dropped.

## §5 — Visemes: real lip-sync

- §5.1 The mouth **MUST** be driven from the **RMS amplitude of the audio actually playing** —
  a WebAudio `AnalyserNode` on the playback graph (`web/js/viseme.js`) staging the `aa`
  expression in loop step 9. Because the analyser reads the same buffers the speaker plays,
  mouth and voice cannot drift.
- §5.2 The driver **MUST** apply attack/release smoothing and a silence gate (perceptual
  `amp^0.7` curve, fast attack, slower release, gate below ≈ 0.04, weight cap ≈ 0.7) so the
  mouth doesn't chatter on noise or freeze open between sentences.
- §5.3 When audio is present the text-length flap **MUST NOT** drive the mouth. The `mouth`
  command (§4) remains the puppet-channel override for scripted use. The full phoneme tier is
  the documented upgrade seam, deliberately not built: amplitude-on-real-audio already gives
  exact sync; phonemes add mouth *shape* at the cost of a heavier dependency.

## §6 — The sanctuary scene

- §6.1 The room is canonical and its elements are normative: a **small room** — one unit high in
  a stacked block over the Sprawl — with **low warm light** (a lamp), a **window with rain**, a
  **window seat** under it, and a **single plant** on the sill. The window is the room's hero and
  faces the camera, so the **city beyond the glass** is her backdrop: neon, hoardings, hover
  traffic, weather. She **MUST NOT** stand in a void or a default grey scene. The rest of the
  set — desk and terminal, holo table, the cat, the cove neon, the block's dying fixtures — is
  furnishing, free to change; the five elements above are not. The cat (`web/js/stage/sanctuary/Cat.js`,
  lorebook "The cat") is furnishing that decides where to be: it chooses among the room's perches,
  walks a path around the furniture and jumps at the end of it, on the same reduced tier as the
  rest of §6.2. It is unnamed on purpose — naming it is {{user}}'s, and §27's promise scenario
  already assumes she has cat names to sleep on.
- §6.2 The set **MUST** be procedural three.js geometry and shader work — no binary scene assets
  in git: surfaces are canvases drawn at boot, the city is a baked canvas plus a small animated
  overlay, and the weather is a shader. Rain **MUST** respond to the `rain` command (§4): a
  glass streak-and-bead shader, falling drops outside, the far rain over the city, and a
  synthesized rain-noise bed (client-side) whose gain follows intensity. The room renders
  through a post chain (bloom → tone-map → grade) — without it the neon does not leave the
  surface it is painted on. Cost is a first-class constraint: this GPU is usually also holding
  her model (§3), and on a handset it is also holding the browser. The room therefore **MUST**
  carry reduced tiers, decided in one place (`web/js/stage/quality.js`) and forceable with
  `?fx=full|low|phone`: `low` — a narrow window or a large touch display — drops the planar
  floor reflection, the shadow map, the area lights and the post-AA; `phone` — a coarse pointer
  on a small screen — additionally caps the drawn pixels, halves the resolution the bloom is
  built at, folds the tone-map into the grade so one full-screen pass comes off, cuts the room's
  nine lights to four, and thins everything that moves, is redrawn or is uploaded per frame. A
  tier is a guess about a device, so the client **MUST** also measure: it watches the frame
  clock and gives render resolution back until the frame fits.
- §6.3 The page chrome carries **the switchboard's design system** — one look across the
  character board (`web/dashboard/`), both bodies (§6.6) and the shared `.env` panel (§11):
  Inter for prose, IBM Plex Mono for the small uppercase labels, 1px rules and 2px corners on
  green-black, an acid-lime accent, and mint/amber/red for live/near/failed wherever a state is
  shown. Entering a character **MUST NOT** feel like leaving the app. The chrome floats over the
  room and recedes — a topbar and a column at the edges, translucent, never a frame around her.
  The camera is fixed and cinematic — framing her in the room with
  subtle mouse parallax — not an orbit-controls model viewer: a place, not an asset inspector.
- §6.4 **The enter gesture.** The page **MUST** gate on one click ("enter the sanctuary")
  before connecting the sockets, so the `AudioContext` is user-activated and the greeting (§9)
  is audible. A **boot board** (`yurios/world/boot.py`) shows the kernel-boot log while she
  wakes; the SSE stream opens after the gesture and the enter gate polls `/api/health` for
  readiness. Desktop mode (§6.5) auto-enters but **MUST** still resume a suspended context on
  first click, so the worst case is a quiet greeting, never a dead one.
- §6.5 **Desktop presence — the room, set aside.** `python -m yurios.world --window` **MAY**
  host the served page in a frameless, transparent, always-on-top native window (pywebview;
  `yurios/world/window.py`) pointed at `?desktop=1`, so she floats on the desktop. What the flag
  *means* is the page's decision: in desktop mode the page **MUST NOT** build the sanctuary (no
  room — the desktop is the room), the renderer **MUST** clear to alpha 0, a neutral light rig
  **MUST** replace the lamp, and the camera frames the full body. Both sockets, every §4 command,
  the tools, and ambient life are unchanged; `rain` arrives as sound only. Not required for the
  DoD.
- §6.6 **The second body.** A Live2D web client is served at `/live2d/` (`web/live2d/`); its
  Cubism runtime and rig ship with the build. It carries the §2.6 chat column and one
  `events.js` that maps
  `avatar`/`expression` events from `/api/events` onto the pixi body. Audio rides `/ws/voice`
  unchanged; `/api/config` (`yurios/world/routes/live2d.py`) answers its rig-registry needs.
  `DESKTOP_BODY=vrm|live2d` (or `--window --body …`) picks which body the §6.5 window floats.
  The Live2D body realises only the `expression` op — it remains a guest, not a second puppet.
- §6.7 **No body at all.** A **text client** **MUST** be served at `/text/` (`web/text/`, and
  `/characters/<id>/text/` on a multi-character node), because a room she cannot be reached
  in is not a feature: an integrated GPU, a phone on a train, a screen reader and a remote
  session are all normal ways to want her. It is a **separate page**, not a mode on §6.1's:
  it builds no WebGL context, loads no VRM and imports none of the room, so the ~1 MB of
  three.js never reaches a client that will not draw. What it carries is everything that is
  not a body — the §2.6 transcript, the §24.3 inner-life panel, the §11 `.env` panel and
  gauge, the two footer switches (§9.10, §10.5, with Telegram sending **off** by default in
  here), and the §9 voice loop with its mic and barge-in intact — over the same `/api/events`
  bus and the same `/ws/voice` socket, byte for byte (§10). `avatar` events still arrive and
  are simply not realised. The §6.4 enter gesture still applies: autoplay policy does not
  care that there is no body, and her greeting still has to be audible. The switchboard
  (§6.3) **MUST** offer all three ways in for every character.

## §7 — Tools via MCP: the hands

- §7.1 **Four tools, real MCP.** An in-repo MCP server (`yurios/world/tools/server.py`, FastMCP
  over stdio) exposes exactly:

  | tool | args | returns | side effect |
  |---|---|---|---|
  | `set_timer` | `minutes` (0 < m ≤ `TIMER_MAX_MINUTES`), `label?` | `{id, label, seconds, due}` | host schedules the announcement (§7.5) |
  | `play_music` | `action`, `track?`, `volume?` | `{playing, track}` | `music` event to the stage (§4) |
  | `get_weather` | `city?` (default `WEATHER_CITY`) | `{city, temp_c, condition, wind_kmh}` | none |
  | `take_selfie` | `look?` (the whole picture in her own words), `scene?`, `framing?`, `lighting?`, `mood?`, `wardrobe?`, `avoid?` (template keys or free-form — carried verbatim, never refused; unnamed slots are left unnamed, never rolled) | `{id, look, scene, framing, lighting, mood, wardrobe, avoid, kind:"selfie", status:"started"}` | host renders off-turn, posts the photo (§7.6) |
  | `show_picture` | `subject` (required — the whole picture in her own words; no library, no slots), `avoid?` | `{id, subject, avoid, kind:"picture", status:"started"}` | host renders off-turn *without her likeness*, posts the picture (§7.6) |
  | `web_search` | `query`, `k?` (≤ `SEARCH_RESULTS`) | `{query, results:[{title, url, snippet}]}` | none (§7.7) |
  | `read_page` | `url` | `{url, title, gist, chars, text, status:"read"}` | host shelves the full text as knowledge (§7.7, §20) |
  | `research` | `topic`, `depth?` (≤ `RESEARCH_MAX_PAGES`) | `{id, topic, depth, kind:"research", status:"started"}` | host searches, reads and shelves off-turn, posts what it found (§7.7) |

  The surface **MUST NOT** grow a shell — the heavy, sandboxed hands are a named later rung
  (§26). With `SELFIE_BACKEND=off` neither camera tool **MUST** be advertised: no hand, not a
  dead one; with `SEARCH_BACKEND=off` the three web tools **MUST NOT** be advertised either.
- §7.2 **A genuine MCP client, and more than one server.** The brain side **MUST** connect over MCP
  (`yurios/world/tools/client.py`, stdio, spawning `yurios.world.tools.server`), discover tools
  with `list_tools`, and build the §7.4 directive from the discovered schemas. If the SDK or
  server fails, the build **MUST** degrade to tools-off and keep talking; `/api/health` reports
  the truth.

  `MCP_SERVERS` **MAY** name a JSON file in the conventional `{"mcpServers": {…}}` shape, whose
  servers are mounted alongside hers behind the same `ToolRunner` seam (`MultiToolRunner`), so the
  brain still sees one runner and one flat list. Unset, the behaviour **MUST** be identical to the
  single-server case. Her own server **MUST** be mounted first and tool names **MUST** stay
  unprefixed, so a third-party server cannot shadow one of her hands — the collision is dropped and
  logged. A configured server that fails to start **MUST** be skipped rather than costing her the
  others. Tools discovered from a mounted server **MUST** be admitted to the §7.3 allowlist at that
  server's rate (or `TOOL_RATE_EXTERNAL`); tools from her own server **MUST NOT** be admitted this
  way, because their configured rates encode decisions discovery cannot see.
- §7.3 **Guardrails.** Every call **MUST** pass `yurios/world/tools/guard.py`: an **allowlist**
  (exactly the discovered tools; anything else denied), **per-tool rate limits** (token bucket
  on the injected clock), a **per-turn call cap** (`TOOL_MAX_CALLS_PER_TURN`), a **per-call
  timeout**, and **result truncation**. Every call — allowed or denied — **MUST** append one
  JSONL audit line (`ts, tool, args, verdict, duration_ms, result`) to `TOOL_LOG_DIR`. She can
  be *asked* anything; the guard decides what her hands actually do.
- §7.4 **The in-stream call protocol.** A `## TOOLS` block appended to the system prompt
  instructs the model: speak a short lead-in sentence first, then emit `[[tool_name {"arg":
  value}]]`. The streaming parser (`yurios/world/tooltags.py`) **MUST** strip markers from
  speech, tolerate token-boundary splits, and drop unclosed, unknown, or oversized markers
  silently (a 12B local model *will* emit a broken one). On a closed marker: guard-check → MCP
  call → a **continuation stream** (original messages + the partial reply + a `((tool result:
  …))` cue) the model finishes as the same turn — so she *speaks to* what her hands found.
  First audio **MUST NOT** wait on a tool: the lead-in sentence reaches TTS before the call runs.
  Barge-in **MUST** cancel the continuation, and a barged-in tool turn persists nothing.
  The block lists each discovered tool with its **whole** description, unwrapped to one line: a
  description is prose whose newlines are typography, so truncating at the first one keeps a
  fragment and discards the sentences that say *when to reach for the tool* — a hand she can
  call but was never told the purpose of is one she does not use, which is indistinguishable
  from not having it. A length cap **MAY** bound a mounted third-party server (§7.2) but
  **MUST** sit above every first-party description. The grammar is taught by a **concrete**
  example; a metavariable like `tool_name` in the block is emitted verbatim by small models and
  arrives at the guard as a call to a tool that does not exist. Every parameter **MUST** be
  explained in its tool's description — the schema carries names and types, and the description
  is the only place their *meaning* reaches her; an undocumented optional argument is one she
  fills with the prose that had nowhere else to go. The client **MUST** fit arguments to the
  discovered schema before the call: coerce what is coercible, drop an **optional** argument
  that cannot be made to fit so the tool's own default applies, and leave a **required** one for
  the tool to reject with its own better message. A fumbled optional scalar **MUST NOT** cost
  the call the required arguments the model got right.
- §7.5 **Semantics.** The MCP server is the *contract and audit point* for `set_timer` — it
  validates and records — but the **host** schedules the wake (`yurios/world/tools/timers.py`,
  on the injected clock), because only the host owns her voice; when a timer elapses she
  **MUST** announce it aloud through the ambient seam (§9), queued until deliverable.
  `get_weather` **MUST** be a real HTTP lookup (Open-Meteo, keyless) behind a `WeatherProvider`
  seam with an offline fake. `play_music` drives the browser-side synthesized ambience (§6.2) —
  a generative pad, not a media library; the seam is the point.
- §7.6 **Her camera: `take_selfie` / `show_picture`, start-don't-await.** The two hands that
  share a camera teach the one lesson the others can't: **a slow tool must not sit inside the turn.** A hosted render takes 10–30 s;
  dead air after her lead-in would read as a hang. So the tool follows the *start work, never
  await it* rule: the MCP server carries her ask through — `look` is the whole picture in her own
  words and **MUST** lead, the library slots refine it, a named template key
  renders from the library, anything else passes through verbatim, the contract refuses nothing
  (its tool description **MUST** be built *from* the library — the same merged book the host
  renders from, overlay and its optional `tool_hint` line included — so the model's choices
  can't drift from the yaml) — and returns `{status:"started"}` immediately; the turn ends on
  budget. A slot she did *not* name **MUST NOT** be rolled for her: the library is optional
  shorthand, and a rotation she never asked for is how one request became two different photos.
  What she leaves unnamed **MAY** be filled from the live situation instead (the hour as light,
  the weather on the glass — `render_visual_situation`), and that filling **MUST NOT** argue with
  anything she did name: a picture she placed herself gets no context appended at all.
  The
  shipped library stays in the everyday register; personal registers layer on from an optional
  user-supplied overlay file outside the repo (`SELFIE_TEMPLATES_EXTRA`, merged key-by-key —
  forge/templates.py). A character **MAY** carry her own library at
  `data/characters/<id>/selfie.yaml` (`SELFIE_TEMPLATES`, edited in the card studio), and when
  she does it **MUST** *replace* the shipped book rather than merge over it — an overlay can only
  add rows, and the shipped book describes one character's world down to the tail in half its
  scenes. The tool description **MUST** be built from the same merged book, so what she is
  offered is always what her camera would compose. The env overlay still layers over whichever
  base wins.

  `show_picture` is the same camera pointed away from her — the street below, a sketch, whatever
  she is describing and would rather show. It has **no library, no slots and no rotation**: the
  `subject` is the whole prompt, because no menu could anticipate what she might want to show
  you. Her likeness **MUST** be left out of that frame (`include_character=False`) — a photo of
  the rain does not have her in it just because she took it — and both tools **MUST** share one
  lab, one VRAM loan and one announce path, differing only in what is in the picture.

  Whose likeness `take_selfie` renders **MUST** come from the running character's own
  `appearance.yaml` (§31.2, `SELFIE_CHARACTER`), never from whichever character the house
  happens to ship; a character with no appearance file **MUST** render a neutral stand-in, because
  a photo of the wrong person is worse than a photo of no one. The **host**
  realises the shot: `yurios/world/selfies.py`'s `SelfieLab` renders off-turn through the forge
  (`yurios/forge` — the locked art register, the selfie template library, provenance stripping),
  saves the PNG + a provenance sidecar under `SELFIE_DIR` (served at `/selfies/`), posts an
  `image_url` `message` to the chat (`proactive`), and offers one spoken line through the ambient
  seam — dropped if she's busy, because the photo itself already landed. The default backends are
  GPU-free: `openrouter` (default `bytedance-seed/seedream-4.5`; point `SELFIE_MODEL` at
  `sourceful/riverflow-v2.5-pro` for the brand-art register) or `mock` (deterministic
  placeholders; the tests). Two opt-in backends render locally instead, each on a user-supplied
  single-file checkpoint that is never shipped: `diffusers` (an SDXL UNet) and `krea2` (a Krea 2
  diffusion transformer, kept in INT4 and never dequantized — at bf16 it fits no consumer card).
  Which of the two a given checkpoint needs **MUST** be read off the file itself
  (`forge/backends/sniff.py`), so `SELFIE_BACKEND=diffusers` names *the local camera*, not one
  architecture. Any backend that cannot run — `openrouter` with no key, a local one missing deps,
  its checkpoint, or (krea2) access to the gated companion repo its text encoder and VAE come
  from — **MUST** degrade to `mock` with one loud WARNING; a failed render **MUST** become a
  quiet chat message, never a crash and never silence.
- §7.7 **The web: `web_search` / `read_page` / `research`, and what she reads she keeps.** The
  three hands arrive together or not at all — searching with no way to open what you found is half
  a capability — behind one `SEARCH_BACKEND` knob whose default is `off`.

  Search **MUST** sit behind a `SearchProvider` seam (`yurios/world/tools/search.py`) with an
  offline fake, and the reference backend **MUST** be a **self-hosted SearXNG** instance. Where
  Open-Meteo is chosen for needing no key, this is chosen for needing no third party: the record of
  what she searched for is a file on the user's own machine. The instance's JSON format is disabled
  in stock SearXNG, and the resulting 403 **MUST** be reported as that rather than as an HTTP error,
  because a bare 403 sends the reader to authentication.

  Fetching **MUST** sit behind a `PageFetcher` seam (`yurios/world/tools/fetch.py`) with an offline
  fake, extract text with no new dependency, and — the load-bearing rule — **validate the URL
  before every request and again on every redirect hop**: http(s) only, and never an address that
  resolves into private, loopback, link-local, reserved or multicast space. `url` is the first tool
  argument authored by a language model rather than by a person, and the local network it would
  otherwise reach includes her own control surface (§11.4). Redirects **MUST** therefore be followed
  by hand. Non-text responses and bodies past `FETCH_MAX_BYTES` **MUST** be refused.

  **A page she read is knowledge, not a tool result.** The full text of every page — fetched by
  `research` or by a `read_page` she made herself — **MUST** be ingested into the §20
  `KnowledgeStore` and carry its source URL in the document, so the doc+span citation survives the
  round trip back to where it came from. The model **MUST** see only a short `gist`: §7.3's result
  truncation bounds the model-facing and audit copies while the host realises against the
  untruncated one (`ToolBrain._execute`), which is the same two-audience contract the camera's
  contract JSON already relies on. With no mind running there is no shelf; she **MUST** still
  search, read and report, and **MUST** say that it wasn't kept rather than implying it was.

  Because the dependency is a **service and not a package**, the runtime **MUST** look after it:
  `install.sh` asks (defaulting to off when no terminal is attached — a service is not something to
  stand up for somebody who isn't watching), creates the container with the json format already
  enabled, and degrades to `SEARCH_BACKEND=off` rather than failing the install when Docker is
  unusable. `yurios start` **MUST** bring a stopped container up, and **MUST NOT** treat a failure
  to do so as fatal. `yurios doctor` **MUST** report whether she can search *right now*, telling
  missing, stopped and refusing-JSON apart, since those are three different fixes behind one
  configuration. A non-loopback `SEARXNG_URL`, or a loopback instance the runtime did not create,
  **MUST** be reported and never managed — and the probe **MUST** be consulted before the container
  state, so a working instance run another way is never diagnosed as a missing one.

  `research` **MUST** follow §7.6's start-don't-await rule — the server validates and answers
  `{status:"started"}`, and `yurios/world/research.py` does the work off-turn, posting what it found
  to the originating channel and offering one spoken line through the ambient seam (§9). A page that
  won't open **MUST** be skipped rather than failing the run, and a run where nothing opens **MUST**
  still end in words.

## §8 — Ambient life is the mind's, not a scripted machine

There is no scripted idle state machine. Ambient speech and timer announcements are *decided*
acts of the tick loop (§15.5), and the body micro-acts (gaze drift, expression pulse, posture,
rain-gazing) survive as REGULATE-owned reflexes on a seeded RNG and the same config windows
(`IDLE_ACT_*`, `IDLE_TALK_*`, `IDLE_SETTLE_S`). The obligations of ambient speech survive as
obligations on the mind, cited from §15: sim-time discipline (§15.1), an ambient line is a real
turn minus the memory (§9), and the per-connection ambient seam (§9). With `MIND_ENABLED=false`
the body still has cheap client-side aliveness (§3.5) but no host-driven ambient life.

## §9 — The voice loop

The real-time loop that gives her a spoken body. All backend-facing voice surfaces sit behind
Protocols (`yurios/desktop/voice/protocols.py`); nothing else in the voice layer imports an
STT/TTS/VAD SDK, and fakes implement each seam so the whole loop runs offline (§27).

- §9.1 **The seams.** Audio is float32 mono in [-1, 1] with its sample rate alongside.
  **STT** (`reset`/`feed`/`final`; default `faster_whisper`, tuned for latency) **MUST** drop
  segments flagged non-speech and the loop **MUST** reject a transcript with no alphanumeric
  content — a `you: . . . .` line **MUST NOT** reach the brain or the Vault. **TTS**
  (`stream(text)`, **MUST** yield sentence-by-sentence for short time-to-first-audio) defaults
  to `kokoro` (a fixed CPU voice that needs no GPU and leaves the GPU for the LLM and the
  avatar); `qwen3_tts` (a designed persona voice, cloned from one frozen clip so the timbre
  can't drift between filler and reply) and `gpt_sovits` (a canon clone) are one-line
  `TTS_BACKEND` swaps. **VAD** (default Silero): the **edge** VAD (barge-in) runs client-side
  (§6.6/§8.3-parity), the server confirms endpointed utterances. Turn-taking **MUST** be
  *debounced* (`SpeechGate`): act only after N *consecutive* speech frames, and confirm a
  barge-in with a strictly higher count than a new-turn onset — a single keystroke transient
  **MUST NOT** trigger a turn or a barge-in.
- §9.2 **The real-time turn** (`yurios/desktop/voice/turn.py`, `TurnController.run_turn`, one
  per connection). Reply tokens **MUST** be consumed while earlier sentences are still
  synthesizing (producer → sentence queue → consumer); the first audio chunk **MUST** emit as
  soon as sentence one renders. Sentence splitting **MUST** be incremental.
- §9.3 **Latency budget.** The loop **MUST** measure end-of-speech → first-audio; target ≤
  **1200 ms** end-to-end, held even on turns that call a tool (§7.4). The end-to-end number is
  the measurement of record; traces **SHOULD** be written to `TRACE_DIR` (gitignored).
- §9.4 **Latency masking.** The gap **SHOULD** be covered by an **instant acknowledgment**: on
  endpoint, before the first token, a short pre-rendered filler ("mm—"). The bank is
  pre-rendered once, persona-tuned, and **MUST NOT** repeat the same clip twice in a row. It is
  interruptible audio: barge-in kills it. Masking is disable-able.
- §9.5 **Barge-in is a cancel.** `cancel()` **MUST** tear down TTS emission *and* the in-flight
  brain generation together, **MUST** be idempotent (the mic handler fires it per frame), and
  **MUST** be scoped to the current turn. Barge-in **MUST** cancel the mind's self-initiated
  speech exactly as it cancels a reply, because both run through the same per-connection
  `TurnController` (§15.5).
- §9.6 **Failure/cancel leave no trace.** A barged-in turn and a mid-stream brain error **MUST**
  persist nothing — no corpus line, no commit. Only a fully completed turn calls `persist()`,
  off the hot path. **No trace includes the session window.** `stream_reply` appends the user's
  line to the transcript before the first token (the model must see it) while `persist` appends
  hers, so a turn torn down in between leaves a half-written exchange behind. Every path that
  ends a turn without committing — barge-in, brain error, a client that vanished mid-turn, an
  empty reply — **MUST** call `abandon()`, `persist()`'s opposite number, which drops the
  pending turn and rolls the user's line back out of `sessions.json`. Without it the next
  prompt reads that line as a question still owed an answer and she answers it a second time,
  folded into the new turn.
- §9.7 **Emotion → expression.** The model is asked (appended system blocks, voice-only) to
  treat the exchange as *spoken* (no narration, no stage directions, no asterisk actions) and to
  emit inline expression tags from the §3.4 palette. The parser (`yurios/desktop/voice/emotion.py`)
  **MUST** strip tags from the spoken text, emit an expression event when a tag closes (the face
  leads the voice), tolerate split tags, drop unknown tags silently, and also strip
  `*asterisk narration*` (streaming-safe, dropping an unclosed span rather than speaking it).
- §9.8 **The greeting.** On connect she **SHOULD** greet from memory before the user speaks
  (continuity). The greeting **MUST NOT** be persisted and **MUST NOT** pollute the session
  window, and **MUST** fire at most once per session — a reconnect or a second socket **MUST NOT**
  speak a second greeting over the first. On the **first-ever** arrival there is no memory to
  open from: while `soul/BOOTSTRAP.md` is present and the journal is empty, the greeting **MUST**
  be that file's authored cold open, spoken verbatim, with no model call and no corpus line (the
  text is SOUL, not a completion); once the journal shows she has met someone, the bootstrap
  **MUST** be retired (§5.4) so file-presence remains the "has she met you yet?" flag. This fork
  belongs to the **brain's greeting seam**, not to any one route: every surface that greets — the
  voice socket, the text channels (§10.5) — **MUST** get it from there, or a character imported
  and spoken to through one surface never has her first meeting at all.
- §9.9 **Loaded while she has company.** The voice stack (~20 s cold, ~a gigabyte of weights) is
  wanted by exactly one surface — the audio socket — so it **MUST NOT** be a cost of *starting* a
  character: a host running a registry (§28) starts every autostarted one at boot, and warming a
  stack per character spends memory and boot time on rooms nobody is in. It **MUST** therefore
  load when a client opens `/ws/voice` and be released when the last open socket closes, held up
  meanwhile by a count of listeners so a second client joins the warm stack rather than a second
  copy of it. An empty-room grace period (`VOICE_UNLOAD_AFTER_S`) **SHOULD** cover a page reload,
  and `VOICE_PRELOAD` **MAY** restore warm-at-boot for a single-companion install. It **MUST**
  load off-thread so her body appears immediately, the socket **SHOULD** say it is warming (a
  `warming` frame) rather than look hung, and a connection **MUST** wait for the stack to be ready
  before its first turn rather than answering with a stand-in. Boot services that only load on
  demand **MUST NOT** leave the boot board (§6.4) unfinished — the enter gate waits on it. The
  ambient seam: the world voice route (§2.2) registers
  a per-connection injector, so ambient turns run on that connection's `TurnController` and one
  barge-in path kills everything she says, scripted or replied. Ambient speech is a real turn
  *minus the memory* — it appears in the chat flagged `proactive` but never persists (no corpus
  line, no commit); announcements queue until deliverable, missed self-talk is simply dropped.
- §9.10 **Mute is not deaf.** Every client (§6.6, §6.7) **MUST** carry a switch that silences her
  *output* — the speakers, never the microphone and never the ambience, which has its own. It
  **MUST** act on the playback graph after the lip-sync tap, so a muted page still moves her
  mouth, still shows her captions and still fills the transcript: she is talking, you simply
  cannot hear her. It is remembered per character (`web/js/controls.js`) and re-applied before
  her first syllable, because a mute you must re-press on every load is a chore, not a setting.

## §10 — Topology: one event bus + one audio socket

**Everything the host tells a frontend is one typed event on one bus.** The only thing that
keeps a socket of its own is sound.

- **`EventHub`** (`yurios/world/hub.py`) — the single outbound fan-out. Every host→frontend
  event is one typed JSON dict: `hello` (her name), `message` (chat entries, including
  `image_url` selfies, and the originating `channel`), `draft` / `draft_cancel`, `avatar` (§4,
  scene channels included), and — with the mind — `journal` and `mind` (§24). Publishes are
  non-blocking (a stalled client loses events, never blocks the publisher) and thread-safe (the
  TTS thread publishes). Sticky appearance state is recorded before any subscriber and replayed
  last-write-wins.
- **`GET /api/events`** — the bus's wire: SSE, one `data:` line per event. On attach: `hello`,
  then the sticky replay, then live events. The stream **MUST** end itself on shutdown (a stop
  flag polled every second — an open tab must never hold Ctrl+C hostage) and ping while idle.
  The attach/detach of subscribers **MUST** post `user_present` / `user_absent` signals to the
  mind — presence is a signal, not a guess (§16.2). `GET /api/history` backfills the chat (§2.6).
- **`/ws/voice`** — the audio-only socket: binary mic PCM up, `hello`/`endpoint`/`bargein`/`text`
  control up; `session`, `warming` (her voice is loading for this connection — §9.9),
  `filler`/`audio` (base64 PCM + the sentence text for §5), `done`, `cancelled`, `error` down.
  Turn expressions are re-routed onto the bus (§4), so the face has one lane. An open socket is
  also what keeps the voice stack resident (§9.9). PCM keeps a websocket because audio is the one flow that is bidirectional, binary, and
  latency-critical; everything else is a broadcastable fact, and facts ride the bus.

### §10.5 — Channels

**A frontend is a thin view** — user input becomes a text turn + a `user_message` signal; output
is rendered off the one `EventHub`; nothing talks to the brain directly. Two seams make any medium
a frontend:

- **Inbound** — the shared text-turn runner (`yurios/world/turns.py`): resolve session →
  transcript + `user_message` signal → the brain's token stream (expression tags to the puppet
  lane, stripped from the shown text, sentences as `draft`s) → verbatim persist → `message` commit
  + `turn_committed` signal. It **MUST** mirror the voice route's contract minus the audio,
  including the rule that a failed turn leaves no trace. Text turns from all channels serialise on
  one lock. Exposed as `POST /api/chat` (`{text, session_id?, channel, client_id?}` →
  `{session_id, user_message, message, active_selfies}`). It has `POST /api/chat/cancel` for a
  correlated browser Stop request and **MUST NOT** wait on the voice warm-up. A text channel has
  no *connect*, so the greeting
  (§9.8) is asked for rather than implied: `POST /api/greeting` (`{session_id?, channel}` →
  `{session_id, message}`) runs the same opener through the same runner, commits it as a
  `proactive` message, persists nothing, and is idempotent per session per run (`message: null`
  on a second ask) — the once-per-session rule, shared with the voice route rather than duplicated.
- **Outbound** — an `EventHub` subscription. Committed `message` events carry the originating
  `channel`, so an adapter can filter cross-chat copies. The mind's SUGGEST lines and
  undeliverable SPEAKs land as `proactive` messages on the same bus (§18.3); an outside channel's
  forwarding policy decides whether those leave the host.

Channels in this build (`yurios/world/channels/`; a failed channel is one degraded medium, never a
down host — `/api/health` and the boot board say which):

- **the terminal** — `python -m yurios.chat`: a remote thin client on `POST /api/chat` +
  `/api/events`. Its SSE attach counts as presence, exactly like an open page.
- **Telegram** — `yurios/world/channels/telegram.py`, raw Bot API long-polling. One configured
  chat only (`TELEGRAM_CHAT_ID`; unset = pairing mode: the bot answers with the id to configure and
  processes nothing). Telegram is *reachable, not present*: it posts no presence signals; selfies
  are sent as the file itself. A channel is on when its credentials are set — no separate enable
  flag. Telegram-originated turns always answer in Telegram. Every other origin is filtered by
  `TELEGRAM_SEND_NON_TELEGRAM`, which **MUST default false** and is exposed in settings; browser
  rooms **MUST NOT** show a Telegram forwarding button. This prevents a local web, voice, CLI or
  API conversation from leaking into a separately open Telegram chat unless explicitly enabled.

**One outside account, one character.** The host runs a runtime per character (§29), so a shared
credential would be opened once per character — and an inbox is single-tenant: Telegram answers all
but the last `getUpdates` poller with "Conflict: terminated by other getUpdates request", leaving
her reachable nowhere. Each character therefore **MUST** have her own credentials, named with her
registry id: `TELEGRAM_BOT_TOKEN_<ID>` / `TELEGRAM_CHAT_ID_<ID>`, resolved per runtime by
`world/host.py`'s `telegram_for_character`, and pairing mode names *her* variable. Because the ids
exist only at runtime these keys cannot be `Config` fields, so the resolver **MUST** read the
`.env` file as well as the environment. The settings panel (§11) offers the pair the *open room's*
character reads her bot from, so an edit there can never take over another character's chat. The
unsuffixed pair remains the single-companion install's; `TELEGRAM_CHARACTER` names who keeps it,
and with that unset it is offered to every character without her own bot. A `Channel` **MAY** declare a `claim`
on credentials that can only be held once as the backstop: `ChannelManager` gives the claim to the
first runtime to start and reports the medium as `held by <her>` for the rest — a healthy state,
not a fault. A character with no credentials of her own is simply not on that medium.

Planned on the same contract, not yet implemented: **WhatsApp** (webhook transport) and a
**game-engine NPC API** (a WebSocket the engine connects to: player utterances in as text turns
with scene context, `message` events out as dialogue, the same `avatar`/expression events as
animation cues — a game is another frontend + effector set, never a second brain).

## §11 — Config

Typed (`yurios/world/config.py`), read once from env/`.env`, extending the voice config (which
extends the brain's). Every knob in `.env.example` **MUST** have a default and the default stack
**MUST** need no key (`SELFIE_BACKEND=openrouter` without a key degrades to mock — §7.6 — so the
no-key rule survives it). The port is **8768**. The brain knobs (model routes, `LMSTUDIO_BASE_URL`,
the reasoning switches, `CONTEXT_LENGTH` and the context readout it feeds — §3, `EMBED_BACKEND`
and its auto-reindex, retrieval and summary budgets, the
Vault dir) are inherited; the body knobs are `COMPANION_NAME`, `TOOLS_BACKEND=mcp|fake|off`, the
tool caps/timeouts/log dir and per-tool rate limits, `TIMER_MAX_MINUTES`,
`WEATHER_BACKEND`/`WEATHER_CITY`, `SELFIE_BACKEND`/`SELFIE_MODEL`/`SELFIE_DIR`, `RAIN_INTENSITY`,
`DESKTOP_BODY`, the channel credentials (§10.5), and the reflex windows (`IDLE_SETTLE_S`,
`IDLE_ACT_MIN/MAX_S`, `IDLE_TALK_MIN/MAX_S`). The mind's knobs are §25; `DATA_DIR` — the root
of the character tree — is Part III's (§29.1).

A knob in `.env` is the **host default**: every character inherits it unless her registry record
overrides that field (§31.2). The knobs a character may override are hers alone; the rest —
the port, the room, the reflex windows, what leaves the machine — are the house's.

The local settings panel (`desktop/routes/settings.py`, the gear in every room) edits that file
directly and **MUST** stay loopback-only, since it hands the browser the keys it renders. Its one
SCHEMA drives the form *and* validates the POST, so the two can never disagree; a field's `.env`
key **MAY** be resolved per runtime (the per-character channel credentials, §10.5), and a field the
running build has no knob for **MUST** be dropped rather than shown dead. Values are read from the
live `Config`, writes are surgical (only changed fields, upserted line-by-line so the comments in
`.env` survive), and a save asks for a restart rather than pretending to hot-apply.

The same gear opens a second panel above it with a different owner: **this character's own brain**
(§31.4). Those fields belong to her registry record, every one of them blank by default meaning
*inherit the file below*, and a save there applies to the running conversation at once. One dialog,
two scopes, two honest promises — and the panel **MUST** say which is which.

## §12 — Omissions → superseded by §26

## §13 — Tests → superseded by §27

## §14 — Extends to → superseded by §28

---

# Part II — the mind (§15–§25)

## §15 — The cognitive tick loop

`yurios/mind/loop.py`. The engine runs **SENSE → APPRAISE → DECIDE → ACT → REFLECT →
REGULATE**, forever, as one asyncio task on the server's loop, the caller of every host
surface between turns.

- §15.1 **Three normative rules.** (1) **One intention per tick**: DECIDE commits to
  exactly one act or to resting — the majority of all ticks **MUST** end in REST; an agent
  that does one thing per heartbeat reads like a diary and cannot fan out. (2) **APPRAISE is
  cheap by construction**: pure heuristics (`yurios/mind/policy.py`), runnable every tick,
  **MUST NOT** call a model — the model is invoked only inside ACT, for work already judged
  worth it. (3) **Everything is journaled** (§24.1) and **traced** (§24.2), and every tick
  that changed the Vault ends in **exactly one git commit** (`tick <id>: <intention>`); an
  uneventful tick commits nothing, and that is not an error. Time is **injected**
  (`yurios/world/clock.py`): no wall-clock reads, no bare sleeps, anywhere in the mind — this
  is the entire test story (§27).
- §15.2 **The mind's home is the same Vault.** No second database: the mind reads and writes
  the Vault the brain already keeps, adding `world/` (§19), `knowledge/` (§20), `goals.md`
  (§22), and `state/` (activity, budget, engine snapshot, pending edits, dream progress). All
  writes go through `yurios/mind/vaultio.py`'s `MindVault` — atomic, vault-jailed,
  constitution-refusing (§23.1).
- §15.3 **Where conversation lives.** The reply itself stays on the voice loop's sub-second
  reactive path, which no tick cadence may ever sit in front of. The loop is that path's
  *observer and consequence*: a `user_message` signal **MUST** preempt the activity state to
  ENGAGED from any state, mid-sleep if necessary (the bus wake), and a committed exchange
  arrives as a `turn_committed` signal whose REFLECT share is the world-model update and the
  promise scan (§22.1). One mind at two cadences: the loop owns everything between turns; the
  turn pipeline stays the ENGAGED fast path. (The full one-loop unification — the reply
  generated *by* ACT — is a named next rung, §28.)
- §15.4 **Rehydration and the suspend gap.** The engine's cursor state (`state/engine.json`:
  bus offset, interrupt counts, consideration cooldowns, last tick) **MUST** survive restart —
  a rebooted mind resumes, it does not wake amnesiac. A real gap since the last tick (> 2 h,
  or twice the DORMANT cadence) **MUST** synthesize one `suspend_gap` signal: one catch-up
  appraisal over the whole gap — goals reconsidered by commitment (§22.2), one journal line —
  never a pile of stale reactions, and never thirty good-mornings.
- §15.5 **The strings, held.** ACT reaches the world only through surfaces the host already
  owns: ambient speech through `Runtime.speak_ambient` (the same per-connection
  `TurnController` — barge-in-able, latency-masked, never persisted to memory, `proactive` in
  the chat), chat lines through `post_message`, the body through `VrmController`, the countdowns
  through the `TimerBoard` (whose landed timers arrive as `timer` signals; an announcement is a
  promise and **MUST** queue until deliverable). The **self-talk murmur** survives as a decided
  impulse: only in IDLE, only with the user present, only after the configured quiet window —
  and dropped, never queued, when nobody can hear. The **body reflexes** (gaze drift, expression
  pulse, posture, rain-gazing at the scene's canonical window target) survive as REGULATE-owned
  reflexes: no model, no journal, seeded RNG, silent while engaged, while the room is empty, and
  in DORMANT/DREAM.

## §16 — The signal bus (inbound)

`yurios/mind/signals.py`. Everything that happens *to* her is one typed, timestamped `Signal`,
appended to one inbox and drained by SENSE by offset. Producers post facts; the loop decides
what they mean — no producer may call into the mind.

- §16.1 Posting **MUST** be safe from the event loop or a worker thread, **MUST** wake the loop
  early from any cadence sleep, and **MUST** append one line per arrival to `signals.jsonl` (the
  arrival record — "what woke her at 3am" is a file you read).
- §16.2 The type enum is open: `user_message`, `turn_committed`, `user_present`, `user_absent`,
  `timer`, `task_completion`, `selfedit_decision`, `wakeup`, `fs_event`, `suspend_gap`. Producers
  in this build: the world voice route (the tee), the `/api/events` route (presence), the timer
  board, the self-edit API. Unknown types are legal and appraise low. `user_present`/`user_absent`
  are bookkeeping — observed by the world model, never chosen as intentions (the greeting is the
  voice route's job).

## §17 — Activity states and the budget governor

`yurios/mind/policy.py` (`ActivityController`), `yurios/mind/budget.py`. Cost and thermal control
as a design driver: an always-on mind is affordable only because it is almost always nearly asleep.

- §17.1 **Four states govern cadence:** ENGAGED (talking; short ticks) · IDLE (user recently
  around; goal work) · DORMANT (long quiet; resting) · DREAM (consolidation, entered from DORMANT
  inside a configured local-time window, chunked ticks). Everything but the preempt is a slow drift
  *down* the cost ladder on configured timeouts. The state **MUST** persist (`state/activity.json`)
  and resume across restarts.
- §17.2 **The preempt overrides everything:** a user turn pulls the loop to ENGAGED from any state.
  Nothing else moves up the ladder.
- §17.3 **The budget governor** holds estimated tokens spent today against a daily cap
  (`MIND_DAILY_TOKENS`), debited by every utility call and every line the mind composes; at
  pressure ≥ 1.0 REGULATE **MUST** shed IDLE to DORMANT (goal work stops). It **MUST NOT** gate
  conversation — a governor that silences her when the user speaks has failed at its one job. The
  ledger (`state/budget.json`) rolls at local midnight on the injected clock and is rendered by the
  dashboard.
- REGULATE **MAY** shorten the next heartbeat below the state cadence when a goal comes due sooner
  or when more than one appraisal crossed gate 1 this tick (the backlog drains one intention at a
  time, never piles into one tick).

## §18 — The salience and interrupt model

`yurios/mind/policy.py`. The make-or-break component: **two distinct thresholds**, and collapsing
them is precisely the always-interrupting-assistant failure.

- §18.1 **Gate 1 — salience-to-act** runs every tick, over every sensed signal and every open goal
  (with a per-goal reconsideration cooldown), plus the standing impulses (a pending announcement, a
  new document, DREAM backlog, the murmur). Pure heuristics: a base score per signal type — nothing
  outranks the person speaking — plus a surprise bonus from violated expectations (§19.3); goals
  score on priority, due-ness, and commitment. Below `MIND_ACT_THRESHOLD` the tick RESTs, and most
  do.
- §18.2 **Gate 2 — salience-to-interrupt** is scored only when a `reach_out` goal has already
  crossed gate 1, from named factors the trace records verbatim: relevance, time-sensitivity, hours
  since she last reached out (contact license), inferred availability by hour, and a welcome term
  that decays with each interruption today. Two rules are **hard gates, not weights**: quiet hours
  (roughly 22:00–09:00) are SILENT regardless of score, and `MIND_MAX_INTERRUPTS_PER_DAY` zeroes the
  score outright. Both dials are the **user's** (§25) — you cannot tune the dial against someone who
  holds it.
- §18.3 **Outcomes, ascending imposition:** **SILENT** — the default: do it quietly and journal it
  (a stale non-blind goal is let go with a journal line; the journal, not notifications, carries the
  value); **SUGGEST** — one composed line posted to the chat, waiting for the user's next glance,
  never spoken aloud; **SPEAK** — aloud through the ambient seam if a page is open (full turn
  pipeline, barge-in-able), as a `proactive` chat line if the room is empty. Every delivery **MUST**
  bump the daily count, note the contact in the world model, and close the goal.

## §19 — The world model (the present tense)

`yurios/mind/world.py` — the `WorldModelStore`, the organ the situation block (§2.5) is a rendering
of. SENSE writes it, APPRAISE scores against it, DECIDE plans over it, and every prompt is built
from it.

- §19.1 **Beliefs, not facts.** Every entry is a time-stamped, confidence-tagged belief in an
  append-only log (`world/beliefs.jsonl`); `query(q, at=…)` answers "what was believed when" (the
  snapshot stage of point-in-time; the temporal graph is a sanctioned later stage and out of scope,
  §26). Structured now-state (`world/state.json`): presence, last contact each way, open threads,
  expectations.
- §19.2 **`situation()` — the stage every prompt carries.** It **MUST** compose the host lines (the
  injected clock's time, the **embodiment truth** verbatim, the room's sticky scene state, pending
  timers — still rendered by `yurios/world/situation.py`) with what only a store can know: whether
  the user is here, how long they've been away (minutes/hours/days phrasing), what's in progress,
  what she half-expects. It **MUST** be written to `vault/world/situation.md` whenever it changes —
  her picture of *now* is a file you can `cat` — and it feeds the brain via `ToolBrain.set_world`
  (the §2.5 seam swap).
- §19.3 **Expectation and surprise.** `expect(text, due, keys)` stores a checkable belief about what
  comes next. A later observation that matches its keys resolves it quietly; one that finds it past
  due produces **prediction-error = surprise**, which **MUST** feed APPRAISE as a salience bonus —
  the cheapest good salience signal there is.

## §20 — The knowledge layer (drop-folder RAG)

`yurios/mind/knowledge.py` — the `KnowledgeStore`, a **sibling of memory, never folded in**. The
boundary is enforced by shape: **knowledge cites a document + span; memory cites a conversation
turn** — separate files, separate indexes, separate `inspect()`.

- §20.1 **Drop and she reads it.** Files (`.md`/`.txt`) landing in `vault/knowledge/reference/`
  **MUST** be noticed by SENSE (a cheap size+mtime scan, no signal required), ingested as an ACT —
  chunked by paragraph budget, each chunk situated with a short blurb (utility model; doc-name
  fallback offline), embedded, and hybrid-indexed (vector similarity blended with keyword idf) — and
  journaled ("read and shelved …"). Re-ingest replaces a doc's chunks, never duplicates. A doc that
  fails to ingest (no embedder backend, a mangled file) is marked seen with one loud WARNING and
  retried only when the file changes — a broken shelf item **MUST NOT** become a retry loop.
- §20.2 **Retrieval is grounded, and it reaches the prompt.** Every returned `Chunk` carries `doc`
  + `span` (character range) — a citation she can show. `search()` **MUST** run on every assembled
  turn and join conversation as the assembler's knowledge slot (§7.1 block 8), carrying its
  citations with it; a store that indexes what it is never asked for is not a knowledge layer.
  The store is late-bound onto the brain (`set_knowledge`, the `set_world` pattern), because it
  belongs to the MindLoop and there is no shelf with the mind off. Retrieval is an **enhancement,
  never a dependency**: a search that raises costs the block, not the reply. Every route onto the
  shelf — a dropped file, `read_page`, `research` — is the same store and therefore the same slot.
  `search()` **MUST NOT** re-parse the index per turn (cache on the index file's own size+mtime, so
  a fresh ingest is picked up without a signal). `forget(selector)` drops a doc off the shelf and
  out of the index. The index (`knowledge/index/`) is derived, gitignored, rebuildable.

## §21 — DREAM consolidation

`yurios/mind/dream.py` — the `consolidate()` contract, implemented: she wakes changed by yesterday.

- Runs **only** in the DREAM activity state (§17.1). Each DREAM tick chews what
  `MIND_DREAM_TICK_TOKENS` allows: finished days of the episodic journal — **never today's live
  file** — are summarised to at most a few durable facts (utility model; an offline heuristic keeps
  the pass alive with no model), deduped against `memory/semantic/facts.md`, appended there with
  their source day, and indexed at **salience 2.0** so recall prefers the distilled fact over the
  raw exchange.
- **Oldest-first and resumable** (`state/dream_progress.json`): a night that runs out of budget
  leaves a backlog, not an overrun, and the next DREAM tick resumes. The night's work is journaled
  ("slept on it: folded … into what I keep").

### §21.2 — The pipeline (more than one job)

`yurios/mind/dreamjobs.py` — consolidation is the first job of the night, not the only one. Sleep
is where the expensive, unhurried, nobody-is-waiting work goes; §21 is the most obvious member of
that set and a poor place to stop.

- A **job** is a name, a backlog, and a `run` (`DreamJob`). Four ship built in — `consolidate`
  (§21, wrapping `DreamConsolidator`), `diary`, `strategy`, `selfie` — and adding a fifth **MUST**
  require only a class and a name in `BUILTIN_JOBS`: the ladder, the trace, the budget, the debug
  page and the manual trigger all derive from the roster.
- **Priority order over one shared budget.** Jobs run highest-priority first and share
  `MIND_DREAM_TICK_TOKENS`; `consolidate` runs first because the others read `facts.md`. The first
  item of a night **MUST** run however big it is (§21's anti-wedge rule, which matters more here:
  a veto on the first item starves every job behind it too). A job's estimate **MUST** price the
  prompt it will actually send — a day's journal is capped at `JOURNAL_CHARS` before it reaches a
  model, so charging the file's full size bills a talkative day thirty times over and spends the
  night on one entry. And a job that hits the ceiling mid-backlog **MUST** stop that job, not the
  night: the cheaper jobs behind it are each gated by the same check and may still fit.
- **Per-job resumable progress** in `state/dream_jobs.json`. `consolidate` is the exception and
  keeps `state/dream_progress.json`, because that ledger predates this section and exists in every
  shipped vault.
- **ME and THEM, before a prompt sees it.** A journal labels the other person `you:`, which is
  right for a human reading it and unusable in a prompt that opens "You are {char}" — the word
  then points at two people at once and the model gives her side away. Every job that reads a
  journal **MUST** read it through `relabel()`, which rewrites the two halves positionally. Prompt
  wording is not an acceptable substitute; two rounds of it failed against one pronoun.
- **A night's hands are audited like her daytime hands.** A desk write or a camera dispatch made
  by a job **MUST** leave a `tool-logs/calls.jsonl` line (§7.3), so the Tools surface answers "what
  touched this vault" for the unattended hours too. A dry run makes no calls and **MUST** claim
  none.
- **Handled is not produced.** A job that decided there was nothing to write **MUST** still mark
  its day done. Otherwise the backlog never empties, `DREAM → DORMANT` never fires, and she spends
  every night re-deciding not to write the same note.
- A job that raises is caught, reported, **MUST NOT** mark its day (so it retries), and **MUST
  NOT** end the night. One bad prompt added last week must never cost consolidation.
- Jobs write to `workspace/` (§34) through `DreamContext.put`, never to `memory/` or `soul/`. A
  nightly job that could append to semantic memory would be a second, unaudited consolidator; one
  that could touch `soul/` would be §23.2 with the gate removed. `consolidate` writes to `memory/`
  because it *is* the consolidator, through its own long-standing path.
- Every model call goes through `DreamContext.ask`, which records it verbatim — the transcript is
  complete by construction, and is what §21.3 serves.

### §21.3 — Running a night by hand

`GET /api/mind/dream` serves the roster (per job: enabled, backlog, last run, last result).
`POST /api/mind/dream/run` runs it, taking optional `job`, `day`, `dry_run` and `budget`.

- The response **MUST** include, for every model call the run made, the exact system message, the
  exact input and the raw completion. A dream job is a prompt whose output is otherwise invisible
  until the next morning; without this the iteration loop is one day long.
- `dry_run` **MUST** make the same model calls, report what it *would* write, and write nothing,
  mark no day done and leave no commit.
- It runs **inline**, unlike §24.3's self-edit decision — a decision belongs to the loop's next
  tick, a test you are watching has to answer you — and **MUST NOT** move the activity ladder. A
  night you asked for is not evidence she drifted into one.

## §22 — Goals and intentions

`yurios/mind/goals.py` — `vault/goals.md` is the store: a human-readable markdown checklist, because
what an agent intends to do should be a file her user can open. Each goal carries kind, priority,
optional due time, **provenance**, and a **commitment strategy**; lifecycle
`pending → active → waiting → done | abandoned`.

- §22.1 **Goal genesis is designed, not assumed.** Sources, stamped as provenance: the user's
  explicit asks (`user:remind-me`, scanned from their turns); **her own promises**
  (`promise:her-own-words`) — REFLECT scans every committed reply for first-person commitments
  ("I'll look into that") and files each as a `reach_out` goal with a due time, because a companion
  who forgets her own promises is worse than one who forgets yours; and maintenance (DREAM backlog,
  shelf drops). Near-duplicate open goals **MUST** merge, not multiply.
- §22.2 **Commitment governs staleness:** `blind` is defended past due (a birthday is a birthday),
  `single-minded` drops only when moot, `open-minded` is abandoned the moment it stops being timely.
  The suspend-gap catch-up (§15.4) applies these in one pass.

## §23 — The SOUL split and gated self-edits

`yurios/mind/selfedit.py`, `yurios/mind/vaultio.py`. Who she is, immutably; who she's becoming,
reviewably.

- §23.1 **The constitution is read-only, even to her.** Every mind write path goes through
  `MindVault`, which refuses `soul/CONSTITUTION.md` unconditionally — and the self-edit flow refuses
  even to *queue* a proposal against it. The other `soul/*.md` identity surfaces require the gate
  token only the self-edit flow holds; a store or a stray ACT cannot quietly become who she is. Paths
  **MUST** be jailed to the Vault.
- §23.2 **Risk-gated proposals.** `propose(surface, content, reason)`: low risk (memory, world,
  knowledge, goals — working products) applies immediately and commits; high risk (any `soul/`
  surface, and every unknown surface — fail safe) is **queued** in `state/pending_edits.json` with
  its full content and reason, rendered by the inner-life panel with approve/reject. The decision
  returns as a `selfedit_decision` signal the loop consumes (§16.2) — applied edits are git commits,
  so drift is never silent and `git revert` undoes any of it; the ruling itself is journaled ("you
  applied/rejected my edit to …").

## §24 — The journal, the trace, and the inner-life surface

The product half of autonomy: what converts an always-on process from creepy to *an inner life*.

- §24.1 **The journal.** Her autonomous acts write into the **same episodic day files as the
  conversation** (`memory/episodic/YYYY-MM-DD.md`), as `### HH:MM  [she] …` lines — one journal, two
  authors, one DREAM pass over both. Each line is indexed into memory (she can recall her own past
  acts) and published as a `journal` event on the bus. SILENT outcomes journal; ambient murmurs do
  not (the never-persist rule holds for them).
- §24.2 **The tick trace** (`traces/ticks.jsonl`): one structured record per tick — sensed, appraised
  (with scores), decided (with runners-up), acted, and the full interrupt decision with its factors.
  The scenario tests (§27.2) are queries over this file; the "why did she…" answer is always in it.
- §24.3 **The surface.** `GET /api/mind` (state, cadence, budget, goals, shelf, pending edits),
  `GET /api/mind/journal?days=` (her `[she]` lines by day), `GET /api/mind/trace?n=`,
  `POST /api/mind/edits/{id}` (`{"approve": bool}` → a signal, §23.2). The browser page's chat column
  grows a second tab — **inner life** (`web/js/mind.js`): right-now state and budget, edits waiting on
  you (with content and one-click approve/reject), goals with provenance, the shelf, and the journal,
  refreshed live off the same one bus (`journal`/`mind` events). Everything reads *through* the mind's
  own stores; the dashboard can never disagree with the files.

## §25 — Config (the mind's knobs)

Extends §11 (`yurios/world/config.py`); every knob has a default and the default stack still needs no
key. `MIND_ENABLED` (off = the reactive body minus ambient life); `MIND_SEED`; the two dials
`MIND_ACT_THRESHOLD` / `MIND_INTERRUPT_THRESHOLD` and the hard cap `MIND_MAX_INTERRUPTS_PER_DAY`;
`MIND_CONSIDER_COOLDOWN_S`; `MIND_DAILY_TOKENS`, `MIND_DREAM_TICK_TOKENS`; the cadences and drift
timeouts `MIND_{ENGAGED,IDLE,DORMANT,DREAM}_CADENCE_S`, `MIND_ENGAGED_TIMEOUT_S`, `MIND_IDLE_TIMEOUT_S`,
`MIND_DREAM_START_HOUR`/`END_HOUR`; and the reflex windows `IDLE_SETTLE_S`, `IDLE_ACT_MIN/MAX_S`,
`IDLE_TALK_MIN/MAX_S` (§15.5). Her desk (§34): `WORKSPACE_ENABLED`, `WORKSPACE_DIGEST_FILES`,
`SKILLS_ENABLED`, `TOOL_RATE_DESK`. The DREAM pipeline (§21.2) has **no** per-job switch: the
roster lives in `mind/dreamjobs.py`, and a job whose prerequisite is absent (the camera, for
`selfie`) takes itself out of the night through `DreamJob.enabled`. The port is **8768**.

---

## §26 — Omissions (normative)

This is a reference implementation of *initiative*, not the fully productised runtime. **No sandboxed
workshop**: no code execution, no shell, no autonomous research-and-build, no wiki authoring — the
heavy hands remain the named next rung, and §23.2's gate is where their products would cross into the
mind. **No multimodal sensing**: SENSE reads text, time, files, and its own completions — no vision,
no prosody — which is enough to prove an interrupt threshold can stay silent. **The world model stops
at the snapshot**: no temporal knowledge graph, no multi-hop queries (§19.1 names the stage). **One
process**: the mind runs in-process on the host's event loop, not as a supervised per-character OS
process behind a wire protocol — 0.2's host supervises isolated in-process character runtimes,
while the two-tier host/engine split with a brokered IPC seam remains the productisation rung.
The host registry provides multi-character routing and rejects shared writable roots, but does not
yet provide process-level crash or credential isolation. **No affective state file** — the reflex
pulses approximate warmth without a model of it.
Conversation is observed by the loop, not generated by it (§15.3) — full one-loop unification lands
with the two-tier split. And the mind never *initiates* tool calls (her MCP hands stay
conversational); a tool-bearing autonomous act needs the broker that comes with the workshop.

## §27 — Tests (the hard gate)

`pytest` **MUST** ship and be green from the project root, entirely offline — fakes for STT/TTS/VAD,
a fake tool runner, `httpx.MockTransport` for weather, an in-memory MCP session for the contract
tests, and `VirtualClock` for everything timed. All mind tests run on `VirtualClock` + the real
brain with fake models.

- §27.1 **Mechanics.** The reactive body: emotion and tool-tag parsing (whole, split, unknown/
  unclosed/oversized dropped, never spoken); the tool loop end-to-end over a scripted fake stream
  (guard consulted, result reaches the continuation, call cap enforced, tool error still completes
  the turn); **barge-in mid-continuation cancels and persists nothing**; guard allowlist/rate-limit/
  audit; the real MCP server's contract (`list_tools` = exactly four, three with selfies off; schema,
  bounds, the `take_selfie` slot contract — named template keys render from the
  library, anything else passes through verbatim, the contract refuses nothing); the selfie lab (a started contract becomes a PNG + provenance
  sidecar and an `image_url` message in sim time; the announce cue offered and dropped when busy; a
  broken forge becomes a quiet message; no key degrades openrouter → mock loudly); timer scheduling
  and queued announcements; every §4 op's event shape including `rain`/`music`; the hub (typed
  fan-out, sticky replayed last-write-wins, a full queue drops without blocking, thread-safe publish);
  the SSE route and the world `/ws/voice` route (greeting-once, noise-drop, barge-in, ambient
  injection reaches the client and is not persisted, the transcript tee, expressions on the bus and
  off the wire); the debounced `SpeechGate` and the transcript sanity filter (an all-noise utterance
  dropped while a real one is taken); first audio precedes tool execution on a tool turn; the §3.4
  palette map (every brain palette name has a frontend catalog entry — source-scanned); the §2.5
  situation block (the stated time is the injected clock's and moves when it advances, the embodiment
  truth present verbatim — never "no body", weather/music following the sticky scene state, pending
  timers listed and leaving when they land); the desktop launcher and both bodies honouring the flag;
  and an end-to-end turn over the **real brain** proving one corpus line + one Vault commit. The mind:
  one intention per tick and the trace shape; REST majority over a quiet simulated day; the ENGAGED
  preempt from any state; one git commit per dirty tick and none for a resting tick; the murmur
  needing company, quiet, and IDLE (and never appraising into an empty room); goal work silent and
  journaled; the budget debited by her own words; activity drift down the ladder, the DREAM window,
  budget pressure shedding IDLE, and restart-resume; gate-1 ordering (nothing outranks the person
  speaking) and the surprise bonus; gate-2 quiet hours as a gate, the hard daily cap, the shown
  factors; the world model (situation assertions verbatim, presence arithmetic, expectation met vs.
  violated, `query(at=…)`); knowledge (drop→scan→ingest→cited search, re-ingest replaces, forget, the
  memory boundary, the failed-ingest degrade); DREAM (backlog excludes today, oldest-first resumable
  budget, dedupe, salience-2.0 indexing, the offline heuristic); the DREAM **pipeline** (priority
  order, the shared budget leaving a backlog not an overrun, a day priced by its prompt and not by
  its file, a job at the ceiling not taking the cheap jobs with it, the first item always running, per-job
  resumable progress, a once-a-night job not walking backwards through history, a job that wrote
  nothing still clearing its day so the ladder can leave DREAM, a failing job neither ending the
  night nor marking its day, the dry run that thinks and writes nothing, and the report carrying the
  prompt verbatim — `test_dreamjobs.py`); her **desk** (every climb, dotfile and symlink out refused
  by one enforcement point, the ceilings, the digest being an index not the contents, the skill
  catalog costing a line per skill, a disabled skill leaving the catalog but not the disk, a mangled
  `SKILL.md` costing one entry — `test_workspace.py`) and its hands (unadvertised without a vault,
  a note round-tripping, a refusal that names a working path, every argument documented where she
  reads it — `test_mcp_contract.py`); goals (roundtrip, dedupe,
  commitment-aware reconsideration, promise extraction incl. negations); the SOUL gate (constitution
  refused even gated and never queued, identity surfaces gate-only, low-risk applies, unknown fails
  safe, approve applies + commits, reject leaves no change, vault jail); the routes (snapshot, journal,
  decision-as-signal consumed on the next tick and journaled, 404, the 503 + health truth when
  mindless, the DREAM roster in priority order, a hand-run night answering with its prompts, dry vs.
  wet, and the ladder not moving because you pressed a button); and the boot path (`create_app` over the real brain: mind running, health/boot reporting
  it, the §19.2 seam actually wired).
- §27.2 **The scenario battery** — multi-day sim-user runs asserted over the tick trace, because "it
  felt right when I watched it for an evening" is not a gate: **the interview was Tuesday** (told
  Monday; user leaves; exactly one reach-out, inside the right window Tuesday, visible SILENT restraint
  before it, factors in the trace, nothing spoken into the empty room); **the dark weekend** (user gone
  60 h: not one message, but DREAM consolidated Monday into facts, DORMANT visible, REST majority, the
  journal carrying the night's work); **the machine sleeps** (a 10-hour power-off: one suspend-gap
  catch-up, journaled, not re-sensed); **her own promise** ("I'll sleep on cat names" → a `reach_out`
  goal with promise provenance and a due time, journaled as made); **a timer is a promise** (announce
  queues while nobody can hear, delivers when a page attaches).
- §27.3 **The house (Part III).** The card parser against malformed, oversized and hostile PNGs
  (`test_characters_card.py`); registry round-trips, schema refusal, path escapes, and rollback on a
  failed write (`test_characters_registry.py`); import as a transaction — a failure leaves no
  partial character, a generic card lands under review, a name collision takes the next id
  (`test_characters_importer.py`); the host — overlapping storage refused at construction, per-character
  config resolution and Telegram pair resolution, routing to the right runtime, the profile save that
  accepts a review, portrait cache headers, her own connection beating the profile she points at and
  an override cleared handing her back to the `.env` (`test_host.py`); the live model swap — both
  providers and the memory store's reference rebuilt, the per-call knobs needing none, a registry
  string coerced or refused, an injected model left alone (`test_rewire.py`); and the migration — check/dry-run/run,
  the refusals of §33.3, legacy roots left untouched, the marker written last, and the default
  portrait installed exactly once (`test_migration.py`).

## §28 — Extends to

The ladder's last rung inside one process; every seam past it is already shaped. The **two-tier
split**: the mind's stores speak narrow contracts over an in-process seam — promoting them to a wire
protocol and the engine to a supervised per-character process is a topology change, not a rewrite, and
it brings the broker (the Guard's grown-up form), the model router's privacy boundary, and true
one-loop conversation (§15.3's named rung) with it. The **workshop**: a sandboxed workspace beside the
Vault where ACT dispatches real work — research, code, builds — to an embedded harness and never awaits
it (the selfie lab's start-don't-await rule, generalised), with §23.2's gated flow as the one door from
work-product to self. The **temporal knowledge graph** behind `WorldModelStore`'s unchanged contract
when "what was true when" starts to bite.

And **distribution** — the last item on this list, and the one that is now **built** (§28.1):
this Vault's SOUL exports as a `.PNG` character card and boots on someone else's machine, which
is the point of the whole ladder — the companion you own, that you can move by copying a folder.

### §28.1 — The card studio (built)

`yurios/characters/exporter.py` is the importer's mirror: it resolves `soul.yaml` against the
soul files the mind has been living in, flattens them into a Character Card V3 (`ccv3` + a `chara`
fallback, `tEXt`, spliced after IHDR), and carries the soul files **verbatim** in a
`data.extensions.yurios` block so a re-import reconstructs `vault/soul/` byte-for-byte rather
than re-deriving flattened prose with holes in it. Published as `docs/card-format.md`, so the
format is a contract other runtimes can read rather than a quirk. `generation` counts the hops;
`growth` carries counts and never content.

Three things the flattening gets wrong if you write it naively, all pinned by test: `{{user}}`
and `{{char}}` **MUST NOT** be expanded (the loader expands them because it is building a prompt;
an export that did would bake the exporting user's name into a stranger's card); `BOOTSTRAP.md`
is consumed-once (§5.4), so `first_mes` **MUST** fall back to a return greeting on exactly the
grown characters this feature exists for; and the export is *authored* from an allowlist, never
copied from `card.json`.

**The scrub** (`yurios/characters/privacy.py`) is the load-bearing half, defended four times:
the exporter takes no path from any caller, so the private surfaces are never named; the SOUL
reader is jailed and refuses `USER.md`, `MEMORY.md` and the manifest's own `runtime_only:` list
however a `fields:` reference asks; the card is built key by key from the V3 allowlist; and then
canaries harvested from *this* vault's private surfaces are asserted absent from both the card
and the final bytes. Credentials and a distinctive `USER_NAME` are hard blocks at any length —
and `privacy.py` is the only module in the export path permitted to read `os.environ`, so the
module that can see a secret cannot emit one and the module that emits cannot see. A passage
present in both her soul and a private surface is the honest hard case (she learned it, you
approved it at the §23 gate) and fails **closed** pending one human acknowledgement.

A card starts the relationship at zero: `USER.md` arrives empty, `memory/`,
`goals.md`, `corpus/` and `traces/` are empty, and the new Vault's history begins at one commit
(`soul-src`, D-014). The import path **MUST** seed a Vault the way `scripts/seed_vault.py` does,
`MEMORY.md` included: it is `runtime_only:` because it is *memory*, so it lands as
`memory/semantic/facts.md` + `forgotten.md` and **MUST NOT** also be written under `soul/`, where
nothing reads it and her gated self-edits (§23) would be offered it as a place to put a memory. The studio (`/studio/`, `web/studio/`) is the surface: create a character
without a card to import, edit one as prose with her own grown edits marked and diffable, pick a
portrait or a selfie, and read what stays on the machine before pressing export.

---

# Part III — the house (§29–§33)

Parts I and II specify **one** companion. YuriOS 0.2 runs a house of them: the process that
starts is a **host** which owns the registry, the storage tree and the ports, and starts one
isolated **runtime** — the whole of Parts I and II — per character. Nothing in Parts I or II
changes; a runtime does not know it has neighbours.

## §29 — The host and the character registry

- §29.1 **The storage tree.** `DATA_DIR` (default `./data`) is the root the host owns. Under it:
  `characters.json` (the registry), `connections.json` (§31.1), `archives/` (§29.6), and
  `characters/<id>/` — one self-contained root per character holding `source-card.png`,
  `card.json`, `portrait.png`, `vault/`, `corpus/`, `traces/`, `tool-logs/` and `selfies/`
  (`characters/models.py`'s `CharacterPaths`). A character is a directory: moving her is copying
  it, which is the Part I promise held at house scale.
- §29.2 **The registry is one atomic JSON file.** `characters.json` carries
  `{schema_version: 1, characters: […]}`; an unknown `schema_version` **MUST** be refused, not
  guessed at. Every write is a whole-file atomic replace (temp file → `fsync` → `os.replace` →
  directory `fsync`, `characters/registry.py`), and an in-memory mutation that fails to persist
  **MUST** be rolled back, so the file on disk and the process never disagree. Persisted paths
  **MUST** be relative to `DATA_DIR` and **MUST NOT** escape it on load — a registry is portable,
  and a path that climbs out of the tree is a rejected registry, not a warning.
- §29.3 **Ids.** A character id is 1–64 characters of lowercase ASCII, digits, `.`, `_` or `-`,
  derived from her display name, with `_v2`, `_v3`… appended when the name is taken. The id is
  the URL segment, the env-var suffix (§10.5) and the directory name — one identifier, everywhere.
- §29.4 **Isolation is checked at construction.** `CharacterHost.__init__` **MUST** refuse to
  build when any two characters' writable roots overlap — equal, or one inside the other. Two
  minds sharing a Vault would interleave commits and consolidate each other's memories; the host
  fails loudly at start rather than discovering it at 3 a.m. in DREAM. Process-level isolation is
  explicitly *not* provided (§26): runtimes share one event loop and one address space.
- §29.5 **Lifecycle.** `start(id)` builds a runtime app for exactly one character and starts it;
  a character that is disabled or still under review (§30.3) **MUST NOT** start. Start/stop/restart
  serialise on one lock. A runtime that fails to start leaves the host up with that character in
  `failed` and its error kept for the board — one broken companion is never a down house. At boot
  the host starts every character that is `enabled` **and** `autostart` **and** not under review,
  and a failure there is skipped, not fatal. Shutdown stops runtimes in reverse start order.
- §29.6 **Archive and purge are different acts.** `archive` stops the runtime and *renames* her
  root under `archives/<id>-<timestamp>` — her files survive, she leaves the board. `purge`
  deletes the root and **MUST** require a confirmation string matching her id or display name.
  Nothing else may delete a character root.
- §29.7 **Routing.** The host serves the switchboard at `/`, a character's page at
  `/characters/<id>/sanctuary/`, and dispatches `/api/characters/<id>/…` and `/ws/characters/<id>/…`
  into that character's runtime by rewriting the path to the runtime's own `/api/…` or `/ws/…`
  — so every Part I route exists per character, unchanged, and the runtime is unaware of the
  prefix. A request for a character that is not running **MUST** answer 404 with a plain reason.
  One **primary** character (the first enabled autostart one, else the first to start) additionally
  answers the unprefixed Part I routes, which is what keeps the single-companion install, the
  terminal channel and the desktop window working with no character in the URL. With no character
  running, those routes answer 503.

## §30 — Character cards: import, review, edit, export

- §30.1 **The card reader is a strict parser, not a loader.** `characters/card.py` reads
  SillyTavern V2/V3 cards out of PNG `tEXt` chunks (`ccv3` preferred, `chara` accepted), and
  **MUST** bound everything before decoding it: file bytes, chunk bytes, chunk count, decoded
  metadata bytes, image width/height/pixels (`CardLimits`). A card is a file from the internet;
  an invalid V3 chunk **MUST NOT** silently fall back to a V2 one. A card that carries the same
  keyword more than once — the shape an edited-and-re-uploaded card actually arrives in — is read
  from the **first** payload in the file, and the parser **MUST** report the choice
  (`ParsedCard.warnings`) rather than make it quietly; the importer **MUST** carry that sentence
  into `NOTES.md`, where a reviewer reads, and **MUST NOT** let such a file import enabled however
  native it claims to be, since the block vouching for it is one of the payloads in question. The
  number of repeats is itself bounded (`max_card_chunks`).
- §30.2 **Import is transactional.** The whole character — source card, `card.json`, a
  re-encoded portrait, a seeded Vault with her SOUL files written from the card's fields, the
  empty corpus/traces/tool-logs/selfies roots, and a `git init` of the Vault — is assembled in a
  staging directory on the same filesystem and made visible with **one rename**; the registry
  entry is added last. A failure at any point **MUST** leave no partial character behind. The
  portrait is re-encoded from the PNG's pixels rather than copied, so no chunk of the uploaded
  file survives into the served image.
- §30.3 **Generic cards arrive under review.** A card that does not declare itself a YuriOS card
  (a `yurios` key on the card, its fields, or its `extensions`) is imported **disabled**, with
  `review_required` set: her capabilities do not run and no mind wakes. Saving her profile once
  (§30.4) is the human act that accepts the review and starts her. A YuriOS-native card may
  import enabled. The switchboard **MUST** show a character under review as needing attention.
- §30.4 **The SOUL files are authoritative; the card is the interchange format.** Editing a
  character's profile writes `card.json` *and* rewrites the corresponding SOUL sections in her
  Vault (`description`/`system_prompt`/`post_history_instructions` → `CONSTITUTION.md`,
  `scenario` → `SCENARIO.md`, `first_mes` → the cold open, `personality` → `PERSONA.md`
  frontmatter, `creator_notes` → `NOTES.md`, `name` → `soul.yaml`), and **MUST** commit the
  Vault. Prompts are assembled from the SOUL (§2.1), never from `card.json` — so an edit that
  did not reach the files would be an edit that did not happen.
- §30.5 **Export is identity, never intimacy.** An exported PNG carries her portrait with both a
  `chara` (V2) and a `ccv3` (V3) chunk built from `card.json` — identity, persona, scenario, lore.
  It **MUST NOT** carry `USER.md`, relationship memory, the corpus, traces, tool audit, selfies
  or any credential. Sharing her is sharing who she is, not who you are.
- §30.6 **A foreign card is re-filed on the way in, and may be re-filed again by a model.**
  Every card site lays a character out differently and there is no schema to parse, so the
  importer **MUST** make a mechanical best effort and the studio **MUST** offer a better one.
  The mechanical half (`characters/cardsplit.py`) routes the card's `description` into the four
  backbone sections by reading its section headers, and **MUST** be lossless: every line lands in
  exactly one section, in its own words, and a layout it cannot read leaves everything under
  `#Identity` — which is what the importer did before it existed. The model half
  (`characters/optimize.py`, `POST /api/studio/optimize`) sends the whole draft to a
  user-chosen model, which may move, split, merge and re-register any field, and takes a free-text
  instruction from the user for the second job this serves — not repair but preference
  (*"she is too guarded; make her devoted from the first line"*). It **MUST NOT** write:
  the route returns a proposed draft plus a field-by-field diff, and only the ordinary studio
  PATCH (§30.4) reaches the Vault. That is also the injection boundary — a card is a file from
  the internet, so the worst its text can do is propose an edit a human then declines. The
  model's answer **MUST** be merged against the draft's own types, never trusted as given, and a
  truncated answer **MUST** be salvaged down to the fields the model finished and reported as
  partial rather than presented as a complete pass. The optimiser re-files; it **MUST NOT** invent
  facts about the character. `examples` is the one exception, because most cards ship none and an
  empty examples field is the difference between a model that has heard her speak and one guessing
  from adjectives: where a card has no examples the optimiser **MAY** compose them, bounded to
  demonstrating a voice the card already describes — an example asserting a new fact is an invented
  fact — and where a card *has* examples they **MUST** be kept as the author wrote them. `scripts/bench_cards.py` scores a folder of
  real cards through the whole path, because "does the importer handle foreign layouts" is not a
  question one card can answer.
- §30.7 **The optimiser runs in passes, because the reply voice thinks.** A reasoning model spends
  its `<think>` tokens out of the same window as its answer, and a local model is routinely
  *loaded* with a context far below what it supports. One call carrying a whole card is therefore
  not a slower design, it is a broken one: measured on a 12B local model, the single-call form
  spent every available token reasoning and returned an empty string. So the re-file **MUST** be
  split into passes that each send only the material informing their own group and ask for only
  that group, run sequentially over the accumulating draft; each pass's budget **MUST** carry a
  flat reasoning allowance on top of its answer estimate; and a pass **MUST NOT** write outside its
  own fields. A pass that fails **MUST NOT** discard the passes that succeeded. An answer that is
  empty because the model reasoned until the window closed **MUST** be reported with the numbers
  that identify it — the window it stopped at, the prompt's share of it, and the tokens spent
  thinking — because the remedy is a setting the user can change and a shrug is not. A run of
  several sequential model calls is minutes long, so the route **MUST** be able to report progress
  as it happens — a line per pass start, retry, completion and failure — and the studio **MUST**
  show it; a button that shows nothing for minutes is indistinguishable from a broken one. That
  reporting **MUST** be decoration: a listener that fails, or a client that hangs up, **MUST NOT**
  change the result of an optimisation, and the endpoint **MUST** still answer with a single
  object for a caller that did not ask to watch.

## §31 — Connections and per-character bindings

- §31.1 **Named connection profiles.** `connections.json` (`{schema_version: 1, profiles: […]}`)
  holds `{name, backend, endpoint, api_key_env}` — a profile names *where* a model is reached and
  *which environment variable* holds the key. It **MUST NOT** contain a secret; keys live in the
  host `.env` and are read from the environment by name. On a first run with no file, the host
  seeds `default` and `legacy-default` from the host's own `.env` route, so an upgraded
  single-companion install already has the profile its characters point at.
- §31.2 **A character's record overrides the host default, field by field.** `config_for_character`
  builds a runtime `Config` from the host's, always replacing the character-scoped identity and
  paths (name, vault, corpus, traces, tool logs, selfies, the loop switches, her Telegram pair),
  and replacing a field only when her record names one: chat and utility model, TTS/STT backend
  and voice register, body backend and avatar model, plus any `.env` knob named in
  `models.options` (`temperature`, the reasoning switches, `MAX_REPLY_TOKENS`, `CONTEXT_LENGTH`…),
  coerced to that field's own type — the registry is JSON, and a value that will not coerce is
  dropped with a warning rather than taking her runtime down. A blank binding therefore means
  *inherit*, which is what makes one `.env` still configure a house.
  **Her own connection wins over the profile she points at**: a profile is the house's shared
  connection and her record is the exception she was given, so `connection.endpoint` (else the
  profile's) re-points the LM Studio or Ollama base url her models actually route to, and
  `connection.api_key_env` (else the profile's) names the variable her key is read from. The key
  is read from the environment at resolution time and **MUST NOT** be written to the registry.
  An inherited endpoint that is verbatim the host's url for the *other* local provider is
  ignored — the seeded `default` profile carries whichever provider the host's own model uses
  (§31.1), and a character who moves to the other one inherits the host's url for hers.
- §31.3 **Loop switches are per character.** `mind`, `utility` and `dream` are registry fields, not
  just `.env` knobs: one companion may be a fully autonomous mind while another is reactive-only.
  Toggling `mind` **MUST** take effect on the live runtime without a restart; toggling `utility`
  or `dream` **MAY** restart her runtime, because they are wired at construction.
- §31.4 **Her brain settings change without a restart.** The model a character speaks through,
  its route, its key and its per-call knobs **MUST** be swappable on a running runtime: the
  providers are rebuilt from the live `Config` — which is *mutated in place*, since one object is
  shared by the runtime, the brain, the memory store, the mind and the VRAM parker — and
  `AppState.chat`/`AppState.utility` (and the memory store's own utility reference) are
  re-pointed (`world/rewire.py`). A stream already in flight holds its provider and finishes on
  it; everything after it speaks through the new one, in the same session, with the same memory,
  mind and voice. Pinning a newly chosen local model into LM Studio happens *behind* the answer, and
  the context gauge is re-probed when it lands. The embedder is explicitly **not** hot-swappable:
  changing it re-indexes the Vault (§4.3). `GET|PATCH /api/brain` (and `/api/characters/<id>/brain`,
  which is what the gear in her room calls) is the surface. A profile save (§30.4) **MUST** take
  the same path rather than restarting whenever it left everything the runtime was *built* with —
  her name, her voice, her body, the utility and dream loops — where it found it. That decision is
  made by comparing the record before and after the save, not by which keys were sent: the
  switchboard posts the whole form every time, and re-submitting an unchanged voice is not a
  reason to take her conversation down.

## §32 — The switchboard

- §32.1 **The board is the front door.** `/` serves the character board (`web/dashboard/`): one
  tile per registered character with her portrait, name, state, model and voice, an import
  control, and a drawer with her journal, log and context history. Entering a character is a
  navigation to `/characters/<id>/sanctuary/`; **leaving her room returns to the board without
  stopping her runtime** — a companion's life does not depend on being looked at.
- §32.2 **State is the truth, not a guess.** A character's reported state is `attention` when she
  is under review, else her mind's activity state when a runtime is up, else the host's own
  `offline` / `starting` / `ready` / `failed`. A failed character reports her error. The board
  **MUST** display an unrecognised state as unknown rather than inventing one.
- §32.3 **One design system.** The board, both bodies (§6.6) and the shared `.env` panel (§11)
  carry the same chrome (§6.3): entering a character must not feel like leaving the app.
- §32.4 **The API is same-origin JSON** (`web/dashboard/API.md`): `GET /api/characters`,
  `GET /api/connections`, `POST /api/characters/import`, `GET|PATCH /api/characters/<id>/profile`,
  `GET|PATCH /api/characters/<id>/brain` (§31.4 — also unprefixed, for the primary),
  `PATCH /api/characters/<id>/loop`, `PATCH /api/characters/<id>/controls`,
  `GET /api/characters/<id>/{portrait,export,journal,log,context-history}`,
  `POST /api/characters/<id>/archive`, `DELETE /api/characters/<id>/purge?confirm=…`. The portrait
  route **MUST** send `Cache-Control: no-cache`: one stable URL whose bytes genuinely change
  (a re-render, a replaced file, a fresh install on the same port) must not show yesterday's face.

## §33 — The 0.1 → 0.2 migration

- §33.1 **It runs itself, once, before any mind wakes.** On the first 0.2 start, the legacy roots
  (`VAULT_DIR`, `corpus/`, `traces/`, `tool-logs/`, `selfies/`) are assembled into a registered
  character under `DATA_DIR` (`yurios/migrate.py`). `python -m yurios.migrate --check` reports
  without touching anything; `--dry-run` shows the plan; `--data-dir` targets another root.
- §33.2 **Copy, never move.** The legacy directories **MUST** survive untouched as the backup.
  The character is assembled in a staging directory on the same filesystem and made visible with
  one rename, and `layout.json` is written **last** — its presence is the durable record that
  migration completed, so an interrupted run is a no-op that can simply be run again.
- §33.3 **It refuses rather than risks.** Symlinked or unreadable legacy trees, a Vault whose
  `soul.yaml` is invalid or declares an unsupported `vault_format`, a registry rooted at a
  different `DATA_DIR`, a colliding destination, or a Vault git repository that cannot be
  committed **MUST** abort the migration with an explanatory error and no partial character.
  A legacy Vault that is a git repository keeps its history and gains one migration commit.
- §33.4 **She arrives with a face.** A migrated character whose display name is the shipped
  companion's and who has no `portrait.png` gets the packaged default portrait, once, and only
  when the file is absent — a portrait the user replaced or the forge rendered is hers and is
  never overwritten. A missing packaged portrait is a cosmetic loss and **MUST NOT** fail a
  migration.

## §34 — Her desk and her skills

`yurios/mind/workspace.py` — two directories inside the Vault that she may write without asking,
and the sandbox that makes that safe. Every other write path in the mind is narrow on purpose
(`memory/` by DREAM, `world/` by SENSE, `soul/` only through §23.2), which is the right shape for
the things she *is* and the wrong shape for the things she is *doing*.

- §34.1 **`vault/workspace/` is her desk.** No schema, no ceremony: drafts, research scratch, the
  middle of a thought. `vault/skills/` is the same primitive pointed at instructions. Both live
  **inside** the Vault and travel when the folder is copied; only skills are **versioned**.

  The desk **MUST NOT** be git-tracked. Scratch churns, and a draft rewritten four times while she
  works something out is four commits of a diff nobody will read — the Vault's `git log` is the
  diary of how she grew, and working notes bury the entries that matter. Skills **MUST** stay
  versioned: a skill is a durable statement about how she does something, changing one is exactly
  the kind of change worth reverting, and they are written rarely enough to stay legible.

  The rule lives in `workspace/.gitignore`, **inside** the folder, for the reason
  `KnowledgeStore.INDEX_GITIGNORE` gives: a Vault's own `.gitignore` is written once at seed time
  and never refreshed, so a line added to it today protects no existing vault. It **MUST** also be
  written by the seeders (`characters/importer.py`, `scripts/seed_vault.py`) before their first
  commit — `.gitignore` cannot untrack a path git already knows. Both folders are seeded with a
  README saying what they are for, because a directory the docs address by name has to exist to be
  dropped into.

  A desk write therefore **MUST NOT** dirty the Vault. What a night did still reaches `git log`
  through the journal line and the job ledger; the diary entry itself does not.

- §34.2 **The sandbox is the design, and it is dull.** `Workspace.resolve()` is the single
  enforcement point every read and write passes through, including listings. It **MUST** refuse an
  absolute path, any `..`, and any component beginning with `.` (`.git/`, `.gitignore` and `.env`
  are all within reach of a root that is otherwise hers, and none of them are notes). Symlinks
  **MUST** be resolved *before* the containment test, so a link planted inside the desk that points
  at `soul/CONSTITUTION.md` fails the same check a `../` would. Per-file, whole-tree and file-count
  ceilings **MUST** refuse rather than fill a disk.

  The hands are `list_notes`, `read_note`, `write_note`, `append_note`, `delete_note`,
  `read_skill`, `write_skill`, `delete_skill`, advertised by the tool server only when `VAULT_DIR`
  is in its environment — the `SELFIE_BACKEND=off` rule (§7.6). That path **is** the sandbox root
  and is fixed at spawn time, so one character's hands can never reach another's desk. A refusal
  **MUST** say what a working path looks like: "denied" teaches nothing and the same path is tried
  again next turn.

  The tool server is a separate process writing straight to disk, so a desk write **MUST** be
  reported back to the host (`_realise` → `MindLoop._desk_written`), which marks the Vault dirty
  and journals it. Without that a note she wrote at noon lands in whatever commit fires next,
  labelled as something else.

- §34.3 **Skills are progressive by format.** One folder per skill, each with a `SKILL.md` of YAML
  frontmatter (`name`, `description`, `author`, `enabled`) plus a body of instructions. Every turn
  carries the **catalog** — one line per enabled skill, name plus `description` — and the body is
  loaded only through `read_skill`, once she has decided this is the skill the moment calls for.
  Twenty skills therefore cost twenty lines of context until one is used. The `description` is
  written as *when to reach for this*, not as a title. The desk gets the same treatment: the prompt
  carries a listing of the newest `WORKSPACE_DIGEST_FILES` files, never their contents. A mangled
  `SKILL.md` **MUST** cost that one entry, never the block.

- §34.4 **Nothing here executes.** The desk holds inert text. The coming code harness (§28's
  workshop) gets its own workspace **outside** the Vault precisely so that "she can write here" and
  "this can run" never become the same sentence, and its own skills folder for the same reason.
