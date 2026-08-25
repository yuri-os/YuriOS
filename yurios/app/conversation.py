"""The conversation on disk (SPEC §2.6, §7.1) — one log, two readers.

There used to be two per-message stores. `sessions.json` kept a `transcript[]`
per session so the §7.1 window could be rebuilt; `state/transcript.jsonl` kept
the chat column so a restart didn't open her room onto a blank page. Both
recorded the same events. They differed only in *payload*, and that difference
is real — the page draws the sentences she actually spoke (`" ".join(shown)`)
while the model must see what it actually produced, tags, `*narration*` and all
— but a difference in payload is a reason for two fields, not two files.

So: one append-only log, and the two consumers read the columns they need.

**Three record kinds, one line each.** The file is append-only because a
transcript gets a line per sentence and rewriting it per message is how
`sessions.json` came to rewrite 42 KB to add 200 bytes. But a line is written in
two halves at two different moments — the runtime posts what was drawn *before*
the brain knows what it generated — so the second half arrives as its own
record and is folded back on read:

  `{"id": …, "role": …, "text": …, "ts": …}`   the line
  `{"raw_for": "<id>", "raw": …, "w": 1}`      what the model produced for it
  `{"drawn_for": "<id>", …}`                   how the page draws it
  `{"unwind": "<id>"}`                         it leaves the window, not the page

Either half may be written first, so both can create the line and both can patch
it. A reply is drawn the instant the stream ends and admitted a moment later
when the turn persists; a greeting is the other way round — the brain appends
its text before the runtime posts it. Whichever arrives second finds the row and
fills in its column, which is why neither `add` nor the patches assume they are
first.

`unwind` is the §4.4 rollback and the reason a tombstone is not a deletion: a
barged-in turn must stop feeding the next prompt, but your own line stays on
screen where you can see you said it. Two facts about one line that the old
split expressed by accident — the chat file kept it, the session file dropped it
— and that a merged store has to say out loud.

**The window is admitted to, never inferred.** `w` is what puts a line in the
§7.1 window, and only `SessionStore.append_message` sets it. That matters
because far more is drawn than is prompted with: a greeting (§9.8), an ambient
murmur (§9.9), a selfie, a research digest and every mind reach-out are all
lines on the page that the next prompt must not see. Deriving membership from
"has a session_id" would quietly widen the window the first time one of those
was written with one.

**Where it lives.** `<vault>/state/conversation.jsonl`, and untracked, for the
reason the chat log gave before it: what she *remembers* is the corpus and the
journal, which are committed; this is one line per sentence either of you says,
and versioning it would put a commit per turn in the diary. `sessions.json`
stays tracked and keeps what it is actually good at — ids, counts, last_active.

Deliberately **not** fsynced. A torn tail line after a crash is expected and
skipped on read: this is the draw buffer for a chat column and the input to a
prompt that is about to be rebuilt anyway, and paying a disk sync per sentence
to guarantee the last line of a crashed process would be spending the wrong
currency.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from yurios.app import vaultgit

log = logging.getLogger("app.conversation")

#: How far back the log goes. Deep enough that "load the previous six" walks a
#: real conversation rather than hitting the floor in a minute, and shallow
#: enough that reading the whole file to answer one page load stays cheap. Past
#: this the oldest lines fall off; the corpus is the archive.
MAX_ENTRIES = 2000

#: How far past the cap the file may drift before it is rewritten. Compaction
#: costs a full read + write, so it must not happen once per message.
SLACK = 500

#: Fields that belong to the model's side of a line and never to the page's.
_WINDOW_ONLY = ("raw", "turn_id")

#: What the page needs and the window has no use for. `post_message` builds
#: exactly these (`world/main.py`), and a line missing them was never drawn.
_DRAWN_ONLY = ("text", "ts", "role", "image_url", "proactive", "channel",
               "client_id", "selfie_id", "report_path", "report_title",
               "report_job", "unheard")

GITIGNORE = (
    "# the conversation itself: one line per sentence either of you says, plus\n"
    "# what the model produced for it. What she *remembers* is the corpus and\n"
    "# the journal, which are committed; this is the column the page redraws\n"
    "# after a restart and the window the next prompt is built from, and\n"
    "# versioning it would put one commit per turn in the diary.\n"
    "conversation.jsonl\n")


class ConversationLog:
    """One character's conversation, on disk.

    Constructed with no vault (the bare-runtime tests, a config with no Vault
    yet) it is a working no-op: every read answers empty and every write is
    dropped. Losing the scrollback is never a reason to fail a turn.
    """

    def __init__(self, vault: Path | str | None):
        self.vault = Path(vault) if vault else None
        self._lock = threading.RLock()
        self._ensured = False
        self._lines: int | None = None      # records written, not lines folded
        self.upgrade()

    # ---- where the file is ---------------------------------------------------

    @property
    def path(self) -> Path | None:
        return self.vault / "state" / "conversation.jsonl" if self.vault else None

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
            if "conversation.jsonl" not in existing:
                # Append rather than overwrite — the inbox writes its own line
                # into this same file, and whichever of us gets there second
                # must not clobber the first.
                vaultgit.atomic_write(ignore, existing + GITIGNORE)
        except OSError:
            log.warning("couldn't prepare the conversation log under %s",
                        self.vault, exc_info=True)

    # ---- reading -------------------------------------------------------------

    def _fold(self) -> list[dict]:
        """Every line the log holds, oldest first, with its late halves folded
        in. Caller holds the lock.

        One pass, because the records that patch a line always follow it: a
        `raw_for` merges into the row it names and an `unwind` flags it. A patch
        naming a line that has fallen off the front is simply dropped — the line
        it belonged to is already gone.
        """
        path = self.path
        if path is None or not path.exists():
            return []
        rows: list[dict] = []
        index: dict[str, dict] = {}
        records = 0
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue      # a torn tail line after a crash; skip it
                    if not isinstance(rec, dict):
                        continue
                    records += 1
                    target = (rec.get("raw_for") or rec.get("drawn_for")
                              or rec.get("unwind"))
                    if target:
                        row = index.get(target)
                        if row is None:
                            continue
                        if rec.get("unwind"):
                            row["unwound"] = True
                        elif rec.get("drawn_for"):
                            # The page's half landing second overwrites `text`,
                            # and on a line the window created that text *is*
                            # the model's own. Keep it before it is restated:
                            # the greeting is written this way round, and
                            # without this her `*narration*` would vanish out of
                            # the next prompt the moment the page drew her.
                            if (rec.get("text") is not None and row.get("w")
                                    and row.get("raw") is None
                                    and rec["text"] != row.get("text")):
                                row["raw"] = row.get("text")
                            row["d"] = 1          # the page has a line to draw
                            for field in _DRAWN_ONLY:
                                if rec.get(field) is not None:
                                    row[field] = rec[field]
                        else:
                            row["w"] = 1          # admitted to the window (§7.1)
                            for field in _WINDOW_ONLY:
                                if rec.get(field) is not None:
                                    row[field] = rec[field]
                        continue
                    if not rec.get("id"):
                        continue
                    rows.append(rec)
                    index[rec["id"]] = rec
        except OSError:
            # An unreadable log is an empty one. Refusing to start her because
            # the scrollback is corrupt would be the worse bug.
            log.warning("conversation log at %s is unreadable; starting empty",
                        path, exc_info=True)
            return []
        self._lines = records
        return rows

    def entries(self) -> list[dict]:
        """Every line the page may draw, oldest first, folded.

        `d` is the filter, and it is why Build #1's `/api/chat` does not put
        rows in anybody's chat column: that route admits lines to the window and
        never draws one, so its rows carry no display half and this skips them.
        """
        with self._lock:
            return [dict(r) for r in self._fold() if r.get("d")]

    def tail(self, n: int) -> list[dict]:
        """The newest `n`, oldest first — what seeds the chat ring at boot."""
        return self.entries()[-n:] if n > 0 else []

    def window(self, session_id: str, n: int) -> list[dict]:
        """The last `n` messages of one session, as the prompt wants them (§7.1).

        `raw or text`, because the model must see what it produced — the
        expression tags and the `*narration*` the page strips before drawing.
        A line that was unwound (§4.4) is skipped: it is still on screen, it is
        simply no longer part of the conversation the next prompt continues. So
        is every line that was never admitted (`w`) — the greeting, the murmur,
        the selfie, the digest: drawn, but not what the next prompt continues.
        """
        if n <= 0:
            return []
        with self._lock:
            mine = [r for r in self._fold()
                    if r.get("session_id") == session_id
                    and r.get("w") and not r.get("unwound")]
        return [{"role": r.get("role", "assistant"),
                 "content": r.get("raw") or r.get("text") or "",
                 "ts": r.get("ts", "")}
                for r in mine[-n:]]

    def pending(self, session_id: str, role: str) -> dict | None:
        """The line a late half belongs to, or None if there isn't one.

        The runtime posts what was drawn and the brain admits it a moment later,
        so the row waiting for that second half is the session's newest — and it
        only counts if it is `role`'s and has not been admitted already. Anything
        else means nothing drew this line (Build #1's own chat route posts no
        message at all), and the caller should write the whole row itself.
        """
        with self._lock:
            for row in reversed(self._fold()):
                if row.get("session_id") != session_id:
                    continue
                if row.get("role") == role and not row.get("w"):
                    return row
                return None            # the newest line is not the one we mean
        return None

    def undrawn(self, session_id: str, role: str) -> dict | None:
        """The line waiting to be drawn, or None. The mirror of `pending`: the
        greeting reaches the window before it reaches the page, so the runtime
        posting it must find the row the brain already wrote instead of adding a
        second one and showing the same sentence twice."""
        with self._lock:
            for row in reversed(self._fold()):
                if row.get("session_id") != session_id:
                    continue
                if row.get("role") == role and not row.get("d"):
                    return row
                return None
        return None

    def attach_drawn(self, message_id: str, entry: dict) -> None:
        """The page's half of a line the window already holds."""
        if not message_id:
            return
        record = {k: v for k, v in entry.items() if k in _DRAWN_ONLY}
        self._append({"drawn_for": message_id, **record})

    def last_admitted(self, session_id: str, role: str) -> str | None:
        """The newest line of this session's window, if `role` said it — what a
        rollback unwinds (§4.4). Mirrors the old `drop_last`'s guard: it undoes
        a line only when that line is the last thing in the window."""
        with self._lock:
            for row in reversed(self._fold()):
                if row.get("session_id") != session_id or not row.get("w"):
                    continue
                if row.get("unwound"):
                    continue
                return row.get("id") if row.get("role") == role else None
        return None

    # ---- writing -------------------------------------------------------------

    def add(self, entry: dict, *, window: bool = False,
            drawn: bool = True) -> None:
        """File one committed line, verbatim.

        `window=True` writes it straight into the §7.1 window as well, and
        `drawn=False` says the page never saw it — together, the case where
        nothing drew the line first (Build #1's `/api/chat`, which has no chat
        column to post to, and the greeting, whose text the brain appends before
        the runtime posts it).

        The transcript entry itself (`world/main.py::post_message`), not a
        projection of it: the page that redraws this line after a restart must
        get the same object the live event carried — same `id`, so a client
        holding both resolves them to one message, and the same `image_url`,
        `report_path` and `proactive` flag, so a restored line is not a
        downgraded one.
        """
        if entry.get("id"):
            row = dict(entry)
            if window:
                row["w"] = 1
            if drawn:
                row["d"] = 1
            self._append(row)

    def attach_raw(self, message_id: str, raw: str | None,
                   turn_id: str | None = None) -> None:
        """The model's own words for a line already on the page.

        The second half, and it arrives later by construction: the runtime
        commits what was spoken the moment the stream ends, while the brain only
        knows the raw completion — and the corpus id it joins to — once the turn
        persists.

        Always written, even when the model's words are exactly what was drawn —
        which is every user line without a picture note. This record is what
        admits the line to the §7.1 window (`w`), so skipping it because the text
        matched would quietly drop the line out of the next prompt. Only the
        redundant `raw` field itself is left out.
        """
        if not message_id:
            return
        self._append({"raw_for": message_id, "w": 1, "turn_id": turn_id,
                      "raw": raw if raw is not None else None})

    def unwind(self, message_id: str) -> None:
        """Take one line out of the window and leave it on the page (§4.4).

        A turn torn down mid-stream — a barge-in, a brain failure — must not
        leave an unanswered question for the next prompt to answer twice. But
        your line stays drawn: you said it, and a chat that quietly deletes what
        you typed is lying about what happened.
        """
        if message_id:
            self._append({"unwind": message_id})

    def _append(self, record: dict) -> None:
        path = self.path
        if path is None:
            return
        self._ensure()
        record = {k: v for k, v in record.items() if v is not None}
        with self._lock:
            try:
                with open(path, "a", encoding="utf-8") as f:
                    # `default=str`: a stray Path or datetime in a payload should
                    # land as its repr, never raise into the turn that wrote it.
                    f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                if self._lines is None:
                    self._fold()                  # first write of the process
                else:
                    self._lines += 1
                if self._lines is not None and self._lines > MAX_ENTRIES + SLACK:
                    self._compact()
            except OSError:
                log.warning("couldn't append to the conversation log at %s",
                            path, exc_info=True)

    def _compact(self) -> None:
        """Rewrite the file as its newest `MAX_ENTRIES` lines, folded — so the
        patch records collapse into the rows they belonged to and stop counting
        toward the cap. Caller holds the lock. Atomic, so a crash mid-compaction
        leaves the previous file whole rather than a half-written one."""
        path = self.path
        if path is None:
            return
        kept = self._fold()[-MAX_ENTRIES:]
        text = "".join(json.dumps(e, ensure_ascii=False, default=str) + "\n"
                       for e in kept)
        try:
            vaultgit.atomic_write(path, text)
            self._lines = len(kept)
        except OSError:
            log.warning("couldn't compact the conversation log at %s",
                        path, exc_info=True)

    # ---- the upgrade path ----------------------------------------------------

    def _repair(self) -> None:
        """Fix a log adopted before adoption knew what drawing was.

        A build between the two stores and this one moved `sessions.json` into
        here and marked the rows for the window only — so they were in every
        prompt and on no page, and the column they were migrated to save came up
        empty. The tell is a log with lines in the window and not one line
        anywhere that was ever drawn, which no working version produces.

        A vault that only ever ran Build #1's `/api/chat` matches too, and that
        is the right answer for it as well: those lines are real conversation,
        and a room that grows a chat column should show them.
        """
        path = self.path
        if path is None or not path.exists():
            return
        with self._lock:
            rows = self._fold()
            if not rows or any(r.get("d") for r in rows):
                return
            for row in rows:
                row["d"] = 1
                if row.get("role") != "user" and row.get("raw") is None:
                    drawn = _speakable(row.get("text") or "")
                    if drawn != row.get("text"):
                        row["raw"] = row["text"]     # the window keeps the tokens
                        row["text"] = drawn          # …the page gets the line
            try:
                vaultgit.atomic_write(path, "".join(
                    json.dumps(r, ensure_ascii=False, default=str) + "\n"
                    for r in rows))
                self._lines = len(rows)
                log.info("repaired %d undrawn lines in %s", len(rows), path)
            except OSError:
                log.warning("couldn't repair the conversation log at %s",
                            path, exc_info=True)

    def upgrade(self) -> int:
        """Fold both stores this one replaces into it, once. Returns lines moved.

        Runs from `__init__` rather than from a caller because both readers
        build their own handle and either may be first, and the two legacy files
        have to be merged *together* — sorted into one conversation by timestamp
        — or the column comes back interleaved wrongly.

        Losing this scrollback on upgrade would be the same bug the log exists to
        fix, one version later: `sessions.json` holds every line either of you
        said, and `transcript.jsonl` holds what the page drew, including the
        greetings and reach-outs that were never in a window. Both are real.
        """
        if self.vault is None or self.path is None:
            return 0
        self._repair()
        legacy_chat = self.vault / "state" / "transcript.jsonl"
        legacy_sessions = self.vault / "state" / "sessions.json"
        if not legacy_chat.exists() and not legacy_sessions.exists():
            return 0
        rows: list[dict] = []
        sessions: dict | None = None
        try:
            if legacy_sessions.exists():
                data = json.loads(legacy_sessions.read_text(encoding="utf-8"))
                sessions = data.get("sessions") or {}
                rows += _from_sessions(sessions)
            if legacy_chat.exists():
                rows += _from_transcript(legacy_chat)
        except (OSError, ValueError):
            log.warning("couldn't read the stores this log replaces under %s",
                        self.vault, exc_info=True)
            return 0
        if not rows:
            return 0
        rows.sort(key=lambda r: r.get("ts") or "")
        with self._lock:
            known = {r.get("id") for r in self._fold()}
            fresh = [r for r in rows if r["id"] not in known]
        for row in fresh:
            self._append(row)
        # Retire the sources so this cannot run twice — the ids are derived and
        # would dedup anyway, but a file that has been read is better gone than
        # left to look authoritative.
        try:
            if legacy_chat.exists():
                legacy_chat.unlink()
            if sessions and any(s.get("transcript") for s in sessions.values()):
                for session in sessions.values():
                    session.pop("transcript", None)
                vaultgit.atomic_write(legacy_sessions,
                                      json.dumps({"sessions": sessions}, indent=1,
                                                 ensure_ascii=False))
        except OSError:
            log.warning("couldn't retire the legacy stores under %s",
                        self.vault, exc_info=True)
        return len(fresh)

