# `forge/` — her camera (SPEC §7.6)

The image / selfie service behind `take_selfie`: a swappable-backend `ImageForge`
(→ book ch. 26). First-party YuriOS code, maintained here.

Shipped backends are **`mock`** (needs nothing) and **`openrouter`** (needs a key) —
YuriOS's selfies are GPU-free by construction, because the voice stack already owns
the local compute budget. The local-GPU / hosted-GPU paths (`comfyui`, `replicate`,
`diffusers`), the standalone CLI, and `config.yaml` are intentionally not built in;
the `ImageForge` surface is stable, so a local uncensored diffusion path can be added
as another backend without touching callers.

## Notes on the `openrouter` backend

- The constructor takes an `api_key`; the env fallback also accepts
  `OPENROUTER_API_KEY` (YuriOS's convention — the typed config reads `.env` without
  exporting, so the host injects the key).
- `modalities` handling is general: it asks `["image"]` first and retries once with
  `["image","text"]` on a 404, so any OpenRouter image route works without a
  per-prefix registry (the default here is `bytedance-seed/seedream-4.5`).
- `characters/yuri.yaml` carries no `trigger`/`lora` block — those drive a diffusion
  backend that isn't wired in, and a stray trigger token would pollute the hosted
  prompt.
- `backends/__init__.py` registers `mock` + `openrouter` (see the file header).
