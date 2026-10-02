"""The corpus logger (B1 §8, SPEC §2.1, §37) — capture the corpus from day one.

Every generated reply appends one faithful record to an append-only JSONL log —
the ONLY place raw, trainable conversation data is kept (the index is derived
and lossy). It is the seed of the eventual distillation corpus (→ ch. 20,
ch. 30): costs almost nothing now, cannot be reconstructed later.

`corpus/` is personal data, not code: gitignored, outside the Vault, on owned
hardware, never committed. It is your training asset, not part of her mind.
"""
from __future__ import annotations

import datetime
import json
import pathlib
import uuid

README = """\
# corpus/ — the conversation corpus (Appendix D schema)

Written by: Build #1 (minimum-viable-waifu), from {date}.
`turns.jsonl` — one line per assistant reply (append-only).
`ratings.jsonl` — 👍/👎 sidecar, keyed by turn id; last line wins, 0 takes
    a rating back; merged at export.
`utility.jsonl` — debug sidecar: one line per utility-model call (what it
    proposed for USER.md / the summary, and how triage handled it). Not training
    data; not read by export. Peek it: `cat utility.jsonl`.
Personal data — never commit, never share. Export: `python scripts/export_corpus.py <character>`.
"""


#: What a rating may say: up, down, or "I take that back" (SPEC §37.3).
THUMBS = (1, -1, 0)


class UnratableLine(Exception):
    """A line with no corpus record behind it (SPEC §37.1) — said, but not a
    reply she made in a turn, so there is nothing a rating could be joined to."""


def read_jsonl(path: pathlib.Path) -> list[dict]:
    """Every whole record in one of these logs, oldest first. A torn tail line
    — a crash mid-append — is skipped, the same as every other log here."""
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def standing_ratings(path: pathlib.Path) -> dict[str, dict]:
    """turn id -> the rating that stands on it (SPEC §37.3): last line wins and
    a `0` removes it. The whole file, read in one pass — these lines are tiny,
    and a page of history wants ratings for turns from anywhere in it."""
    out: dict[str, dict] = {}
    for row in read_jsonl(path):
        turn_id, thumbs = row.get("id"), row.get("thumbs")
        if not isinstance(turn_id, str) or isinstance(thumbs, bool) \
                or thumbs not in THUMBS:
            continue
        if thumbs == 0:
            out.pop(turn_id, None)
        else:
            out[turn_id] = row
    return out


class CorpusLogger:
    def __init__(self, corpus_dir: pathlib.Path):
        self.dir = pathlib.Path(corpus_dir)
        self.turns = self.dir / "turns.jsonl"
        self.ratings = self.dir / "ratings.jsonl"

    def _ensure_dir(self) -> None:
        if not self.dir.exists():
            self.dir.mkdir(parents=True)
            (self.dir / "README.md").write_text(
                README.format(date=datetime.date.today().isoformat()),
                encoding="utf-8")

    def log_turn(self, *, session_id: str, turn_index: int, messages: list[dict],
                 completion: str, model: str, card_version: str,
                 model_role: str = "production", source: str = "live_play",
                 collection_scope: str = "self", companion: str = "yuri",
                 **optional) -> str:
        """Called once per reply (§8.2). Returns the record id — ratings that
        arrive later key to it (never patched into the line)."""
        # the sovereignty boundary, in code (§8.4): there is NO value that means
        # "a downloader's data" — a shipped card never logs a stranger's chat home.
        assert collection_scope in ("self", "consented_hosted")
        rec = {"id": str(uuid.uuid4()), "session_id": session_id,
               "turn_index": turn_index,
               "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
               "companion": companion, "messages": messages,
               "completion": completion, "model": model,
               "model_role": model_role, "source": source,
               "collection_scope": collection_scope, "card_version": card_version,
               **{k: v for k, v in optional.items() if v is not None}}
        self._ensure_dir()
        with self.turns.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec["id"]

    def log_rating(self, turn_id: str, thumbs: int, by: str = "user") -> None:
        """Append-only sidecar (B1 §8.1, SPEC §37.3); merged at export. These
        ratings are the KTO/DPO asset (→ ch. 20). `0` takes a rating back — a
        line of its own rather than an edit, so the file stays append-only and
        the last line about a turn is the one that stands."""
        if isinstance(thumbs, bool) or thumbs not in THUMBS:
            raise ValueError(f"thumbs is one of {THUMBS}, not {thumbs!r}")
        self._ensure_dir()
        with self.ratings.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"id": turn_id, "thumbs": thumbs, "by": by,
                                "timestamp": datetime.datetime.now(
                                    datetime.UTC).isoformat()}) + "\n")

    def standing(self) -> dict[str, dict]:
        """Every rating that currently stands, by turn id."""
        return standing_ratings(self.ratings)


