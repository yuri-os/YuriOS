"""Session bookkeeping — `vault/state/sessions.json` (B1 §4.1).

Plain JSON in the Vault (committed with each turn, like everything durable):
ids, counts, last_active. Single-user, so a flat file is exactly enough.

**The messages are not here.** They used to be: each session carried a
`transcript[]` that the §7.1 window was sliced out of, and the whole file was
rewritten on every single message to append one — 42 KB to add 200 bytes, and
growing without bound. Worse, it was a second copy: `state/transcript.jsonl`
recorded the same events for the chat column, differing only in whether the text
was what the model produced or what the page drew.

Both now live as one line each in `app/conversation.py`, which is append-only
and holds both payloads. This file went back to what it is good at — which is
ids and counters, small enough that rewriting it per turn costs nothing and
belongs in the diary. The window methods below keep their old signatures and
semantics; only where the words are kept has changed.
"""
from __future__ import annotations

import datetime
import json
import re
import uuid
from pathlib import Path

from yurios.app import vaultgit
from yurios.app.conversation import ConversationLog

SESSION_ID_RE = re.compile(r"^[0-9a-f]{32}$")  # ids are ours; anything else is rejected (§10)


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def _stamp() -> str:
    """A line's timestamp, in the shape `post_message` stamps one: local ISO
    seconds. The chat column sorts and renders on this, so a line written from
    here must not arrive in a different timezone from a line written there."""
    return datetime.datetime.now().isoformat(timespec="seconds")


class SessionStore:
    def __init__(self, vault: Path, log: ConversationLog | None = None):
        self.path = Path(vault) / "state" / "sessions.json"
        # An injected log is the runtime sharing its own instance; on its own
        # this builds one over the same file, which is safe because every read
        # re-reads and every write is one appended line. Building it *first*
        # matters: a vault written before the log keeps its conversation in the
        # file read on the next line, and the log's upgrade is what moves it —
        # so what is read below is already the emptied version.
        self.log = log if log is not None else ConversationLog(vault)
        self._data: dict = {"sessions": {}}
        if self.path.exists():
            self._data = json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self) -> None:
        vaultgit.atomic_write(self.path, json.dumps(self._data, indent=1,
                                                    ensure_ascii=False))

    @staticmethod
    def valid_id(session_id: str) -> bool:
        """Every handler treats session_id as untrusted (§10) — the Vault path
        is fixed; ids never touch the filesystem, but reject garbage anyway."""
        return bool(SESSION_ID_RE.match(session_id or ""))

    def create(self) -> str:
        sid = uuid.uuid4().hex
        self._data["sessions"][sid] = {
            "created": _now(), "last_active": _now(), "turn_count": 0}
        self._save()
        return sid

    def get(self, session_id: str) -> dict | None:
        if not self.valid_id(session_id):
            return None
        return self._data["sessions"].get(session_id)

    def append_message(self, session_id: str, role: str, content: str,
                       turn_id: str | None = None) -> None:
        """Admit one line to the §7.1 window.

        Two shapes, because a line reaches the window by two routes. Usually the
        runtime drew it a moment ago (`post_message`) and this is the second
        half arriving — the model's own words, tags and `*narration*` intact,
        plus the corpus id — so it attaches to that row and nothing is written
        twice. Where nothing drew it (Build #1's `/api/chat` has no chat column)
        the whole line is written here instead.

        Either way it is *this* call that puts the line in the window and
        nothing else does: far more is drawn than is prompted with (a greeting,
        a murmur, a selfie, a digest), and inferring membership from the fact
        that a line has a session would quietly widen the next prompt.
        """
        s = self._data["sessions"][session_id]
        row = self.log.pending(session_id, role)
        if row is not None:
            # `raw` only when it differs from what was drawn — a user line
            # without a picture note says the same thing twice otherwise.
            drawn = row.get("text") or ""
            self.log.attach_raw(row["id"],
                                content if content != drawn else None,
                                turn_id)
        else:
            # `drawn=False`: nothing has put this line on a page. Either
            # nothing ever will (Build #1's `/api/chat` has no chat column) or
            # the runtime is about to, and its post finds this row rather than
            # writing the same sentence a second time.
            self.log.add({"id": uuid.uuid4().hex[:8], "role": role,
                          "text": content, "ts": _stamp(),
                          "session_id": session_id,
                          **({"turn_id": turn_id} if turn_id else {})},
                         window=True, drawn=False)
        s["last_active"] = _now()
        self._save()

    def drop_last(self, session_id: str, role: str) -> bool:
        """Undo the last `append_message` if it was `role`'s. A turn is written
        to the window in two halves — the user's line when the reply starts
        streaming, hers when it commits — so a turn torn down in between (a
        barge-in, a brain failure) leaves an orphaned user line that the next
        prompt reads as an unanswered question. This is the rollback: a turn
        that didn't happen leaves no trace in the window (SPEC §4.4).

        It takes the line out of the *window*, not off the page. You said it and
        the chat still shows you saying it; what changes is only that the next
        prompt no longer treats it as a question still waiting for an answer.
        """
        message_id = self.log.last_admitted(session_id, role)
        if message_id is None:
            return False
        self.log.unwind(message_id)
        return True

    def bump_turn(self, session_id: str) -> None:
        s = self._data["sessions"][session_id]
        s["turn_count"] += 1
        s["last_active"] = _now()
        self._save()

    def window(self, session_id: str, n: int) -> list[dict]:
        """Last n admitted messages, chronological (§7.1). Small on purpose —
        the rolling summary carries older context (§7.2)."""
        return self.log.window(session_id, n)
