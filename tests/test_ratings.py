"""👍/👎 on her replies, and the export that joins them (SPEC §37).

The sidecar and Build #1's route had existed since the first build; nothing in
the world server could reach either. A room had no route to post to, and no id
to post: the corpus id lands on a line *after* the page drew it, and only on the
host's side. These pin the three seams that closed that gap — which lines the
host calls rateable, the route that resolves a transcript id to a corpus record,
and the export that reads the two files back as one.
"""
from __future__ import annotations

import json

import pytest

from yurios.app.conversation import ConversationLog
from yurios.app.corpus import (CorpusLogger, UnratableLine, export,
                               standing_ratings)
from yurios.app.sessions import SessionStore

pytest.importorskip("fastapi")
from starlette.testclient import TestClient                  # noqa: E402

from yurios.desktop.voice.backends.fakes import FakeBrain    # noqa: E402
from yurios.world.main import create_app                     # noqa: E402


def spy(rt) -> list[tuple[str, dict]]:
    """Every event the runtime publishes from here on, in order."""
    published: list[tuple[str, dict]] = []
    real = rt.hub.publish

    def publish(type_, payload, **kw):
        published.append((type_, dict(payload)))
        return real(type_, payload, **kw)

    rt.hub.publish = publish
    return published


def jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


# ---- the sidecar -----------------------------------------------------------

def test_the_last_word_on_a_turn_is_the_one_that_stands(tmp_path):
    corpus = CorpusLogger(tmp_path)
    corpus.log_rating("t1", 1)
    corpus.log_rating("t2", 1)
    corpus.log_rating("t1", -1)          # changed your mind
    corpus.log_rating("t2", 0)           # …and took this one back
    assert {k: v["thumbs"] for k, v in corpus.standing().items()} == {"t1": -1}
    # append-only: four lines, none rewritten
    assert [r["thumbs"] for r in jsonl(corpus.ratings)] == [1, 1, -1, 0]


def test_a_rating_is_a_thumb_and_nothing_else(tmp_path):
    corpus = CorpusLogger(tmp_path)
    for bad in (2, True, "1"):
        with pytest.raises(ValueError):
            corpus.log_rating("t1", bad)       # type: ignore[arg-type]
    assert not corpus.ratings.exists()


def test_a_torn_or_foreign_line_is_skipped(tmp_path):
    (tmp_path / "ratings.jsonl").write_text(
        '{"id": "t1", "thumbs": 1}\n'
        '{"id": "t2", "thumbs": true}\n'        # a bool is not a thumb
        '{"id": "t3", "thumbs": 1', encoding="utf-8")   # torn tail
    assert list(standing_ratings(tmp_path / "ratings.jsonl")) == ["t1"]


# ---- which lines are rateable ----------------------------------------------

@pytest.fixture
def rt(cfg):
    cfg = cfg.model_copy(update={"tools_backend": "off", "mind_enabled": False})
    return create_app(cfg, brain=FakeBrain()).state.rt


def store(rt) -> tuple[SessionStore, str]:
    """The brain's half of the same log — what files a corpus id on a line."""
    sessions = SessionStore(rt.cfg.vault_dir, rt.chatlog)
    return sessions, sessions.create()


def test_a_text_reply_becomes_rateable_once_its_turn_is_filed(rt):
    """The text order: drawn the instant the stream ends, filed a moment later.
    The page learns from a `rating` event, not from a reload."""
    sessions, sid = store(rt)
    events = spy(rt)
    entry = rt.post_message("assistant", "It's by the door.", session_id=sid)
    assert "rateable" not in entry               # nothing to join a rating to yet

    sessions.append_message(sid, "assistant", "[warm] It's by the door.",
                            turn_id="turn-1")
    assert rt.turn_filed(entry["id"]) is True
    assert events[-1] == ("rating", {"id": entry["id"], "thumbs": 0})
    assert rt.history()["messages"][-1]["rateable"] is True


