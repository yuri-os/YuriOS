# YuriOS Agent Guide

## Toolchain and verification

- Support Python `>=3.11,<3.14`; Python 3.12 is the installer target. Use the project interpreter when present: `.venv/bin/python`.
- Run the offline suite with `.venv/bin/python -m pytest -q`. Focus a change with `.venv/bin/python -m pytest -q tests/test_file.py::test_name`.
- Tests deliberately replace dotenv loading and use fake voice, tools, image, model, and clock seams. Do not require a configured model, API key, GPU, or live service for test coverage.
- There is no configured lint, formatter, or type-check task. For Python changes, run the relevant pytest scope; for `install.sh`, run `bash -n install.sh` (and `shellcheck install.sh` when available).
- The browser app is a separate Vite build. After changing `web/`, run `(cd web && npm ci && npm run build)`; FastAPI serves the ignored `web/dist/` output at `/`. Use `(cd web && npm run dev)` only for Vite development.

## Runtime and configuration

- `yurios` is the console entry point and defaults to starting the daemon. Invoke it from the project root: it treats the current directory as the installation and reads its `.env`, `.yurios/`, and `.venv`.
- Use `yurios start --foreground` for attached server logs; `yurios status`, `stop`, `restart`, and `log` manage the background daemon. `yurios configure` saves the house model choice to `.env`; restart before the daemon uses it.
- `yurios start` launches the supervisor (`yurios/daemon.py`), which owns `.yurios/yurios.pid` as a held lock, restarts the server when it dies (with backoff and a crash-loop stop), and records `.yurios/last-exit.json`. Treat the lock as the only answer to "is she running" — never a bare pid check.
- `python -m yurios.world` is the server entry point. Normal startup migrates legacy single-character state into `DATA_DIR` before creating the character host.
- The `.env` house configuration is read at boot. Per-character model and loop overrides live in `DATA_DIR/characters.json` and may be applied live by the host; do not conflate those two configuration paths.
- `.env`, `.yurios/`, `data/`, `vault/`, `models/`, `traces/`, `corpus/`, `tool-logs/`, and Vite output are ignored personal/generated state. Do not add them to commits or use them as fixtures.

## Architecture constraints

- `yurios/world` hosts FastAPI, character routing, event/channel plumbing, MCP tools, and the voice-facing runtime; `yurios/mind` owns autonomous ticks; `yurios/app` owns the SOUL, Vault, memory, and model providers; `yurios/characters` owns registry and card import/export; `yurios/forge` owns image backends; `yurios/desktop` owns STT/TTS/VAD and native-window support.
- Optional heavyweight backends are lazy, replaceable seams with fakes. Preserve that property: avoid importing model, voice, camera, or GPU dependencies at module import time, and retain a testable fake/degraded path when changing a seam.
- The mind relies on injected time. Do not add direct wall-clock reads or bare sleeps in `yurios/mind`; use the runtime clock so VirtualClock tests remain deterministic.
- The browser is a thin client: host-to-client updates use the shared SSE `EventHub`. Add cross-surface state as typed events rather than a frontend-specific polling path.
