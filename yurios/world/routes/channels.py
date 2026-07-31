"""/api/channels — the sanctuary's switches for the outside channels (SPEC §10.5).

A channel is still *on* when its credentials are set — that's how she stays
reachable. The settings panel owns the persistent cross-chat forwarding
default. This endpoint remains for integrations that need a temporary runtime
override; browser chat rooms do not expose it as a button.
"""
from __future__ import annotations

import ipaddress

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter()


def _telegram_channels(request: Request) -> list:
    """This runtime's telegram adapters (one per character under the host;
    the dispatcher already routed us to hers)."""
    rt = getattr(request.app.state, "rt", None)
    mgr = getattr(rt, "channels", None)
    return [c for c in getattr(mgr, "channels", []) if c.name == "telegram"]


@router.get("/api/channels/telegram/sending")
async def telegram_sending(request: Request) -> dict:
    """Whether this character has a telegram channel, and whether it sends."""
    channels = _telegram_channels(request)
    return {"configured": bool(channels),
            "sending_enabled": bool(channels) and
                               all(c.sending_enabled for c in channels)}


class SendingSwitch(BaseModel):
    enabled: bool


@router.post("/api/channels/telegram/sending")
async def set_telegram_sending(body: SendingSwitch, request: Request) -> dict:
    """Flip outbound delivery on the live adapter — inbound keeps working."""
    host = request.client.host if request.client else None
    try:
        local = host == "testclient" or (
            host is not None and ipaddress.ip_address(host).is_loopback)
    except ValueError:
        local = False
    if not local:
        raise HTTPException(403, "channel settings are local-only")
    channels = _telegram_channels(request)
    if not channels:
        raise HTTPException(404, "no telegram channel configured")
    for ch in channels:
        ch.sending_enabled = body.enabled
    return {"configured": True, "sending_enabled": body.enabled}
