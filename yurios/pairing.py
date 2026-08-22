"""Getting the owner token onto a phone, without anyone typing it (SPEC §11.1).

A non-loopback bind requires `OWNER_TOKEN` (`yurios/security.py`), and a token
worth having is 43 random characters — which is fine until the device that needs
it is a phone across the room. So: generate it here, and hand it over as a link
in a QR code. The phone scans, opens `/auth?token=…`, and comes back holding the
HttpOnly session cookie; the token itself is never typed and never stored on the
phone.

What this module owns is the *link*, which is the part neither the panel nor the
CLI should be guessing at. `HOST=0.0.0.0` says nothing about where a phone should
point its browser, so the addresses are collected from the machine — and, when
this is answering a request, from the Host header the browser already reached her
on, which is the single most reliable candidate there is: something on this
network demonstrably routes to her that way.

Both surfaces render the same `describe()` payload — the settings panel draws the
QR as SVG in the pairing dialog, `yurios pair` prints it to the terminal — so
what you scan from a phone is the same link either way.
"""
from __future__ import annotations

import ipaddress
import json
import socket
import subprocess
from typing import Any
from urllib.parse import quote, urlsplit

from yurios import qr
from yurios.security import is_loopback


def _dialable(host: str | None) -> bool:
    """Whether a phone could actually point a browser at this.

    Loopback is out for the obvious reason. So is the unspecified address:
    `0.0.0.0` is what she BINDS to — every interface — and it reads like an
    address right up until you type it into a phone. It also arrives as a Host
    header, from anything that built its own URL out of `HOST`.
    """
    if not host or is_loopback(host):
        return False
    try:
        return not ipaddress.ip_address(host).is_unspecified
    except ValueError:
        return True                      # a name, which is somebody's answer

# Enough for a laptop with wifi, ethernet and a VPN interface up at once; past
# that the list stops being a choice and starts being a wall of QR codes.
MAX_CANDIDATES = 5


def lan_addresses() -> list[str]:
    """This machine's non-loopback IPv4 addresses, best guess first.

    The UDP socket sends nothing — connecting a datagram socket only asks the
    routing table which local address would be used to reach that destination,
    which is exactly the question "what is my address on the network I am on".
    The documentation address it names is never contacted.
    """
    found: list[str] = []
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("192.0.2.1", 9))          # TEST-NET-1, RFC 5737
            found.append(probe.getsockname()[0])
        finally:
            probe.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(str(info[4][0]))
    except OSError:
        pass
    return [address for address in dict.fromkeys(found) if not is_loopback(address)]


def tailscale_origins(cfg: Any) -> list[str]:
    """HTTPS Serve origins that proxy this YuriOS loopback listener.

    This is deliberately narrower than "whatever Tailscale knows": the local
    daemon must report a root web handler whose target is loopback on YuriOS's
    configured port. That gives a settings page opened at 127.0.0.1 the same
    usable QR it would receive when opened through the public Serve URL, without
    putting the owner token in a link to an unrelated tailnet service.
    """
    port = int(getattr(cfg, "port", 8768) or 8768)
    try:
        result = subprocess.run(
            ["tailscale", "serve", "status", "--json"],
            capture_output=True, text=True, timeout=1, check=False)
        payload = json.loads(result.stdout) if result.returncode == 0 else {}
    except (FileNotFoundError, OSError, subprocess.SubprocessError, ValueError):
        return []

    origins: list[str] = []
    for authority, web in (payload.get("Web") or {}).items():
        handlers = web.get("Handlers") if isinstance(web, dict) else None
        root = handlers.get("/") if isinstance(handlers, dict) else None
        proxy = root.get("Proxy", "") if isinstance(root, dict) else ""
        try:
            target = urlsplit(proxy)
            public = urlsplit(f"//{authority}")
            target_port = target.port or (443 if target.scheme == "https" else 80)
        except ValueError:
            continue
        if (target.scheme not in ("http", "https") or not is_loopback(target.hostname)
                or target_port != port or not public.hostname):
            continue
        shown = public.hostname
        if ":" in shown:
            shown = f"[{shown}]"
        public_port = public.port
        origins.append(f"https://{shown}" +
                       (f":{public_port}" if public_port not in (None, 443) else ""))
    return list(dict.fromkeys(origins))


