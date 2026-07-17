# YuriOS — SPEC

Normative specification for YuriOS. Keywords **MUST**, **SHOULD**, **MAY** are used in
the RFC-2119 sense.

YuriOS is **one independent project**, maintained here: the body, the room, the voice
loop, the hands, and the one outbound bus are this project's own code (their origin in
the book's reference builds is recorded in `PROVENANCE.md`, not tracked as an external
dependency). Its chassis grew out of the book's Builds #1/#2/#4, and this document is
now the normative text for the whole of it. The section numbering **§1–§14 follows the
inherited body's structure** so every in-file `SPEC §n` comment stays meaningful —
cited as **B4 §n** for the 3D body (design in book ch. 34), **B2 §n** for the voice
loop (ch. 32), **B1 §n** for the brain (ch. 31); each section either carries that
inherited contract forward by citation or states its amendment. The new work — the
mind — is **§15–§25**, with omissions, tests, and the growth path at §26–§28.

---

# Part I — the body (§1–§14): the inherited chassis, amended

## §1 — Goal and properties

A browser-based 3D companion — the VRM body in the canonical sanctuary, the chat beside
her, the real-time voice loop, four MCP tools, one event bus — driven by an
**always-on autonomy engine**. She runs
continuously whether or not anyone is looking: pursues small goals when the user is
away, consolidates memory while they sleep, keeps her own promises, reads what lands on
her shelf, and reaches out *first* when — and only when — a salience model says it is
welcome.

- It **MUST** add **property 3b — initiative**: the always-on half of owned agency
  (→ ch. 03), completing the conjunction Builds #1–#4 assembled (identity, memory,
  hands, body, one-on-one) plus property 6 (yours: every dial, file, and log below is
  on the user's disk).
- The mind **MUST** be an always-running process state, not a callback: between turns
  it exists, ticks, and decides — the opposite default from every request/response
  chat loop (→ ch. 18).
- Everything Build #4 shipped **MUST keep working unchanged**: same wire protocols,
  same frontends, same latency budget (B4 §1), same reactive tool loop. With
  `MIND_ENABLED=false` the build degrades to exactly Build #4 minus the scripted idle
  machine — the mind is additive, never load-bearing for conversation.
- The interrupt model is the make-or-break component and it **MUST** ship conservative:
  a build that pings constantly has failed as surely as one that never speaks (§18).

```
 python -m yurios.world — one process, one origin (:8768)
 ┌────────────────────────────────────────────────────────────────────────────────────┐
 │  the reactive body (§2–§10):                                                       │
 │  the brain · the voice loop · ToolBrain + MCP hands · SelfieLab ·                  │
 │  VrmController → EventHub → /api/events (SSE) · /ws/voice (audio only)             │
 │                          ▲ the same strings                                        │
 │                          │                                                         │
 │  mind/ — THE TICK LOOP (§15):  SENSE → APPRAISE → DECIDE → ACT → REFLECT → REGULATE│
 │     ▲ SignalBus (§16): user turns · timers · presence · drops · decisions          │
 │     ├ activity states ENGAGED/IDLE/DORMANT/DREAM + budget governor (§17)           │
 │     ├ gate 1 salience-to-act · gate 2 salience-to-interrupt (§18)                  │
 │     ├ WorldModelStore (§19) · KnowledgeStore (§20) · DREAM (§21) · goals (§22)     │
 │     ├ SOUL split + gated self-edits (§23)                                          │
 │     └ journal + tick trace → /api/mind + the inner-life panel (§24)                │
 └────────────────────────────────────────────────────────────────────────────────────┘
```

## §2 — The brain and the voice (B4 §2, amended)

B4 §2 applies: the brain lives in `yurios/app`, the voice stack in `yurios/desktop`, the
image service in `yurios/forge`, the SOUL source in `./soul-src` — all first-party
packages of this project; `ToolBrain` subclasses the voice stack's `BrainAdapter` (an
ordinary internal base class, called not copied). Amendments:

