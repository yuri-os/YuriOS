"""Can her model's server be reached — and, when it is ours to wake, wake it
(SPEC §31.5).

Every other part of boot degrades loudly or not at all; this is the one that
used to degrade in silence. A house whose CHAT_MODEL is `lm_studio/…` booted
with LM Studio closed logged one "none pinned" line, opened the room, and left
the first turn to fail — or, with the GGUF fallback on, quietly moved her onto
llama.cpp with an id that was never a Hugging Face repo, where she stayed after
LM Studio came up. Nothing said "the server isn't there", and nothing offered
to fix it.

This module is the answer to both halves, with no knowledge of runtimes, rooms
or events (world/connection.py holds those):

  - **Which servers she depends on.** Her chat model, her utility model and a
    server-backed embedder each live behind one, and two of them on the same
    server are one dependency, not two — so `links_for` groups the seams by
    (provider, endpoint) and names the roles each one serves.
  - **Whether each answers.** One cheap GET per server, never a completion: a
    model listing for a local server (and whether the model she names is in
    it), the key check for OpenRouter. A 401/403 is a key the server wants, not
    a server that is missing (§31.1). A hosted route LiteLLM reaches that has no
    free check (`anthropic/…`, `openai/…`) is `unchecked`, and its failures are
    learned from the turn that hit them.
  - **Starting a local one.** A server on this machine that we know how to run
    — LM Studio through its `lms` CLI, Ollama through `ollama serve` — can be
    started and waited for. Nothing else is: a remote server is somebody else's
    process, and an OpenAI-compatible one on localhost has no command we could
    know. The argv is built here from the binary we found and the port in the
    configured url; nothing a request carries reaches it.

Blocking on purpose (httpx sync, subprocess): every caller is a worker thread or
an `asyncio.to_thread`, because the host is one process holding every character
on the node (§2.4).
"""
from __future__ import annotations

import ipaddress
import logging
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from urllib.parse import urlsplit

import httpx

log = logging.getLogger("mvw.reach")

OK = "ok"
DOWN = "down"            # nothing answered at the address
REFUSED = "refused"      # it answered, and wants a key it was not given (or refused ours)
MISSING = "missing"      # it answered, and has no model by the name she uses
FAILED = "failed"        # a turn through it failed and the probe cannot say why
STARTING = "starting"    # we are bringing it up right now
UNCHECKED = "unchecked"  # no free way to ask; her turns are the check

#: Worst first. The room shows the first link on this list that is not fine.
SEVERITY = (DOWN, REFUSED, FAILED, MISSING, STARTING, UNCHECKED, OK)
#: States that mean "her replies will not work through this", what the banner
#: and /api/health's `degraded` treat as trouble.
TROUBLE = frozenset({DOWN, REFUSED, FAILED, MISSING})

_LABELS = {"lmstudio": "LM Studio", "ollama": "Ollama",
           "openai_compatible": "OpenAI-compatible server",
           "openrouter": "OpenRouter", "hosted": "hosted model"}

_PROBE_TIMEOUT_S = 4.0
_OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"


@dataclass
class Link:
    """One server she depends on, and what it said the last time it was asked."""
    provider: str                 # lmstudio | ollama | openai_compatible | openrouter | hosted
    endpoint: str                 # the url as configured ("" for a hosted route)
    roles: list[str] = field(default_factory=list)     # chat · utility · memory
    models: list[str] = field(default_factory=list)    # ids as the server names them
    api_key: str = field(default="", repr=False)       # never on the wire (as_dict)
    state: str = UNCHECKED
    detail: str = ""

    @property
    def label(self) -> str:
        return _LABELS.get(self.provider, self.provider)

    @property
    def local(self) -> bool:
        return is_loopback(self.endpoint)

    @property
    def startable(self) -> bool:
        """Ours to start: on this machine, and a server we know the command for."""
        return self.local and launch_argv(self) is not None

    def as_dict(self) -> dict:
        out = asdict(self)
        out.pop("api_key", None)
        out.update(label=self.label, local=self.local, startable=self.startable,
                   where=display_endpoint(self))
        return out


