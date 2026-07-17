"""Local Embedder via LM Studio (SPEC §3 — the `EMBED_BACKEND=lm_studio` option).

LM Studio serves embeddings on the same OpenAI-compatible server as its chat
models (POST {base}/embeddings), so a single local process can back BOTH the mind
(CHAT_MODEL=lm_studio/…) and its memory — no Ollama needed. Load an embedding
model in LM Studio (e.g. `text-embedding-nomic-embed-text-v1.5`, 768-d) and set
EMBED_MODEL + EMBED_DIM to match. A local chat model routes through LiteLLM
(`CHAT_MODEL=lm_studio/<name>`), so only the Embedder seam lives here (§3.1).
"""
from __future__ import annotations

import httpx


class LMStudioEmbedder:
    def __init__(self, model_name: str = "text-embedding-nomic-embed-text-v1.5",
                 dim: int = 768, base_url: str = "http://localhost:1234/v1"):
        self.model_name = model_name
        self.dim = dim
        self.base_url = base_url.rstrip("/")

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        with httpx.Client(timeout=60) as client:
            r = client.post(f"{self.base_url}/embeddings",
                            json={"model": self.model_name, "input": texts})
            r.raise_for_status()
            # OpenAI shape: {"data": [{"index": i, "embedding": [...]}, ...]}.
            # Sort by index — the server does not guarantee input order.
            rows = sorted(r.json()["data"], key=lambda d: d["index"])
        out = [row["embedding"] for row in rows]
        for vec in out:
            if len(vec) != self.dim:
                raise ValueError(
                    f"EMBED_DIM={self.dim} but {self.model_name} returned "
                    f"{len(vec)}-d — fix .env (§3)")
        return out
