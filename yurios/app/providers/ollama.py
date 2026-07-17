"""Local Embedder via Ollama (SPEC §3 — the `EMBED_BACKEND=ollama` alternative).

Uses the local Ollama server's /api/embeddings endpoint (default
`nomic-embed-text`, 768-d — set EMBED_MODEL and EMBED_DIM together).
A local *chat* model also routes through LiteLLM (`CHAT_MODEL=ollama/<name>`),
so there is no ChatModel class here — that seam already exists (§3.1).
"""
from __future__ import annotations

import httpx


class OllamaEmbedder:
    def __init__(self, model_name: str = "nomic-embed-text", dim: int = 768,
                 base_url: str = "http://localhost:11434"):
        self.model_name = model_name
        self.dim = dim
        self.base_url = base_url

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        with httpx.Client(timeout=60) as client:
            for text in texts:
                r = client.post(f"{self.base_url}/api/embeddings",
                                json={"model": self.model_name, "prompt": text})
                r.raise_for_status()
                vec = r.json()["embedding"]
                if len(vec) != self.dim:
                    raise ValueError(
                        f"EMBED_DIM={self.dim} but {self.model_name} returned "
                        f"{len(vec)}-d — fix .env (§3)")
                out.append(vec)
        return out
