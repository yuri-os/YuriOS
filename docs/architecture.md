# Architecture

One project, one process, one origin. The body, the voice loop, the brain, the image service, the
mind and the character host are all first-party packages under `yurios/` — copy the folder,
install, run; nothing points at a sibling build.

```
 python -m yurios.world  (FastAPI on :8768)
 │
 ├── CharacterHost — the registry, the storage tree, the switchboard, per-character routing
 │   └── one isolated runtime per character:
 │
 ├── the reactive body: ToolBrain over the brain ·
 │   the voice loop (/ws/voice) · MCP hands + Guard · SelfieLab · VrmController ·
 │   EventHub → /api/events (SSE) · both bodies · the desktop window
 │
 ├── SignalBus — the inbound inbox: user turns (teed by the voice route) ·
 │   presence (page attach/detach) · landed timers · finished tasks ·
 │   your self-edit decisions → signals.jsonl
 │
 └── MindLoop — SENSE → APPRAISE → DECIDE → ACT → REFLECT → REGULATE
       ├── activity: ENGAGED / IDLE / DORMANT / DREAM + the budget governor
       ├── gate 1 (act) + gate 2 (interrupt): SILENT | SUGGEST | SPEAK
       ├── WorldModelStore — the situation every prompt carries
       ├── KnowledgeStore — drop-folder RAG, citable to doc+span
       ├── DreamConsolidator — episodic → semantic, nightly, resumable
       ├── GoalStore — goals.md, promises extracted from her own replies
       ├── SelfEdit — constitution read-only; persona edits queue for you
       └── Journal + TickTrace → /api/mind + the inner-life tab
```

The control model: **the body is a puppet, the brain holds the strings.** All decisions live in
Python; the browser is a render-and-control client.

## The packages

| Package | What it is |
|---|---|
| `yurios/app` | the brain: the SOUL, the Vault, prompt assembly, memory, the provider seams |
| `yurios/desktop` | the voice stack: STT/TTS/VAD backends, the turn controller, the latency budget, the native window |
| `yurios/forge` | the image service behind her camera (`take_selfie`, `show_picture`): character register, template library, backends, provenance |
| `yurios/world` | the body, the tools, the bus, the server, the channels, the host |
| `yurios/mind` | the autonomy engine |
| `yurios/kernel` | below all of them: the injected clock, the `corr_id`, the one event bus — stdlib only |
| `yurios/characters` | the registry, the card parser/importer, connection profiles |
| `soul-src/` | the SOUL she's seeded from |
| `web/` | the frontends: the VRM stage, the Live2D client, the switchboard, the shared chrome |

## The Vault

Her mind's home is one folder and one git repo — the files *are* the database:

```
vault/
├── soul/               CONSTITUTION.md (immutable) · PERSONA.md (editable, gated) · …
├── memory/episodic/    conversation AND her own acts ([she] lines)
├── memory/semantic/    facts.md, grown by DREAM · forgotten.md, the forget ledger
├── knowledge/reference/  the drop folder (index derived, gitignored)
├── workspace/          her desk — notes, drafts, diary, what DREAM's jobs write
│                         (gitignored: scratch is not history)
├── skills/             one folder per skill, each a SKILL.md she or you wrote
├── world/              situation.md + beliefs.jsonl — her picture of NOW
│                         (+ setting.md — where she is, from her card, yours to edit)
├── goals.md            her to-do list, human-readable
└── state/              activity · budget · pending edits · engine cursor · dream progress
```

Human-readable, greppable, diffable, revertable. Moving her is copying a folder.

Beside the Vault, and deliberately outside it, sit the records *about* her — derived, rotating,
never part of what she is:

```
traces/     ticks.jsonl · activity.jsonl (state transitions) · signals.jsonl
            context.jsonl · prompts.jsonl (every context window she was given)
tool-logs/  calls.jsonl — every call her hands made, allowed or denied
corpus/     turns.jsonl (the trainable log) · ratings.jsonl · utility.jsonl
selfies/    generations.jsonl — how each photo was rendered
```

