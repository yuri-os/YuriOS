"""The spawn wire: every setting the MCP tool server is given (SPEC §7.2).

Her hands run in a *separate process*. The host spawns
`python -m yurios.world.tools.server` and the only thing it can hand across
that boundary is a dict of strings — no config object, no runtime, no shared
memory. Fifteen keys, and until this module existed they were written out
twice: once in `world/main.py` as `str(cfg.something)`, and once in
`server.py` as `os.environ.get("SOMETHING", "a-default-typed-out-again")`.

Two files, two spellings of every key, and two independent opinions about what
each one means when it is absent. Nothing failed when they disagreed — the
server just quietly ran on a different number than the host thought it had.

So the wire is one object now. It knows each setting's name, its type and what
the server assumes when the key is absent; `from_config` is the encoding side,
`from_environ` the decoding side, and `to_environ` is what actually crosses.
`tests/test_spawn_env.py` round-trips it, which is the check neither side could
perform alone.

**The absent-key defaults are the standalone server's, not a Config's**, and
three of them differ on purpose. Run by hand with no host, the server has no
camera to reach (`selfies` on — it can still describe the hand), no Vault
(`vault_dir` empty, so the desk tools are unadvertised), and no mind to read a
self-edit queue (`selfedit` off). A `Config` answers all three differently
because a host *does* have those things. That is not drift; it is the
difference between the two ways this process starts.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:                                  # the encoding side's input;
    from yurios.world.config import Config         # never imported at runtime,
    # because this module is read by the spawned server, which has no Config
    # and must not pay for pydantic-settings (or read a `.env`) to start.


def _flag(value: bool) -> str:
    return "1" if value else "0"


def _read_flag(environ: Mapping[str, str], key: str, default: bool) -> bool:
    """`"0"` is off; anything else — including an empty string — is on.

    Absence means the default, which is how a knob the host does not set at all
    differs from one it deliberately set to `0`.
    """
    raw = environ.get(key)
    return default if raw is None else raw != "0"


@dataclass(frozen=True)
class ToolServerEnv:
    """Everything the tool server reads, with its type and its absent-key value.

    Malformed numbers still raise, exactly as the hand-written `int(...)` calls
    did: the host never writes one, so a value that will not parse came from
    somebody exporting it by hand, and a server that silently ran on a
    substituted number would be worse than one that refused to start.
    """

    #: `set_timer`'s upper bound (§7.1).
    timer_max_minutes: float = 180.0
    #: Whether the camera hand is advertised at all (§7.6) — off is *absent*,
    #: never a hand that fails when she reaches for it.
    selfies: bool = True
    #: Her own selfie library, which *replaces* the shipped one, and the
    #: house-wide overlay that adds to whichever base wins (world/selfies.py).
    #: The contract side builds `take_selfie`'s description from the same
    #: merged book the host renders from, so the two cannot drift.
    selfie_templates: str = ""
    selfie_templates_extra: str = ""
    #: The web hands (§7.7). They go together or not at all: searching with no
    #: way to read what you found is half a capability, and `research` is the
    #: two in sequence. `off` is the §7.6 rule again — unadvertised.
    search_backend: str = "off"
    searxng_url: str = "http://localhost:8080"
    search_results: int = 5
    search_language: str = "en"
    search_safesearch: int = 1
    fetch_timeout_s: float = 8.0
    fetch_max_bytes: int = 2_000_000
    research_max_pages: int = 5
    #: Her desk and her skills (§34.2). The path IS the sandbox root, which is
    #: what scopes the hands to *this* character's Vault: the host passes the
    #: one whose server this is, so one character's hands can never reach
    #: another's desk. Decided at spawn time, never by an argument she writes.
    vault_dir: str = ""
    workspace: bool = True
    skills: bool = True
    #: The self-edit door (§23) — off without a mind, because the queue it
    #: writes into is only ever read by the loop and the inner-life panel.
    selfedit: bool = False

    @property
    def vault_path(self) -> Path | None:
        """The Vault root, or None — and None means the desk is unadvertised."""
        return Path(self.vault_dir) if self.vault_dir else None

    @classmethod
    def from_config(cls, cfg: "Config") -> "ToolServerEnv":
        """The host's side: one Config, for the one character being spawned."""
        return cls(
            timer_max_minutes=float(cfg.timer_max_minutes),
            selfies=cfg.selfie_backend != "off",
            selfie_templates=cfg.selfie_templates,
            selfie_templates_extra=cfg.selfie_templates_extra,
            search_backend=cfg.search_backend,
            searxng_url=cfg.searxng_url,
            search_results=int(cfg.search_results),
            search_language=cfg.search_language,
            search_safesearch=int(cfg.search_safesearch),
            fetch_timeout_s=float(cfg.fetch_timeout_s),
            fetch_max_bytes=int(cfg.fetch_max_bytes),
            research_max_pages=int(cfg.research_max_pages),
            vault_dir=str(cfg.vault_dir),
            workspace=bool(cfg.workspace_enabled),
            skills=bool(cfg.skills_enabled),
            selfedit=bool(cfg.mind_enabled),
        )

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> "ToolServerEnv":
        """The spawned server's side: what actually arrived, typed."""
        env = os.environ if environ is None else environ
        return cls(
            timer_max_minutes=float(env.get("TIMER_MAX_MINUTES", "180")),
            selfies=_read_flag(env, "SELFIE_ENABLED", True),
            selfie_templates=env.get("SELFIE_TEMPLATES", ""),
            selfie_templates_extra=env.get("SELFIE_TEMPLATES_EXTRA", ""),
            search_backend=env.get("SEARCH_BACKEND", "off"),
            searxng_url=env.get("SEARXNG_URL", "http://localhost:8080"),
            search_results=int(env.get("SEARCH_RESULTS", "5")),
            search_language=env.get("SEARCH_LANGUAGE", "en"),
            search_safesearch=int(env.get("SEARCH_SAFESEARCH", "1")),
            fetch_timeout_s=float(env.get("FETCH_TIMEOUT_S", "8")),
            fetch_max_bytes=int(env.get("FETCH_MAX_BYTES", "2000000")),
            research_max_pages=int(env.get("RESEARCH_MAX_PAGES", "5")),
            vault_dir=env.get("VAULT_DIR", ""),
            workspace=_read_flag(env, "WORKSPACE_ENABLED", True),
            skills=_read_flag(env, "SKILLS_ENABLED", True),
            selfedit=_read_flag(env, "SELFEDIT_ENABLED", False),
        )

    def to_environ(self) -> dict[str, str]:
        """What crosses the process boundary. Every value a string, by then."""
        return {
            "TIMER_MAX_MINUTES": _number(self.timer_max_minutes),
            "SELFIE_ENABLED": _flag(self.selfies),
            "SELFIE_TEMPLATES": self.selfie_templates,
            "SELFIE_TEMPLATES_EXTRA": self.selfie_templates_extra,
            "SEARCH_BACKEND": self.search_backend,
            "SEARXNG_URL": self.searxng_url,
            "SEARCH_RESULTS": str(self.search_results),
            "SEARCH_LANGUAGE": self.search_language,
            "SEARCH_SAFESEARCH": str(self.search_safesearch),
            "FETCH_TIMEOUT_S": _number(self.fetch_timeout_s),
            "FETCH_MAX_BYTES": str(self.fetch_max_bytes),
            "RESEARCH_MAX_PAGES": str(self.research_max_pages),
            "VAULT_DIR": self.vault_dir,
            "WORKSPACE_ENABLED": _flag(self.workspace),
            "SKILLS_ENABLED": _flag(self.skills),
            "SELFEDIT_ENABLED": _flag(self.selfedit),
        }


def _number(value: float) -> str:
    """A float that is a whole number crosses as `180`, not `180.0`.

    Cosmetic on the way in — `float("180.0")` and `float("180")` are the same
    number — but the value is also read by a human running `env` against a
    stuck tool server, and `TIMER_MAX_MINUTES=180.0` reads like a setting
    somebody typed rather than the integer they actually configured.
    """
    return str(int(value)) if float(value).is_integer() else str(value)


#: Every key that crosses, derived from the encoder rather than restated — the
#: list a test walks, so a knob added to one side and not the other is caught.
KEYS: tuple[str, ...] = tuple(ToolServerEnv().to_environ())

#: The settings themselves, for the same reason.
SETTINGS: tuple[str, ...] = tuple(f.name for f in fields(ToolServerEnv))
