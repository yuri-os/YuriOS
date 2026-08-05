# The mind

The reactive companion — voice, chat, tools, a body — exists only while you're talking to her.
The **mind** is what runs the rest of the time: a loop that ticks whether or not anyone is
looking, pursues small goals, keeps the promises she made, reads what lands on her shelf,
consolidates memory while you sleep, and now and then decides to reach out first.

It's additive. `MIND_ENABLED=false` gives you the reactive companion minus ambient life, and
conversation never depends on it. Timer expiries currently use the mind loop for delivery, so
enable the mind and select a model if they must be announced or queued.

Normative detail: [`SPEC.md` §15–§25](../SPEC.md).

## The tick loop

Every heartbeat runs six phases:

| Phase | What happens |
|---|---|
| **SENSE** | drain the signal inbox, scan the shelf, fold everything into the world model |
| **APPRAISE** | score everything with cheap heuristics — *never* a model |
| **DECIDE** | commit to exactly one act, or to resting |
| **ACT** | reach the world only through surfaces the host already owns |
| **REFLECT** | journal it; scan her own reply for promises |
| **REGULATE** | drift the activity state, debit the budget, commit the Vault if anything changed |

Three rules hold it together:

1. **One intention per tick.** Most ticks end in REST, and that's the design. An agent that does
   one thing per heartbeat can be read like a diary — and is.
2. **APPRAISE never calls a model.** That single rule is what makes always-on affordable; the
   model is invoked inside ACT, for work already judged worth it.
3. **Everything is journaled and traced**, and every tick that changed the Vault ends in exactly
   one git commit. An uneventful tick commits nothing.

Time is injected everywhere — no wall-clock reads, no bare sleeps — which is why days of an
always-on mind can run in milliseconds in the test suite.

### Conversation stays on the fast path

The reply pipeline (ears → brain → voice, with barge-in and the latency budget) is untouched by
all this. No tick cadence sits in front of it. The loop is its *observer and consequence*: a user
turn preempts the activity state to ENGAGED from any state, and the committed exchange comes back
as a signal whose REFLECT share is the world-model update and the promise scan. One mind, two
cadences.

## Signals

Everything that happens *to* her is one typed, timestamped signal appended to one inbox:
`user_message`, `turn_committed`, `user_present`, `user_absent`, `timer`, `task_completion`,
`selfedit_decision`, `wakeup`, `fs_event`, `suspend_gap`. Producers post facts; the loop decides
what they mean.

Each arrival appends a line to `signals.jsonl` — "what woke her at 3 a.m." is a file you read.

## Activity states

An always-on mind is affordable only because it's almost always nearly asleep.

| State | When | Cadence |
|---|---|---|
| **ENGAGED** | talking | `MIND_ENGAGED_CADENCE_S` (10 s) |
| **IDLE** | you were recently around — goal work happens here | `MIND_IDLE_CADENCE_S` (60 s) |
| **DORMANT** | long quiet | `MIND_DORMANT_CADENCE_S` (900 s) |
| **DREAM** | consolidation, entered from DORMANT inside a local-time window | `MIND_DREAM_CADENCE_S` (120 s, chunked) |

Everything except the preempt is a slow drift *down* the cost ladder on configured timeouts
(`MIND_ENGAGED_TIMEOUT_S`, `MIND_IDLE_TIMEOUT_S`). A user turn is the only thing that moves up it,
and it does so from any state, mid-sleep if necessary. The state persists and resumes across
restarts.

**Rehydration.** Her cursor state survives a restart — a rebooted mind resumes rather than waking
amnesiac. A real gap since the last tick (a machine that was off for ten hours) synthesizes one
`suspend_gap` signal: one catch-up appraisal over the whole gap, one journal line. Never a pile of
stale reactions, and never thirty good-mornings.

## The budget governor

```ini
MIND_DAILY_TOKENS=200000
```

Estimated tokens spent today are held against a daily cap, debited by every utility call and every
line the mind composes. At pressure ≥ 1.0, REGULATE sheds IDLE to DORMANT and goal work stops.

It **never** gates conversation. A governor that silences her when you speak has failed at its one
job. The ledger rolls at local midnight and is rendered in the inner-life tab.

## The two gates

This is the make-or-break component, and collapsing the two thresholds is precisely the
always-interrupting-assistant failure.

**Gate 1 — salience to act** runs every tick over every sensed signal and every open goal, plus
the standing impulses (a pending announcement, a new document, DREAM backlog, the murmur). Pure
heuristics: a base score per signal type — nothing outranks the person speaking — plus a surprise
bonus from violated expectations. Below `MIND_ACT_THRESHOLD` (0.4) the tick rests, and most do.

