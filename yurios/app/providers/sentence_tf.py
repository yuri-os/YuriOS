"""Local Embedder via sentence-transformers (SPEC §3).

The chat model may be rented (Build #1 accepts a hosted reply voice) but the
*mind* — including the embeddings that index it — stays local and ownable
(→ ch. 19). Default model: BAAI/bge-small-en-v1.5, 384-d.
"""
from __future__ import annotations


class SentenceTFEmbedder:
    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5", dim: int = 384):
        # Lazy import: torch is heavy; tests use a fake Embedder instead.
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(model_name)
        self.dim = dim
        actual = self._model.get_sentence_embedding_dimension()
        if actual != dim:
            raise ValueError(
                f"EMBED_DIM={dim} but {model_name} produces {actual}-d vectors — "
                "the index dimension is config, never hard-coded (§3); fix .env"
            )

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._model.encode(texts, normalize_embeddings=True).tolist()
