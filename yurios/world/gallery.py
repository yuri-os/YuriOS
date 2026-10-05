"""Her gallery (SPEC §7.6) — every picture she has taken, and what you made of it.

Nothing here renders anything. The shelf is already written by the time this
module is asked about it: `world/selfies.py` saves the PNG and the forge writes
the ledger line beside it (`forge/service.py::_write_provenance`), so a gallery
is a *reader* over `generations.jsonl` plus one append-only sidecar of its own.

Two rules, both borrowed from surfaces that already work here:

  * Paging is newest-first over the ledger (`mind/util.jsonl_page`), never a
    directory read into memory. The shelf grows forever, and the thousandth
    shot must cost what the tenth did. It is the same reason the panel loads
    nothing until you open the tab — a room you are not looking at should not
    be pulling a hundred PNGs down a socket.
  * A score is an append-only sidecar keyed by file name, exactly like the
    turn ratings in `app/corpus.py`. A judgement that arrives long after the
    thing it judges is never patched into the record of how that thing was
    made: `generations.jsonl` stays provenance, `ratings.jsonl` is opinion,
    the last line for a name wins, and `null` clears it again.

Why record quality at all. Her camera has a dozen knobs — backend, checkpoint,
sampler, the slot rows in her selfie library — and until now no way back from
"that one came out badly" to the settings that made it. A score lands beside
the seed and the prompt that produced it, so *which of these actually takes a
good picture* becomes a question the ledger can answer.
"""
from __future__ import annotations

import datetime
import json
import os
import threading
from collections import Counter
from pathlib import Path
from typing import Any

from yurios.mind.util import jsonl_append, jsonl_page, jsonl_read

#: The forge's own ledger — one line per render, in the shelf directory.
LEDGER = "generations.jsonl"
#: Ours, beside it — one line per rating, keyed by image name.
RATINGS = "ratings.jsonl"
#: …and the owner's word that they have seen a picture outside the chat — in
#: the gallery, say — so it is off her list of pictures not yet sent (§18.2a).
SEEN = "seen.jsonl"

#: A page of tiles. Small on purpose: a thumbnail is the full-size PNG, and
#: twelve of them is already more than a 352px chat column shows at once.
DEFAULT_LIMIT = 12
MAX_LIMIT = 60

#: Out of ten, the way the ask was phrased. `None` is "no opinion", not zero.
SCORE_MIN, SCORE_MAX = 1, 10


class UnknownShot(ValueError):
    """A name that is not a file on this shelf."""


def resolve(directory: str | Path, name: str) -> Path:
    """One image on the shelf, by bare file name.

    The same pinning `/selfies/{name}` uses (routes/events.py): the shelf is
    flat, so anything whose parent is not the shelf is not on it — which covers
    every path a `..` could otherwise walk to.
    """
    base = Path(directory).resolve()
    path = (base / name).resolve()
    if not name or path.parent != base or not path.is_file():
        raise UnknownShot(name)
    return path


