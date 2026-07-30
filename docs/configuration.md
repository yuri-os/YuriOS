# Configuration

Every knob lives in `.env` at the project root, is typed
(`yurios/world/config.py` → `yurios/desktop/config.py` → `yurios/app/config.py`), and is read
**once at boot**. Copy `.env.example` — it is the annotated version of this page — and change what
you need:

```bash
cp .env.example .env
```

Every knob has a default, and **the default stack needs no cloud key**: pull the network cable and
she still runs.

Config is read once, so a change takes effect on the next restart. The settings panel says so out
loud after a save rather than pretending to hot-apply.

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

## Brain

| Key | Default | |
|---|---|---|
| `CHAT_MODEL` | `lm_studio/HauhauCS/Gemma-4-E4B-Uncensored-HauhauCS-Aggressive` | her reply voice; the prefix picks the provider |
| `UTILITY_MODEL` | same | summaries, extraction, DREAM — off the hot path |
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

Full explanation: [Models & connections](models.md).

## Memory & embeddings

| Key | Default | |
|---|---|---|
| `EMBED_BACKEND` | `lm_studio` | `lm_studio` · `ollama` · `sentence_tf` (in-process) |
| `EMBED_MODEL` | `text-embedding-nomic-embed-text-v1.5` | must match the backend |
| `EMBED_DIM` | `768` | must equal the model's vector width |
| `RAW_WINDOW_TURNS` | `6` | raw messages kept in-prompt |
| `RETRIEVAL_K` | `6` | recalled memories injected |
| `RETRIEVAL_MIN_SIM` | `0.25` | drop below this cosine similarity |
| `HALF_LIFE_DAYS` | `30` | recency-decay half-life |
| `SUMMARY_EVERY_N` | `8` | summarise cadence, in turns |
| `SUMMARY_BUDGET_TOKENS` | `300` | |
| `LOREBOOK_BUDGET_TOKENS` | `400` | |
| `SYSTEM_BUDGET_TOKENS` | `8000` | overflow ceiling for the system block |

Changing the backend, model or dimension re-indexes the Vault from its `.md` files automatically.

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

Backend-specific keys (`QWEN_*`, `SOVITS_*`) are in [Voice](voice.md).

## Tools

| Key | Default | |
|---|---|---|
| `TOOLS_BACKEND` | `mcp` | `mcp` · `fake` · `off` |
| `TOOL_MAX_CALLS_PER_TURN` | `2` | |
| `TOOL_TIMEOUT_S` | `10` | |
| `TOOL_RATE_TIMER` / `_MUSIC` / `_WEATHER` / `_SELFIE` | `6` / `6` / `4` / `2` | calls per minute |
| `TIMER_MAX_MINUTES` | `180` | |
| `WEATHER_BACKEND` | `open_meteo` | `open_meteo` · `fake` |
| `WEATHER_CITY` | `Seoul` (`.env.example`) | the default when she isn't told one |

See [Tools](tools.md).

## Selfies

| Key | Default | |
|---|---|---|
| `SELFIE_BACKEND` | `openrouter` | `openrouter` · `diffusers` · `krea2` · `mock` · `off` |
| `SELFIE_MODEL` | `bytedance-seed/seedream-4.5` | the hosted route's model |
| `SELFIE_TEMPLATES_EXTRA` | *(empty)* | your own template overlay yaml |
| `SELFIE_LOCAL_MODEL` | *(empty)* | path to a `.safetensors` checkpoint |
| `SELFIE_LOCAL_DEVICE` | `cuda` | |
| `SELFIE_LOCAL_STEPS` / `_CFG` | `30` / `5.0` | SDXL sampling |
| `SELFIE_LOCAL_HIRES` / `_SCALE` / `_DENOISE` | `true` / `1.5` / `0.35` | the second pass |
| `SELFIE_LOCAL_CPU_OFFLOAD` | `false` | ~2× slower, much less VRAM |
| `SELFIE_KREA2_STEPS` / `_CFG` | `0` / `-1` | `0`/`-1` = read it off the checkpoint |
| `SELFIE_LLM_PARK` | `true` | lend the LLM's VRAM to a local render |

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

Neither disables anything of hers. `python -m yurios.doctor` prints what this section and your
model choices add up to on the wire.

## Per-character overrides

A knob in `.env` is the **house default**. A character's registry record may override her own
name, storage paths, loop switches, chat model, utility model, TTS/STT backend, voice register,
body backend, avatar model and Telegram pair; a blank binding means *inherit*. Everything else —
the port, the room, the reflex windows, what leaves the machine — belongs to the house.

See [Characters → connection profiles](characters.md#connection-profiles).
