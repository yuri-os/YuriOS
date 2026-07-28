"""Host-owned named provider profiles; secrets remain environment references."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .registry import atomic_write_json


@dataclass(slots=True)
class ConnectionProfile:
    name: str
    backend: str = "litellm"
    endpoint: str = ""
    api_key_env: str = ""

    def __post_init__(self):
        if not self.name or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for ch in self.name):
            raise ValueError("connection profile names use lowercase letters, digits, '-' and '_'")


class ConnectionProfiles:
    def __init__(self, data_root: Path):
        self.path = Path(data_root).resolve() / "connections.json"
        self._profiles: dict[str, ConnectionProfile] = {}
        if self.path.is_file():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if raw.get("schema_version") != 1:
                raise ValueError("unsupported connection profile schema")
            self._profiles = {row["name"]: ConnectionProfile(**row)
                              for row in raw.get("profiles", [])}

    def list(self) -> list[ConnectionProfile]:
        return [self._profiles[key] for key in sorted(self._profiles)]

    def get(self, name: str) -> ConnectionProfile | None:
        return self._profiles.get(name)

    def upsert(self, profile: ConnectionProfile) -> None:
        self._profiles[profile.name] = profile
        atomic_write_json(self.path, {"schema_version": 1,
                          "profiles": [asdict(item) for item in self.list()]})