def test_a_voice_reply_is_rateable_the_moment_it_lands(rt):
    """The voice order: the brain files the turn before the socket posts it, so
    the `message` event can say so itself."""
    sessions, sid = store(rt)
    sessions.append_message(sid, "assistant", "[happy] Morning.", turn_id="turn-2")
    entry = rt.post_message("assistant", "Morning.", channel="voice", session_id=sid)
    assert entry["rateable"] is True
    assert len(rt.chatlog.entries()) == 1        # still one line, not two


@pytest.mark.parametrize("line", [
    {"role": "assistant", "text": "Oh, there you are.", "proactive": True},  # greeting
    {"role": "assistant", "text": "", "image_url": "/selfies/a.png"},         # a selfie
    {"role": "user", "text": "hi"},                                           # yours
])
def test_a_line_with_no_record_behind_it_cannot_be_rated(rt, line):
    entry = rt.post_message(line.pop("role"), line.pop("text"), **line)
    assert rt.turn_filed(entry["id"]) is False
    assert "rateable" not in rt.history()["messages"][-1]
    with pytest.raises(UnratableLine):
        rt.rate(entry["id"], 1)
    assert not (rt.cfg.corpus_dir / "ratings.jsonl").exists()


def test_a_rating_is_filed_against_the_turn_and_drawn_on_the_line(rt):
    sessions, sid = store(rt)
    entry = rt.post_message("assistant", "Tea?", session_id=sid)
    sessions.append_message(sid, "assistant", "Tea?", turn_id="turn-3")
    events = spy(rt)

    assert rt.rate(entry["id"], 1) == {"id": entry["id"], "thumbs": 1}
    assert events == [("rating", {"id": entry["id"], "thumbs": 1})]
    assert [(r["id"], r["thumbs"]) for r in
            jsonl(rt.cfg.corpus_dir / "ratings.jsonl")] == [("turn-3", 1)]
    assert rt.history()["messages"][-1]["thumbs"] == 1

    rt.rate(entry["id"], 0)                      # taken back
    assert "thumbs" not in rt.history()["messages"][-1]
    assert rt.history()["messages"][-1]["rateable"] is True


def test_a_rating_never_touches_the_conversation_or_the_vault(rt):
    """§37.3: about her, not to her. Nothing she reads next turn changes."""
    sessions, sid = store(rt)
    entry = rt.post_message("assistant", "Tea?", session_id=sid)
    sessions.append_message(sid, "assistant", "Tea?", turn_id="turn-4")
    before = rt.chatlog.path.read_bytes()
    rt.rate(entry["id"], -1)
    assert rt.chatlog.path.read_bytes() == before
    assert not list(rt.cfg.vault_dir.rglob("ratings.jsonl"))


# ---- over the wire ---------------------------------------------------------

class FilingBrain(FakeBrain):
    """FakeBrain, plus the one thing its persist leaves out: filing the reply's
    corpus id on the line the runtime drew, the way `SessionStore` does."""

    def __init__(self):
        super().__init__(reply="Ten minutes. I'll call you.")
        self.log: ConversationLog | None = None

    async def persist(self, session_id, user_text, reply):
        row = self.log.pending(session_id, "assistant") if self.log else None
        if row is not None:
            self.log.attach_raw(row["id"], reply, f"turn-{len(self.persist_calls)}")
        return await super().persist(session_id, user_text, reply)


@pytest.fixture
def client(cfg):
    cfg = cfg.model_copy(update={"tools_backend": "off", "mind_enabled": False})
    brain = FilingBrain()
    app = create_app(cfg, brain=brain)
    brain.log = app.state.rt.chatlog
    with TestClient(app) as c:
        c.app = app
        yield c


