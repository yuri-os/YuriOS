"""The corpus logger (SPEC §8) — capture the corpus from day one.

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
`ratings.jsonl` — 👍/👎 sidecar, keyed by turn id; merged at export.
`utility.jsonl` — debug sidecar: one line per utility-model call (what it
    proposed for USER.md / the summary, and how triage handled it). Not training
    data; not read by export. Peek it: `cat utility.jsonl`.
Personal data — never commit, never share. Export: `python scripts/export_corpus.py`.
"""


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
        """Append-only sidecar (§8.1); merged at export. These ratings are the
        KTO/DPO asset (→ ch. 20)."""
        self._ensure_dir()
        with self.ratings.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"id": turn_id, "thumbs": thumbs, "by": by,
                                "timestamp": datetime.datetime.now(
                                    datetime.UTC).isoformat()}) + "\n")


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
