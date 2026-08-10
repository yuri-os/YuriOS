"""Local Embedder via sentence-transformers (SPEC §3).

The chat model may be rented (Build #1 accepts a hosted reply voice) but the
*mind* — including the embeddings that index it — stays local and ownable
(→ ch. 19). Default model: BAAI/bge-small-en-v1.5, 384-d.

A plain ``SentenceTransformer(name)`` etag-checks every file of the repo
against Hugging Face on EVERY load — cached or not — which is a dozen-plus
requests in the boot log. So the load is offline-first: a fully cached model
never touches the network, and only a first-ever (or incomplete) download
falls back to the hub.

And every character runtime builds its own embedder (world/main.py), which
used to mean one full model load PER CHARACTER: three residents, three
"Loading weights" bars, 3× the RSS for identical weights. The weights are
read-only after load, so one SentenceTransformer per model name is shared
process-wide instead.
"""
from __future__ import annotations

import logging
import threading


log = logging.getLogger(__name__)


_INSTALL_HINT = (
    "EMBED_BACKEND=sentence_tf needs sentence-transformers, which should be installed "
    "with YuriOS. Reinstall it with `pip install -e .`; on Linux fetch the "
    "CPU torch build first to skip ~4 GB of CUDA you won't use: `pip install torch "
    "--index-url https://download.pytorch.org/whl/cpu` — or embed against a server "
    "you're already running and install nothing: EMBED_BACKEND=lm_studio (the "
    "default) or EMBED_BACKEND=ollama.")

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_DIM = 384

_shared: dict[str, object] = {}            # model name -> loaded SentenceTransformer
_shared_lock = threading.Lock()
_encode_lock = threading.Lock()            # several mind loops can land here at once


def _load_shared(model_name: str):
    """The one process-wide SentenceTransformer for this model name."""
    with _shared_lock:
        model = _shared.get(model_name)
        if model is None:
            # Lazy import: torch is heavy; tests use a fake Embedder instead. This
            # is the one heavy backend with no fake to degrade into — her memory
            # can't silently run on nothing — so it fails loudly, and says how to
            # fix it both ways.
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as e:  # pragma: no cover — needs a torch-free env
                raise RuntimeError(_INSTALL_HINT) from e
            try:
                # A complete local cache: no etag checks, no hub chatter at all.
                model = SentenceTransformer(model_name, local_files_only=True)
            except Exception as offline_error:
                # First run, or a partial cache: go to the hub once, and every
                # later boot takes the offline path above.
                log.info("embeddings: %s is not fully cached (%s: %s) — downloading "
                         "from Hugging Face once; later starts load offline",
                         model_name, type(offline_error).__name__, offline_error)
                model = SentenceTransformer(model_name)
            _shared[model_name] = model
        return model


class SentenceTFEmbedder:
    def __init__(self, model_name: str = DEFAULT_MODEL, dim: int = DEFAULT_DIM):
        self._model = _load_shared(model_name)
        self.dim = dim
        # Renamed in sentence-transformers 5.x; read whichever this install has.
        dimension = getattr(self._model, "get_embedding_dimension", None) \
            or self._model.get_sentence_embedding_dimension
        actual = dimension()
        if actual != dim:
            raise ValueError(
                f"EMBED_DIM={dim} but {model_name} produces {actual}-d vectors — "
                "the index dimension is config, never hard-coded (§3); fix .env"
            )

    def embed(self, texts: list[str]) -> list[list[float]]:
        with _encode_lock:
            return self._model.encode(texts, normalize_embeddings=True).tolist()
