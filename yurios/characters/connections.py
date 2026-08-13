"""Host-owned named provider profiles; secrets remain environment references."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .registry import atomic_write_json


_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MODEL_KEY_ENV_PREFIX = "YURIOS_MODEL_API_KEY_"


def is_model_key_env(name: str) -> bool:
    """Only the dedicated namespace may expose process secrets to model servers."""
    return name == "OPENROUTER_API_KEY" or (
        name.startswith(MODEL_KEY_ENV_PREFIX) and
        len(name) > len(MODEL_KEY_ENV_PREFIX) and
        _ENV_NAME_RE.fullmatch(name) is not None)


@dataclass(frozen=True, slots=True)
class ConnectionProfile:
    name: str
    backend: str = "litellm"
    endpoint: str = ""
    api_key_env: str = ""

    def __post_init__(self) -> None:
        if (not isinstance(self.name, str) or not self.name or len(self.name) > 64
                or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for ch in self.name)):
            raise ValueError("connection profile names use lowercase letters, digits, '-' and '_'")
        if not isinstance(self.backend, str) or self.backend != "litellm":
            raise ValueError("connection profile backend must be litellm")
        if not isinstance(self.endpoint, str):
            raise ValueError("connection profile endpoint must be a string")
        if self.endpoint:
            if self.endpoint != self.endpoint.strip() or any(
                    character.isspace() for character in self.endpoint):
                raise ValueError("connection profile endpoint must not contain whitespace")
            parsed = urlsplit(self.endpoint)
            try:
                port = parsed.port
            except ValueError as exc:
                raise ValueError("connection profile endpoint has an invalid port") from exc
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None
                    or parsed.query or parsed.fragment or (port is not None and port < 1)):
                raise ValueError(
                    "connection profile endpoint must be an http(s) URL without credentials, query, or fragment"
                )
        if not isinstance(self.api_key_env, str):
            raise ValueError("connection profile api_key_env must be a string")
        if self.api_key_env and not _ENV_NAME_RE.fullmatch(self.api_key_env):
            raise ValueError("connection profile api_key_env must be an environment variable name")
        if self.api_key_env and not is_model_key_env(self.api_key_env):
            raise ValueError(
                "connection profile credentials must use OPENROUTER_API_KEY or "
                f"the {MODEL_KEY_ENV_PREFIX}* namespace")
        if self.endpoint and self.api_key_env == "OPENROUTER_API_KEY":
            raise ValueError("OPENROUTER_API_KEY cannot be paired with a custom endpoint")


class ConnectionProfiles:
    def __init__(self, data_root: Path):
        self.path = Path(data_root).resolve() / "connections.json"
        self._profiles: dict[str, ConnectionProfile] = {}
        if self.path.is_file():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if raw.get("schema_version") != 1:
                raise ValueError("unsupported connection profile schema")
            profiles = [ConnectionProfile(**row) for row in raw.get("profiles", [])]
            if len({profile.name for profile in profiles}) != len(profiles):
                raise ValueError("duplicate connection profile name")
            self._profiles = {profile.name: profile for profile in profiles}

    def list(self) -> list[ConnectionProfile]:
        return [self._profiles[key] for key in sorted(self._profiles)]

    def get(self, name: str) -> ConnectionProfile | None:
        return self._profiles.get(name)

    def upsert(self, profile: ConnectionProfile) -> None:
        previous = self._profiles.get(profile.name)
        self._profiles[profile.name] = profile
        try:
            atomic_write_json(self.path, {"schema_version": 1,
                              "profiles": [asdict(item) for item in self.list()]})
        except Exception:
            if previous is None:
                del self._profiles[profile.name]
            else:
                self._profiles[profile.name] = previous
            raise