def is_loopback(url: str) -> bool:
    host = (urlsplit(url).hostname or "") if url else ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def display_endpoint(link: Link) -> str:
    """`localhost:1234` — where, in the few characters a banner has room for."""
    if not link.endpoint:
        return "openrouter.ai" if link.provider == "openrouter" else ""
    parts = urlsplit(link.endpoint)
    return parts.netloc or link.endpoint


def _port(url: str, default: int) -> int:
    try:
        return urlsplit(url).port or default
    except ValueError:
        return default


# --- which servers she depends on -------------------------------------------

def _route(cfg, model: str) -> tuple[str, str, str, str] | None:
    """(provider, endpoint, model id as that server names it, key) for a LiteLLM
    model id — or None for a route with no server of its own to ask (an unset
    model, or a direct gguf/… file that runs in-process)."""
    model = (model or "").strip()
    if not model or model.upper() == "NONE" or model.startswith("gguf/"):
        return None
    if model.startswith("lm_studio/"):
        return ("lmstudio", cfg.lmstudio_base_url, model.split("/", 1)[1],
                cfg.lmstudio_api_key)
    if model.startswith("ollama/"):
        return ("ollama", cfg.ollama_base_url, model.split("/", 1)[1],
                getattr(cfg, "connection_api_key", ""))
    if model.startswith("openai_compatible/"):
        return ("openai_compatible", cfg.openai_compatible_base_url,
                model.split("/", 1)[1], cfg.openai_compatible_api_key)
    if model.startswith(("openai/", "anthropic/")):
        return ("hosted", "", model, "")
    # a bare id is OpenRouter's (providers/openrouter._route)
    return ("openrouter", "", model.removeprefix("openrouter/"), cfg.openrouter_api_key)


def links_for(cfg, *, chat: bool = True, utility: bool = True,
              embed: bool = True) -> list[Link]:
    """The servers behind the seams being built, unprobed, one per server.

    The flags are the seams this process actually builds — an injected fake
    reaches no server, so a test that injects its model never asks one."""
    seams: list[tuple[str, tuple[str, str, str, str] | None]] = []
    if chat:
        seams.append(("chat", _route(cfg, cfg.chat_model)))
        if getattr(cfg, "utility_enabled", False) and utility:
            seams.append(("utility", _route(cfg, cfg.utility_model)))
    if embed:
        backend = getattr(cfg, "embed_backend", "")
        if backend == "lm_studio":
            seams.append(("memory", ("lmstudio", cfg.lmstudio_base_url,
                                     cfg.embed_model, cfg.lmstudio_api_key)))
        elif backend == "ollama":
            seams.append(("memory", ("ollama", cfg.ollama_base_url,
                                     cfg.embed_model, "")))
    links: dict[tuple[str, str], Link] = {}
    for role, route in seams:
        if route is None:
            continue
        provider, endpoint, model, key = route
        key_ = (provider, endpoint.rstrip("/"))
        link = links.setdefault(key_, Link(provider=provider, endpoint=endpoint,
                                           api_key=key))
        if role not in link.roles:
            link.roles.append(role)
        if model not in link.models:
            link.models.append(model)
        if key and not link.api_key:
            link.api_key = key
    return list(links.values())


# --- asking -----------------------------------------------------------------

def default_transport() -> httpx.BaseTransport | None:
    """The transport probes use when none is passed — the real network. The test
    suite replaces this (tests/conftest.py) so no probe ever leaves the box."""
    return None


def _client(link: Link, transport) -> httpx.Client:
    headers = {"Authorization": f"Bearer {link.api_key}"} if link.api_key else {}
    return httpx.Client(timeout=_PROBE_TIMEOUT_S, headers=headers,
                        transport=transport if transport is not None
                        else default_transport())


def _root(url: str) -> str:
    root = url.rstrip("/")
    return root[: -len("/v1")] if root.endswith("/v1") else root


def _matches(want: str, have: Sequence[str]) -> bool:
    """Is *want* among the ids a server listed? Generous on purpose: a false
    "missing" in red is worse than a missing model the turn reports itself.
    Case-blind; a publisher prefix (`qwen/…`) and Ollama's `:latest` are
    ignored on either side."""
    def forms(name: str) -> set[str]:
        name = name.strip().lower()
        tail = name.rsplit("/", 1)[-1]
        out = {name, tail}
        out |= {n.removesuffix(":latest") for n in list(out)}
        return out
    wanted = forms(want)
    return any(wanted & forms(name) for name in have if name)


