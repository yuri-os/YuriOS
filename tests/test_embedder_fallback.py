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


# ---- the local embedder loads offline once the model is cached ----------------

def _fake_sentence_transformers(monkeypatch, recorder, failures=()):
    """A SentenceTransformer seam: `failures` are raised for the first calls."""
    import sys
    from types import SimpleNamespace

    monkeypatch.setattr(sentence_tf, "_shared", {})   # no model leaks between tests
    calls = []

    class SentenceTransformer:
        def __init__(self, model_name, **kwargs):
            calls.append(kwargs)
            if len(calls) <= len(failures):
                raise failures[len(calls) - 1]
            recorder.append((model_name, kwargs))

        def get_embedding_dimension(self):
            return 384

    monkeypatch.setitem(sys.modules, "sentence_transformers",
                        SimpleNamespace(SentenceTransformer=SentenceTransformer))
    return calls


def test_a_cached_embedding_model_loads_without_touching_the_hub(monkeypatch):
    loaded = []
    _fake_sentence_transformers(monkeypatch, loaded)

    embedder = sentence_tf.SentenceTFEmbedder()

    assert loaded == [(sentence_tf.DEFAULT_MODEL, {"local_files_only": True})]
    assert embedder.dim == 384


def test_an_uncached_embedding_model_downloads_once_then_loads(monkeypatch):
    loaded = []
    calls = _fake_sentence_transformers(monkeypatch, loaded,
                                        failures=[OSError("not in the local cache")])

    embedder = sentence_tf.SentenceTFEmbedder()

    assert calls == [{"local_files_only": True}, {}]   # offline tried, then the hub
    assert loaded == [(sentence_tf.DEFAULT_MODEL, {})]
    assert embedder.dim == 384


def test_a_full_card_moves_the_embedder_to_the_cpu(monkeypatch, caplog):
    """A 27B chat model fills a 15 GiB card, and then 46 MiB of embedder is what
    fails — taking every character down with it. Her memory is 130 MB of weights;
    the CPU runs it fine."""
    class OutOfMemoryError(RuntimeError):
        pass

    loaded = []
    calls = _fake_sentence_transformers(
        monkeypatch, loaded,
        failures=[OutOfMemoryError("CUDA out of memory. Tried to allocate 46.00 MiB")])

    with caplog.at_level("INFO", logger=sentence_tf.log.name):
        embedder = sentence_tf.SentenceTFEmbedder()

    # straight to the CPU — not to Hugging Face, which frees no VRAM at all
    assert calls == [{"local_files_only": True},
                     {"device": "cpu", "local_files_only": True}]
    assert embedder.dim == 384
    assert "not fully cached" not in caplog.text
    assert "no room on the GPU" in caplog.text


def test_every_characters_embedder_shares_one_loaded_model(monkeypatch):
    # One Runtime per character used to mean one full model load per character:
    # three residents, three copies of identical weights in RSS.
    loaded = []
    _fake_sentence_transformers(monkeypatch, loaded)

    first = sentence_tf.SentenceTFEmbedder()
    second = sentence_tf.SentenceTFEmbedder()
    third = sentence_tf.SentenceTFEmbedder()

    assert len(loaded) == 1                      # loaded once, not per character
    assert first._model is second._model is third._model
