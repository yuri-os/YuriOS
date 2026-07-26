"""YuriOS — an always-on, local-first agentic companion runtime."""
from __future__ import annotations

import os

from dotenv import dotenv_values

# Two of our libraries reach the network on nothing but an `import`, and both read
# their off switch from the ENVIRONMENT at that moment — before any typed config
# object exists, so neither can be a Config field (§11). Local-first is a promise
# about what leaves the machine, so the defaults are set here, in the package every
# entry point imports before it imports them:
#
#   LITELLM_LOCAL_MODEL_COST_MAP — `import litellm` GETs a 1.67 MB price map from
#     raw.githubusercontent.com on EVERY process start. Nothing here reads it (the
#     forge takes `cost` from OpenRouter's own response, the context meter counts
#     tokens), litellm ships the same file in the wheel, and it already falls back
#     to that copy when the fetch fails. The cost of pinning local: those prices
#     age with the pinned litellm version.
#   HF_HUB_DISABLE_TELEMETRY — Hugging Face downloads (kokoro, faster-whisper; once
#     per model — a cached load makes no request at all, and silero-vad ships its
#     .onnx in the wheel) carry a user-agent reporting your torch build and which
#     AI harness is driving the machine. This drops both from it.
#
# `setdefault` keeps the precedence honest: a real environment variable wins, then
# `.env` — the same file the typed config reads, read the same way, without
# exporting the rest of it — then these.
_LOCAL_FIRST_DEFAULTS = {
    "LITELLM_LOCAL_MODEL_COST_MAP": "True",
    "HF_HUB_DISABLE_TELEMETRY": "1",
}

_dotenv = dotenv_values(".env")
for _key, _default in _LOCAL_FIRST_DEFAULTS.items():
    os.environ.setdefault(_key, _dotenv.get(_key) or _default)