def _listed(link: Link, client: httpx.Client) -> list[str] | None:
    """The model ids the server says it can serve, or None if it can't say
    (which is not a fault — the check just skips the model test)."""
    if link.provider == "ollama":
        data = client.get(f"{link.endpoint.rstrip('/')}/api/tags").raise_for_status().json()
        return [m.get("name", "") for m in data.get("models") or []]
    base = link.endpoint.rstrip("/")
    data = client.get(f"{base}/models").raise_for_status().json()
    ids = [m.get("id", "") for m in data.get("data") or []]
    if link.provider != "lmstudio":
        return ids
    # LM Studio's /v1/models lists only what is loaded when JIT loading is off,
    # and nothing is loaded before the pin — so ask the developer API, which
    # lists what is on disk. An older build without one: the OpenAI list it is.
    try:
        catalog = client.get(f"{_root(base)}/api/v1/models").raise_for_status().json()
        ids += [m.get("key", "") for m in catalog.get("models") or []]
        ids += [f"{m.get('publisher', '')}/{m.get('key', '')}"
                for m in catalog.get("models") or [] if m.get("publisher")]
    except Exception:              # noqa: BLE001 — optional, see above
        pass
    return ids


def probe(link: Link, *, transport: httpx.BaseTransport | None = None) -> Link:
    """Ask the server once and return the link with `state` and `detail` filled.
    Never raises: every way of not answering is a state."""
    where = display_endpoint(link)
    if link.provider == "hosted":
        return replace(link, state=UNCHECKED,
                       detail="no free check for this route — her turns will say")
    if link.provider == "openrouter":
        return _probe_openrouter(link, transport)
    if not link.endpoint:
        return replace(link, state=DOWN,
                       detail=f"no address is set for the {link.label}")
    try:
        with _client(link, transport) as client:
            listed = _listed(link, client)
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        if status in (401, 403):
            return replace(link, state=REFUSED, detail=(
                f"{link.label} at {where} refused the API key it was sent"
                if link.api_key else
                f"{link.label} at {where} requires an API key") + f" (HTTP {status})")
        return replace(link, state=DOWN,
                       detail=f"{link.label} at {where} answered HTTP {status}")
    except (httpx.ConnectError, httpx.ConnectTimeout):
        hint = " — it isn't running" if link.local else ""
        return replace(link, state=DOWN,
                       detail=f"{link.label} isn't answering at {where}{hint}")
    except Exception as e:         # noqa: BLE001 — a timeout, bad json, anything
        return replace(link, state=DOWN,
                       detail=f"{link.label} at {where} didn't answer properly "
                              f"({type(e).__name__})")
    if listed:
        absent = [m for m in link.models if not _matches(m, listed)]
        if absent:
            return replace(link, state=MISSING, detail=(
                f"{link.label} is running at {where}, but has no model "
                f"called {absent[0]!r} — download it there, or choose another"))
    return replace(link, state=OK, detail=f"{link.label} at {where}")


def _probe_openrouter(link: Link, transport) -> Link:
    if not link.api_key:
        return replace(link, state=REFUSED,
                       detail="OpenRouter needs an API key (OPENROUTER_API_KEY)")
    try:
        with _client(link, transport) as client:
            r = client.get(_OPENROUTER_KEY_URL)
    except Exception as e:         # noqa: BLE001
        return replace(link, state=DOWN,
                       detail=f"couldn't reach OpenRouter ({type(e).__name__}) — "
                              "is this machine online?")
    if r.status_code in (401, 403):
        return replace(link, state=REFUSED,
                       detail=f"OpenRouter refused the API key (HTTP {r.status_code})")
    if r.status_code >= 500:
        return replace(link, state=DOWN,
                       detail=f"OpenRouter is having trouble (HTTP {r.status_code})")
    # 2xx is a key it knows; anything else (a moved endpoint) is still a
    # server that answered — her turns are the rest of the check.
    return replace(link, state=OK, detail="OpenRouter · key accepted")


def check(links: Sequence[Link], *,
          transport: httpx.BaseTransport | None = None) -> list[Link]:
    return [probe(link, transport=transport) for link in links]


