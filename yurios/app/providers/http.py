"""One kept-alive HTTP client per server embedder (SPEC §2.4).

An embedder is called on every turn — recall before the reply, remember after
it, a journal row on every tick — and it used to open a fresh `httpx.Client`
for each call: a new pool, a new TCP connection to the same localhost port, a
teardown, every time. This keeps one client for the embedder's lifetime.

It is shared across threads on purpose. The event loop never calls `embed()`
itself; every caller reaches it through `asyncio.to_thread`, so two workers can
be inside one embedder at once, and the sync connection pool under
`httpx.Client` is locked for exactly that. Creation is locked here too, and a
closed client is replaced rather than raised on: a host may hand one injected
embedder to several characters, and one of them stopping must not break the
rest.
"""
from __future__ import annotations

import threading

import httpx


class PooledClient:
    def __init__(self, *, timeout: float,
                 transport: httpx.BaseTransport | None = None):
        self._timeout = timeout
        self._transport = transport          # a test seam: httpx.MockTransport
        self._client: httpx.Client | None = None
        self._lock = threading.Lock()

    def client(self) -> httpx.Client:
        client = self._client
        if client is not None and not client.is_closed:
            return client
        with self._lock:
            if self._client is None or self._client.is_closed:
                self._client = httpx.Client(timeout=self._timeout,
                                            transport=self._transport)
            return self._client

    def close(self) -> None:
        with self._lock:
            client, self._client = self._client, None
        if client is not None:
            client.close()
