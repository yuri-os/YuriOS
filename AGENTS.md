# YuriOS Agent Guide

## Specification

- `SPEC.md` is the normative specification: RFC-2119 language, cited from code as `SPEC §n` comments (hundreds of them). When changing specified behavior, update `SPEC.md` in the same change; when code and spec disagree, the spec is authoritative by convention — treat the mismatch as a bug.
- Section numbers are stable and never renumbered — code, tests, and scripts cite them, so only ever append or revise within a section.
- A bare `SPEC §n` means *this* spec and must resolve — `tests/test_spec_citations.py` fails on a citation to a section that doesn't exist, or to one SPEC.md marks superseded. A predecessor build's spec carries its prefix instead (`B1`/`B2`/`B4`, per `PROVENANCE.md`'s map of which package came from which build).
- `docs/spec-map.md` is the inverse index — section → the code that implements it — generated from those citations by `python scripts/spec_map.py`. Use it to go from a section to its modules; the file's own docstring goes the other way. Regenerate it (and `--check` it) after moving code between modules.
- `docs/` is the plain-language companion; `SPEC.md` wins on any disagreement. Don't treat `docs/` as a source of truth for behavior.

## Toolchain and verification

- Support Python `>=3.11,<3.14`; Python 3.12 is the installer target. Use the project interpreter when present: `.venv/bin/python`.
- Run the offline suite with `.venv/bin/python -m pytest -q -n 8` — 1,668 tests in ~70s across eight workers, ~7m40s serial, so pass `-n 8`. Focus a change with `.venv/bin/python -m pytest -q tests/test_file.py::test_name`, and drop `-n` when you need a readable traceback (xdist interleaves eight workers' output). Don't raise the worker count much past 8: each worker pays a one-off ~24s importing torch, and `-n auto` measured slower than `-n 8` on a 20-core box.
- Tests deliberately replace dotenv loading and use fake voice, tools, image, model, and clock seams. Do not require a configured model, API key, GPU, or live service for test coverage.
- `./scripts/check.sh` is the gate: `ruff check`, `mypy`, pytest, and the web suite, each stage running even when an earlier one failed. Use `--fast` to skip pytest while iterating, `--release` to add the install smoke test. It needs `pip install -e ".[dev]"`. There is no CI; this is the whole gate.
- Lint and typecheck are configured in `pyproject.toml` and are green — keep them that way. Ruff is `check` only, never `format` (the formatting is hand-set). Mypy skips the 36 modules listed in its overrides, which predate it: that list may only shrink, and new modules are checked from the start.
- For `install.sh`, run `bash -n install.sh` (and `shellcheck install.sh` when available).
- The browser app is a separate Vite build. After changing `web/`, run `(cd web && npm ci && npm run build)`; FastAPI serves the ignored `web/dist/` output at `/`. Use `(cd web && npm run dev)` only for Vite development.
- `web/tests/` holds the frontend suite (`npm test`, vitest). Scope it to modules that decide something, not to the three.js room: a test that mocks WebGL asserts only that the mock was called.
- After changing a dependency in `pyproject.toml`, regenerate the pins with `./scripts/pin_deps.sh` and run `./scripts/smoke_install.sh` — the resolve check is the only thing that sees a version ceiling breaking, and pytest never will.

## Runtime and configuration

- `yurios` is the console entry point and defaults to starting the daemon. Invoke it from the project root: it treats the current directory as the installation and reads its `.env`, `.yurios/`, and `.venv`.
- Use `yurios start --foreground` for attached server logs; `yurios status`, `stop`, `restart`, and `log` manage the background daemon. `yurios configure` saves the house model choice to `.env`; restart before the daemon uses it.
- `yurios start` launches the supervisor (`yurios/daemon.py`), which owns `.yurios/yurios.pid` as a held lock, restarts the server when it dies (with backoff and a crash-loop stop), and records `.yurios/last-exit.json`. Treat the lock as the only answer to "is she running" — never a bare pid check.
- `python -m yurios.world` is the server entry point. Normal startup migrates legacy single-character state into `DATA_DIR` before creating the character host.
- The `.env` house configuration is read at boot. Per-character model and loop overrides live in `DATA_DIR/characters.json` and may be applied live by the host; do not conflate those two configuration paths.
- `.env`, `.yurios/`, `data/`, `vault/`, `models/`, `traces/`, `corpus/`, `tool-logs/`, and Vite output are ignored personal/generated state. Do not add them to commits or use them as fixtures.

## Architecture constraints

- `yurios/world` hosts FastAPI, character routing, event/channel plumbing, MCP tools, and the voice-facing runtime; `yurios/mind` owns autonomous ticks; `yurios/app` owns the SOUL, Vault, memory, and model providers; `yurios/characters` owns registry and card import/export; `yurios/forge` owns image backends; `yurios/desktop` owns STT/TTS/VAD and native-window support.
- `yurios/kernel` sits below all of them and holds the three primitives they share — the injected clock, the `corr_id`, the `EventHub`. It imports the standard library and nothing from `yurios`; `tests/test_layering.py` enforces that. Put something there only if every layer above could need it, and never let it grow a dependency on a package that imports it.
- The same test refuses a new import cycle. A function-local import is not an escape from one — it makes a cycle work at runtime while hiding it, which is how the two in `KNOWN_CYCLES` survived. That list follows the mypy overrides' rule: it may only shrink, and the test fails both on a new cycle and on a listed one that has been fixed but not struck off.
- Optional heavyweight backends are lazy, replaceable seams with fakes. Preserve that property: avoid importing model, voice, camera, or GPU dependencies at module import time, and retain a testable fake/degraded path when changing a seam.
- What a brain is, is written down: `world/brain_protocol.py` names `ConversationalBrain` (a room's needs) and `AutonomousBrain` (a mind's, on top). Check the shape with `isinstance` against those rather than `hasattr` on one method, and when you add or rename a seam, change the Protocol in the same edit — `tests/test_brain_protocol.py` compares signatures as well as names, which is what stops a fake drifting silently.
- There are two `/ws/voice` routes — `world/routes/voice_ws.py` (the browser) and `desktop/routes/voice_ws.py` (the native window) — and they differ on purpose. The wire under them does not: the connection cap, the hello exchange, the size ceilings and the STT session are `desktop/voice/ws_session.py`, and `desktop/voice/ws_limits.py` holds the numbers. Change a wire invariant there, not in a route; `tests/test_voice_handshake.py` runs every one of them against both apps.
- The mind relies on injected time. Do not add direct wall-clock reads or bare sleeps in `yurios/mind`; use the runtime clock so VirtualClock tests remain deterministic.
- The browser is a thin client: host-to-client updates use the shared SSE `EventHub`. Add cross-surface state as typed events rather than a frontend-specific polling path.

## Commits

- Commit subjects use scope prefixes (`mind:`, `world:`, `forge:`, `studio:`, …) — match that style. No AI-attribution trailers.
