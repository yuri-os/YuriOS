"""Typed, JSON-serializable character registry models."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Collection
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def new_character_id(name: str, unavailable: Collection[str] = ()) -> str:
    """Return a name-based ID, adding ``_vN`` when the name is unavailable."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    base = re.sub(r"[^a-z0-9]+", "_", ascii_name.lower()).strip("_")[:64]
    base = base or "character"
    used = set(unavailable)
    if base not in used:
        return base
    version = 2
    while True:
        suffix = f"_v{version}"
        candidate = f"{base[:64 - len(suffix)].rstrip('_')}{suffix}"
        if candidate not in used:
            return candidate
        version += 1


def _validate_id(value: str) -> None:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ValueError(
            "character id must be 1-64 lowercase ASCII letters, digits, '.', '_' or '-'"
        )


def _options(value: object) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("binding options must be a JSON object")
    return dict(value)


@dataclass(slots=True)
class DisplayMetadata:
    name: str
    creator: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("character display name must not be empty")
        if not all(isinstance(tag, str) for tag in self.tags):
            raise ValueError("character tags must be strings")


@dataclass(slots=True)
class LifecycleFlags:
    enabled: bool = False
    autostart: bool = False
    review_required: bool = True


@dataclass(slots=True)
class LoopSwitches:
    mind: bool = True
    utility: bool = True
    dream: bool = True


@dataclass(slots=True)
class ConnectionBinding:
    profile: str = "default"
    backend: str = "litellm"
    endpoint: str | None = None
    api_key_env: str | None = None
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ModelBinding:
    chat: str = ""
    utility: str = ""
    dream: str = ""
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class VoiceBinding:
    tts_backend: str = ""
    voice_id: str = ""
    stt_backend: str = ""
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BodyBinding:
    backend: str = ""
    model: str = ""
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CharacterPaths:
    root: Path
    source_card: Path
    card_json: Path
    portrait: Path
    vault: Path
    corpus: Path
    traces: Path
    tool_logs: Path
    selfies: Path

    @classmethod
    def under(cls, root: Path) -> "CharacterPaths":
        root = Path(root)
        return cls(
            root=root,
            source_card=root / "source-card.png",
            card_json=root / "card.json",
            portrait=root / "portrait.png",
            vault=root / "vault",
            corpus=root / "corpus",
            traces=root / "traces",
            tool_logs=root / "tool-logs",
            selfies=root / "selfies",
        )


@dataclass(slots=True)
class CharacterRecord:
    id: str
    display: DisplayMetadata
    paths: CharacterPaths
    lifecycle: LifecycleFlags = field(default_factory=LifecycleFlags)
    loops: LoopSwitches = field(default_factory=LoopSwitches)
    connection: ConnectionBinding = field(default_factory=ConnectionBinding)
    models: ModelBinding = field(default_factory=ModelBinding)
    voice: VoiceBinding = field(default_factory=VoiceBinding)
    body: BodyBinding = field(default_factory=BodyBinding)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def __post_init__(self) -> None:
        _validate_id(self.id)

    @property
    def character_id(self) -> str:
        return self.id

    def to_dict(self, *, data_root: Path | None = None) -> dict[str, Any]:
        result = asdict(self)
        root = Path(data_root).resolve() if data_root is not None else None
        path_data: dict[str, str] = {}
        for key, value in asdict(self.paths).items():
            path = Path(value)
            if root is not None:
                try:
                    path = path.resolve().relative_to(root)
                except ValueError as exc:
                    raise ValueError(f"character path is outside data root: {value}") from exc
            path_data[key] = path.as_posix()
        result["paths"] = path_data
        return result

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any], *, data_root: Path | None = None
    ) -> "CharacterRecord":
        try:
            raw_paths = value["paths"]
            if not isinstance(raw_paths, Mapping):
                raise ValueError("character paths must be an object")
            base = Path(data_root).resolve() if data_root is not None else None
            paths: dict[str, Path] = {}
            for key in CharacterPaths.__dataclass_fields__:
                path = Path(str(raw_paths[key]))
                if base is not None:
                    if path.is_absolute():
                        raise ValueError("persisted character paths must be relative")
                    path = (base / path).resolve()
                    try:
                        path.relative_to(base)
                    except ValueError as exc:
                        raise ValueError("character path escapes data root") from exc
                paths[key] = path

            return cls(
                id=str(value["id"]),
                display=DisplayMetadata(**dict(value["display"])),
                paths=CharacterPaths(**paths),
                lifecycle=LifecycleFlags(**dict(value.get("lifecycle", {}))),
                loops=LoopSwitches(**dict(value.get("loops", {}))),
                connection=ConnectionBinding(
                    **_binding_data(value.get("connection", {}))
                ),
                models=ModelBinding(**_binding_data(value.get("models", {}))),
                voice=VoiceBinding(**_binding_data(value.get("voice", {}))),
                body=BodyBinding(**_binding_data(value.get("body", {}))),
                created_at=str(value.get("created_at", "")),
            )
        except (KeyError, TypeError) as exc:
            raise ValueError(f"invalid character record: {exc}") from exc


def _binding_data(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("character binding must be an object")
    result = dict(value)
    result["options"] = _options(result.get("options"))
    return result
