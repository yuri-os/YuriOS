# Provenance

YuriOS is one independent, self-contained project, maintained in this repository.
Its subsystems have a history worth recording, but that history is **provenance, not
a dependency**: nothing here is a vendored copy to be re-synced from somewhere else,
and every file is owned and maintained here.

## Where the code came from

YuriOS grew out of the reference implementations in the book (*Building Agentic
Waifus*), assembled and then carried forward as first-party code:

- **`yurios/app/`** — the brain: prompt assembly, the file-backed MemoryStore, the
  training corpus log, one-commit-per-turn vault git, and the model-provider seams.
  Originated as **Build #1 (the Minimum Viable Waifu)**, book ch. 31.
- **`yurios/desktop/`** — the real-time voice loop: STT/TTS/VAD seams and backends,
  the turn spine with barge-in-as-cancel, the debounced SpeechGate, filler masking,
  emotion-tag parsing, latency tracing. Originated as **Build #2 (the Desktop
  Companion)**, book ch. 32.
- **`yurios/forge/`** — her camera / selfie service (`ImageForge`). A slice of the
  book's image-forge, book ch. 26. See `yurios/forge/README.md`.
- **`yurios/world/`** + **`web/`** — the VRM body, the sanctuary scene, the MCP
  tools, and the one outbound event bus. Originated as **Build #4 (the 3D World
  Companion)**, book ch. 34.
- **`web/live2d/`** — the Live2D second body, book ch. 32/34. See
  `web/live2d/README.md`. (Its Cubism runtime is genuinely third-party and fetched,
  not committed — that is the one vendored piece; `python scripts/fetch_live2d.py`.)
- **`soul-src/`** — the SOUL source the Vault is seeded from.

`world/brain.py` subclasses `desktop/brain.py`'s `BrainAdapter` to add the tool loop;
`world/routes/voice_ws.py` is a documented fork of `desktop/routes/voice_ws.py` (every
divergence marked `FORK`). These are ordinary internal-reuse relationships between
packages of one project — call the base class, mark the fork — not cross-repo vendoring.

## What the mind (`yurios/mind/`) added on top of the Build #4 body

The autonomy engine is the work that turned the reactive 3D companion into an
always-on one. For the record, its arrival:

**Deleted** (replaced by the mind):

- `world/idle.py`, `tests/test_idle_machine.py` — the scripted idle machine. The tick
  loop (`mind/loop.py`) holds the same strings; its body micro-acts survive as
  REGULATE reflexes, its announce/self-talk as decided acts (SPEC §15.5).

**Modified** (each change small and purposeful):

- `world/main.py` — the `Runtime` builds a `SignalBus` + `MindLoop` instead of the
  `IdleMachine`; the boot board's `idle` service becomes `mind`; the mind router is
  included.
- `world/routes/voice_ws.py` — one more marked fork block, `FORK(B5 §16)`: the signal
  tee (`user_message`, `turn_committed`).
- `world/routes/events.py` — presence signals on subscriber attach/detach (SPEC §16.2).
- `world/routes/health.py` — reports `mind` + `activity` instead of `idle`.
- `world/brain.py` — `set_world()`: the situation block is filled by the
  `WorldModelStore` when the mind runs (the situation-seam swap, SPEC §19.2).
- `world/situation.py` — demoted (docstring only) from "the world model" to the world
  model's host-lines renderer; the rendering itself is unchanged.
- `world/clock.py`, `world/tools/timers.py`, `world/boot.py` — re-aimed at the mind;
  behaviour unchanged (timers' due queue is now drained by the loop's SENSE).
- `world/config.py`, `.env.example` — the `IDLE_ENABLED`/`IDLE_SEED` knobs give way to
  the `MIND_*` family (SPEC §25); the reflex windows (`IDLE_*_S`) survive; port 8768.
- `web/index.html`, `web/sanctuary.css` — the chat column gains the **inner life** tab
  (SPEC §24.3); `web/js/mind.js` is new.
- `scripts/demo_avatar.py` — `MIND_ENABLED=false`.

**Added**: `mind/` (the autonomy engine), `world/routes/mind.py`, `web/js/mind.js`, and
the `tests/test_mind_*`, `test_policy`, `test_world_model`, `test_knowledge`,
`test_dream`, `test_selfedit` suites.
