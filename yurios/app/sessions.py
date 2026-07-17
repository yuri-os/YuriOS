"""Session + turn bookkeeping — `vault/state/sessions.json` (SPEC §4.1).

Plain JSON in the Vault (committed with each turn, like everything durable):
ids, counts, last_active, and the raw transcript that feeds the §7.1 window.
Single-user, so a flat file is exactly enough.
"""
from __future__ import annotations

import datetime
import json
import re
import uuid
from pathlib import Path

from yurios.app import vaultgit

SESSION_ID_RE = re.compile(r"^[0-9a-f]{32}$")  # ids are ours; anything else is rejected (§10)


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


class SessionStore:
    def __init__(self, vault: Path):
        self.path = Path(vault) / "state" / "sessions.json"
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
            "created": _now(), "last_active": _now(),
            "turn_count": 0, "transcript": []}
        self._save()
        return sid

    def get(self, session_id: str) -> dict | None:
        if not self.valid_id(session_id):
            return None
        return self._data["sessions"].get(session_id)

    def append_message(self, session_id: str, role: str, content: str,
                       turn_id: str | None = None) -> None:
        s = self._data["sessions"][session_id]
        msg = {"role": role, "content": content, "ts": _now()}
        if turn_id:
            msg["turn_id"] = turn_id
        s["transcript"].append(msg)
        s["last_active"] = _now()
        self._save()

    def bump_turn(self, session_id: str) -> None:
        s = self._data["sessions"][session_id]
        s["turn_count"] += 1
        s["last_active"] = _now()
        self._save()

    def window(self, session_id: str, n: int) -> list[dict]:
        """Last n raw transcript messages, chronological (§7.1). Small on
        purpose — the rolling summary carries older context (§7.2)."""
        s = self._data["sessions"][session_id]
        return s["transcript"][-n:] if n > 0 else []