Every one of these rotates to a single `.1` generation and then goes; an always-on mind writes to
them forever, so a cap is the difference between a log and a leak. All of them live under
`traces/`, `tool-logs/`, `corpus/` and `selfies/`, which are `PRIVATE_SURFACES`
(`characters/privacy.py`) — they never leave with an exported card. `prompts.jsonl` matters most
there: an assembled prompt contains `USER.md` and her recalled memories verbatim.

The four files are written by four different objects, so **one `corr_id` per unit of work**
(`kernel/correlate.py`) is what makes them one story: the tick that decided, the prompt that
phrased it, the call that ran it, and the photo that came back minutes later all carry the same
key. Chat turns are the one deliberate split — `prompts.jsonl` holds an index row pointing at
`corpus/turns.jsonl`, because that is the training asset and `ratings.jsonl` joins to its id.

## Where things live in the code

| Piece | Code |
|---|---|
| **The tick loop** | `mind/loop.py` — `MindLoop.tick()` |
| **The inbound signal bus** | `mind/signals.py` + the `FORK` tee in `world/routes/voice_ws.py` |
| **Activity states + budget** | `mind/policy.py` — `ActivityController` · `mind/budget.py` |
| **The two salience gates** | `mind/policy.py` — `appraise_*`, `score_interrupt` |
| **Gate 2 in action** | `mind/loop.py` — `_act_reach_out` |
| **Where a reach-out waits** | `world/inbox.py` + `world/routes/inbox.py` |
| **…and how it reaches you** | `world/channels/notify.py` → `desktop-shell/main.js` · `notify-send` |
| **The world model** | `mind/world.py` + `world/situation.py` |
| **The world-model seam swap** | `world/brain.py` — `set_world` / `_assemble` |
| **Drop-folder RAG** | `mind/knowledge.py` |
| **DREAM consolidation** | `mind/dream.py` |
| **Goals, promises, commitment** | `mind/goals.py` — `extract_promises`, `reconsider` |
| **The SOUL split** | `mind/selfedit.py` + `mind/vaultio.py` |
| **The journal + trace** | `mind/journal.py`, `mind/trace.py` |
| **Every prompt she was given** | `mind/promptlog.py` → `traces/prompts.jsonl` |
| **What caused what** | `kernel/correlate.py` — one `corr_id` per unit of work |
| **The inner-life surface** | `world/routes/mind.py` + `web/js/mind.js` |
| **The mind debug page** | `world/debug.py` + `web/mind/` (host routes under `…/debug/*`) |
| **The host + registry API** | `world/host/` — `hosting.py` is the host, `app.py` composes the routes |
| **The character registry** | `characters/registry.py`, `characters/models.py` |
| **The card parser / importer** | `characters/card.py`, `characters/importer.py` |
| **The 0.1 → 0.2 migration** | `migrate.py` |
| **The MCP server / client / guard** | `world/tools/server.py`, `client.py`, `guard.py` |
| **The selfie lab** | `world/selfies.py` + `forge/` |
| **The channels seam** | `world/channels/base.py`, `manager.py`, `telegram.py` |
| **Injected time everywhere** | `kernel/clock.py` — `Clock` / `VirtualClock` |

## Seams and fakes

Optional heavy backends are lazy imports behind seams, and every optional runtime seam has a fake.
The local sentence-transformer embedder is a core dependency, so memory works without an embedding
server; model weights still load only when it starts.

That's not politeness — it's what makes the suite runnable. The whole test suite is green on a
machine with no model loaded or CUDA, which is also why a thin install is a *testable* install.

## Running the tests

```bash
pip install -e ".[test]"           # nothing else
pytest
```

The suite runs offline, on fake models, on a `VirtualClock` — so **days of an always-on mind run
in milliseconds**. That's the only way the make-or-break component, the interrupt threshold, ships
tuned rather than vibed. The scenario battery asserts over the tick trace: *the interview was
Tuesday*, *the dark weekend*, *the machine sleeps*, *her own promise*, *a timer is a promise*.

The room has its own suite — the modules that decide something rather than draw it: the activity
ladder's vocabulary, the shapes the API's rows are normalised into, the context gauge's
thresholds, the quality tier a device lands on.

