#!/usr/bin/env python
"""Exercise the real local embedder — the one core dependency pytest cannot see.

Every other heavy backend is optional and has a fake to degrade into, so a
green suite says something about all of them. `sentence-transformers` is
neither: her memory is built on it, it has no fake by design (§3 —
"her memory can't silently run on nothing"), and the suite therefore replaces
it with `FakeEmbedder` in every single test. Which means a
sentence-transformers release can change the shape of the thing her index is
made of and `./scripts/check.sh` stays green from here to the horizon.

That is not hypothetical: 5.x renamed `get_sentence_embedding_dimension` to
`get_embedding_dimension`, and `sentence_tf.py` carries a `getattr` for both
because of it. `pin_deps.sh` will happily write a new major into
`constraints.txt` — resolving is all it checks.

So: run this after moving `sentence-transformers`, `transformers`,
`huggingface-hub` or `torch`.

    python scripts/check_embedder.py            # the cached path
    python scripts/check_embedder.py --cold     # …and a first-ever download

It downloads ~130 MB on a cold cache and is deliberately not a gate stage.
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def check() -> None:
    import huggingface_hub
    import sentence_transformers
    import torch
    import transformers

    print(f"sentence-transformers {sentence_transformers.__version__} · "
          f"transformers {transformers.__version__} · "
          f"huggingface-hub {huggingface_hub.__version__} · torch {torch.__version__}")

    from yurios.app.providers.sentence_tf import (
        DEFAULT_DIM, DEFAULT_MODEL, SentenceTFEmbedder)

    embedder = SentenceTFEmbedder(DEFAULT_MODEL, DEFAULT_DIM)
    vectors = embedder.embed(["the kettle is on",
                              "she remembers the kettle",
                              "quantum chromodynamics"])

    assert len(vectors) == 3, f"asked for 3 vectors, got {len(vectors)}"
    for vector in vectors:
        assert len(vector) == DEFAULT_DIM, \
            f"{DEFAULT_MODEL} returned {len(vector)}-d, not {DEFAULT_DIM}-d"
        # `embed` asks for normalised vectors and recall's cosine assumes it
        length = math.sqrt(sum(x * x for x in vector))
        assert abs(length - 1.0) < 1e-3, f"vector is not normalised: |v| = {length}"

    def cosine(a, b):
        return sum(x * y for x, y in zip(a, b))

    near, far = cosine(vectors[0], vectors[1]), cosine(vectors[0], vectors[2])
    print(f"  {DEFAULT_DIM}-d, normalised · related {near:.3f} > unrelated {far:.3f}")
    assert near > far + 0.15, \
        "the two sentences about a kettle are no closer than one about physics — " \
        "the space still has the right shape but no longer the right meaning"

    # one model per name, process-wide: three residents must not be three loads
    again = SentenceTFEmbedder(DEFAULT_MODEL, DEFAULT_DIM)
    assert again._model is embedder._model, \
        "the process-wide share broke — every character will load its own copy"

    # and the dimension guard, which is what stands between a config typo and
    # an index quietly full of the wrong-shaped vectors
    try:
        SentenceTFEmbedder(DEFAULT_MODEL, DEFAULT_DIM * 2)
    except ValueError:
        pass
    else:
        raise AssertionError("the EMBED_DIM guard did not fire on a wrong dim — "
                             "the model no longer reports its dimension where "
                             "sentence_tf.py looks for it")
    print("  shared instance · EMBED_DIM guard · ok")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cold", action="store_true",
                    help="use a throwaway HF cache: exercises the download path "
                         "a first-ever boot takes, not the offline one")
    args = ap.parse_args()

    if args.cold:
        # `local_files_only=True` must *fail* here and fall through to the hub.
        # That fallback is the whole first-run experience and it is the half a
        # warm machine never runs again.
        with tempfile.TemporaryDirectory(prefix="yurios-hf-") as cache:
            os.environ["HF_HOME"] = cache
            os.environ["HF_HUB_CACHE"] = cache
            print(f"cold cache: {cache}")
            check()
    else:
        check()
    print("the embedder is fine")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