#: What the export can write (SPEC §37.4).
EXPORT_FORMATS = ("raw", "kto")


def export(corpus_dir: pathlib.Path, *, fmt: str = "raw",
           rated_only: bool = False) -> tuple[list[dict], dict]:
    """The corpus with its ratings joined in (SPEC §37.4) — rows, and a count of
    what went into them.

    `raw` is every turn record verbatim, plus a `rating` object on the ones a
    rating stands on. `kto` is the rated turns only, as preference data:
    `prompt` (the messages as sent), `completion` (her reply as one assistant
    message) and `label` (true for 👍) — the conversational shape TRL's KTO
    trainer reads. `rated_only` narrows `raw` the same way.

    Reads only. A rating whose turn the log no longer holds is counted, not
    guessed at: there is nothing to join it to.
    """
    if fmt not in EXPORT_FORMATS:
        raise ValueError(f"format is one of {EXPORT_FORMATS}, not {fmt!r}")
    corpus_dir = pathlib.Path(corpus_dir)
    turns = read_jsonl(corpus_dir / "turns.jsonl")
    standing = standing_ratings(corpus_dir / "ratings.jsonl")
    seen = {t.get("id") for t in turns}
    stats = {"turns": len(turns), "up": 0, "down": 0,
             "orphaned": sum(1 for turn_id in standing if turn_id not in seen)}
    rows: list[dict] = []
    for turn in turns:
        rating = standing.get(str(turn.get("id")))
        if rating:
            stats["up" if rating["thumbs"] > 0 else "down"] += 1
        if rating is None and (rated_only or fmt == "kto"):
            continue
        if fmt == "kto":
            assert rating is not None
            rows.append({"id": turn["id"], "prompt": turn.get("messages") or [],
                         "completion": [{"role": "assistant",
                                         "content": turn.get("completion") or ""}],
                         "label": rating["thumbs"] > 0})
        else:
            rows.append({**turn, **({"rating": {k: rating[k] for k in
                                                ("thumbs", "by", "timestamp")
                                                if k in rating}}
                                    if rating else {})})
    return rows, stats


class UtilityLogger:
    """Transparency sidecar for the utility model (§6.3, → ch. 05, ch. 19).

    The utility model quietly decides what enters USER.md and what the summary
    says — decisions about the user's *own* files, made unobserved. This logs
    one line per utility call (extraction + summarisation) so those decisions
    are peekable: `cat corpus/utility.jsonl` answers "what did the model
    propose, and why did this fact land / that one not?".

    Debug/observability data, not training data — but it lives in `corpus/`
    because that is already the gitignored, owned, never-shared bucket. The
    export script only reads turns/ratings, so this file is ignored there too.
    """

    def __init__(self, corpus_dir: pathlib.Path):
        self.dir = pathlib.Path(corpus_dir)
        self.path = self.dir / "utility.jsonl"

    def log(self, *, kind: str, **fields) -> None:
        """Append one record. `kind` is "extract" | "summarise"; the rest of the
        payload (raw_reply, parsed/applied/quarantined ops, inputs) is caller-
        supplied. Best-effort: never raise into the post-turn pipeline."""
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            rec = {"timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
                   "kind": kind, **fields}
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:  # a debug log must never break the turn it observes
            pass
