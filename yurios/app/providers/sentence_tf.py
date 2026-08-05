"""Local Embedder via sentence-transformers (SPEC §3).

The chat model may be rented (Build #1 accepts a hosted reply voice) but the
*mind* — including the embeddings that index it — stays local and ownable
(→ ch. 19). Default model: BAAI/bge-small-en-v1.5, 384-d.
"""
from __future__ import annotations


_INSTALL_HINT = (
    "EMBED_BACKEND=sentence_tf needs sentence-transformers, which should be installed "
    "with YuriOS. Reinstall it with `pip install -e .`; on Linux fetch the "
    "CPU torch build first to skip ~4 GB of CUDA you won't use: `pip install torch "
    "--index-url https://download.pytorch.org/whl/cpu` — or embed against a server "
    "you're already running and install nothing: EMBED_BACKEND=lm_studio (the "
    "default) or EMBED_BACKEND=ollama.")

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_DIM = 384


class SentenceTFEmbedder:
    def __init__(self, model_name: str = DEFAULT_MODEL, dim: int = DEFAULT_DIM):
        # Lazy import: torch is heavy; tests use a fake Embedder instead. This is the
        # one heavy backend with no fake to degrade into — her memory can't silently
        # run on nothing — so it fails loudly, and says how to fix it both ways.
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:  # pragma: no cover — needs a torch-free env
            raise RuntimeError(_INSTALL_HINT) from e

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
