"""Local Embedder via sentence-transformers (SPEC §2.4).

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

The load itself is the slow part of a cold boot (a torch model, ~20 s alone in
a process and a good deal more with five characters building around it) and
**MUST NOT** hold up the rest of her (SPEC §2.4): `begin_load` kicks the thread
off and returns, `embed()` waits, and a recall that arrives before the weights
do behaves as an empty Vault rather than freezing the event loop.

Most of that time is `import sentence_transformers`, not the weights — which is
why *when* the thread starts matters as much as that it is one. CPython holds a
lock per module being imported, so a second thread importing anything the same
chain touches blocks until the first is done. Kicked off from the constructor,
this raced the build's own `import litellm` and the build waited on it; started
after, both run alone. `world/runtime.build_brain` owns that ordering.
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
_done: dict[str, threading.Event] = {}     # set when that load finished (ok or fail)
_errors: dict[str, BaseException] = {}
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


def reset_shared() -> None:
    """Drop the process-wide model cache. Tests only."""
    with _shared_lock:
        _shared.clear()
        _errors.clear()
        _done.clear()


def begin_load(model_name: str) -> None:
    """Start loading this model if nobody has. Returns immediately (SPEC §2.4).

    The first caller in the process starts the thread; everyone else is a
    no-op. ``ensure_ready`` and ``embed`` both call it, so nothing that needs
    a vector has to remember to; construction with ``wait=False`` deliberately
    does *not*, because it is what decides when the import runs (see below).
    """
    with _shared_lock:
        if model_name in _shared or model_name in _done:
            return
        done = threading.Event()
        _done[model_name] = done
    threading.Thread(
        target=_load_in_background, args=(model_name, done),
        daemon=True, name=f"embed-{model_name.split('/')[-1]}",
    ).start()


def _load_in_background(model_name: str, done: threading.Event) -> None:
    try:
        model = _load_model(model_name)
        with _shared_lock:
            if _done.get(model_name) is done:
                _shared[model_name] = model
    except Exception as e:
        with _shared_lock:
            if _done.get(model_name) is done:
                _errors[model_name] = e
        log.exception("embeddings: failed to load %s", model_name)
    finally:
        done.set()


def _load_model(model_name: str):
    """The actual SentenceTransformer construction. No lock held.

    Lazy import: torch is heavy; tests use a fake Embedder instead. This is
    the one heavy backend with no fake to degrade into — her memory can't
    silently run on nothing — so it fails loudly, and says how to fix it
    both ways.
    """
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as e:  # pragma: no cover — needs a torch-free env
        raise RuntimeError(_INSTALL_HINT) from e
    try:
        # A complete local cache: no etag checks, no hub chatter at all.
        return _load(SentenceTransformer, model_name, local_files_only=True)
    except Exception as offline_error:
        # First run, or a partial cache: go to the hub once, and every later
        # boot takes the offline path above. Only reasons the local cache
        # can't answer reach here — a full card was already dealt with, and
        # downloading the weights again would not have freed a byte of it.
        log.info("embeddings: %s is not fully cached (%s: %s) — downloading "
                 "from Hugging Face once; later starts load offline",
                 model_name, type(offline_error).__name__, offline_error)
        return _load(SentenceTransformer, model_name)


def wait_loaded(model_name: str):
    """The one process-wide SentenceTransformer for this model name.

    Starts the load if needed, then blocks until it finishes. Raises whatever
    the loader raised.
    """
    begin_load(model_name)
    with _shared_lock:
        done = _done.get(model_name)
    if done is not None:
        done.wait()
    with _shared_lock:
        error = _errors.get(model_name)
        if error is not None:
            raise error
        return _shared[model_name]


class SentenceTFEmbedder:
    def __init__(self, model_name: str = DEFAULT_MODEL, dim: int = DEFAULT_DIM,
                 *, wait: bool = True):
        self.model_name = model_name
        self.dim = dim
        self._model = None
        if wait:
            self.ensure_ready()
        # `wait=False` leaves this inert: no thread, no import. The caller says
        # when — `world/runtime.build_brain` does it once the brain's own
        # imports are done, because two threads importing at the same time
        # serialise on CPython's per-module lock and the build ends up paying
        # for the load anyway (SPEC §2.4).

    @property
    def ready(self) -> bool:
        """True when ``embed()`` will not wait on a load.

        False while the weights are still coming, and false if the load
        failed — a recall that checks this treats both as an empty Vault
        rather than raising or freezing the event loop (SPEC §2.4).
        """
        if self._model is not None:
            return True
        with _shared_lock:
            return self.model_name in _shared

    def ensure_ready(self) -> None:
        """Block until the shared model is loaded, then bind and check dim."""
        if self._model is not None:
            return
        model = wait_loaded(self.model_name)
        # Renamed in sentence-transformers 5.x; read whichever this install has.
        dimension = getattr(model, "get_embedding_dimension", None) \
            or model.get_sentence_embedding_dimension
        actual = dimension()
        if actual != self.dim:
            raise ValueError(
                f"EMBED_DIM={self.dim} but {self.model_name} produces {actual}-d "
                "vectors — the index dimension is config, never hard-coded "
                "(§2.4); fix .env"
            )
        self._model = model

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.ensure_ready()
        model = self._model
        assert model is not None
        with _encode_lock:
            return model.encode(texts, normalize_embeddings=True).tolist()
