# YuriOS — SPEC

Normative specification for YuriOS. Keywords **MUST**, **SHOULD**, **MAY** are used in
the RFC-2119 sense.

YuriOS is **one independent, self-contained project**: an always-on, local-first
companion who lives on the user's own machine — a 3D body you can see, a real-time voice
loop you can speak into, four tools she can reach for, one outbound event bus, and an
**always-on mind** that keeps running whether or not anyone is looking. Everything below
is this project's own code, run as **one process, one origin** (`python -m yurios.world`,
port **8768**). The Python packages are `yurios/app` (the brain), `yurios/desktop` (the
voice loop), `yurios/forge` (the image service), `yurios/world` (the body, the tools, the
bus, the server), and `yurios/mind` (the autonomy engine); the SOUL she is seeded from
lives in `soul-src/`, the browser frontend in `web/`. Where a subsystem has a history
worth recording, that history is documented in `PROVENANCE.md` — provenance, not a
dependency.

This document is organised in two parts. **Part I — the body (§1–§14)** specifies the
reactive companion: the brain and its file-Vault, the voice loop, the 3D body, the hands,
and the one bus. **Part II — the mind (§15–§25)** specifies the always-on autonomy engine
built on top of it, with omissions, tests, and the growth path at §26–§28. Section
numbers are stable: in-source `SPEC §n` comments cite them.

---

# Part I — the body (§1–§14)

## §1 — Goal and properties

A browser-based 3D companion — a VRM body in the canonical sanctuary, a chat transcript
beside her, a real-time voice loop, four tools over MCP, one event bus — driven by an
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
8. THE HONESTY CONSTRAINT (§2.1, fixed text)
9. EXAMPLE VOICE (if budget allows)
```

followed by the last `RAW_WINDOW_TURNS` raw messages (default 6) and the new user message.
`CONSTITUTION.md#Hard limits` (post-history instructions) **MUST** be appended **after** the
history, so it is the last thing read before replying. The raw window **MUST** stay small
(long raw context degrades middle recall); the rolling summary carries older context
cheaply. On overflow, **drop recalled memories first, lorebook second; never drop the voice
law, persona, `USER.md`, or the honesty constraint.**

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

The default stack is **local and needs no key**: an LM Studio model backs the mind, and an
LM Studio embedder (`EMBED_BACKEND=lm_studio`) reuses the same server for memory — one local
process behind both. Embeddings are always local and ownable. A failed backend **MUST**
degrade gracefully (keep talking, log the truth) rather than crash.

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

- §6.1 The room is canonical and its elements are normative: a **small room** with **low warm
  light** (a lamp), a **window with rain**, a **window seat**, and a **single plant**. She
  **MUST NOT** stand in a void or a default grey scene.
- §6.2 The set **MUST** be procedural three.js geometry and shader work — no binary scene assets
  in git. Rain **MUST** respond to the `rain` command (§4): a window-pane streak shader plus
  falling drops outside, and a synthesized rain-noise bed (client-side) whose gain follows
  intensity.
- §6.3 The page chrome carries the dark-sanctuary brand (JetBrains Mono; magenta/cyan/amber
  accents on near-black). The camera is fixed and cinematic — framing her in the room with
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

## §7 — Tools via MCP: the hands

- §7.1 **Four tools, real MCP.** An in-repo MCP server (`yurios/world/tools/server.py`, FastMCP
  over stdio) exposes exactly:

  | tool | args | returns | side effect |
  |---|---|---|---|
  | `set_timer` | `minutes` (0 < m ≤ `TIMER_MAX_MINUTES`), `label?` | `{id, label, seconds, due}` | host schedules the announcement (§7.5) |
  | `play_music` | `action`, `track?`, `volume?` | `{playing, track}` | `music` event to the stage (§4) |
  | `get_weather` | `city?` (default `WEATHER_CITY`) | `{city, temp_c, condition, wind_kmh}` | none |
  | `take_selfie` | `scene?`, `mood?` (template keys; empty = her choice) | `{id, scene, mood, status:"started"}` | host renders off-turn, posts the photo (§7.6) |

  The surface **MUST NOT** grow a shell — the heavy, sandboxed hands are a named later rung
  (§26). With `SELFIE_BACKEND=off` the fourth tool **MUST NOT** be advertised: no hand, not a
  dead one.