def _from_sessions(sessions: dict) -> list[dict]:
    """The §7.1 window as it was kept before this file: per session, and raw."""
    rows: list[dict] = []
    for session_id, session in (sessions or {}).items():
        for i, message in enumerate(session.get("transcript") or []):
            content = message.get("content")
            if not content:
                continue
            role = message.get("role", "assistant")
            drawn = _speakable(content) if role != "user" else content
            rows.append({"id": _adopted_id(session_id, i), "role": role,
                         "text": drawn, "ts": (message.get("ts") or "")[:19],
                         "session_id": session_id, "w": 1, "d": 1,
                         **({"raw": content} if drawn != content else {}),
                         **({"turn_id": message["turn_id"]}
                            if message.get("turn_id") else {})})
    return rows


def _speakable(text: str) -> str:
    """What the page would have drawn for a line only the window kept.

    The old window store held the model's own output, so an adopted line of hers
    arrives with its `[expression]` tags and `*narration*` still in it — and the
    column has never shown those. This is the same parser the voice pipeline
    runs on the way to TTS (`desktop/voice/emotion.py`), so a recovered line is
    drawn as the line it was, not as the tokens behind it. The original is kept
    as `raw`, which is what the window wanted from it anyway.

    Imported here rather than at module scope: `desktop` is built on `app`, and
    the dependency must not run the other way at import time.
    """
    try:
        from yurios.desktop.voice.emotion import EmotionParser
        parser = EmotionParser()
        parser.push(text)
        parser.finish()
        return parser.clean.strip() or text
    except Exception:               # a strip that fails is not worth a lost line
        return text


def _from_transcript(path: Path) -> list[dict]:
    """The chat column as it was kept before this file. These already carry
    `post_message`'s own ids, so they need no deriving — and they are drawn but
    never admitted: nothing in that file was ever read back into a prompt."""
    rows: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("id"):
                rows.append({**row, "d": 1})
    return rows


def _adopted_id(session_id: str, index: int) -> str:
    """An 8-hex id for a line that was written before ids existed. Same shape as
    `post_message`'s (`uuid4().hex[:8]`) because the page's dedup key, the
    walk-back anchor and the read-it-out lookup all assume that shape."""
    import hashlib
    return hashlib.blake2s(f"{session_id}:{index}".encode(),
                           digest_size=4).hexdigest()
