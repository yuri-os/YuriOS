"""The terminal chat (SPEC §10.5) — `python -m yurios.chat`.

A thin remote frontend on the channel seam: POST /api/chat for turns, the
/api/events SSE stream for her replies and proactive lines. Runs against an
already-running `python -m yurios.world`; needs only httpx."""