def worst(links: Sequence[Link]) -> str:
    states = {link.state for link in links}
    return next((s for s in SEVERITY if s in states), OK)


# --- starting a local one ---------------------------------------------------

def _lms_binary() -> str | None:
    found = shutil.which("lms")
    if found:
        return found
    home = Path.home() / ".lmstudio" / "bin" / "lms"
    return str(home) if home.is_file() and os.access(home, os.X_OK) else None


def launch_argv(link: Link) -> list[str] | None:
    """The command that starts this server here, or None when there isn't one.

    Built only from a binary found on this machine and the port in the
    configured url — never from anything a request carried."""
    if link.provider == "lmstudio":
        lms = _lms_binary()
        if lms:
            return [lms, "server", "start", "--port", str(_port(link.endpoint, 1234))]
    if link.provider == "ollama":
        ollama = shutil.which("ollama")
        if ollama:
            return [ollama, "serve"]
    return None


#: (argv, env, detach) → None. `detach` is a long-lived server (`ollama serve`)
#: to leave running on its own; otherwise the command returns once it has done
#: its work (`lms server start`), and a non-zero exit raises.
Launcher = Callable[[list[str], dict[str, str], bool], None]


def _run(argv: list[str], env: dict[str, str], detach: bool) -> None:
    if detach:
        # its own session, so it outlives her — the server is the machine's,
        # like LM Studio's daemon, not a child of this process
        subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return
    done = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=120)
    if done.returncode != 0:
        said = (done.stderr or done.stdout or "").strip().splitlines()
        raise RuntimeError(said[-1] if said else f"exit {done.returncode}")


def default_launcher() -> Launcher:
    """What `start` runs commands with when none is passed. The suite replaces
    it (tests/conftest.py): no test may start a server on the developer's box."""
    return _run


_START_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_START_LOCKS_GUARD = threading.Lock()


def _start_lock(link: Link) -> threading.Lock:
    key = (link.provider, link.endpoint.rstrip("/"))
    with _START_LOCKS_GUARD:
        return _START_LOCKS.setdefault(key, threading.Lock())


def start(link: Link, *, wait_s: float = 60.0, poll_s: float = 0.5,
          launcher: Launcher | None = None,
          transport: httpx.BaseTransport | None = None,
          sleep: Callable[[float], None] = time.sleep,
          monotonic: Callable[[], float] = time.monotonic) -> Link:
    """Start a local server and wait until it answers; return it re-probed.

    One start per server at a time, process-wide: a house boots every character
    on its own worker thread, and five of them sharing one LM Studio must not
    run five `lms server start`s. The ones that queue behind the lock re-probe
    first and find it up. Never raises — a server that would not come up is a
    `down` link whose detail says why."""
    argv = launch_argv(link) if link.local else None
    if argv is None:
        why = ("it is not on this machine" if not link.local
               else f"no command to start the {link.label} was found here")
        now = probe(link, transport=transport)
        if now.state != DOWN:
            return now
        return replace(now, detail=f"{now.detail} — can't start it: {why}")
    with _start_lock(link):
        now = probe(link, transport=transport)
        if now.state != DOWN:
            return now                  # somebody (or something) beat us to it
        env = dict(os.environ)
        if link.provider == "ollama":
            parts = urlsplit(link.endpoint)
            env["OLLAMA_HOST"] = f"{parts.hostname or '127.0.0.1'}:{_port(link.endpoint, 11434)}"
        log.info("starting %s: %s", link.label, " ".join(argv))
        try:
            (launcher or default_launcher())(argv, env, link.provider == "ollama")
        except Exception as e:      # noqa: BLE001 — a failed start is a state
            log.warning("could not start %s (%s)", link.label, e)
            return replace(now, detail=f"{link.label} would not start: {str(e)[:160]}")
        deadline = monotonic() + wait_s
        while True:
            now = probe(link, transport=transport)
            if now.state != DOWN or monotonic() >= deadline:
                break
            sleep(poll_s)
        if now.state == DOWN:
            return replace(now, detail=f"{link.label} was started but still isn't "
                                       f"answering at {display_endpoint(link)}")
        log.info("%s is up at %s", link.label, display_endpoint(link))
        return now
