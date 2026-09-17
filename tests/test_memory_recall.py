"""`FileMemoryStore.recall` — the retrieval contract, on its own.

It had no test at all, which was survivable while one caller used it and is not
now that goal work asks it the same questions the conversation does (§22.4).

Note what is and is not asserted here. The similarity floor and the tombstone
filter decide *membership*, and both are pinned. Final *order* is MMR's — which
now ranks on the blended `similarity * salience * recency` it is handed, so
tuning `half_life_days` does move the list. It used to re-score from raw
similarity and throw that sort away, which is how a fifteen-day-old line took a
slot from an answer an hour old; `test_recency_decides_which_of_two_answers`
is that bug, pinned.
"""
from __future__ import annotations

import datetime

from yurios.app import vaultgit
from yurios.app.memory.store import FileMemoryStore

from .conftest import FakeEmbedder


def _store(tmp_path) -> FileMemoryStore:
    (tmp_path / "memory" / "semantic").mkdir(parents=True)
    (tmp_path / "soul").mkdir(parents=True)
    return FileMemoryStore(tmp_path, FakeEmbedder(), embed_dim=32)


def _put(store, id_: str, text: str, *, days_ago: float = 0.0,
         salience: float = 1.0) -> None:
    when = (datetime.datetime.now(datetime.UTC)
            - datetime.timedelta(days=days_ago))
    store.index.upsert(id=id_, kind="turn", text=text, source_path="t",
                       source_span="", created_at=when.isoformat(),
                       salience=salience,
                       embedding=store.embedder.embed([text])[0])


def test_an_empty_vault_recalls_nothing_rather_than_raising(tmp_path):
    assert _store(tmp_path).recall("anything at all", 6) == []


def test_recall_is_empty_while_the_embedder_is_still_loading(tmp_path):
    """A cold sentence-transformers load must not freeze the event loop
    (SPEC §2.4): until the weights land, recall is the empty-Vault path."""
    class Loading:
        ready = False
        dim = 32

        def embed(self, texts):
            raise AssertionError("recall must not wait on a load")

    (tmp_path / "memory" / "semantic").mkdir(parents=True)
    (tmp_path / "soul").mkdir(parents=True)
    store = FileMemoryStore(tmp_path, Loading(), embed_dim=32)
    store.index.upsert(id="x", kind="turn", text="the good knives",
                       source_path="t", source_span="",
                       created_at="2026-01-01T00:00:00+00:00",
                       salience=1.0, embedding=[1.0] + [0.0] * 31)
    assert store.recall("knives", 6) == []


def test_the_floor_drops_the_merely_unrelated(tmp_path):
    store = _store(tmp_path)
    _put(store, "near", "the good knives are in the second drawer")
    _put(store, "far", "trombones")
    got = [m.text for m in store.recall("where are the good knives", 6)]
    assert any("knives" in t for t in got)
    assert "trombones" not in got, \
        "retrieval_min_sim is what keeps the prompt from filling with noise"


def test_recall_diversifies_instead_of_returning_k_paraphrases(tmp_path):
    """MMR earns its place: the small load-bearing detail beats a third
    restatement of the thing she already said."""
    store = _store(tmp_path)
    _put(store, "a", "the good knives are in the second drawer")
    _put(store, "b", "the good knives are in the second drawer indeed")
    _put(store, "c", "the knives want sharpening before the good dinner")
    got = [m.text for m in store.recall("where are the good knives", 2)]
    assert len(got) == 2
    assert any("sharpening" in t for t in got), \
        "two paraphrases crowded out the one new fact"


def test_an_old_memory_fades_but_does_not_vanish(tmp_path):
    """Recency is a weight, not a cutoff — §6.4. Something from six months ago
    is still recallable when nothing newer answers the question."""
    store = _store(tmp_path)
    _put(store, "old", "the good knives are in the second drawer", days_ago=200)
    got = store.recall("where are the good knives", 6)
    assert [m.text for m in got] == ["the good knives are in the second drawer"]
    assert got[0].score < got[0].similarity, "the decay was not applied at all"


def test_a_tombstoned_memory_is_gone_from_every_future_prompt(tmp_path):
    store = _store(tmp_path)
    vaultgit.ensure_repo(tmp_path)          # forget() commits the tombstone
    _put(store, "secret", "the good knives are in the second drawer")
    assert store.recall("where are the good knives", 6)
    store.forget("the good knives are in the second drawer")
    assert not [m for m in store.recall("where are the good knives", 6)
                if "knives" in m.text], "forgetting has to reach recall"


def test_recency_decides_which_of_two_answers_is_recalled(tmp_path):
    """MMR chooses on the blend, not raw similarity.

    Two memories answer the question equally well and only their age differs.
    While `_mmr` re-scored from `similarity`, the sort by
    `similarity * salience * recency` above it was dead code and the older one
    could win — recency was computed, reported on `Memory.score`, and never
    allowed to decide anything.
    """
    store = _store(tmp_path)
    _put(store, "stale", "the good knives are in the second drawer",
         days_ago=120)
    _put(store, "fresh", "the good knives are in the second drawer",
         days_ago=0)
    got = store.recall("where are the good knives", 1)
    assert len(got) == 1
    age_days = (datetime.datetime.now(datetime.UTC)
                - datetime.datetime.fromisoformat(got[0].created_at)).days
    assert age_days < 1, \
        f"recall returned the {age_days}-day-old copy over today's"


def test_the_answer_to_a_question_is_not_buried_as_a_duplicate_of_it(tmp_path):
    """The shape that cost a promised photograph.

    She asks something; they answer; the two turns are near-identical in
    embedding space *because* one answers the other. Recall is then probed with
    text containing the question, so the question's own turn scores best — and
    MMR docks the answer for being a duplicate of it. Caller-side, goal work
    drops the echo (mind/goalwork.py); here the floor is simply that both can
    be returned when there is room for both.
    """
    store = _store(tmp_path)
    _put(store, "asked", "do you want the private one or the one that hangs")
    _put(store, "answered", "I want the private one, only I can see it")
    got = [m.text for m in store.recall(
        "do you want the private one or the one that hangs", 2)]
    assert any("I want the private one" in t for t in got), \
        f"the reply to the question never came back: {got}"