- **§2.5 (the situation block) is where the promised seam swap happens.** The block's
  place in the prompt does not move; what fills it does. With the mind running, every
  prompt's `## THE SITUATION RIGHT NOW` is the **`WorldModelStore.situation()`** (§19.2)
  — which still *contains* B4 §2.5's host lines (clock, embodiment truth, scene state,
  timers) via the same `world/situation.py` renderer, now extended with presence, open
  threads, and expectations. Mindless, the brain **MUST** fall back to the bare B4
  rendering.
- The forked voice route gains one more marked fork block, **FORK(B5 §16)**: the signal
  tee (§15.3).

## §3–§7 — The body, the control channel, visemes, the sanctuary, the hands

B4 §3 (the VRM stage), §4 (`avatar` events on the bus; the `VrmController` surface),
§5 (visemes), §6 (the sanctuary scene, the enter gesture and boot log, the desktop
window, the second body), and §7 (the four MCP tools, the guard, the selfie lab) apply
**by citation, unchanged**. Two contractual amendments:

- B4 §4 promised the controller's method surface as "the strings Build #5's tick loop
  will hold" — §15.5 holds it to that.
- **The frontend build.** B4 §3 shipped `three.js` + `three-vrm` as *vendored,
  no-build ES modules* loaded via an importmap from `web/vendor/`. As the public
  YuriOS this is **replaced**: those libraries are now `npm` dependencies pinned in
  `web/package-lock.json` and bundled by **Vite** into `web/dist` (`web/vite.config.js`),
  so they receive upstream security updates via `npm audit` / `npm update` instead of
  going stale in the repo. The server serves `web/dist` at `/` and the large runtime
  binaries (`web/models`) at `/models` (`world/main.py`); the vendored Live2D client
  under `web/live2d/` is unaffected — it remains its own raw-served app. Build step:
  `cd web && npm ci && npm run build`.

## §8 — ~~The idle machine~~ REPLACED (was B4 §8)

`world/idle.py` is **deleted**. Its five states, its dice, and its windows are
superseded by the mind: ambient speech and timer announcements are now *decided* acts
of the tick loop (§15.5), and the body micro-acts survive as REGULATE-owned reflexes
(§15.5) on the same seeded RNG and the same config windows (`IDLE_ACT_*`,
`IDLE_TALK_*`, `IDLE_SETTLE_S`). B4 §8.2 (sim-time discipline), §8.3 (ambient speech
is a real turn, minus the memory) and §8.4 (the per-connection ambient seam) survive
as obligations on the mind, cited from §15.

## §9 — The voice loop, preserved

B4 §9 applies by citation. Barge-in **MUST** cancel the mind's self-initiated speech
exactly as it cancels a reply, because both run through the same per-connection
`TurnController` (§15.5).

## §10 — Topology (B4 §10, amended)

B4 §10 applies: one outbound `EventHub`, one SSE stream, one audio socket. Amendment:
**the inbound mirror B4 deliberately omitted now exists** — the `SignalBus` (§16) is
the inbox the tick loop consumes, and B4 §10's "taken when the tick loop gives them a
consumer" clause is hereby exercised. New outbound event types: `journal` and `mind`
(§24). The attach/detach of `/api/events` subscribers **MUST** post
`user_present`/`user_absent` signals — presence is a signal, not a guess.

### §10.5 — Channels (new)

The frontend rule generalises past this origin's own pages: **a frontend is a
thin view — user input becomes a text turn + a `user_message` signal; output is
rendered off the one `EventHub`; nothing talks to the brain directly.** Two
seams make any medium a frontend:

- **Inbound** — the shared text-turn runner (`world/turns.py`): resolve
  session → transcript + `user_message` signal → the brain's token stream
  (expression tags to the puppet lane, stripped from the shown text, sentences
  as `draft`s) → verbatim persist → `message` commit + `turn_committed` signal.
  It **MUST** mirror the voice route's contract minus the audio, including B2
  §4.4's rule: a failed turn leaves no trace. Text turns from all channels
  serialise on one lock. Exposed over HTTP as `POST /api/chat`
  (`{text, session_id?, channel}` → `{session_id, message}`), which **MUST NOT**
  wait on the voice warm-up.
