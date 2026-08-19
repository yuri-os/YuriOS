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


def _out_of_memory(error: BaseException) -> bool:
    """Is this "the card is full" rather than "the files aren't here"?

    Matched by name and text rather than by catching torch.OutOfMemoryError,
    because importing torch to name an exception type would undo the lazy import
    that keeps it out of every process that never embeds anything."""
    return (type(error).__name__ == "OutOfMemoryError"
            or "out of memory" in str(error).lower())


def _load(SentenceTransformer, model_name: str, **kwargs):
    """Load the model, and put it on the CPU if the GPU has no room for it.

    sentence-transformers grabs cuda:0 whenever there is a CUDA build present,
    and on a single-card machine that card is already holding her chat model —
    a 27B fills 14 of 15 GiB, and then 46 MiB of embedder is what fails. The
    weights are 130 MB and the index is 384-d: the CPU runs them fine, which
    makes "no room on the GPU" a placement decision rather than a failed boot.
    Her memory is what the whole runtime is built on, so it must not be the
    thing that can't start."""
    try:
        return SentenceTransformer(model_name, **kwargs)
    except Exception as e:
        if not _out_of_memory(e):
            raise
        log.warning("embeddings: no room on the GPU for %s (%s) — loading it on "
                    "the CPU instead; the card stays with the model she talks "
                    "with", model_name, e)
        return SentenceTransformer(model_name, device="cpu", **kwargs)


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
                model = _load(SentenceTransformer, model_name, local_files_only=True)
            except Exception as offline_error:
                # First run, or a partial cache: go to the hub once, and every
                # later boot takes the offline path above. Only reasons the local
                # cache can't answer reach here — a full card was already dealt
                # with, and downloading the weights again would not have freed a
                # byte of it.
                log.info("embeddings: %s is not fully cached (%s: %s) — downloading "
                         "from Hugging Face once; later starts load offline",
                         model_name, type(offline_error).__name__, offline_error)
                model = _load(SentenceTransformer, model_name)
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
