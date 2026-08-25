"""The multi-character host — the switchboard, and the runtimes below it.

    hosting.py      `CharacterHost`, the per-character config arithmetic, the dispatcher
    app.py          `create_host_app` — the app's shape, and the order things mount in
    switchboard.py  the house: who exists, their state, approve / loop / archive / purge
    brains.py       which model she thinks with, house-wide and per character
    studio.py       the card studio: write her, dress her, export her
    debug.py        the journal, and everything under `/debug/*`
    pages.py        the five HTML entry points

This module is the package's public face. Note what it deliberately does *not*
re-export: `create_app`, `DIST_DIR`, `CharacterImporter` and `shutil` are patched
by name in the tests, and a name re-exported here would be a second binding that
the code below never reads — the patch would take, and change nothing. Patch them
where they are used.
"""
from __future__ import annotations

from .app import create_host_app
from .hosting import (JOURNAL_PAGE_SIZE, PURGE_CHALLENGE_TTL_S,
                      CharacterHost, TelegramCredentials, _close_reason,
                      _construction_fingerprint, _update_soul,
                      brain_overrides, config_for_character,
                      save_brain_overrides, telegram_env_suffix,
                      telegram_for_character)

__all__ = [
    "JOURNAL_PAGE_SIZE", "PURGE_CHALLENGE_TTL_S", "CharacterHost",
    "TelegramCredentials", "brain_overrides", "config_for_character",
    "create_host_app", "save_brain_overrides", "telegram_env_suffix",
    "telegram_for_character",
    # Reached for by name in the tests that pin their behaviour down.
    "_close_reason", "_construction_fingerprint", "_update_soul",
]
