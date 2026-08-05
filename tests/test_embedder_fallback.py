"""The default LM Studio embedder degrades to the local sentence-transformer."""
from __future__ import annotations

import httpx

from yurios.app import main
from yurios.app.config import Config
from yurios.app.providers import lmstudio, sentence_tf


def test_unavailable_lmstudio_embeddings_fall_back_to_sentence_transformers(monkeypatch):
    class UnavailableLMStudioEmbedder:
        def __init__(self, *args):
            self.args = args

        def embed(self, texts):
            raise httpx.ConnectError("connection refused")

    class LocalEmbedder:
        def __init__(self, model_name, dim):
            self.model_name = model_name
            self.dim = dim

    monkeypatch.setattr(lmstudio, "LMStudioEmbedder", UnavailableLMStudioEmbedder)
    monkeypatch.setattr(sentence_tf, "SentenceTFEmbedder", LocalEmbedder)
    cfg = Config(_env_file=None, embed_backend="lm_studio",
                 embed_model="text-embedding-nomic-embed-text-v1.5", embed_dim=768)

    embedder = main._default_embedder(cfg)

    assert isinstance(embedder, LocalEmbedder)
    assert (embedder.model_name, embedder.dim) == (
        sentence_tf.DEFAULT_MODEL, sentence_tf.DEFAULT_DIM)
    assert (cfg.embed_backend, cfg.embed_model, cfg.embed_dim) == (
        "sentence_tf", sentence_tf.DEFAULT_MODEL, sentence_tf.DEFAULT_DIM)


def test_available_lmstudio_embeddings_keep_the_configured_backend(monkeypatch):
    class WorkingLMStudioEmbedder:
        def __init__(self, *args):
            self.args = args

        def embed(self, texts):
            return [[0.0] * 768 for _ in texts]

    monkeypatch.setattr(lmstudio, "LMStudioEmbedder", WorkingLMStudioEmbedder)
    cfg = Config(_env_file=None, embed_backend="lm_studio",
                 embed_model="text-embedding-nomic-embed-text-v1.5", embed_dim=768)

    embedder = main._default_embedder(cfg)

    assert isinstance(embedder, WorkingLMStudioEmbedder)
    assert cfg.embed_backend == "lm_studio"
