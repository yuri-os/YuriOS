# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## The agent guide

`AGENTS.md` is the shared guide for every coding agent in this repo — the spec's authority, the
toolchain, the verification gate, runtime and configuration, and the commit convention. It is
imported here so it loads with this file; read it as part of this one, and put new agent-facing
rules there rather than duplicating them below.

@AGENTS.md

## Orientation

Everything below is the map, not the rules. `SPEC.md` is normative; `docs/architecture.md` is the
long-form version of this section.

### One process, one origin

`python -m yurios.world` is a single FastAPI server on `:8768`. It hosts a `CharacterHost` — the
registry and per-character routing — and under it one isolated runtime per character: the reactive
body (`ToolBrain`, the voice loop at `/ws/voice`, MCP hands behind a `Guard`, the selfie lab, the
`EventHub` feeding `/api/events`), a `SignalBus` inbox, and a `MindLoop`. The browser is a
render-and-control client: all decisions live in Python.

The mind's cycle is SENSE → APPRAISE → DECIDE → ACT → REFLECT → REGULATE, with activity states
(`ENGAGED` / `IDLE` / `DORMANT` / `DREAM`), a budget governor, two salience gates, and DREAM
consolidation.

### Package boundaries

| Package | What it owns |
|---|---|
| `yurios/app` | the brain: SOUL, Vault, prompt assembly, memory, provider seams |
| `yurios/world` | FastAPI, the host, character routing, event/channel plumbing, MCP tools, voice-facing runtime |
| `yurios/mind` | the autonomy engine — the tick loop and everything it drives |
| `yurios/kernel` | below everything: injected clock, `corr_id`, `EventHub` — stdlib only, enforced by a test |
| `yurios/characters` | registry, card parse/import/export, connection profiles, privacy |
| `yurios/forge` | image backends behind her camera |
| `yurios/desktop` | STT/TTS/VAD and the native window |
| `web/` | the frontends (VRM stage, Live2D client, switchboard, shared chrome) — separate Vite build |
| `soul-src/` | the SOUL a fresh Vault is seeded from |

Top-level `yurios/` modules are the entry points and one-offs: `cli.py` (the `yurios` console
script), `daemon.py` (the supervisor), `migrate.py` (0.1 → 0.2), `doctor.py`, `pairing.py`.

### State on disk

The Vault (`vault/`) is her mind's home: one folder, one git repo, the files *are* the database —
`soul/`, `memory/episodic/`, `memory/semantic/`, `knowledge/reference/`, `workspace/`, `skills/`,
`world/`, `goals.md`, `state/`. Moving her is copying a folder.

Records *about* her sit deliberately outside it and rotate to a single `.1` generation:
`traces/` (ticks, activity, signals, context, prompts), `tool-logs/calls.jsonl`, `corpus/`,
`selfies/`. These are `PRIVATE_SURFACES` (`characters/privacy.py`) and never leave with an
exported card. Four different objects write them, so **one `corr_id` per unit of work**
(`kernel/correlate.py`) is what joins the tick, the prompt, the call and the photo into one story.

### Contracts to preserve

- **The mind is a process state, not a callback** — between turns it exists, ticks and decides.
- **The reactive body must keep working with the mind disabled.** `MIND_ENABLED=false` degrades to
  the reactive companion minus ambient life, and nothing else.
- **APPRAISE must not call a model.** That rule is what makes always-on affordable.
- **One outbound event bus** — every host→frontend update is typed JSON on the SSE `EventHub`, so
  any medium is a thin view. Add cross-surface state as an event, not a frontend-specific poll.
- **A failed turn leaves no trace** — no memory line, no commit.
- **Time is injected** (`kernel/clock.py`) — no wall-clock reads or bare sleeps in `yurios/mind`,
  so the `VirtualClock` scenario battery stays deterministic.

### Extension points

A new image provider is one `ImageBackend` registered in `forge/backends/__init__.py`. A new medium
is one `Channel` (`world/channels/base.py`). A new model route is a LiteLLM prefix. Optional heavy
backends stay lazy imports behind seams with fakes — that property is what keeps the suite runnable
with no model, no GPU and no API key.