- **Outbound** — an EventHub subscription. Committed `message` events now carry
  the originating `channel`, so an adapter can filter its own echoes. Because
  the mind's SUGGEST lines and undeliverable SPEAKs land as `proactive`
  messages on the same bus (§18.3), every channel receives her initiative for
  free.

Channels in this build (`world/channels/`, lifecycle beside the server's; a
failed channel is one degraded medium, never a down host — `/api/health` and
the boot panel say which):

- **the terminal** — `python -m yurios.chat`: a remote thin client on
  `POST /api/chat` + `/api/events`. Its SSE attach counts as presence, exactly
  like an open page.
- **Telegram** — `world/channels/telegram.py`, raw Bot API long-polling. One
  configured chat only (`TELEGRAM_CHAT_ID`; unset = pairing mode: the bot
  answers with the id to configure and processes nothing). Telegram is
  *reachable, not present*: it posts no presence signals; selfies are sent as
  the file itself.

Planned on the same contract, not yet implemented: **WhatsApp** (webhook
transport; needs an ingress story) and a **game-engine NPC API** (a WebSocket
the engine connects to: player utterances in as text turns with scene context,
`message` events out as dialogue, the same `avatar`/expression events as
animation cues — a game is another frontend + effector set, never a second
brain).

## §11 — Config (B4 §11, extended by §25)

B4 §11 applies for every inherited knob (the port moves to **8768**). The mind's knobs
are §25.

## §12 — Omissions → superseded by §26

## §13 — Tests → superseded by §27 (every inherited B4 §13 obligation still holds and
still runs; the suite grows, it never shrinks)

## §14 — Extends to → superseded by §28

---

# Part II — the mind (§15–§25)

## §15 — The cognitive tick loop

`mind/loop.py`. The engine runs **SENSE → APPRAISE → DECIDE → ACT → REFLECT →
REGULATE**, forever, as one asyncio task on the server's loop, replacing the idle
machine as the caller of every host surface.

- §15.1 **Three normative rules.** (1) **One intention per tick**: DECIDE commits to
  exactly one act or to resting — the majority of all ticks **MUST** end in REST; an
  agent that does one thing per heartbeat reads like a diary and cannot fan out.
  (2) **APPRAISE is cheap by construction**: pure heuristics (`mind/policy.py`),
  runnable every tick, **MUST NOT** call a model — the model is invoked only inside
  ACT, for work already judged worth it. (3) **Everything is journaled** (§24.1) and
  **traced** (§24.2), and every tick that changed the Vault ends in **exactly one git
  commit** (`tick <id>: <intention>`); an uneventful tick commits nothing, and that is
  not an error. Time is **injected** (`world/clock.py`): no wall-clock reads, no bare
  sleeps, anywhere in the mind — this is the entire test story (§27).
- §15.2 **The mind's home is the same Vault.** No second database: the mind reads and
  writes the Build #1 Vault the brain already keeps, adding `world/` (§19),
  `knowledge/` (§20), `goals.md` (§22), and `state/` (activity, budget, engine
  snapshot, pending edits, dream progress). All writes go through `mind/vaultio.py`'s
  `MindVault` — atomic, vault-jailed, constitution-refusing (§23.1).
- §15.3 **Where conversation lives.** The reply itself stays on Build #2's turn
  pipeline — the voice socket's sub-second reactive path, which no tick cadence may
  ever sit in front of. The loop is that path's *observer and consequence*: a
  `user_message` signal **MUST** preempt the activity state to ENGAGED from any state,
  mid-sleep if necessary (the bus wake), and a committed exchange arrives as a
  `turn_committed` signal whose REFLECT share is the world-model update and the
  promise scan (§22.1). One mind at two cadences: the loop owns everything between
  turns; the turn pipeline stays the ENGAGED fast path. (The full one-loop
  unification — the reply generated *by* ACT — is a named next rung, §28.)
