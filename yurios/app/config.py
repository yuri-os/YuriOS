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
    # picks the route (openrouter/… hosted, ollama/… or lm_studio/… local); a bare
    # id is assumed OpenRouter. The openrouter/ prefix is added in the provider.
    openrouter_api_key: str = ""
    chat_model: str = "lm_studio/google/gemma-4-12b-qat"
    utility_model: str = "lm_studio/google/gemma-4-12b-qat"
    # Base url for a local LM Studio server (used only for lm_studio/… model ids;
    # OpenAI-compatible, so this is its /v1 endpoint).
    lmstudio_base_url: str = "http://localhost:1234/v1"
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
