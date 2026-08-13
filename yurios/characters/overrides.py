"""What each character has taken for herself, and how to give it back (SPEC §31.2).

A character's record may override the house `.env`: her own chat model and her
own reasoning switches, while a host-owned named profile grants any endpoint and
credential pairing. That is the point of the registry — one house, many
companions, each with the brain she needs. It is also the setting
most likely to outlive its reason. Swap the house model in `.env` and every
character who never asked for one of her own follows; the one who *did* keeps
the old connection, silently, until somebody wonders why LM Studio is being
dialled by an install whose `.env` says `gguf/…`.

So the overrides are inspectable from outside a running host: `describe()` reads
the registry and the profiles and says, per character, which model she will
actually connect with and which of the settings behind that are hers rather than
the house's. `yurios start` prints it, so a character-specific connection is
visible as a character-specific thing; `yurios configure` offers to clear it,
so choosing a new house model does not leave one companion behind on the old
one.

This module holds no policy of its own beyond that. The endpoint rule lives here
because two callers need the same answer — the host, when it builds her runtime
config, and this report, when it says where she will connect.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .connections import ConnectionProfile
from .models import CharacterRecord


# The knobs a record may hold, and where each one lives in it. Mirrors
# world/rewire.OVERRIDE_SCHEMA, which is the settings screen's form for the same
# set; kept here as plain data so reading a registry never imports the world.
#   store: "chat"/"utility" have named homes; the rest ride `models.options`.
OVERRIDE_KEYS: tuple[tuple[str, str], ...] = (
    ("chat_model", "chat"),
    ("utility_model", "utility"),
    ("chat_thinking", "options"),
    ("utility_thinking", "options"),
    ("temperature", "options"),
    ("max_reply_tokens", "options"),
    ("context_length", "options"),
)


def endpoint_field(model: str) -> str | None:
    """Which base-url knob a model id is served by, or None if it is hosted.

    `gguf/…` answers None as well: it is loaded in this process, so there is no
    server to name and no endpoint that could mean anything.
    """
    if model.startswith("lm_studio/"):
        return "lmstudio_base_url"
    if model.startswith("ollama/"):
        return "ollama_base_url"
    return None


def resolve_endpoint(chat: str, utility: str, *, profile_endpoint: str | None,
                     lmstudio_url: str,
                     ollama_url: str) -> tuple[str | None, str]:
    """Which base url her models actually reach, as (config field, url).

    An endpoint names one server, so it re-points whichever local provider her
    models route to — her chat model's, or her utility model's when only that one
    is local — and answers (None, "") when neither routes anywhere local, which
    is the `gguf/…` and hosted cases both. It is also dropped when it is verbatim
    one of the house's *other* server's urls: the `default` profile is seeded
    from whichever provider the host's own model used (§31.1), so a character who
    moves to the other one would otherwise inherit an endpoint naming the wrong
    server entirely.
    """
    endpoint = profile_endpoint or ""
    if not endpoint:
        return None, ""
    field = endpoint_field(chat) or endpoint_field(utility)
    if not field:
        return None, ""
    house = {lmstudio_url: "lmstudio_base_url", ollama_url: "ollama_base_url"}
    if house.get(endpoint, field) != field:
        return None, ""
    return field, endpoint


@dataclass(frozen=True)
class Override:
    """One setting a character holds instead of inheriting it."""

    key: str          # the Config field name, as the settings screen names it
    value: Any        # what her record says
    house: Any        # what the host's `.env` says

    @property
    def differs(self) -> bool:
        return self.value != self.house

    @property
    def note(self) -> str:
        """What inheriting instead would have given her."""
        return f"house: {self.house}" if self.differs else "same as the house"


@dataclass(frozen=True)
class CharacterConnection:
    """Where one character's brain will connect, and how much of that is hers."""

    id: str
    name: str
    autostart: bool
    chat_model: str
    utility_model: str
    endpoint: str                      # "" when her models need no server
    overrides: tuple[Override, ...]

    @property
    def differs(self) -> bool:
        """Does she connect somewhere the house `.env` did not send her?"""
        return any(item.differs for item in self.overrides)

    def summary(self) -> str:
        """Her connection in one line: the model, and the server if there is one."""
        line = self.chat_model or "NONE"
        if self.utility_model and self.utility_model != self.chat_model:
            line += f" (utility {self.utility_model})"
        if self.endpoint:
            line += f" → {self.endpoint}"
        return line


def _house_value(cfg: Any, key: str, chat: str, utility: str) -> Any:
    """What this override would have been, had she inherited it."""
    if key == "endpoint":
        field = endpoint_field(chat) or endpoint_field(utility)
        return getattr(cfg, field, "") if field else ""
    return getattr(cfg, key, "")


def describe_record(cfg: Any, record: CharacterRecord,
                    profile: ConnectionProfile | None = None) -> CharacterConnection:
    """One character's effective connection, from her record and the host config.

    The model half of `world.host.config_for_character`, without building a
    runtime config: a blank binding means *inherit the house*, which is what
    keeps one `.env` configuring a house (§11).
    """
    chat = record.models.chat or cfg.chat_model
    utility = record.models.utility or cfg.utility_model
    _, endpoint = resolve_endpoint(
        chat, utility,
        profile_endpoint=profile.endpoint if profile else "",
        lmstudio_url=getattr(cfg, "lmstudio_base_url", ""),
        ollama_url=getattr(cfg, "ollama_base_url", ""))

    held: list[Override] = []
    for key, store in OVERRIDE_KEYS:
        if store == "options":
            if key not in record.models.options:
                continue
            value: Any = record.models.options[key]
        else:
            holder = record.models
            value = getattr(holder, store, "") or ""
            if not value:
                continue
        held.append(Override(key, value, _house_value(cfg, key, chat, utility)))

    return CharacterConnection(
        id=record.id, name=record.display.name or record.id,
        autostart=record.lifecycle.enabled and record.lifecycle.autostart,
        chat_model=chat, utility_model=utility, endpoint=endpoint,
        overrides=tuple(held))


def describe(cfg: Any, registry: Iterable[CharacterRecord],
             profiles: Mapping[str, ConnectionProfile] | Any = None,
             ) -> list[CharacterConnection]:
    """Every character's connection. `profiles` is a ConnectionProfiles, or None."""
    getter = getattr(profiles, "get", None)
    return [describe_record(cfg, record,
                            getter(record.connection.profile) if getter else None)
            for record in registry]


def clear_record(record: CharacterRecord) -> list[str]:
    """Give her model settings back to the house; return what was cleared.

    Her named bindings and her `models.options` both — everything `describe`
    reports as hers. Nothing else in the record is touched: her voice, her body
    and her loops are not the connection, and a model change is no reason to
    take them.
    """
    cleared: list[str] = []
    for key, store in OVERRIDE_KEYS:
        if store == "options":
            if key in record.models.options:
                del record.models.options[key]
                cleared.append(key)
        elif getattr(record.models, store, ""):
            setattr(record.models, store, "")
            cleared.append(key)
    return cleared


def clear(registry: Any, ids: Iterable[str]) -> dict[str, list[str]]:
    """Clear each named character's model settings and save the registry."""
    cleared: dict[str, list[str]] = {}
    for character_id in ids:
        record = registry.get(character_id)
        if record is None:
            continue
        keys = clear_record(record)
        if keys:
            cleared[character_id] = keys
            registry.upsert(record)
    return cleared