- §7.2 **A genuine MCP client.** The brain side **MUST** connect over MCP
  (`yurios/world/tools/client.py`, stdio, spawning `yurios.world.tools.server`), discover tools
  with `list_tools`, and build the §7.4 directive from the discovered schemas. If the SDK or
  server fails, the build **MUST** degrade to tools-off and keep talking; `/api/health` reports
  the truth.
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
- §7.5 **Semantics.** The MCP server is the *contract and audit point* for `set_timer` — it
  validates and records — but the **host** schedules the wake (`yurios/world/tools/timers.py`,
  on the injected clock), because only the host owns her voice; when a timer elapses she
  **MUST** announce it aloud through the ambient seam (§9), queued until deliverable.
  `get_weather` **MUST** be a real HTTP lookup (Open-Meteo, keyless) behind a `WeatherProvider`
  seam with an offline fake. `play_music` drives the browser-side synthesized ambience (§6.2) —
  a generative pad, not a media library; the seam is the point.
- §7.6 **Her camera: `take_selfie`, start-don't-await.** The fourth hand teaches the one lesson
  the others can't: **a slow tool must not sit inside the turn.** A hosted render takes 10–30 s;
  dead air after her lead-in would read as a hang. So the tool follows the *start work, never
  await it* rule: the MCP server validates `scene`/`mood` against the template library (its tool
  description **MUST** be built *from* the library so the model's choices can't drift from the
  yaml) and returns `{status:"started"}` immediately; the turn ends on budget. The **host**
  realises the shot: `yurios/world/selfies.py`'s `SelfieLab` renders off-turn through the forge
  (`yurios/forge` — the locked art register, the selfie template library, provenance stripping),
  saves the PNG + a provenance sidecar under `SELFIE_DIR` (served at `/selfies/`), posts an
  `image_url` `message` to the chat (`proactive`), and offers one spoken line through the ambient
  seam — dropped if she's busy, because the photo itself already landed. Backends are GPU-free by
  construction: `openrouter` (default `bytedance-seed/seedream-4.5`; point `SELFIE_MODEL` at
  `sourceful/riverflow-v2.5-pro` for the brand-art register) or `mock` (deterministic
  placeholders; the tests). A configured `openrouter` with no key **MUST** degrade to `mock`
  with one loud WARNING; a failed render **MUST** become a quiet chat message, never a crash and
  never silence.

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
  off the hot path.
- §9.7 **Emotion → expression.** The model is asked (appended system blocks, voice-only) to
  treat the exchange as *spoken* (no narration, no stage directions, no asterisk actions) and to
  emit inline expression tags from the §3.4 palette. The parser (`yurios/desktop/voice/emotion.py`)
  **MUST** strip tags from the spoken text, emit an expression event when a tag closes (the face
  leads the voice), tolerate split tags, drop unknown tags silently, and also strip
  `*asterisk narration*` (streaming-safe, dropping an unclosed span rather than speaking it).
- §9.8 **The greeting.** On connect she **SHOULD** greet from memory before the user speaks
  (continuity). The greeting **MUST NOT** be persisted and **MUST NOT** pollute the session
  window, and **MUST** fire at most once per session — a reconnect or a second socket **MUST NOT**
  speak a second greeting over the first.
- §9.9 **Warm in the background.** The voice stack (~20 s cold) **MAY** load off-thread so her
  body appears immediately; a connection **MUST** wait for the stack to be ready before its first
  turn rather than answering with a stand-in. The ambient seam: the world voice route (§2.2) registers
  a per-connection injector, so ambient turns run on that connection's `TurnController` and one
  barge-in path kills everything she says, scripted or replied. Ambient speech is a real turn
  *minus the memory* — it appears in the chat flagged `proactive` but never persists (no corpus
  line, no commit); announcements queue until deliverable, missed self-talk is simply dropped.

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
  control up; `session`, `filler`/`audio` (base64 PCM + the sentence text for §5), `done`,
  `cancelled`, `error` down. Turn expressions are re-routed onto the bus (§4), so the face has one
  lane. PCM keeps a websocket because audio is the one flow that is bidirectional, binary, and
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
  one lock. Exposed as `POST /api/chat` (`{text, session_id?, channel}` → `{session_id, message}`),
  which **MUST NOT** wait on the voice warm-up.
- **Outbound** — an `EventHub` subscription. Committed `message` events carry the originating
  `channel`, so an adapter can filter its own echoes. Because the mind's SUGGEST lines and
  undeliverable SPEAKs land as `proactive` messages on the same bus (§18.3), every channel receives
  her initiative for free.

Channels in this build (`yurios/world/channels/`; a failed channel is one degraded medium, never a
down host — `/api/health` and the boot board say which):

- **the terminal** — `python -m yurios.chat`: a remote thin client on `POST /api/chat` +
  `/api/events`. Its SSE attach counts as presence, exactly like an open page.
- **Telegram** — `yurios/world/channels/telegram.py`, raw Bot API long-polling. One configured
  chat only (`TELEGRAM_CHAT_ID`; unset = pairing mode: the bot answers with the id to configure and
  processes nothing). Telegram is *reachable, not present*: it posts no presence signals; selfies
  are sent as the file itself. A channel is on when its credentials are set — no separate enable
  flag.

