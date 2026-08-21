"""Her inbox (SPEC §10.5, §18.3) — where a reach-out waits when nobody is home.

The mind's initiative already survives Gate 2 before it reaches anything here:
`MindLoop._act_reach_out` scores the interrupt, spends one of her daily budget,
and only then puts a line on the bus. What happened next, until this file
existed, was nothing. `post_message` appends to the in-memory transcript ring
and publishes a `message` event; with no page open, no terminal attached and no
Telegram credentials set, that event has no subscribers and the ring dies with
the process. She decided to interrupt you, paid for the privilege out of a
budget of two or three a day, and the line went nowhere.

So proactive messages are *also* filed here, on disk, and stay pending until you
have actually been in the room to see them. That is the whole idea: the
transcript is what a page shows, the inbox is what is owed to you.

**Where it lives.** `<vault>/state/inbox.json`, beside the other bookkeeping the
scheduler keeps, and **untracked** — `state/.gitignore` names it, written into
the directory itself rather than added to the Vault's root `.gitignore`, for the
reason `KnowledgeStore.INDEX_GITIGNORE` gives: a Vault's root ignore file is
written once at seed time and never refreshed, so a line added to it today
protects no vault that already exists. An ignore file inside the directory it
describes needs no migration to arrive.

Untracked is the right call because the *history* of a reach-out is already in
git: the mind journals every one of them ("left them a note about …") and the
journal is committed. What this file adds is delivery state — pending or seen —
which flips on every glance at her room and would otherwise land in whatever
commit fired next, labelled as something else. That is the failure §34.2
describes for desk writes, and the same answer applies: **a write here MUST NOT
dirty the Vault.**

The file is deliberately not a `MindVault` write for exactly that reason: it
goes straight to disk through `vaultgit.atomic_write`, and nothing marks dirty.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from yurios.app import vaultgit

log = logging.getLogger("world.inbox")

#: Kept short on purpose. This is a list of things she is still waiting to tell
#: you, not an archive — the corpus and the journal are the archive. A hundred
#: undelivered reach-outs means something upstream is wrong, and truncating the
#: oldest is a better failure than an unbounded file.
MAX_ENTRIES = 100

GITIGNORE = (
    "# delivery state, not memory: which of her reach-outs you have actually\n"
    "# seen. It flips every time you open her room, and what she *said* is\n"
    "# already in the journal and the corpus — committing this would put one\n"
    "# entry per glance in the Vault's history.\n"
    "inbox.json\n")


def _kind(entry: dict) -> str:
    """What this row is, for the badge and for the view that renders it.

    Three kinds now: a `selfie` she sent, a `report` a night wrote and was told
    to deliver (§18.2a), and a plain `message` — everything else she said first.
    """
    if entry.get("image_url"):
        return "selfie"
    if entry.get("report_path"):
        return "report"
    return "message"


class Inbox:
    """One character's pending proactive messages, on disk.

    Constructed with no vault (the bare-runtime tests, a config with no Vault
    yet) it is a working no-op: every read answers empty and every write is
    dropped. An inbox is a convenience for the user, and failing to have one is
    never a reason to fail a turn.
    """

    def __init__(self, vault: Path | str | None):
        self.vault = Path(vault) if vault else None
        self._lock = threading.Lock()
        self._ensured = False

    # ---- where the file is ---------------------------------------------------

    @property
    def path(self) -> Path | None:
        return self.vault / "state" / "inbox.json" if self.vault else None

    def _ensure(self) -> None:
        """Make `state/` a place that exists and mark the file untracked.

        Best-effort, like `KnowledgeStore._ensure_shelf`: a Vault on a read-only
        mount is a strange configuration, not a reason to refuse to deliver a
        message that is also going to the screen anyway.
        """
        if self._ensured or self.vault is None:
            return
        self._ensured = True                      # try once per process, not per write
        try:
            state = self.vault / "state"
            state.mkdir(parents=True, exist_ok=True)
            ignore = state / ".gitignore"
            existing = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
            if "inbox.json" not in existing:
                # Append rather than overwrite: `state/` may already carry an
                # ignore file somebody else wrote, and clobbering it to add one
                # line is not a trade worth making.
                vaultgit.atomic_write(ignore, existing + GITIGNORE)
        except OSError:
            log.warning("couldn't prepare the inbox under %s", self.vault, exc_info=True)

    # ---- reading -------------------------------------------------------------

    def _load(self) -> list[dict]:
        path = self.path
        if path is None or not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A truncated write or a hand-edit. An unreadable inbox is an empty
            # one — refusing to start her because a convenience file is corrupt
            # would be the worse bug, and the next `add` rewrites it whole.
            log.warning("inbox at %s is unreadable; starting empty", path, exc_info=True)
            return []
        entries = data.get("entries") if isinstance(data, dict) else data
        return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []

    def entries(self) -> list[dict]:
        """Everything on file, oldest first — read and unread both."""
        with self._lock:
            return self._load()

    def pending(self) -> list[dict]:
        """What she is still owed a look at, oldest first."""
        return [e for e in self.entries() if not e.get("read")]

    def unread(self) -> dict:
        """The switchboard badge, in the one shape the tile renders: how many
        are waiting and whether any of them is a picture (a selfie she sent
        unprompted reads differently from a line of text, and the tile says so).

        Cheap enough to build from a bare vault path, which is what the board
        does for a character with no live runtime (`host.summary`) — and a
        reach-out she made before the last restart is exactly the one most worth
        still showing.
        """
        pending = self.pending()
        return {"count": len(pending),
                "selfies": sum(1 for e in pending if e.get("kind") == "selfie"),
                "latest": pending[-1]["ts"] if pending else None}

    # ---- writing -------------------------------------------------------------

    def _save(self, entries: list[dict]) -> None:
        path = self.path
        if path is None:
            return
        self._ensure()
        try:
            vaultgit.atomic_write(
                path,
                json.dumps({"entries": entries[-MAX_ENTRIES:]},
                           ensure_ascii=False, indent=2) + "\n")
        except OSError:
            log.warning("couldn't write the inbox at %s", path, exc_info=True)

    def add(self, entry: dict) -> dict | None:
        """File one committed `message` entry. Returns the inbox row, or None
        when there is nowhere to file it.

        Takes the transcript entry itself (`world/main.py::post_message`) so the
        `id` is shared: the page that later renders this row and the page that
        saw the live event resolve to the same message and show it once.
        """
        if self.vault is None:
            return None
        row = {"id": entry.get("id"),
               "ts": entry.get("ts"),
               "kind": _kind(entry),
               "text": entry.get("text", ""),
               "read": False}
        for key in ("image_url", "selfie_id", "report_path", "report_title",
                    "report_job"):
            if entry.get(key):
                row[key] = entry[key]
        with self._lock:
            entries = self._load()
            if any(e.get("id") == row["id"] for e in entries):
                return None               # a re-publish is not a second message
            self._supersede(entries, row)
            entries.append(row)
            self._save(entries)
        return row

    @staticmethod
    def _supersede(entries: list[dict], row: dict) -> None:
        """A new report from a job retires that job's older pending one.

        Only reports, and only within one job. A nightly brief is a standing
        answer to a standing question, so what is owed to you is *this
        morning's*, not one per night you were away — come back after a week to
        five stale market reads and the useful one is the hardest to find. The
        files are all still on her desk; this is delivery state, not the archive
        (see this module's header).

        Deliberately not applied to a selfie or a reach-out: those are each a
        separate thing she did, and none of them replaces another.
        """
        job = row.get("report_job")
        if row.get("kind") != "report" or not job:
            return
        for entry in entries:
            if (entry.get("kind") == "report" and not entry.get("read")
                    and entry.get("report_job") == job):
                entry["read"] = True

    def mark_read(self) -> int:
        """Everything pending has now been seen. Returns how many that was.

        There is no per-entry acknowledgement on purpose: being in her room *is*
        the acknowledgement, which is the rule the chat view implements (it
        renders the pending run, then calls this). A dismiss button would be a
        second, contradictory answer to "did you see this?".
        """
        with self._lock:
            entries = self._load()
            marked = 0
            for entry in entries:
                if not entry.get("read"):
                    entry["read"] = True
                    marked += 1
            if marked:
                self._save(entries)
            return marked
