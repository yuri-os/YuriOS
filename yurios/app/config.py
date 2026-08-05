"""Typed configuration (SPEC §11).

Read once at boot from the environment / `.env` into a pydantic-settings
object. No secrets in code; every knob the spec names is here, defaulted
to the spec's defaults.
"""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # model access (via LiteLLM — the router seam, §3.1). The model id's PREFIX
    # picks the route (openrouter/… hosted; ollama/…, lm_studio/…, or gguf/… local);
    # a bare id is assumed OpenRouter. The openrouter/ prefix is added in the provider.
    openrouter_api_key: str = ""
    chat_model: str = "NONE"
    utility_model: str = "NONE"
    # Base url for a local LM Studio server (used only for lm_studio/… model ids;
    # OpenAI-compatible, so this is its /v1 endpoint).
    lmstudio_base_url: str = "http://localhost:1234/v1"
    # Load the lm_studio/… chat model and embedder at boot and pin them there, so
    # LM Studio's JIT loader stops evicting one to serve the other every turn
    # (providers/lmstudio.ensure_resident). Off = the old behaviour, one reload per
    # turn. The timeout only has to cover a cold load off disk.
    lmstudio_preload: bool = True
    lmstudio_load_timeout_s: float = 600.0
    # When an lm_studio/ route cannot reach LM Studio, run its matching GGUF
    # directly through llama.cpp instead. The configured model id after
    # `lm_studio/` is its Hugging Face repo unless gguf_repo overrides it.
    # The matching Q4_K_M file is downloaded into Hugging Face's normal cache on
    # first fallback use, so an ordinary install needs no LM Studio application.
    gguf_fallback: bool = True
    gguf_repo: str = ""
    gguf_quant: str = "Q4_K_M"
    gguf_cache_dir: str = "./models"
    gguf_context_length: int = 0
    gguf_n_gpu_layers: int = 0
    gguf_n_threads: int = 0
    # Gemma 4's mixed V-cache dimensions are impractical at a long context
    # without Flash Attention, especially when the model is fully offloaded.
    gguf_flash_attn: bool = True
    # The context window to run her in, in tokens. 0 = don't ask for one: the
    # provider serves whatever it defaults to (for LM Studio, the per-model
    # config its own UI would load with — often far smaller than the model can
    # do, which is how a long conversation ends in "Context size has been
    # exceeded"). Set it and the number becomes real in two places: it is sent
    # as `context_length` when the model is pinned at boot (lmstudio.py), and it
    # is the ceiling the UI's context readout measures against. Bigger windows
    # cost RAM/VRAM for the KV cache, so this is your dial, not a default.
    context_length: int = 0
    # Base url for a local Ollama server. Chat routing (ollama/… ids) uses LiteLLM's
    # own default; this knob is what the settings panel queries to list the models
    # you actually have pulled (GET {base}/api/tags).
    ollama_base_url: str = "http://localhost:11434"
    # The utility model does fact-extraction + summarisation (§6.3, §7.3). When it
    # is a *reasoning* model (qwen3, r1, gemma-…-qat, …) its <think> block needs room
    # before the JSON answer — too small a budget truncates it to nothing. Reasoning
    # is ON by default and is a knob, not a hardcode: set utility_thinking=false to
    # trade some extraction quality for speed on a reasoning model.
    utility_thinking: bool = True
    utility_max_tokens: int = 2048
    # The reply voice's reasoning pass. ON by default (a reasoning chat model thinks
    # before it speaks); set chat_thinking=false to disable it for speed — Build #2
    # does this so the voice loop stays real-time on a local reasoning model.
    chat_thinking: bool = True
    # Reply/greeting token ceiling. Big enough that a *reasoning* chat model has room
    # for its <think> pass AND the reply — too small and the think block eats it all
    # and the reply comes back empty. A no-think model never nears the cap.
    max_reply_tokens: int = 2048

    # embeddings — local, always (§3: the mind stays ownable). lm_studio reuses the
    # same local server as an lm_studio/ chat model (LMSTUDIO_BASE_URL), so one
    # process can back both the mind and its memory (set EMBED_MODEL + EMBED_DIM to
    # the loaded embedding model, e.g. text-embedding-nomic-embed-text-v1.5 @ 768).
    embed_backend: str = "sentence_tf"  # sentence_tf | ollama | lm_studio
    embed_model: str = "BAAI/bge-small-en-v1.5"
    embed_dim: int = 384  # MUST equal the index vector width (§3.1 Embedder.dim)

    # where to serve (used by `python -m app`; uvicorn CLI flags still override).
    # 8765 deliberately dodges the local-AI stack's defaults — SillyTavern (8000),
    # Gradio/A1111/ooba (7860), Kobold (5001), ComfyUI (8188), LM Studio (1234).
    host: str = "127.0.0.1"
    port: int = 8765

    # the mind (§4)
    vault_dir: Path = Path("./vault")
    soul_src: Path = Path("../yuri-soul")
    user_name: str = "you"  # {{user}} substitution (§5.3)

    # prompt & memory knobs (§6.4, §7)
    raw_window_turns: int = 6        # raw messages kept in-prompt (3 exchanges)
    retrieval_k: int = 6             # recalled memories injected
    retrieval_min_sim: float = 0.25  # drop below this cosine similarity
    half_life_days: float = 30       # recency-decay half-life
    summary_every_n: int = 8         # summarise cadence (turns)
    summary_budget_tokens: int = 300
    lorebook_budget_tokens: int = 400
    system_budget_tokens: int = 8000  # §7.2 overflow ceiling for the system block
    temperature: float = 0.9

    # the corpus (§8) — outside the Vault, append-only, gitignored
    corpus_dir: Path = Path("./corpus")

    # Host-managed character registry.  The legacy roots above remain valid
    # inputs to the 0.1 -> 0.2 migration.
    data_dir: Path = Path("./data")
    utility_enabled: bool = True
    dream_enabled: bool = True