```bash
cd web && npm install && npm test
```

## The check before a commit

```bash
pip install -e ".[dev]"            # ruff, mypy, and the build backend
./scripts/check.sh                 # lint, typecheck, both suites
./scripts/check.sh --fast          # …without pytest (~70s across 8 workers)
./scripts/check.sh --release       # …plus the install smoke test, on Python 3.11
```

There is no CI behind this, on purpose: YuriOS is installed from a checkout onto one machine, and
the thing most worth checking — that the wheel installs and the dependencies still resolve — is
what a hosted runner would have to fake hardest. So the gate runs where the venv is. Every stage
runs even when an earlier one failed, and the summary names what broke.

**The lint** is `ruff check`, and deliberately not `ruff format` — the formatting here is hand-set
and a formatter would rewrite 36k lines to say nothing new. **The typecheck** is `mypy`, set where
the codebase is rather than where a greenfield project would start: the 36 modules written before
it existed are listed in `[tool.mypy]`'s overrides and skipped, the other 132 are checked, and
that list may only shrink. Both are configured in `pyproject.toml`, next to a note on why each
rule is where it is.

## Does it still install?

```bash
./scripts/smoke_install.sh                  # under a minute
./scripts/smoke_install.sh --python 3.11    # the floor in requires-python
./scripts/smoke_install.sh --full           # really install it all; minutes, and gigabytes
```

`pytest` runs against the working tree, so everything between the code and a stranger's machine is
invisible to it — the wheel's contents, the console script, the package data, and above all
whether the declared dependency set still *resolves*. That gap is where this project's worst
failures have lived (litellm's Python ceiling; `mcp` 2.0 renaming the module her tool server is
built on, while `import mcp` kept working), and all of them were silent.

`constraints.txt` records the versions the project was last known to work with — a pip
*constraints* file, so it fixes what gets installed and asks for nothing on its own. Install
against it with `./install.sh --pinned`, and regenerate it with `./scripts/pin_deps.sh` after
changing a dependency. torch is deliberately not pinned there: which build belongs on a machine
(CPU, 747 MB, vs CUDA, 4.5 GB) is that machine's call, and the installer asks it.

## Contracts worth knowing before you change things

- **The mind is a process state, not a callback.** Between turns it exists, ticks and decides.
- **The reactive body must keep working with the mind disabled.** `MIND_ENABLED=false` degrades to
  exactly the reactive companion minus ambient life.
- **APPRAISE must not call a model.** That rule is what makes always-on affordable.
- **One outbound event bus.** Every host→frontend event is typed JSON on it, so a frontend is a
  thin view and any medium is a frontend.
- **A failed turn leaves no trace** — no memory line, no commit.
- **Time is injected.** No wall-clock reads, no bare sleeps, anywhere in the mind.

## Extending it

The seams past this build are already shaped:

- **The two-tier split** — promote the stores' contracts to a wire protocol and the engine to a
  supervised per-character process. That brings the broker (the guard's grown-up form), the model
  router's privacy boundary, and true one-loop conversation with it.
- **The workshop** — a sandboxed workspace beside the Vault where ACT dispatches real work and
  never awaits it (the selfie lab's start-don't-await rule, generalised), with the gated self-edit
  flow as the one door from work-product to self. Distinct from `vault/workspace/`, which is
  already built: that one is *hers* and inert; this one is where code runs, and it lives outside
  the Vault for exactly that reason.
- **The temporal knowledge graph** behind `WorldModelStore`'s unchanged contract.
- **The card studio** — export the Vault's SOUL as a `.PNG` that boots on someone else's machine,
  which is the point of the whole design.

New provider? Write one `ImageBackend` and register it (`forge/backends/__init__.py`). New medium?
Write one `Channel` (`world/channels/base.py`). New model route? It's a LiteLLM prefix.

Normative detail for all of it: [`SPEC.md`](../SPEC.md). Where a subsystem has a history worth
recording, it's in [`PROVENANCE.md`](../PROVENANCE.md).
