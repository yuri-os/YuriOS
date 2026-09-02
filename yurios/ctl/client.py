"""HTTP client for the running host (SPEC §36).

The CLI is a client of the daemon, not a second implementation of the
registry. Connection errors become one sentence; HTTP refusals keep the
server's own `detail`.
"""
from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from typing import Any

import httpx

from yurios.world.config import Config

DAEMON_DOWN = "YuriOS is not running. Start it with `yurios start`."


class HostDown(Exception):
    """The daemon did not answer."""


class HostError(Exception):
    """The host answered, and refused."""

    def __init__(self, message: str, status: int | None = None,
                 payload: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.payload = payload


def _origin(cfg: Config) -> str:
    host = "127.0.0.1" if cfg.host in ("0.0.0.0", "::", "") else cfg.host
    return f"http://{host}:{cfg.port}"


def _headers(cfg: Config) -> dict[str, str]:
    token = cfg.owner_token
    return {"Authorization": f"Bearer {token}"} if token else {}


def _detail(response: httpx.Response) -> tuple[str, Any]:
    try:
        body = response.json()
    except Exception:                              # noqa: BLE001
        text = (response.text or response.reason_phrase or "").strip()
        return (text[:400] or f"HTTP {response.status_code}"), None
    detail = body.get("detail", body) if isinstance(body, dict) else body
    if isinstance(detail, dict):
        message = str(detail.get("detail") or json.dumps(detail))
        return message, detail
    return str(detail).strip() or f"HTTP {response.status_code}", detail


def character_path(character_id: str, resource: str = "") -> str:
    base = f"/api/characters/{character_id}"
    return f"{base}/{resource.lstrip('/')}" if resource else base


class HostClient:
    """Synchronous httpx client aimed at this installation's daemon."""

    def __init__(self, cfg: Config | None = None, *,
                 transport: httpx.BaseTransport | None = None,
                 timeout: float = 30.0) -> None:
        self.cfg = cfg or Config()
        self.base = _origin(self.cfg)
        self._client = httpx.Client(
            base_url=self.base, headers=_headers(self.cfg),
            timeout=timeout, transport=transport)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HostClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise HostDown(DAEMON_DOWN) from exc
        if response.status_code >= 400:
            message, payload = _detail(response)
            raise HostError(message, status=response.status_code, payload=payload)
        return response

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        return self.request("POST", path, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> Any:
        return self.request("PATCH", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> Any:
        return self.request("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Any:
        return self.request("DELETE", path, **kwargs)

    def json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self.request(method, path, **kwargs)
        if not response.content:
            return None
        return response.json()

    def iter_sse(self, path: str, *, timeout: float | None = None
                 ) -> Iterator[dict[str, Any]]:
        try:
            with self._client.stream("GET", path, timeout=timeout) as response:
                if response.status_code >= 400:
                    body = response.read()
                    fake = httpx.Response(response.status_code, content=body,
                                          headers=response.headers,
                                          request=response.request)
                    message, payload = _detail(fake)
                    raise HostError(message, status=response.status_code,
                                    payload=payload)
                for line in response.iter_lines():
                    if not line.startswith("data: "):
                        continue
                    try:
                        event = json.loads(line[len("data: "):])
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event, Mapping):
                        yield dict(event)
        except HostError:
            raise
        except httpx.HTTPError as exc:
            raise HostDown(DAEMON_DOWN) from exc

    def stream_ndjson(self, method: str, path: str, **kwargs: Any
                      ) -> Iterator[dict[str, Any]]:
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Accept"] = "application/x-ndjson"
        try:
            with self._client.stream(method, path, headers=headers, **kwargs) as response:
                if response.status_code >= 400:
                    # Need the body to format the refusal; stream it in.
                    body = response.read()
                    fake = httpx.Response(response.status_code, content=body,
                                          headers=response.headers,
                                          request=response.request)
                    message, payload = _detail(fake)
                    raise HostError(message, status=response.status_code,
                                    payload=payload)
                for line in response.iter_lines():
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event, Mapping):
                        yield dict(event)
        except HostError:
            raise
        except httpx.HTTPError as exc:
            raise HostDown(DAEMON_DOWN) from exc


def connect(cfg: Config | None = None, **kwargs: Any) -> HostClient:
    return HostClient(cfg, **kwargs)


def fail(exc: Exception) -> int:
    """Print a host refusal and return the process status."""
    import sys
    if isinstance(exc, HostDown):
        print(str(exc), file=sys.stderr)
        return 1
    if isinstance(exc, HostError):
        extra = f" (HTTP {exc.status})" if exc.status else ""
        print(f"{exc.message}{extra}", file=sys.stderr)
        return 1
    print(str(exc), file=sys.stderr)
    return 1