- §15.4 **Rehydration and the suspend gap.** The engine's cursor state
  (`state/engine.json`: bus offset, interrupt counts, consideration cooldowns, last
  tick) **MUST** survive restart — a rebooted mind resumes, it does not wake amnesiac.
  A real gap since the last tick (> 2 h, or twice the DORMANT cadence) **MUST**
  synthesize one `suspend_gap` signal: one catch-up appraisal over the whole gap —
  goals reconsidered by commitment (§22.2), one journal line — never a pile of stale
  reactions, and never thirty good-mornings.
- §15.5 **The strings, held.** ACT reaches the world only through surfaces the host
  already owned in Build #4: ambient speech through `Runtime.speak_ambient` (the same
  per-connection `TurnController` — barge-in-able, latency-masked, never persisted to
  memory, `proactive` in the chat), chat lines through `post_message`, the body
  through `VrmController`, the countdowns through the `TimerBoard` (whose landed
  timers now arrive as `timer` signals; an announcement is a promise and **MUST**
  queue until deliverable, B4 §8.3's rule kept verbatim). The **self-talk murmur**
  survives as a decided impulse: only in IDLE, only with the user present, only after
  the configured quiet window — and dropped, never queued, when nobody can hear. The
  **body reflexes** (gaze drift, expression pulse, posture, rain-gazing at the scene's
  canonical window target) survive as REGULATE-owned reflexes: no model, no journal,
  seeded RNG, silent while engaged, while the room is empty, and in DORMANT/DREAM.

## §16 — The signal bus (inbound)

`mind/signals.py`. Everything that happens *to* her is one typed, timestamped
`Signal`, appended to one inbox and drained by SENSE by offset. Producers post facts;
the loop decides what they mean — no producer may call into the mind.

- §16.1 Posting **MUST** be safe from the event loop or a worker thread, **MUST** wake
  the loop early from any cadence sleep, and **MUST** append one line per arrival to
  `signals.jsonl` (the arrival record — "what woke her at 3am" is a file you read).
- §16.2 The type enum is open: `user_message`, `turn_committed`, `user_present`,
  `user_absent`, `timer`, `task_completion`, `selfedit_decision`, `wakeup`,
  `fs_event`, `suspend_gap`. Producers in this build: the forked voice route (the
  tee), the `/api/events` route (presence), the timer board, the self-edit API.
  Unknown types are legal and appraise low. `user_present`/`user_absent` are
  bookkeeping — observed by the world model, never chosen as intentions (the greeting
  is the voice route's job).

## §17 — Activity states and the budget governor

`mind/policy.py` (`ActivityController`), `mind/budget.py`. Cost and thermal control as
a design driver: an always-on mind is affordable only because it is almost always
nearly asleep.

- §17.1 **Four states govern cadence:** ENGAGED (talking; short ticks) · IDLE (user
  recently around; goal work) · DORMANT (long quiet; resting) · DREAM (consolidation,
  entered from DORMANT inside a configured local-time window, chunked ticks).
  Everything but the preempt is a slow drift *down* the cost ladder on configured
  timeouts. The state **MUST** persist (`state/activity.json`) and resume across
  restarts.
- §17.2 **The preempt overrides everything:** a user turn pulls the loop to ENGAGED
  from any state. Nothing else moves up the ladder.
- §17.3 **The budget governor** holds estimated tokens spent today against a daily cap
  (`MIND_DAILY_TOKENS`), debited by every utility call and every line the mind
  composes; at pressure ≥ 1.0 REGULATE **MUST** shed IDLE to DORMANT (goal work
  stops). It **MUST NOT** gate conversation — a governor that silences her when the
  user speaks has failed at its one job. The ledger (`state/budget.json`) rolls at
  local midnight on the injected clock and is rendered by the dashboard.
- REGULATE **MAY** shorten the next heartbeat below the state cadence when a goal
  comes due sooner or when more than one appraisal crossed gate 1 this tick (the
  backlog drains one intention at a time, never piles into one tick).

## §18 — The salience and interrupt model

`mind/policy.py`. The make-or-break component: **two distinct thresholds**, and
collapsing them is precisely Clippy.

- §18.1 **Gate 1 — salience-to-act** runs every tick, over every sensed signal and
  every open goal (with a per-goal reconsideration cooldown), plus the standing
  impulses (a pending announcement, a new document, DREAM backlog, the murmur). Pure
  heuristics: a base score per signal type — nothing outranks the person speaking —
  plus a surprise bonus from violated expectations (§19.3); goals score on priority,
  due-ness, and commitment. Below `MIND_ACT_THRESHOLD` the tick RESTs, and most do.
- §18.2 **Gate 2 — salience-to-interrupt** is scored only when a `reach_out` goal has
  already crossed gate 1, from named factors the trace records verbatim: relevance,
  time-sensitivity, hours since she last reached out (contact license), inferred
  availability by hour, and a welcome term that decays with each interruption today.
  Two rules are **hard gates, not weights**: quiet hours (roughly 22:00–09:00) are
  SILENT regardless of score, and `MIND_MAX_INTERRUPTS_PER_DAY` zeroes the score
  outright. Both dials are the **user's** (§25) — you cannot tune the dial against
  someone who holds it.
- §18.3 **Outcomes, ascending imposition:** **SILENT** — the default: do it quietly
  and journal it (a stale non-blind goal is let go with a journal line; the journal,
  not notifications, carries the value); **SUGGEST** — one composed line posted to the
  chat, waiting for the user's next glance, never spoken aloud; **SPEAK** — aloud
  through the ambient seam if a page is open (full turn pipeline, barge-in-able), as a
  `proactive` chat line if the room is empty. Every delivery **MUST** bump the daily
  count, note the contact in the world model, and close the goal.

## §19 — The world model (the present tense)

`mind/world.py` — the `WorldModelStore`, the organ B4 §2.5 was a rendering of
(→ ch. 19; D-022). SENSE writes it, APPRAISE scores against it, DECIDE plans over it,
and every prompt is built from it.

- §19.1 **Beliefs, not facts.** Every entry is a time-stamped, confidence-tagged
  belief in an append-only log (`world/beliefs.jsonl`); `query(q, at=…)` answers
  "what was believed when" (the snapshot stage of point-in-time; the temporal graph is
  the sanctioned later stage and out of scope, §26). Structured now-state
  (`world/state.json`): presence, last contact each way, open threads, expectations.
- §19.2 **`situation()` — the stage every prompt carries.** It **MUST** compose the
  B4 §2.5 host lines (the injected clock's time, the **embodiment truth** verbatim,
  the room's sticky scene state, pending timers — still rendered by
  `world/situation.py`) with what only a store can know: whether the user is here,
  how long they've been away (minutes/hours/days phrasing), what's in progress, what
  she half-expects. It **MUST** be written to `vault/world/situation.md` whenever it
  changes — her picture of *now* is a file you can `cat` — and it feeds the brain via
  `ToolBrain.set_world` (§2.5's seam swap).
- §19.3 **Expectation and surprise.** `expect(text, due, keys)` stores a checkable
  belief about what comes next. A later observation that matches its keys resolves it
  quietly; one that finds it past due produces **prediction-error = surprise**, which
  **MUST** feed APPRAISE as a salience bonus — the cheapest good salience signal
  there is.

## §20 — The knowledge layer (drop-folder RAG)

`mind/knowledge.py` — the `KnowledgeStore`, a **sibling of memory, never folded in**
(→ ch. 16; D-019). The boundary is enforced by shape: **knowledge cites a document +
span; memory cites a conversation turn** — separate files, separate indexes, separate
`inspect()`.

- §20.1 **Drop and she reads it.** Files (`.md`/`.txt`) landing in
  `vault/knowledge/reference/` **MUST** be noticed by SENSE (a cheap size+mtime scan,
  no signal required), ingested as an ACT — chunked by paragraph budget, each chunk
  situated with a short blurb (utility model; doc-name fallback offline), embedded,
  and hybrid-indexed (vector similarity blended with keyword idf) — and journaled
  ("read and shelved …"). Re-ingest replaces a doc's chunks, never duplicates. A doc
  that fails to ingest (no embedder backend, a mangled file) is marked seen with one
  loud WARNING and retried only when the file changes — a broken shelf item **MUST
  NOT** become a retry loop.
- §20.2 **Retrieval is grounded.** Every returned `Chunk` carries `doc` + `span`
  (character range) — a citation she can show. `search()` joins conversation via the
  assembler's knowledge slot; `forget(selector)` drops a doc off the shelf and out of
  the index. The index (`knowledge/index/`) is derived, gitignored, rebuildable.

## §21 — DREAM consolidation

`mind/dream.py` — Build #1's `consolidate()` stub, finally implemented: she wakes
changed by yesterday.

- Runs **only** in the DREAM activity state (§17.1). Each DREAM tick chews what
  `MIND_DREAM_TICK_TOKENS` allows: finished days of the episodic journal — **never
  today's live file** — are summarised to at most a few durable facts (utility model;
  an offline heuristic keeps the pass alive with no model), deduped against
  `memory/semantic/facts.md`, appended there with their source day, and indexed at
  **salience 2.0** so recall prefers the distilled fact over the raw exchange.
- **Oldest-first and resumable** (`state/dream_progress.json`): a night that runs out
  of budget leaves a backlog, not an overrun, and the next DREAM tick resumes. The
  night's work is journaled ("slept on it: folded … into what I keep").

## §22 — Goals and intentions

`mind/goals.py` — `vault/goals.md` is the store: a human-readable markdown checklist,
because what an agent intends to do should be a file her user can open. Each goal
carries kind, priority, optional due time, **provenance**, and a **commitment
strategy**; lifecycle `pending → active → waiting → done | abandoned`.

- §22.1 **Goal genesis is designed, not assumed.** Sources, stamped as provenance:
  the user's explicit asks (`user:remind-me`, scanned from their turns); **her own
  promises** (`promise:her-own-words`) — REFLECT scans every committed reply for
  first-person commitments ("I'll look into that") and files each as a `reach_out`
  goal with a due time, because a companion who forgets her own promises is worse
  than one who forgets yours; and maintenance (DREAM backlog, shelf drops).
  Near-duplicate open goals **MUST** merge, not multiply.
- §22.2 **Commitment governs staleness** (→ ch. 18): `blind` is defended past due
  (a birthday is a birthday), `single-minded` drops only when moot, `open-minded`
  is abandoned the moment it stops being timely. The suspend-gap catch-up (§15.4)
  applies these in one pass.

## §23 — The SOUL split and gated self-edits

`mind/selfedit.py`, `mind/vaultio.py` (→ ch. 14; D-002). Who she is, immutably; who
she's becoming, reviewably.

- §23.1 **The constitution is read-only, even to her.** Every mind write path goes
  through `MindVault`, which refuses `soul/CONSTITUTION.md` unconditionally — and the
  self-edit flow refuses even to *queue* a proposal against it. The other
  `soul/*.md` identity surfaces require the gate token only the self-edit flow holds;
  a store or a stray ACT cannot quietly become who she is. Paths **MUST** be jailed
  to the Vault.
- §23.2 **Risk-gated proposals.** `propose(surface, content, reason)`: low risk
  (memory, world, knowledge, goals — working products) applies immediately and
  commits; high risk (any `soul/` surface, and every unknown surface — fail safe) is
  **queued** in `state/pending_edits.json` with its full content and reason, rendered
  by the inner-life panel with approve/reject. The decision returns as a
  `selfedit_decision` signal the loop consumes (§16.2) — applied edits are git
  commits, so drift is never silent and `git revert` undoes any of it; the ruling
  itself is journaled ("you applied/rejected my edit to …").

## §24 — The journal, the trace, and the inner-life surface

The product half of autonomy: what converts an always-on process from creepy to *an
inner life* (→ ch. 18).

- §24.1 **The journal.** Her autonomous acts write into the **same episodic day files
  as the conversation** (`memory/episodic/YYYY-MM-DD.md`), as `### HH:MM  [she] …`
  lines — one journal, two authors, one DREAM pass over both. Each line is indexed
  into memory (she can recall her own past acts) and published as a `journal` event
  on the bus. SILENT outcomes journal; ambient murmurs do not (B4 §8.3's
  never-persist rule holds for them).
- §24.2 **The tick trace** (`traces/ticks.jsonl`): one structured record per tick —
  sensed, appraised (with scores), decided (with runners-up), acted, and the full
  interrupt decision with its factors. The scenario tests (§27.2) are queries over
  this file; the "why did she…" answer is always in it.
- §24.3 **The surface.** `GET /api/mind` (state, cadence, budget, goals, shelf,
  pending edits), `GET /api/mind/journal?days=` (her `[she]` lines by day),
  `GET /api/mind/trace?n=`, `POST /api/mind/edits/{id}` (`{"approve": bool}` → a
  signal, §23.2). The browser page's chat column grows a second tab — **inner life**
  (`web/js/mind.js`): right-now state and budget, edits waiting on you (with content
  and one-click approve/reject), goals with provenance, the shelf, and the journal,
  refreshed live off the same one bus (`journal`/`mind` events). Everything reads
  *through* the mind's own stores; the dashboard can never disagree with the files.

## §25 — Config (the mind's knobs)

Extends B4 §11 (`world/config.py`); every knob has a default and the default stack
still needs no key. `MIND_ENABLED` (off = Build #4 minus ambient life);
`MIND_SEED`; the two dials `MIND_ACT_THRESHOLD` / `MIND_INTERRUPT_THRESHOLD` and the
hard cap `MIND_MAX_INTERRUPTS_PER_DAY`; `MIND_CONSIDER_COOLDOWN_S`;
`MIND_DAILY_TOKENS`, `MIND_DREAM_TICK_TOKENS`; the cadences and drift timeouts
`MIND_{ENGAGED,IDLE,DORMANT,DREAM}_CADENCE_S`, `MIND_ENGAGED_TIMEOUT_S`,
`MIND_IDLE_TIMEOUT_S`, `MIND_DREAM_START_HOUR`/`END_HOUR`; and the inherited reflex
windows `IDLE_SETTLE_S`, `IDLE_ACT_MIN/MAX_S`, `IDLE_TALK_MIN/MAX_S` (§15.5). The
port is **8768**.

---

## §26 — Omissions (normative)

This is a reference implementation of *initiative*, not the productised runtime. **No
sandboxed workshop**: no code execution, no shell, no autonomous research-and-build,
no wiki authoring — the heavy hands remain the named next rung (→ ch. 17 "the heavy
hands", ch. 19), and §23.2's gate is where their products would cross into the mind.
**No multimodal sensing**: SENSE reads text, time, files, and its own completions — no
vision, no prosody (→ ch. 18, ch. 24) — which is enough to prove an interrupt
threshold can stay silent. **The world model stops at the snapshot**: no temporal
knowledge graph, no multi-hop queries (§19.1 names the stage). **One process**: the
mind runs in-process on the host's event loop, not as a supervised per-character OS
process behind a wire protocol — the two-tier host/engine split with a brokered IPC
seam is the productisation rung, and the stores' contracts are already shaped for it.
**No affective state file** (→ ch. 18's mood/temperament split) — the reflex pulses
approximate warmth without a model of it. **No multi-character hosting.** Conversation
is observed by the loop, not generated by it (§15.3) — full one-loop unification lands
with the two-tier split. And the mind never *initiates* tool calls (the four MCP hands
stay conversational); a tool-bearing autonomous act needs the broker that comes with
the workshop.

## §27 — Tests (the hard gate)

`pytest` **MUST** ship and be green from the build root, entirely offline. Everything
B4 §13 pins still runs (minus the deleted idle machine's suite, whose surviving
obligations moved into the loop's). New, all on `VirtualClock` + the real vendored
brain with fake models:

- §27.1 **Mechanics:** one intention per tick and the trace shape; REST majority over
  a quiet simulated day; the ENGAGED preempt from any state; one git commit per dirty
  tick and none for a resting tick; the murmur needing company, quiet, and IDLE — and
  never appraising into an empty room; goal work silent and journaled; the budget
  debited by her own words; activity drift down the ladder, the DREAM window, budget
  pressure shedding IDLE, and restart-resume; gate-1 ordering (nothing outranks the
  person speaking) and the surprise bonus; gate-2 quiet hours as a gate, the hard
  daily cap, the shown factors; the world model (B4's situation assertions verbatim,
  presence arithmetic, expectation met vs. violated, `query(at=…)`); knowledge
  (drop→scan→ingest→cited search, re-ingest replaces, forget, the memory boundary,
  the failed-ingest degrade); DREAM (backlog excludes today, oldest-first resumable
  budget, dedupe, salience-2.0 indexing, the offline heuristic); goals (roundtrip,
  dedupe, commitment-aware reconsideration, promise extraction incl. negations); the
  SOUL gate (constitution refused even gated and never queued, identity surfaces
  gate-only, low-risk applies, unknown fails safe, approve applies + commits, reject
  leaves no change, vault jail); the routes (snapshot, journal, decision-as-signal
  consumed on the next tick and journaled, 404, the 503 + health truth when mindless);
  and the boot path (`create_app` over the real brain: mind running, health/boot
  reporting it, the §19.2 seam actually wired).
- §27.2 **The scenario battery** — multi-day sim-user runs asserted over the tick
  trace, because "it felt right when I watched it for an evening" is not a gate:
  **the interview was Tuesday** (told Monday; user leaves; exactly one reach-out,
  inside the right window Tuesday, visible SILENT restraint before it, factors in the
  trace, nothing spoken into the empty room); **the dark weekend** (user gone 60 h:
  not one message, but DREAM consolidated Monday into facts, DORMANT visible, REST
  majority, the journal carrying the night's work); **the machine sleeps** (a 10-hour
  power-off: one suspend-gap catch-up, journaled, not re-sensed); **her own promise**
  ("I'll sleep on cat names" → a `reach_out` goal with promise provenance and a due
  time, journaled as made); **a timer is a promise** (announce queues while nobody can
  hear, delivers when a page attaches).

## §28 — Extends to

The ladder's last rung inside one process; every seam past it is already shaped. The
**two-tier split** (→ ch. 19): the mind's stores speak narrow contracts over an
in-process seam — promoting them to a wire protocol and the engine to a supervised
per-character process is a topology change, not a rewrite, and it brings the broker
(the Guard's grown-up form), the model router's privacy boundary, and true one-loop
conversation (§15.3's named rung) with it. The **workshop** (→ ch. 16, ch. 17): a
sandboxed workspace beside the Vault where ACT dispatches real work — research, code,
builds — to an embedded harness and never awaits it (the selfie lab's
start-don't-await rule, generalised), with §23.2's gated flow as the one door from
work-product to self. The **temporal knowledge graph** behind `WorldModelStore`'s
unchanged contract when "what was true when" starts to bite. And **distribution**:
this Vault's SOUL is what Build #3's card studio exports — the mind that grew here
ships as a `.PNG` and boots on someone else's machine, which is the point of the
whole ladder (→ ch. 03, property 6).
