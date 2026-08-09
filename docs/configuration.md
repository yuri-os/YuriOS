# Configuration

Every knob lives in `.env` at the project root, is typed
(`yurios/world/config.py` → `yurios/desktop/config.py` → `yurios/app/config.py`), and is read
**once at boot**. Copy `.env.example` — it is the annotated version of this page — and change what
you need:

```bash
cp .env.example .env
```

Every knob has a default, and **the default stack needs no cloud key**. After local model weights
have downloaded and cached, it can run offline; a fresh install still needs network access for
first-use Hugging Face and model downloads.

Config is read once, so a change takes effect on the next restart. The settings panel says so out
loud after a save rather than pretending to hot-apply.

A knob here is the **house default**: every character inherits it unless her own record overrides
that field. Her brain settings are the exception to the restart rule — see
[her own brain](#her-own-brain) below.

## The settings panel

The gear in every room opens a form over these same `.env` keys. One schema drives the form *and*
validates the POST, so the two can never disagree; values are read from the live config, so you
always see the effective setting; and writes are surgical — only changed fields, upserted line by
line, so the comments in your `.env` survive.

It is **loopback-only**, because it hands the browser the keys it renders. A field the running
build has no knob for is dropped rather than shown dead, and a key that differs per companion (her
Telegram pair) resolves to *hers*.

Groups: Brain · Embeddings · Storage · Server · Speech-to-text · Text-to-speech · Turn-taking ·
The loop · Channels · Desktop window.

## Her own brain

Above the `.env` groups, the same dialog carries a panel that is **hers**: chat model, utility
model, endpoint, the environment variable her API key is read from, the two reasoning switches,
temperature, the reply cap and the context window. Leave a field empty and she inherits the house's
— the placeholder tells you what that is.

The difference that matters: **these apply immediately**. Save while you are talking to her and the
next thing she says comes from the new model, in the same session, with the same memory and mind.
Nothing restarts, nothing is lost. (A local model that isn't loaded yet is pinned in the
background, so that first turn may be slow.)

The values live in her registry record (`data/characters.json`), not in `.env`, which is what lets
two companions on one node run two different models. The key itself is never stored — only the
name of the variable holding it. The same fields are on the switchboard's profile drawer.

## Brain

| Key | Default | |
|---|---|---|
| `CHAT_MODEL` | `NONE` | her reply voice; select a model through onboarding or `yurios configure` |
| `UTILITY_MODEL` | `NONE` | summaries, extraction, DREAM — the configurator normally sets it to the selected chat model |
| `LMSTUDIO_BASE_URL` | `http://localhost:1234/v1` | any OpenAI-compatible endpoint |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | routes `ollama/…` and lists your pulled models |
| `OPENROUTER_API_KEY` | *(empty)* | needed for `openrouter/…` routes and hosted selfies |
| `LMSTUDIO_PRELOAD` | `true` | load + pin her models at boot; stops LM Studio's JIT eviction thrash |
| `LMSTUDIO_LOAD_TIMEOUT_S` | `600` | only has to cover a cold load off disk |
| `CONTEXT_LENGTH` | `32768` in `.env.example`, `0` in code | her context window in tokens; `0` = the provider's default |
| `CHAT_THINKING` | `false` | the reply's `<think>` pass — off, so voice stays real-time |
| `UTILITY_THINKING` | `true` | on: it runs off the hot path, where quality beats latency |
| `UTILITY_MAX_TOKENS` | `2048` | room for the `<think>` block *and* the JSON answer |
| `MAX_REPLY_TOKENS` | `1600` | a roomy ceiling, not a target |
| `TEMPERATURE` | `0.9` | |

### Direct GGUF

| Key | Default | |
|---|---|---|
| `GGUF_FALLBACK` | `true` | use llama.cpp for an unavailable `lm_studio/…` route |
| `GGUF_REPO` | *(empty)* | override the Hugging Face repository for an `lm_studio/…` fallback or a serving-model id |
| `GGUF_QUANT` | `Q4_K_M` | matching GGUF filename suffix |
| `GGUF_CACHE_DIR` | `./models` | Hugging Face cache directory for direct GGUF files |
| `GGUF_CONTEXT_LENGTH` | `0` | inherit `CONTEXT_LENGTH`; llama.cpp otherwise uses 8192 |
| `GGUF_N_GPU_LAYERS` | `0` | CPU-first; `-1` uses all layers with a CUDA or Metal build |
| `GGUF_N_THREADS` | `0` | let llama.cpp choose automatically |
| `GGUF_FLASH_ATTN` | `true` | efficient KV cache; required for Gemma 4 at a long context |

Full explanation: [Models & connections](models.md).

## Memory & embeddings

| Key | Default | |
|---|---|---|
| `EMBED_BACKEND` | `sentence_tf` | `sentence_tf` (in-process) · `lm_studio` · `ollama`; OpenRouter embeddings are not supported |
| `EMBED_MODEL` | `BAAI/bge-small-en-v1.5` | must match the backend |
| `EMBED_DIM` | `384` | must equal the model's vector width |
| `RAW_WINDOW_TURNS` | `6` | raw messages kept in-prompt |
| `RETRIEVAL_K` | `6` | recalled memories injected |
| `RETRIEVAL_MIN_SIM` | `0.25` | drop below this cosine similarity |
| `HALF_LIFE_DAYS` | `30` | recency-decay half-life |
| `SUMMARY_EVERY_N` | `8` | summarise cadence, in turns |
| `SUMMARY_BUDGET_TOKENS` | `300` | |
| `LOREBOOK_BUDGET_TOKENS` | `400` | |
| `KNOWLEDGE_K` | `3` | shelf chunks injected per turn — `0` turns the slot off |
| `KNOWLEDGE_MIN_SCORE` | `0.05` | hybrid-score floor, below which a chunk is noise |
| `KNOWLEDGE_BUDGET_TOKENS` | `900` | ceiling on the knowledge block |
| `SYSTEM_BUDGET_TOKENS` | `8000` | overflow ceiling for the system block |

`RETRIEVAL_K` and `KNOWLEDGE_K` are the two retrieval slots, and they are separate on purpose:
memory cites a conversation turn, knowledge cites a document and a character span. Everything
that reaches the shelf goes through the second one — a file you dropped in
`vault/knowledge/reference/`, a page she read with `read_page`, a `research` run — so raising
`KNOWLEDGE_K` widens all three at once. Keep it small: a chunk is a paragraph, so three of them
already outweigh every recalled memory put together, and on overflow knowledge is the first
thing dropped after the example voice.

Changing the backend, model or dimension re-indexes the Vault from its `.md` files automatically.
See [Models & connections](models.md#switching-embedding-backends) for compatible LM Studio and
Ollama settings.

## Storage

| Key | Default | |
|---|---|---|
| `DATA_DIR` | `./data` | the host's character tree — see [Characters](characters.md#where-a-character-lives) |
| `VAULT_DIR` | `./vault` | the legacy/seed Vault; point at an older Vault to continue that companion |
| `SOUL_SRC` | `./soul-src` | the SOUL used to seed a fresh Vault |
| `CORPUS_DIR` | `./corpus` | the raw conversation log — outside the Vault, append-only |
| `TRACE_DIR` | `./traces` | tick traces, latency traces, context history |
| `TOOL_LOG_DIR` | `./tool-logs` | the tool audit |
| `SELFIE_DIR` | `./selfies` | her photos + the provenance ledger |
| `USER_NAME` | `you` | the `{{user}}` substitution |
| `UTILITY_ENABLED` | `true` | the off-hot-path model work |
| `DREAM_ENABLED` | `true` | nightly consolidation |

Under the 0.2 host these per-character paths are set from the registry; the values above are the
defaults a single-companion install and the migration start from.

## Server

| Key | Default | |
|---|---|---|
| `HOST` | `127.0.0.1` | keeps her local-only. The settings panel refuses non-loopback callers regardless |
| `PORT` | `8768` | chosen to dodge the local-AI stack's defaults |

## Voice

| Key | Default | |
|---|---|---|
| `STT_BACKEND` | `faster_whisper` | `faster_whisper` · `fake` |
| `STT_MODEL` | `base.en` | `tiny.en` … `large-v3` |
| `STT_COMPUTE` | `int8` | CTranslate2 quantization |
| `TTS_BACKEND` | `kokoro` | `kokoro` · `qwen3_tts` · `gpt_sovits` · `fake` |
| `TTS_REGISTER` | `default` | `default` · `late_night` · `expressive`, or a kokoro voice id |
| `TTS_SAMPLE_RATE` | `24000` | |
| `VAD_BACKEND` | `silero` | `silero` · `fake` |
| `VAD_THRESHOLD` | `0.5` | speech-probability gate |
| `VAD_MIN_SILENCE_MS` | `250` | endpointing dead air |
| `VAD_ONSET_FRAMES` | `3` | frames that confirm a new turn |
| `VAD_BARGEIN_FRAMES` | `5` | frames that confirm an interruption |
| `VAD_CONFIRM` | `true` | require confirmed speech in an endpointed utterance |
| `FRAME_MS` | `32` | audio frame size |
| `MASK_LATENCY` | `true` | play a filler while the model spins up |
| `EXPRESSION_DEFAULT` | `neutral` | her resting face |
| `VOICE_PRELOAD` | `false` | warm the stack at boot instead of on unmute/listen |
| `VOICE_UNLOAD_AFTER_S` | `60` | empty-room grace before her voice is freed; `0` at once, `-1` never |

Backend-specific keys (`QWEN_*`, `SOVITS_*`) are in [Voice](voice.md).

## Tools

| Key | Default | |
|---|---|---|
| `TOOLS_BACKEND` | `mcp` | `mcp` · `fake` · `off` |
| `TOOL_MAX_CALLS_PER_TURN` | `2` | |
| `TOOL_TIMEOUT_S` | `10` | |
| `TOOL_RATE_TIMER` / `_MUSIC` / `_SELFIE` / `_PICTURE` | `6` / `6` / `2` / `2` | calls per minute |
| `TIMER_MAX_MINUTES` | `180` | |
| `MCP_SERVERS` | *(unset)* | path to `mcp-servers.json` — third-party servers |
| `TOOL_RATE_EXTERNAL` | `4` | default bucket for a tool found by discovery |

See [Tools](tools.md).

## The web

| Key | Default | |
|---|---|---|
| `SEARCH_BACKEND` | `off` | `searxng` · `fake` · `off` — off leaves all three unadvertised |
| `SEARXNG_URL` | `http://localhost:8080` | a loopback URL is the container YuriOS manages; anything else is yours |
| `SEARCH_RESULTS` | `5` | rows per `web_search` |
| `SEARCH_LANGUAGE` | `en` | |
| `SEARCH_SAFESEARCH` | `1` | `0` none · `1` moderate · `2` strict |
| `FETCH_TIMEOUT_S` | `8` | one page, kept inside `TOOL_TIMEOUT_S` |
| `FETCH_MAX_BYTES` | `2000000` | `read_page` stops here |
| `RESEARCH_MAX_PAGES` | `5` | ceiling on `research(depth=…)` |
| `TOOL_RATE_SEARCH` / `_READ` / `_RESEARCH` | `6` / `6` / `2` | calls per minute |

`SEARCH_BACKEND` is the one knob on this page that can turn into a bill: `research` keeps reading
long after it has answered, and `MIND_DAILY_TOKENS` governs the loop's choices rather than capping
spend. See [what it costs](README.md#experimental--and-it-can-spend) before turning it on against a
metered API.

Don't set these by hand for a fresh install — `./install.sh --web-search` pulls and configures the
SearXNG container, then writes `SEARCH_BACKEND` and `SEARXNG_URL` for you. `yurios start` brings the
container up with her; `yurios doctor` says whether she can actually search right now.

See [Tools](tools.md) — including the SearXNG JSON-format trap, which is the one thing that
reliably goes wrong when you point her at an instance you already run.

## Selfies

| Key | Default | |
|---|---|---|
| `SELFIE_BACKEND` | `openrouter` | `openrouter` · `diffusers` · `krea2` · `mock` · `off` |
| `SELFIE_MODEL` | `bytedance-seed/seedream-4.5` | the hosted route's model |
| `SELFIE_TEMPLATES_EXTRA` | *(empty)* | your own template overlay yaml |
| `SELFIE_TEMPLATES` | *(empty)* | a library that **replaces** the shipped one (a character runtime points this at her own `selfie.yaml`) |
| `SELFIE_LOCAL_MODEL` | *(empty)* | path to a `.safetensors` checkpoint |
| `SELFIE_LOCAL_DEVICE` | `cuda` | |
| `SELFIE_LOCAL_STEPS` / `_CFG` | `30` / `5.0` | SDXL sampling |
| `SELFIE_LOCAL_HIRES` / `_SCALE` / `_DENOISE` | `true` / `1.5` / `0.35` | the second pass |
| `SELFIE_LOCAL_CPU_OFFLOAD` | `false` | ~2× slower, much less VRAM |
| `SELFIE_KREA2_STEPS` / `_CFG` | `0` / `-1` | `0`/`-1` = read it off the checkpoint |
| `SELFIE_LLM_PARK` | `true` | lend the LLM's VRAM to a local render |
| `SELFIE_WARM_HEADROOM_GIB` | `6.0` | VRAM her brain needs beside a warm render pipeline; below it the pipeline is dropped after each render |

See [Selfies](selfies.md).

## The mind

All of `MIND_*`, plus the reflex windows — the full table with explanations is in
[The mind → the knobs](mind.md#the-knobs).

## Channels

| Key | Default | |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | *(empty)* | the single-companion pair |
| `TELEGRAM_BOT_TOKEN_<ID>` / `TELEGRAM_CHAT_ID_<ID>` | — | one companion's own bot, by registry id |
| `TELEGRAM_CHARACTER` | *(empty)* | who keeps the unsuffixed pair |
| `TELEGRAM_SEND_NON_TELEGRAM` | `false` | copy web/voice/CLI/API replies to Telegram |

See [Channels](channels.md).

## The room and the window

| Key | Default | |
|---|---|---|
| `RAIN_INTENSITY` | `0.6` | 0..1 |
| `DESKTOP_BODY` | `vrm` | `vrm` · `live2d` |
| `AVATAR_MODEL` | `hiyori` | which Live2D rig |
| `WINDOW_WIDTH` / `WINDOW_HEIGHT` | `360` / `640` | |
| `WINDOW_ON_TOP` | `true` | |
| `WINDOW_GUI` | *(auto)* | `""` · `qt` · `gtk` |

See [Bodies](bodies.md).

## What leaves the machine

Two knobs that are not hers — they're knobs on two *libraries* that reach the network on nothing
but an `import`, and read these from the environment at that moment. YuriOS applies the quiet
setting itself, because a typed config field would arrive too late. They're listed in
`.env.example` so they're visible, and so uncommenting one takes it back.

| Key | Applied value | What the other value does |
|---|---|---|
| `LITELLM_LOCAL_MODEL_COST_MAP` | `True` | `False` re-enables a 1.67 MB price map litellm GETs from GitHub on every start — nothing here reads it, and litellm ships the same file in the wheel |
| `HF_HUB_DISABLE_TELEMETRY` | `1` | `0` lets Hugging Face model downloads report your torch build and which AI harness is running |

Neither disables anything of hers. `yurios doctor` prints what this section and your
model choices add up to on the wire.

## Per-character overrides

A knob in `.env` is the **house default**. A character's registry record may override her own
name, storage paths, loop switches, chat model, utility model, TTS/STT backend, voice register,
body backend, avatar model and Telegram pair; a blank binding means *inherit*. Everything else —
the port, the room, the reflex windows, what leaves the machine — belongs to the house.

See [Characters → connection profiles](characters.md#connection-profiles).