def test_a_text_turn_ends_rateable_and_the_route_rates_it(client):
    rt = client.app.state.rt
    events = spy(rt)
    reply = client.post("/api/chat", json={"text": "tea timer?"}).json()["message"]
    assert ("rating", {"id": reply["id"], "thumbs": 0}) in events

    r = client.post("/api/rate", json={"id": reply["id"], "thumbs": 1})
    assert r.status_code == 200 and r.json() == {"rating": {"id": reply["id"],
                                                            "thumbs": 1}}
    row = client.get("/api/history").json()["messages"][-1]
    assert (row["id"], row["rateable"], row["thumbs"]) == (reply["id"], True, 1)
    assert jsonl(rt.cfg.corpus_dir / "ratings.jsonl")[-1]["id"] == "turn-0"


def test_the_route_refuses_what_it_cannot_file(client):
    rt = client.app.state.rt
    mine = client.post("/api/chat", json={"text": "hi"}).json()["user_message"]
    assert client.post("/api/rate", json={"id": "nosuchline", "thumbs": 1}
                       ).status_code == 404
    assert client.post("/api/rate", json={"id": mine["id"], "thumbs": 1}
                       ).status_code == 409
    for bad in ({"id": mine["id"], "thumbs": True}, {"id": mine["id"], "thumbs": 2},
                {"id": "../x", "thumbs": 1}):
        assert client.post("/api/rate", json=bad).status_code == 422
    assert not (rt.cfg.corpus_dir / "ratings.jsonl").exists()


# ---- the export ------------------------------------------------------------

@pytest.fixture
def corpus(tmp_path):
    logger = CorpusLogger(tmp_path / "corpus")
    ids = [logger.log_turn(session_id="s", turn_index=i,
                           messages=[{"role": "system", "content": "be yourself"},
                                     {"role": "user", "content": f"q{i}"}],
                           completion=f"a{i}", model="m", card_version="yuri-v1@x")
           for i in range(3)]
    logger.log_rating(ids[0], 1)
    logger.log_rating(ids[1], 1)
    logger.log_rating(ids[1], -1)                # the last word wins
    logger.log_rating("gone", 1)                 # a turn the log no longer holds
    return logger.dir, ids


def test_raw_is_every_turn_with_the_rating_joined_in(corpus):
    path, ids = corpus
    rows, stats = export(path)
    assert [r["id"] for r in rows] == ids
    assert [r.get("rating", {}).get("thumbs") for r in rows] == [1, -1, None]
    assert rows[0]["completion"] == "a0"         # the record itself, verbatim
    assert stats == {"turns": 3, "up": 1, "down": 1, "orphaned": 1}
    assert [r["id"] for r in export(path, rated_only=True)[0]] == ids[:2]


def test_kto_is_the_rated_turns_as_preference_data(corpus):
    path, ids = corpus
    rows, _ = export(path, fmt="kto")
    assert rows == [
        {"id": ids[0], "prompt": [{"role": "system", "content": "be yourself"},
                                  {"role": "user", "content": "q0"}],
         "completion": [{"role": "assistant", "content": "a0"}], "label": True},
        {"id": ids[1], "prompt": [{"role": "system", "content": "be yourself"},
                                  {"role": "user", "content": "q1"}],
         "completion": [{"role": "assistant", "content": "a1"}], "label": False}]


def test_the_script_reads_and_writes_nothing_beside_the_corpus(corpus, tmp_path,
                                                               capsys):
    import importlib.util
    from pathlib import Path
    script = Path(__file__).resolve().parents[1] / "scripts" / "export_corpus.py"
    spec = importlib.util.spec_from_file_location("export_corpus", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    path, ids = corpus
    before = sorted(p.name for p in path.iterdir())
    out = tmp_path / "out.jsonl"
    assert module.main(["--corpus", str(path), "--format", "kto",
                        "-o", str(out)]) == 0
    assert [r["label"] for r in jsonl(out)] == [True, False]
    assert sorted(p.name for p in path.iterdir()) == before
    assert "1 👍, 1 👎" in capsys.readouterr().err
