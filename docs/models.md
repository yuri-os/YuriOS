# Models & connections

Her brain is the one part that isn't pip-installable. YuriOS talks to models through
[LiteLLM](https://docs.litellm.ai/), so **the model id's prefix picks the provider** and swapping
provider is a one-line change.

```
CHAT_MODEL=lm_studio/HauhauCS/Gemma-4-E4B-Uncensored-HauhauCS-Aggressive
             ^^^^^^^^^ the route      ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^ the id on that provider
```

| Prefix | Goes to | Endpoint knob |
|---|---|---|
| `lm_studio/…` | a local LM Studio server (OpenAI-compatible) | `LMSTUDIO_BASE_URL` (default `http://localhost:1234/v1`) |
| `ollama/…` | a local Ollama server | `OLLAMA_BASE_URL` (default `http://localhost:11434`) |
| `openrouter/…` | hosted OpenRouter | `OPENROUTER_API_KEY` |
| `openai/…`, `anthropic/…`, … | any other LiteLLM route | that provider's own env vars |
| *(no prefix)* | assumed OpenRouter — the prefix is added for you | `OPENROUTER_API_KEY` |

There are three model roles:

| Knob | Role | Notes |
|---|---|---|
| `CHAT_MODEL` | her reply voice | on the hot path; `CHAT_THINKING=false` by default so a reasoning model answers instead of thinking first |
| `UTILITY_MODEL` | fact extraction, summarisation, DREAM | off the hot path; `UTILITY_THINKING=true`, because quality matters more than latency here |
| `EMBED_MODEL` | her memory's vectors | see [Embeddings](#embeddings) |

The chat and utility models are usually the same id. They don't have to be — a small fast model
for the voice and a larger one for consolidation is a reasonable split.

## LM Studio (the shipped default)

Start the server (Developer tab → **Start Server**, or `lms server start`), then:

```bash
lms get HauhauCS/Gemma-4-E4B-Uncensored-HauhauCS-Aggressive   # her thinking
lms get text-embedding-nomic-embed-text-v1.5                  # her memory's embeddings
```

```ini
CHAT_MODEL=lm_studio/HauhauCS/Gemma-4-E4B-Uncensored-HauhauCS-Aggressive
UTILITY_MODEL=lm_studio/HauhauCS/Gemma-4-E4B-Uncensored-HauhauCS-Aggressive
LMSTUDIO_BASE_URL=http://localhost:1234/v1
EMBED_BACKEND=lm_studio
EMBED_MODEL=text-embedding-nomic-embed-text-v1.5
EMBED_DIM=768
```

**Why an uncensored model:** she's a companion, not an assistant. A refusal-trained model plays
her badly — it breaks character to decline, which is the one thing a person in the room never
does. Any model works; this is a recommendation about *voice*, not a requirement.

### Preloading and the eviction problem

`LMSTUDIO_PRELOAD=true` (the default) loads the chat model — and the embedder, if
`EMBED_BACKEND=lm_studio` — at boot and **pins** them, with no idle unload.

Leave it on. Chat and embeddings share one server, and LM Studio's JIT loader unloads the last
JIT-loaded model to serve the next request: without pinning, every turn evicts one model to load
the other and pays the reload — seconds per turn, forever. Nothing in your LM Studio config is
touched; this is the Load button over HTTP. `LMSTUDIO_LOAD_TIMEOUT_S` (600) only has to cover a
cold load off disk.

### Context length

`CONTEXT_LENGTH` is her context window in tokens. `0` means "whatever the provider defaults to" —
for LM Studio that's the per-model config its own UI would load with, often far below what the
model can do, which is how a good long conversation ends in *"Context size has been exceeded"*
and a lost reply.

Set it and the number becomes real twice over: her model is pinned at that size, and the masthead
shows prompt tokens against it, so you watch the window fill instead of finding out when a turn
fails. Bigger costs RAM/VRAM for the KV cache — **8192 is a floor, 32768 is comfortable**; your
machine decides. `GET /api/context` reports the same numbers to anything that isn't the page.

## Ollama

```bash
ollama pull <your-model>
ollama pull nomic-embed-text
```

```ini
CHAT_MODEL=ollama/<your-model>
UTILITY_MODEL=ollama/<your-model>
OLLAMA_BASE_URL=http://localhost:11434
EMBED_BACKEND=ollama
EMBED_MODEL=nomic-embed-text
EMBED_DIM=768
```

`OLLAMA_BASE_URL` is also what the settings panel queries to list the models you've actually
pulled (`GET {base}/api/tags`), so the model field becomes a browse instead of a guess.

## OpenRouter

```ini
OPENROUTER_API_KEY=sk-or-…
CHAT_MODEL=openrouter/<vendor>/<model>
UTILITY_MODEL=openrouter/<vendor>/<model>
```

Requests carry YuriOS's app-attribution headers so the spend lands on the project's OpenRouter app
page rather than nowhere; local routes send no such headers. Note that a hosted chat model means
her conversation leaves your machine — the local routes above are the default for a reason.

Embeddings never go to OpenRouter: keep `EMBED_BACKEND` on `lm_studio`, `ollama` or `sentence_tf`.

## Any other provider (custom / self-hosted)

Anything LiteLLM can route, YuriOS can use. Two shapes:

**A named LiteLLM provider.** Use its prefix and set its environment variable:

```ini
CHAT_MODEL=openai/gpt-4o-mini        # OPENAI_API_KEY in the environment
CHAT_MODEL=anthropic/claude-…        # ANTHROPIC_API_KEY
```

**Any OpenAI-compatible server** (vLLM, llama.cpp's server, LocalAI, text-generation-webui,
a proxy). Point the LM Studio route at it — the `lm_studio/` prefix means "OpenAI-compatible
endpoint at `LMSTUDIO_BASE_URL`", not "the LM Studio application":

```ini
CHAT_MODEL=lm_studio/<model-id-that-server-serves>
LMSTUDIO_BASE_URL=http://<host>:<port>/v1
LMSTUDIO_PRELOAD=false               # the pin/evict API is LM Studio's own
```

Turn `LMSTUDIO_PRELOAD` off for a non-LM-Studio server: the preload path speaks LM Studio's model
management API, which other servers don't implement. The chat path itself is plain OpenAI.

## Embeddings

Her memory's vectors are always computed locally. Three backends:

| `EMBED_BACKEND` | Where it runs | Needs |
|---|---|---|
| `lm_studio` *(default)* | the same LM Studio server as the chat model | nothing beyond the base install |
| `ollama` | a local Ollama server | nothing beyond the base install |
| `sentence_tf` | **in-process**, via sentence-transformers | `pip install -e ".[local-embed]"` — the fully standalone option |

`EMBED_MODEL` and `EMBED_DIM` must match the backend — a mismatch fails at reindex. Common pairs:
`text-embedding-nomic-embed-text-v1.5` @ 768, `BAAI/bge-small-en-v1.5` @ 384.

Changing any of the three re-indexes the Vault from its `.md` files automatically (there's a
fingerprint check), so switching later is safe — it just costs one reindex. You can force one
with `python scripts/reindex.py`.

## Per-character models

Everything above sets the **house default**. Each character may override her own chat model,
utility model, voice and body in her registry record; a blank binding means "inherit". The
switchboard's profile drawer is the front end for that, and
[Characters → connection profiles](characters.md#connection-profiles) explains named endpoints —
useful when two companions live on two different servers.

## Changing models later

Model knobs are read once at boot. Edit `.env` (by hand or through the gear panel) and restart:
the panel says so out loud after a save rather than pretending to hot-apply. Her memory, persona
and history are model-independent — swapping the model does not cost you the companion.

## Checking what's wired

```bash
python -m yurios.doctor            # what .env selects vs what's installed
curl localhost:8768/api/health     # what's actually running right now
```
