# Models & connections

YuriOS talks to models through [LiteLLM](https://docs.litellm.ai/) or its bundled direct llama.cpp
provider. **The model id's prefix picks the provider** and swapping provider is a one-line change.
Fresh installs set both chat roles to `NONE`, which deliberately makes no connection until the first
dashboard load or `yurios configure` chooses a model.

The terminal configurator has guided choices for direct GGUF, LM Studio, Ollama, and OpenRouter.
For local servers it records the endpoint and checks that the named model is available; for
OpenRouter it asks for the API key without echoing it and verifies the key before saving.

```
CHAT_MODEL=lm_studio/HauhauCS/Gemma-4-E4B-Uncensored-HauhauCS-Aggressive
             ^^^^^^^^^ the route      ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^ the id on that provider
```

| Prefix | Goes to | Endpoint knob |
|---|---|---|
| `lm_studio/…` | a local LM Studio server (OpenAI-compatible) | `LMSTUDIO_BASE_URL` (default `http://localhost:1234/v1`) |
| `gguf/owner/repo` | a local GGUF through llama.cpp | `GGUF_*` |
| `ollama/…` | a local Ollama server | `OLLAMA_BASE_URL` (default `http://localhost:11434`) |
| `openrouter/…` | hosted OpenRouter | `OPENROUTER_API_KEY` |
| `openai/…`, `anthropic/…`, … | any other LiteLLM route | that provider's own env vars |
| *(no prefix)* | assumed OpenRouter — the prefix is added for you | `OPENROUTER_API_KEY` |
| `NONE` | no language-model connection | choose one with `yurios configure` |

There are three model roles:

| Knob | Role | Notes |
|---|---|---|
| `CHAT_MODEL` | her reply voice | on the hot path; `CHAT_THINKING=false` by default so a reasoning model answers instead of thinking first |
| `UTILITY_MODEL` | fact extraction, summarisation, DREAM | off the hot path; `UTILITY_THINKING=true`, because quality matters more than latency here |
| `EMBED_MODEL` | her memory's vectors | see [Embeddings](#embeddings) |

The chat and utility models are usually the same id. They don't have to be — a small fast model
for the voice and a larger one for consolidation is a reasonable split.

## LM Studio

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

### Direct GGUF, no LM Studio

The first-run picker and `yurios configure` currently recommend this GGUF repository:

```ini
CHAT_MODEL=gguf/mradermacher/Qwen3-14B-Uncensored-GGUF
UTILITY_MODEL=gguf/mradermacher/Qwen3-14B-Uncensored-GGUF
GGUF_QUANT=Q4_K_M
```

YuriOS resolves the matching `*.Q4_K_M.gguf` file in that Hugging Face repository, downloads it
once to the ignored `./models` Hugging Face cache, and runs it directly with llama.cpp. Chat and
utility work share one loaded context. The default installer includes this runtime; a manual install needs
`pip install -e ".[llm]"`.

For another GGUF source, make `CHAT_MODEL` and `UTILITY_MODEL` its `gguf/<Hugging Face repo>` name.
If its model id is not its Hugging Face repository, set `GGUF_REPO=owner/repository`.
`GGUF_N_GPU_LAYERS=-1` uses all layers only when `llama-cpp-python` was installed with CUDA or
Metal support; its standard wheel is CPU-first.

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

Embeddings never go to OpenRouter: YuriOS has no OpenRouter embedding backend. Keep
`EMBED_BACKEND` on `lm_studio`, `ollama` or `sentence_tf` so the memory index stays local.

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
| `sentence_tf` *(default)* | **in-process**, via sentence-transformers | included with YuriOS; model weights download once |
| `lm_studio` | the same LM Studio server as the chat model | a running server with an embedding model loaded |
| `ollama` | a local Ollama server | nothing beyond the base install |

`EMBED_MODEL` and `EMBED_DIM` must match the backend — a mismatch fails at reindex. Common pairs:
`text-embedding-nomic-embed-text-v1.5` @ 768, `BAAI/bge-small-en-v1.5` @ 384.

### Switching embedding backends

The default is fully local and requires no server:

```ini
EMBED_BACKEND=sentence_tf
EMBED_MODEL=BAAI/bge-small-en-v1.5
EMBED_DIM=384
```

To reuse an LM Studio embedding model, start its server, load the model, then set:

```ini
EMBED_BACKEND=lm_studio
EMBED_MODEL=text-embedding-nomic-embed-text-v1.5
EMBED_DIM=768
```

For Ollama, pull `nomic-embed-text` and set `EMBED_BACKEND=ollama`,
`EMBED_MODEL=nomic-embed-text`, and `EMBED_DIM=768`. There is no OpenRouter embedding option:
OpenRouter can power chat, but memory vectors always remain local.

Changing any of the three re-indexes the Vault from its `.md` files automatically (there's a
fingerprint check), so switching later is safe — it just costs one reindex. You can force one
with `python scripts/reindex.py`.

## Per-character models

Everything above sets the **house default**. Each character may take her own chat model, utility
model, endpoint, API key variable and model knobs (temperature, the reasoning switches, the reply
cap, the context window) in her registry record; a blank field means "inherit the `.env`". So one
companion can run on a 4 B local model while another answers from OpenRouter, on one node, from
one config file.

Two front ends edit exactly the same record:

- **The gear in her room** — the top panel of the settings dialog is hers, the rest is the house's
  `.env`. Each field's placeholder names what she would inherit if you left it blank.
- **The switchboard's profile drawer** — the same fields on the character board.

[Characters → connection profiles](characters.md#connection-profiles) explains named endpoints,
which is the shared version of the same idea: her own endpoint wins over the profile she points at.
The key itself never enters the registry — `api_key_env` names the environment variable it is read
from, so a character can be copied to another machine without carrying a secret.

## Changing models mid-conversation

**Her** model, route, key and knobs apply the moment you save — no restart, no lost session.
Whatever she is mid-sentence about finishes on the model it started on; the next thing she says
comes from the new one, with the same memory, the same mind and the same voice. A newly chosen
LM Studio model is pinned in the background, so the first turn after a swap may wait on the load.

The **house** knobs in `.env` are still read once at boot — the gear panel says so after a save,
rather than pretending to hot-apply — as is the embedder, which re-indexes the Vault when it
changes. Her memory, persona and history are model-independent either way: swapping the model
does not cost you the companion.

## Checking what's wired

```bash
python -m yurios.doctor            # what .env selects vs what's installed
curl localhost:8768/api/health     # what's actually running right now
```
