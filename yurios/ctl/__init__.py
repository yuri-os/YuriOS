"""Host-client commands for `yurios` (SPEC §36).

The house commands (start/stop/settings/…) stay in `yurios.cli`. Everything
that talks to a running host — characters, chat, camera, dreams, handing her documents — lives here
so it cannot grow a second registry path.
"""
from __future__ import annotations

import argparse

from . import camera, characters, chat, documents, dreams


def register(sub: argparse._SubParsersAction) -> None:
    """Attach the host-client subcommands to the `yurios` parser."""
    characters.register(sub)
    chat.register(sub)
    camera.register(sub)
    dreams.register(sub)
    documents.register(sub)
