"""How YuriOS names itself to OpenRouter (https://openrouter.ai/docs/app-attribution).

OpenRouter keys an *app page* on the `HTTP-Referer` URL of the request — that URL
IS the identity, the title is only the label drawn on it. Two consequences shape
this module:

  * every billed OpenRouter call in the repo must send the SAME url, or the usage
    splits across two app pages that can never be merged. Hence one constant here
    rather than a copy per backend (chat → app/providers/openrouter.py, images →
    forge/backends/openrouter.py);
  * a call that sends no referer is invisible: it is billed to the key, but it
    never reaches the app page or the rankings.

Usage then shows up at openrouter.ai/apps?url=https://yurios.org (models over time,
prompt/completion tokens) and on the model pages' "Apps" tab.
"""
from __future__ import annotations

import sys

# The identity. APP_URL is the primary domain on purpose — a subdomain would be
# read as a different app.
APP_URL = "https://yurios.org"
APP_TITLE = "YuriOS"

# Marketplace categories (openrouter.ai/apps). Only recognised slugs are kept;
# anything else is silently dropped, so these are copied from the docs' list.
# OpenRouter merges what it sees across requests, so the image backend can add
# `image-gen` from its own calls without losing these.
CATEGORIES = ("roleplay", "personal-agent")

# OpenRouter accepts at most two categories per request.
_MAX_CATEGORIES = 2


def _version() -> str:
    try:
        from importlib.metadata import version
        return version("yurios")
    except Exception:                   # a tree that was never pip-installed
        return "0"


APP_VERSION = _version()


def client_token(dist: str) -> str:
    """`litellm/1.93.0`, read from installed metadata rather than by importing the
    library — so the doctor can name the client as cheaply as the caller does."""
    try:
        from importlib.metadata import version
        return f"{dist}/{version(dist)}"
    except Exception:                   # not installed, or no metadata
        return dist


# The camera posts with the standard library, which signs itself this way.
URLLIB_CLIENT = f"Python-urllib/{sys.version_info.major}.{sys.version_info.minor}"


def user_agent(client: str = "") -> str:
    """`YuriOS/0.1.0 litellm/1.93.0` — us first, then whatever is actually holding
    the socket. Left alone, each path announces only its plumbing (`litellm/…` for
    chat, `Python-urllib/…` for the camera) and YuriOS appears nowhere in a
    provider's logs. The composite is the honest version: a user-agent is a stack
    of `Product/Version` tokens, most specific first, and dropping the client
    would hide the one fact worth having when a route misbehaves.

    This changes nothing about attribution — OpenRouter keys the app page on the
    referer — so it is for reading logs and filing tickets, no more."""
    return f"{APP_TITLE}/{APP_VERSION} {client}".strip()


def headers(*, client: str = "",
            categories: tuple[str, ...] = CATEGORIES) -> dict[str, str]:
    """The attribution headers for one OpenRouter request. `client` names the
    library making it, for the composite user-agent."""
    out = {
        "User-Agent": user_agent(client),
        "HTTP-Referer": APP_URL,
        "X-OpenRouter-Title": APP_TITLE,
        # `X-Title` is the older name for the same thing, still honoured. We send
        # both deliberately: LiteLLM puts its own `X-Title: liteLLM` on every
        # openrouter/… request, so dropping ours would leave that default on the
        # wire and the app could come back labelled "liteLLM".
        "X-Title": APP_TITLE,
    }
    if categories:
        out["X-OpenRouter-Categories"] = ",".join(categories[:_MAX_CATEGORIES])
    return out
