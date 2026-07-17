"""Provider seams (SPEC §3.1) — the only vendor-facing surfaces.

Nothing else in the app imports a model SDK directly. Build #2 swaps the
hosted chat model for a local one by adding a file here and changing config;
the loop never notices (→ ch. 13, ch. 19).
"""
from __future__ import annotations

from typing import AsyncIterator, Protocol


class ChatModel(Protocol):
    def stream(self, messages: list[dict], **params) -> AsyncIterator[str]:
        """Stream assistant tokens; the caller accumulates the full text."""
        ...


class UtilityModel(Protocol):
    async def complete(self, messages: list[dict], **params) -> str:
        """Non-streaming; used for fact-update + summarisation. SHOULD support JSON output."""
        ...


class Embedder(Protocol):
    dim: int  # MUST equal the index's vector width (§4.3)

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Batch-embed texts into `dim`-wide vectors."""
        ...
