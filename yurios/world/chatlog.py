"""The transcript on disk (SPEC §2.6) — what the chat still shows after a restart.

`post_message` appends to an in-memory ring and publishes a `message` event, and
until this file existed that ring was the whole of the visible conversation: it
died with the process. Restarting the daemon — a config change, a crash, a
machine that slept — opened her room onto a blank column. Everything you said
last night was still in the Vault, which is her *memory*, but none of it was on
the screen, and "what did we settle on?" had nowhere to look.

So every committed entry is also written here, and the ring is seeded from it at
boot. The distinction §2.6 draws still holds and is the reason this is a
separate file from the Vault: **the Vault is what she remembers, this is only
what the page draws.** Nothing reads it back into a prompt.

**Where it lives.** `<vault>/state/transcript.jsonl`, beside `inbox.json`, and
untracked for the same reason (world/inbox.py's header): it changes on every
line either of you says, the words themselves are already in the corpus and the
journal, and committing it would put one commit per sentence in the diary. The
ignore line is written into `state/.gitignore` — inside the directory it
describes, so a Vault that already exists needs no migration.

**Append, not rewrite.** The inbox rewrites its whole file per entry, which is
right for a list of at most a hundred pending things; a transcript is two
orders of magnitude longer and gets a line per turn. So: one `write` per entry,
and a compaction to the newest `MAX_ENTRIES` only when the file has drifted
`SLACK` lines past the cap. Deliberately **not** fsynced — this is the draw
buffer for a chat column, and paying a disk sync per sentence to guarantee the
last line of a crashed process would be spending the wrong currency. A torn
tail line after a crash is expected and skipped on read.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from yurios.app import vaultgit

log = logging.getLogger("world.chatlog")

#: How far back the archive goes. Deep enough that "load the previous six"
#: walks a real conversation rather than hitting the floor in a minute, and
#: shallow enough that reading the whole file to answer one page load stays
#: cheap. Past this the oldest lines fall off; the corpus is the archive.
MAX_ENTRIES = 2000

#: How far past the cap the file may drift before it is rewritten. Compaction
#: costs a full read + write, so it must not happen once per message.
SLACK = 500

GITIGNORE = (
    "# the visible chat, not memory: one line per sentence either of you says.\n"
    "# What she *remembers* is the corpus and the journal, which are committed;\n"
    "# this is the column the page redraws after a restart, and versioning it\n"
    "# would put one commit per turn in the diary.\n"
    "transcript.jsonl\n")


class ChatLog:
    """One character's visible transcript, on disk.

    Constructed with no vault (the bare-runtime tests, a config with no Vault
    yet) it is a working no-op: every read answers empty and every write is
    dropped. Losing the scrollback is never a reason to fail a turn.
    """

    def __init__(self, vault: Path | str | None):
        self.vault = Path(vault) if vault else None
        self._lock = threading.Lock()
        self._ensured = False
        self._lines: int | None = None      # counted once, then tracked

    # ---- where the file is ---------------------------------------------------

    @property
    def path(self) -> Path | None:
        return self.vault / "state" / "transcript.jsonl" if self.vault else None

    @property
    def active(self) -> bool:
        return self.vault is not None

    def _ensure(self) -> None:
        """Make `state/` exist and mark the file untracked. Best-effort: a Vault
        on a read-only mount is a strange configuration, not a reason to refuse
        to show a line that is going to the screen anyway."""
        if self._ensured or self.vault is None:
            return
        self._ensured = True                      # try once per process, not per write
        try:
            state = self.vault / "state"
            state.mkdir(parents=True, exist_ok=True)
            ignore = state / ".gitignore"
            existing = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
            if "transcript.jsonl" not in existing:
                # Append rather than overwrite — the inbox writes its own line
                # into this same file, and whichever of us gets there second
                # must not clobber the first.
                vaultgit.atomic_write(ignore, existing + GITIGNORE)
        except OSError:
            log.warning("couldn't prepare the chat log under %s", self.vault, exc_info=True)

    # ---- reading -------------------------------------------------------------

    def _load(self) -> list[dict]:
        path = self.path
        if path is None or not path.exists():
            return []
        entries: list[dict] = []
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue      # a torn tail line after a crash; skip it
                    if isinstance(entry, dict) and entry.get("id"):
                        entries.append(entry)
        except OSError:
            # An unreadable log is an empty one. Refusing to start her because
            # the scrollback is corrupt would be the worse bug.
            log.warning("chat log at %s is unreadable; starting empty", path, exc_info=True)
            return []
        self._lines = len(entries)
        return entries

    def entries(self) -> list[dict]:
        """Everything on file, oldest first."""
        with self._lock:
            return self._load()

    def tail(self, n: int) -> list[dict]:
        """The newest `n`, oldest first — what seeds the ring at boot."""
        return self.entries()[-n:] if n > 0 else []

    # ---- writing -------------------------------------------------------------

    def add(self, entry: dict) -> None:
        """File one committed `message` entry, verbatim.

        The transcript entry itself (`world/main.py::post_message`), not a
        projection of it: the page that redraws this line after a restart must
        get the same object the live event carried — same `id`, so a client
        holding both resolves them to one message, and the same `image_url`,
        `report_path` and `proactive` flag, so a restored line is not a
        downgraded one.
        """
        path = self.path
        if path is None or not entry.get("id"):
            return
        self._ensure()
        with self._lock:
            try:
                with open(path, "a", encoding="utf-8") as f:
                    # `default=str`: a stray Path or datetime in a payload should
                    # land as its repr, never raise into the turn that wrote it.
                    f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
                if self._lines is None:
                    self._load()                  # first write of the process
                else:
                    self._lines += 1
                if self._lines is not None and self._lines > MAX_ENTRIES + SLACK:
                    self._compact()
            except OSError:
                log.warning("couldn't append to the chat log at %s", path, exc_info=True)

    def _compact(self) -> None:
        """Rewrite the file as its newest `MAX_ENTRIES` lines. Caller holds the
        lock. Atomic, so a crash mid-compaction leaves the previous file whole
        rather than a half-written one."""
        path = self.path
        if path is None:
            return
        kept = self._load()[-MAX_ENTRIES:]
        text = "".join(json.dumps(e, ensure_ascii=False, default=str) + "\n" for e in kept)
        try:
            vaultgit.atomic_write(path, text)
            self._lines = len(kept)
        except OSError:
            log.warning("couldn't compact the chat log at %s", path, exc_info=True)
