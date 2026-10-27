# `forge/` — her camera (SPEC §7.6)

The image / selfie service behind `take_selfie`: a swappable-backend `ImageForge`
(→ book ch. 26). First-party YuriOS code, maintained here.

Shipped backends are **`mock`** (needs nothing), **`openrouter`** (needs a key), and three
local cameras that load a single-file checkpoint in-process on your own GPU:
**`diffusers`** (an SDXL UNet), **`krea2`** (a Krea 2 diffusion transformer, kept in
INT4 and never dequantized) and **`anima`** (an Anima / Cosmos diffusion transformer). The default pair stays GPU-free by construction, because the
voice stack already owns the local compute budget; the local pair is opt-in by extra
(`.[forge-local]` / `.[forge-krea2]`) and by a user-supplied checkpoint that is never
shipped. Which of them a given checkpoint needs is read off its safetensors header
(`backends/sniff.py`), so `SELFIE_BACKEND=diffusers` names *the local camera*, not one
architecture.

The hosted-GPU paths (`comfyui`, `replicate`), the standalone CLI and `config.yaml` are
intentionally not built in; the `ImageForge` surface is stable, so another provider is one
`ImageBackend` and one registry line.

User-facing docs: [`docs/selfies.md`](../../docs/selfies.md).

## Notes on the `openrouter` backend

- The constructor takes an `api_key`; the env fallback also accepts
  `OPENROUTER_API_KEY` (YuriOS's convention — the typed config reads `.env` without
  exporting, so the host injects the key).
- `modalities` handling is general: it asks `["image"]` first and retries once with
  `["image","text"]` on a 404, so any OpenRouter image route works without a
  per-prefix registry (the default here is `bytedance-seed/seedream-4.5`).
- Requests carry YuriOS's OpenRouter app-attribution headers (`yurios/attribution.py`,
  SPEC §3) under the `image-gen` category, so the selfie spend lands on the same app
  page as the chat path instead of nowhere.
- `characters/yuri.yaml` carries no `trigger`/`lora` block. The `Character` schema still
  reads one, but no shipped backend applies it — the local cameras render from the base
  checkpoint only — and a stray trigger token would pollute the hosted prompt.
- `prompts.py` is how the camera words a picture: prefix, postfix and negatives for every
  model, one architecture or one checkpoint, layered shipped < house < her (SPEC §7.6).
- `backends/__init__.py` registers `mock`, `openrouter`, `diffusers`, `krea2` and `anima` (see the
  file header).