**Gate 2 — salience to interrupt** is scored *only* when a reach-out goal has already crossed gate
1. It's built from named factors the trace records verbatim: relevance, time-sensitivity, hours
since she last reached out, inferred availability by hour, and a welcome term that decays with
each interruption today.

Two things are **hard gates, not weights**:

- quiet hours (roughly 22:00–09:00) are silent regardless of score, and
- `MIND_MAX_INTERRUPTS_PER_DAY` (3) zeroes the score outright.

Three outcomes, in ascending imposition:

| Outcome | What it does |
|---|---|
| **SILENT** *(the default)* | do it quietly and journal it. The journal, not notifications, carries the value |
| **SUGGEST** | one composed line posted to the chat, waiting for your next glance — never spoken |
| **SPEAK** | aloud through the ambient seam if a page is open; a `proactive` chat line if the room is empty |

Both dials are yours, in `.env`. You cannot tune the dial against someone who holds it.

## The world model

Her picture of *now*. Every entry is a time-stamped, confidence-tagged **belief** in an append-only
log, so "what was believed when" is answerable. Structured now-state carries presence, last
contact each way, open threads and expectations.

`situation()` composes the host's lines (the time, the embodiment truth, the room's scene state,
pending timers) with what only a store can know: whether you're here, how long you've been away,
what's in progress, what she half-expects. It's written to `vault/world/situation.md` whenever it
changes — her picture of now is a file you can `cat` — and it's what every prompt carries.

**Expectation and surprise:** she can store a checkable belief about what comes next. An
observation that matches resolves it quietly; one that finds it past due produces prediction
error, and that surprise feeds APPRAISE as a salience bonus — the cheapest good salience signal
there is.

## The shelf (drop-folder RAG)

Drop a `.md` or `.txt` file into her Vault's `knowledge/reference/`:

```bash
cp notes.md data/characters/yuri/vault/knowledge/reference/
```

Within a heartbeat she notices it (a cheap size+mtime scan), ingests it — chunked by paragraph
budget, each chunk situated with a short blurb, embedded, and hybrid-indexed (vector similarity
blended with keyword idf) — and journals "read and shelved …". Re-ingesting a changed file
replaces its chunks rather than duplicating them.

Retrieval is **grounded**: every returned chunk carries its document and a character span, so she
can cite what she's telling you.

Knowledge is a sibling of memory, never folded into it, and the boundary is enforced by shape:
**knowledge cites a document; memory cites a conversation turn.** The book you dropped in is
knowledge; "you told me you play bass" is memory. A document she read never becomes something she
believes about *you*.

A doc that fails to ingest (no embedder, a mangled file) is marked seen with one loud warning and
retried only when the file changes — a broken shelf item never becomes a retry loop. The index is
derived, gitignored and rebuildable.

## DREAM

She wakes changed by yesterday.