Planned on the same contract, not yet implemented: **WhatsApp** (webhook transport) and a
**game-engine NPC API** (a WebSocket the engine connects to: player utterances in as text turns
with scene context, `message` events out as dialogue, the same `avatar`/expression events as
animation cues — a game is another frontend + effector set, never a second brain).

## §11 — Config

Typed (`yurios/world/config.py`), read once from env/`.env`, extending the voice config (which
extends the brain's). Every knob in `.env.example` **MUST** have a default and the default stack
**MUST** need no key (`SELFIE_BACKEND=openrouter` without a key degrades to mock — §7.6 — so the
no-key rule survives it). The port is **8768**. The brain knobs (model routes, `LMSTUDIO_BASE_URL`,
the reasoning switches, `EMBED_BACKEND` and its auto-reindex, retrieval and summary budgets, the
Vault dir) are inherited; the body knobs are `COMPANION_NAME`, `TOOLS_BACKEND=mcp|fake|off`, the
tool caps/timeouts/log dir and per-tool rate limits, `TIMER_MAX_MINUTES`,
`WEATHER_BACKEND`/`WEATHER_CITY`, `SELFIE_BACKEND`/`SELFIE_MODEL`/`SELFIE_DIR`, `RAIN_INTENSITY`,
`DESKTOP_BODY`, the channel credentials (§10.5), and the reflex windows (`IDLE_SETTLE_S`,
`IDLE_ACT_MIN/MAX_S`, `IDLE_TALK_MIN/MAX_S`). The mind's knobs are §25.

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
- §20.2 **Retrieval is grounded.** Every returned `Chunk` carries `doc` + `span` (character range) —
  a citation she can show. `search()` joins conversation via the assembler's knowledge slot;
  `forget(selector)` drops a doc off the shelf and out of the index. The index (`knowledge/index/`)
  is derived, gitignored, rebuildable.

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
`IDLE_TALK_MIN/MAX_S` (§15.5). The port is **8768**.

---

## §26 — Omissions (normative)

This is a reference implementation of *initiative*, not the fully productised runtime. **No sandboxed
workshop**: no code execution, no shell, no autonomous research-and-build, no wiki authoring — the
heavy hands remain the named next rung, and §23.2's gate is where their products would cross into the
mind. **No multimodal sensing**: SENSE reads text, time, files, and its own completions — no vision,
no prosody — which is enough to prove an interrupt threshold can stay silent. **The world model stops
at the snapshot**: no temporal knowledge graph, no multi-hop queries (§19.1 names the stage). **One
process**: the mind runs in-process on the host's event loop, not as a supervised per-character OS
process behind a wire protocol — the two-tier host/engine split with a brokered IPC seam is the
productisation rung, and the stores' contracts are already shaped for it. **No affective state file**
— the reflex pulses approximate warmth without a model of it. **No multi-character hosting.**
Conversation is observed by the loop, not generated by it (§15.3) — full one-loop unification lands
with the two-tier split. And the mind never *initiates* tool calls (the four MCP hands stay
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
  bounds, template-key validation); the selfie lab (a started contract becomes a PNG + provenance
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
  budget, dedupe, salience-2.0 indexing, the offline heuristic); goals (roundtrip, dedupe,
  commitment-aware reconsideration, promise extraction incl. negations); the SOUL gate (constitution
  refused even gated and never queued, identity surfaces gate-only, low-risk applies, unknown fails
  safe, approve applies + commits, reject leaves no change, vault jail); the routes (snapshot, journal,
  decision-as-signal consumed on the next tick and journaled, 404, the 503 + health truth when
  mindless); and the boot path (`create_app` over the real brain: mind running, health/boot reporting
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

## §28 — Extends to

The ladder's last rung inside one process; every seam past it is already shaped. The **two-tier
split**: the mind's stores speak narrow contracts over an in-process seam — promoting them to a wire
protocol and the engine to a supervised per-character process is a topology change, not a rewrite, and
it brings the broker (the Guard's grown-up form), the model router's privacy boundary, and true
one-loop conversation (§15.3's named rung) with it. The **workshop**: a sandboxed workspace beside the
Vault where ACT dispatches real work — research, code, builds — to an embedded harness and never awaits
it (the selfie lab's start-don't-await rule, generalised), with §23.2's gated flow as the one door from
work-product to self. The **temporal knowledge graph** behind `WorldModelStore`'s unchanged contract
when "what was true when" starts to bite. And **distribution**: this Vault's SOUL exports as a `.PNG`
character card and boots on someone else's machine, which is the point of the whole ladder — the
companion you own, that you can move by copying a folder.