def _origin(host: str, port: int) -> str:
    try:
        shown = f"[{host}]" if ipaddress.ip_address(host).version == 6 else host
    except ValueError:
        shown = host
    return f"http://{shown}:{port}"


def candidate_origins(cfg: Any, *, request_origin: str = "",
                      advertised_origins: list[str] | None = None) -> list[str]:
    """Where a phone might reach this installation, most likely first.

    `request_origin` is the browser-visible origin asking — pass it when there
    is one. A browser that is already talking to her over `192.168.1.20:8768` has
    proved that address works from somewhere other than this machine, which no
    amount of interface enumeration can do.
    """
    port = int(getattr(cfg, "port", 8768) or 8768)
    origins: list[str] = []
    if request_origin:
        try:
            parsed = urlsplit(request_origin)
        except ValueError:
            parsed = urlsplit("")
        if (parsed.scheme in ("http", "https") and _dialable(parsed.hostname)
                and not parsed.username and not parsed.password
                and parsed.path in ("", "/") and not parsed.query and not parsed.fragment):
            origins.append(f"{parsed.scheme}://{parsed.netloc}")
    for advertised in advertised_origins or []:
        try:
            parsed = urlsplit(advertised)
        except ValueError:
            continue
        if (parsed.scheme == "https" and _dialable(parsed.hostname)
                and not parsed.username and not parsed.password
                and parsed.path in ("", "/") and not parsed.query and not parsed.fragment):
            origins.append(f"https://{parsed.netloc}")
    configured = str(getattr(cfg, "host", "") or "")
    # A loopback bind has no directly reachable LAN address. A reverse proxy can
    # still prove one by supplying the request origin above (Tailscale Serve is
    # the common case), but interface enumeration must not invent dead links.
    if not is_loopback(configured):
        for address in lan_addresses():
            origins.append(_origin(address, port))
    # A concrete configured bind that no interface reported (a VPN address, a
    # name in /etc/hosts) is still worth offering.
    if _dialable(configured):
        origins.append(_origin(configured, port))
    return list(dict.fromkeys(origins))[:MAX_CANDIDATES]


def link(origin: str, token: str, *, target: str = "/") -> str:
    """The pairing URL: one GET that trades the token for a session cookie."""
    return (f"{origin.rstrip('/')}/auth?token={quote(token, safe='')}"
            f"&next={quote(target, safe='')}")


def describe(cfg: Any, token: str, *, live: bool,
             request_origin: str = "",
             advertised_origins: list[str] | None = None) -> dict[str, Any]:
    """Everything a pairing view needs, for the panel and the terminal alike.

    `reachable` is the honest half of this: a bare loopback bind cannot be paired,
    while a request through a loopback reverse proxy proves its external origin.
    Either way is more useful than drawing a code for `http://127.0.0.1` that a
    phone will never load.
    """
    host = str(getattr(cfg, "host", "") or "")
    if advertised_origins is None:
        advertised_origins = tailscale_origins(cfg)
    origins = candidate_origins(
        cfg, request_origin=request_origin,
        advertised_origins=advertised_origins)
    reachable = bool(origins)
    return {
        "configured": bool(token),
        "live": bool(live),
        "host": host,
        "port": int(getattr(cfg, "port", 8768) or 8768),
        "reachable": reachable,
        "min_length": 32,
        "links": [{"origin": origin, "url": link(origin, token),
                   "qr": qr.svg(qr.encode(link(origin, token)))}
                  for origin in origins] if token else [],
    }