In the DREAM state — entered from DORMANT inside `MIND_DREAM_START_HOUR`..`MIND_DREAM_END_HOUR`
(02:00–06:00 by default) — each tick chews what `MIND_DREAM_TICK_TOKENS` allows: **finished** days
of the episodic journal (never today's live file) are summarised to at most a few durable facts,
deduped against `memory/semantic/facts.md`, appended there with their source day, and indexed at
higher salience so recall prefers the distilled fact over the raw exchange.

It's oldest-first and resumable: a night that runs out of budget leaves a backlog, not an overrun,
and the next DREAM tick picks up where it stopped. The night's work is journaled — "slept on it:
folded … into what I keep".

## Goals and promises

`vault/goals.md` is the store, and it's a human-readable markdown checklist on purpose: what an
agent intends to do should be a file you can open.

```bash
cat data/characters/yuri/vault/goals.md
```

Each goal carries a kind, a priority, an optional due time, its **provenance**, and a **commitment
strategy**, and moves `pending → active → waiting → done | abandoned`.

Goals come from three designed sources:

| Provenance | Where it came from |
|---|---|
| `user:remind-me` | your explicit asks, scanned from your turns |
| `promise:her-own-words` | **her own promises** — REFLECT scans every committed reply for first-person commitments ("I'll look into that") and files each as a reach-out goal with a due time |
| maintenance | DREAM backlog, shelf drops |

A companion who forgets her own promises is worse than one who forgets yours. Near-duplicate open
goals merge rather than multiply.

**Commitment governs staleness.** `blind` is defended past due (a birthday is a birthday);
`single-minded` drops only when it's moot; `open-minded` is abandoned the moment it stops being
timely.

## Self-edits

Who she is, immutably; who she's becoming, reviewably.

`soul/CONSTITUTION.md` is **read-only, even to her** — every mind write goes through a vault layer
that refuses it unconditionally, and the self-edit flow won't even queue a proposal against it.

Everything else is risk-gated:

- **Low risk** (memory, world, knowledge, goals — her working products) applies immediately and
  commits.
- **High risk** (any `soul/` surface, and every *unknown* surface — fail safe) is **queued** with
  its full content and reason, and rendered in the inner-life tab with approve/reject.

Your decision comes back as a signal the loop consumes on its next tick. Applied edits are git
commits, so drift is never silent and `git revert` undoes any of it. Your ruling is journaled too
— "you applied/rejected my edit to …".

## The journal is the product

Her acts write into the **same episodic day files as the conversation**, as `[she]` lines — one
journal, two authors, one DREAM pass over both. Each line is indexed into memory (she can recall
her own past acts) and published live on the event bus.

"What did you do while I was gone?" is a page you open, not a vibe.

```bash
tail -f data/characters/yuri/traces/ticks.jsonl        # one record per heartbeat
git -C data/characters/yuri/vault log                  # one commit per tick that changed anything
```

The **tick trace** is the why-record behind the journal: sensed, appraised (with scores), decided
(with runners-up), acted, and every interrupt decision with its factors. The scenario tests are
queries over this file, and the answer to "why did she…" is always in it.

## The inner-life tab

The chat column's second tab, refreshed live off the same event bus:

- her activity state and heartbeat,
- today's token budget,
- the goals on her mind, with where each came from,
- the shelf,
- edits waiting on your approval, with content and one-click approve/reject,
- and the journal.

Everything reads *through* the mind's own stores — the dashboard can never disagree with the
files. The same data is available at `GET /api/mind`, `/api/mind/journal`, `/api/mind/trace`
([API](api.md#the-mind)).

## The knobs

```ini
MIND_ENABLED=true                 # off = the reactive body, minus ambient life
MIND_SEED=0                       # 0 = unseeded; tests pin a seed
MIND_ACT_THRESHOLD=0.4            # gate 1
MIND_INTERRUPT_THRESHOLD=0.75     # gate 2 — YOUR dial
MIND_MAX_INTERRUPTS_PER_DAY=3     # the hard daily cap
MIND_CONSIDER_COOLDOWN_S=3600     # minimum gap between re-chewing one goal
MIND_DAILY_TOKENS=200000
MIND_DREAM_TICK_TOKENS=40000
MIND_TRACE_MAX_BYTES=2000000       # rotate traces/ticks.jsonl to ticks.jsonl.1 at this size
MIND_ENGAGED_CADENCE_S=10
MIND_IDLE_CADENCE_S=60
MIND_DORMANT_CADENCE_S=900
MIND_DREAM_CADENCE_S=120
MIND_ENGAGED_TIMEOUT_S=180        # quiet this long → IDLE
MIND_IDLE_TIMEOUT_S=3600          # away this long → DORMANT
MIND_DREAM_START_HOUR=2
MIND_DREAM_END_HOUR=6
```

Body reflexes and the murmur keep the old idle machine's windows:

```ini
IDLE_SETTLE_S=20                  # quiet after a turn before ambient life resumes
IDLE_ACT_MIN_S=8                  # reflex window: gaze drift, expression pulse, posture
IDLE_ACT_MAX_S=25
IDLE_TALK_MIN_S=120               # the self-talk impulse: one short in-character line
IDLE_TALK_MAX_S=300               #   after this much quiet
```

The murmur only happens in IDLE, only with you present, only after the quiet window — and it's
dropped rather than queued when nobody can hear. Body reflexes run on a seeded RNG with no model
and no journal, and stay silent while she's engaged, while the room is empty, and in
DORMANT/DREAM.

Per-character overrides: the `mind`, `utility` and `dream` switches live in the registry, so one
companion can be fully autonomous while another stays reactive-only —
see [Characters](characters.md#loop-switches).

## What the mind deliberately doesn't do

- **No tool calls.** Her hands stay conversational; a tool-bearing autonomous act needs a
  broker that comes with the sandboxed workshop.
- **No code execution, no shell, no autonomous research-and-build.**
- **No multimodal sensing** — SENSE reads text, time, files and its own completions.
- **No temporal knowledge graph** — the world model stops at the snapshot.
- **No affective state model** — the reflex pulses approximate warmth without modelling it.

These are named omissions, not gaps waiting to be discovered ([`SPEC.md` §26](../SPEC.md)).