def _score(value: Any) -> int | None:
    """A stored score, or None for anything that isn't one — a hand-edited
    sidecar line should unrate a shot, never crash the panel that reads it."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if SCORE_MIN <= value <= SCORE_MAX else None


def ratings(directory: str | Path) -> dict[str, dict]:
    """Every rated image's current score: last line wins, `null` unrates.

    Read whole rather than paged. These lines are tiny, and a page of tiles
    needs scores for images from anywhere in the ledger — not just the newest
    page of it.
    """
    out: dict[str, dict] = {}
    for row in jsonl_read(Path(directory) / RATINGS):
        name = row.get("image")
        if not isinstance(name, str) or not name:
            continue
        score = _score(row.get("score"))
        if score is None:
            out.pop(name, None)
        else:
            out[name] = {"score": score, "rated_at": row.get("at"),
                         "by": row.get("by")}
    return out


def rate(directory: str | Path, name: str, score: int | None,
         *, by: str = "user") -> dict:
    """Record what you thought of one shot. Appends; never rewrites a line.

    Refuses a score for a name that is not on the shelf — a rating nothing can
    ever be joined back to is worse than no rating.
    """
    resolve(directory, name)
    if score is not None and _score(score) is None:
        raise ValueError(f"a score is {SCORE_MIN}–{SCORE_MAX}, "
                         f"or null to clear it")
    row = {"image": name, "score": score, "by": by,
           "at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")}
    jsonl_append(Path(directory) / RATINGS, row)
    return row


def _caption(row: dict) -> str:
    """Her line about the shot — her own words first, then the slots she named.

    The same order `selfies.py` announces a finished render in, so the tile and
    the sentence in the transcript describe the picture the same way.
    """
    chosen = row.get("template")
    if not isinstance(chosen, dict):
        return ""
    look = chosen.get("look")
    if look:
        return str(look)
    return ", ".join(str(value) for value in
                     (chosen.get("scene"), chosen.get("mood")) if value)


def _shot(base: Path, row: dict, scores: dict[str, dict]) -> dict:
    """One ledger line as a tile: the picture, how it was made, what you said.

    `url` stays the host-relative `/selfies/…` the transcript uses; the page
    maps it per character (web/shared/runtime.js), so a shelf listing never has
    to know which room it is being read in.
    """
    name = str(row.get("image") or "")
    rating = scores.get(name, {})
    try:
        size = (base / name).stat().st_size
    except OSError:
        size = 0
    return {"name": name, "url": f"/selfies/{name}", "caption": _caption(row),
            "created_at": row.get("created_at"), "backend": row.get("backend"),
            "model": row.get("model"), "seed": row.get("seed"),
            "prompt": row.get("prompt"), "negative": row.get("negative"),
            "selfie_id": row.get("selfie_id"), "corr_id": row.get("corr_id"),
            "bytes": size, "score": rating.get("score"),
            "rated_at": rating.get("rated_at")}


def page(directory: str | Path, *, page: int = 0,
         limit: int = DEFAULT_LIMIT) -> dict:
    """One newest-first page of the shelf.

    A ledger line whose PNG has since been deleted is dropped *inside* the
    pager, so a removed image costs its page a hole rather than a broken tile —
    and the page after it is still a full page. A shelf with no ledger yet is
    an empty page, never a 404: a character who has taken no photographs has
    nothing to show, which is not an error.
    """
    base = Path(directory)
    page, limit = max(0, page), max(1, min(limit, MAX_LIMIT))
    scores = ratings(base)

    def kept(row: dict) -> bool:
        name = row.get("image")
        return isinstance(name, str) and bool(name) and (base / name).is_file()

    items, has_more, _ = jsonl_page(
        base / LEDGER, page=page, limit=limit, match=kept,
        shape=lambda row: _shot(base, row, scores))
    # The count is the shelf itself, not the ledger: one directory scan, no
    # stat per row, and it agrees with the pager everywhere except for a PNG
    # that arrived without a provenance line — which the forge never writes.
    total = sum(1 for _ in base.glob("*.png")) if base.is_dir() else 0
    return {"items": items, "page": page, "limit": limit, "has_more": has_more,
            "total": total, "rated": len(scores)}


# --------------------------------------------------- what she hasn't sent yet

#: How much of each picture's look her list quotes — enough to tell the raincheck
#: selfie from the window-seat one, not the whole paragraph she wrote.
UNSENT_CAPTION_CHARS = 160


class Unsent:
    """The pictures she made that never reached the chat (SPEC §18.2a).

    Gate 2 and the goals that hold a picture are how a photograph normally
    travels, and both can lose one: a night job's hands name no goal, a goal
    closes before its render lands, a follow-up is let go of. On 6 Oct three
    of the night's shots sat in the gallery — two of them the best she had
    taken — and nothing anywhere said they existed. So the question is asked of
    the shelf itself: every render on it, less every one the conversation has
    ever carried. Whatever happened in between, that difference is the list.

    "Hers" is everything but the owner's own renders from the gallery page
    (`by: owner`); a ledger line from before `by` was recorded is hers.

    The owner can also take one off by saying they have seen it elsewhere
    (`mark_seen`, `seen.jsonl` beside the ratings): most of what was stranded
    before this list existed had been looked at in the gallery all along.

    Three files, all append-only, each read from where the last read stopped —
    the ledger grows a line per render and the conversation one per sentence,
    so re-reading any per turn would cost more every day she lives. A file
    that got shorter was rewritten (the conversation compacts) and is read
    again from the top.
    """

    def __init__(self, shelf: str | Path, chat: str | Path | None):
        self.shelf = Path(shelf)
        self.chat = Path(chat) if chat else None
        self._lock = threading.Lock()
        self._shots: dict[str, dict] = {}       # image name -> ledger row
        self._sent: set[str] = set()            # image names the chat carried
        self._seen: set[str] = set()            # …and the owner marked seen
        self._read_to = {"ledger": 0, "chat": 0, "seen": 0}

    def _tail(self, which: str, path: Path | None) -> tuple[list[dict], bool]:
        """The whole lines appended since the last read, and whether the file
        was rewritten (so what was learned from it before no longer holds)."""
        start = self._read_to[which]
        try:
            size = path.stat().st_size if path is not None else 0
        except OSError:
            size = 0
        reset = size < start
        if reset:
            start = self._read_to[which] = 0
        if path is None or size == start:
            return [], reset
        with path.open("rb") as f:
            f.seek(start)
            blob = f.read(size - start)
        end = blob.rfind(b"\n")
        if end < 0:                  # half a line, still being written
            return [], reset
        self._read_to[which] = start + end + 1
        rows = []
        for line in blob[:end].splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue             # a torn line, as every reader here skips
            if isinstance(row, dict):
                rows.append(row)
        return rows, reset

    def _refresh(self) -> None:
        rows, reset = self._tail("ledger", self.shelf / LEDGER)
        if reset:
            self._shots.clear()
        for row in rows:
            name = row.get("image")
            if isinstance(name, str) and name and row.get("by") != "owner":
                self._shots[name] = row
        rows, reset = self._tail("chat", self.chat)
        if reset:
            self._sent.clear()
        for row in rows:
            url = row.get("image_url")
            if isinstance(url, str) and url:
                self._sent.add(url.rsplit("/", 1)[-1])
        rows, reset = self._tail("seen", self.shelf / SEEN)
        if reset:
            self._seen.clear()
        self._seen.update(str(row["image"]) for row in rows
                          if isinstance(row.get("image"), str))

    def pictures(self) -> list[dict]:
        """Every one still unsent, oldest first, shaped for her prompt.

        `ref` is what she passes back: the render's own id, which is the one her
        notes and her tool results know it by — or, where two renders share one
        (a night re-run makes a second `dream-<day>`), the file's stem.
        """
        with self._lock:
            self._refresh()
            rows = [row for name, row in self._shots.items()
                    if name not in self._sent and name not in self._seen]
        if not rows:
            return []
        try:
            on_shelf = set(os.listdir(self.shelf))   # one scan, not a stat each
        except OSError:
            return []
        rows = [row for row in rows if row["image"] in on_shelf]
        ids = Counter(str(row.get("selfie_id") or "") for row in rows)
        out = []
        for row in rows:
            name = str(row["image"])
            sid = str(row.get("selfie_id") or "")
            caption = " ".join(_caption(row).split())
            if len(caption) > UNSENT_CAPTION_CHARS:
                caption = caption[:UNSENT_CAPTION_CHARS - 1].rstrip() + "…"
            when = str(row.get("created_at") or "")[:16].replace("T", " ")
            out.append({"ref": sid if sid and ids[sid] == 1 else Path(name).stem,
                        "name": name, "url": f"/selfies/{name}",
                        "selfie_id": sid,
                        # a line from before `kind` was recorded could be
                        # either camera's, and "picture" is true of both
                        "kind": "selfie" if row.get("kind") == "selfie"
                        else "picture",
                        "when": when, "caption": caption})
        return out

    def find(self, ref: str) -> dict | None:
        """The unsent picture `ref` names — by the id her list shows, by the
        file's stem or name, or by a render id only if it names just one."""
        ref = (ref or "").strip()
        if not ref:
            return None
        shots = self.pictures()
        exact = [s for s in shots if ref in (s["ref"], s["name"],
                                             Path(s["name"]).stem)]
        if exact:
            return exact[-1]
        by_id = [s for s in shots if s["selfie_id"] == ref]
        return by_id[0] if len(by_id) == 1 else None


def mark_seen(directory: str | Path, names: list[str], *,
              by: str = "user") -> list[dict]:
    """Say the owner has seen these pictures, so she stops offering them.

    Appends, like a rating, and refuses a name that is not on the shelf — a
    mark nothing can be joined back to is worse than none.
    """
    for name in names:
        resolve(directory, name)
    at = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    rows = [{"image": name, "by": by, "at": at} for name in names]
    for row in rows:
        jsonl_append(Path(directory) / SEEN, row)
    return rows
