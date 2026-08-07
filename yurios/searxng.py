"""Her search instance, as a container the runtime looks after (SPEC §7.7).

`SEARCH_BACKEND=searxng` needs something listening on `SEARXNG_URL`, and unlike
every other backend in this project that something is a *service*, not a pip
extra. A Python dependency is installed once and then simply exists; a container
can be missing, stopped, or running-but-misconfigured, and each of those is a
different sentence to say to the user. So the knowledge of what "set up" means
lives here, once, and the three places that need it — `install.sh` (create it),
`yurios start` (make sure it's up), `yurios doctor` (say what's true) — all ask
this module rather than each growing their own idea of it.

Two rules shape the whole file:

* **A container we didn't create is not ours to manage.** If `SEARXNG_URL` points
  somewhere that isn't loopback, the user has an instance already and this module
  reports on it without touching it.
* **A search instance that won't come up must not stop her booting.** Failing to
  start the container degrades to a warning and she runs without hands for the
  web, the same rule the voice stack and the camera already follow.

The settings file is mounted **read-only** on purpose. SearXNG's entrypoint
chowns `/etc/searxng` to its own uid when it can, which silently takes the user's
own config file away from them — `:ro` keeps the file theirs to edit, and the
container has no reason to write to it.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger("yurios.searxng")

CONTAINER = "yurios-searxng"
IMAGE = "searxng/searxng"
#: Where the settings file lives, relative to the repo root. Under `data/` so it
#: sits with the other per-install state rather than in the tree.
SETTINGS_DIR = Path("data") / "searxng"

#: The whole reason this file exists. Stock SearXNG serves HTML happily and
#: answers every JSON query with 403, because `search.formats` omits json — so
#: an instance somebody set up by hand is *usually* broken for our purposes, and
#: the symptom points at authentication. Ours is created with it on.
SETTINGS_TEMPLATE = """\
# YuriOS's SearXNG instance (SPEC §7.7). Created by install.sh; yours to edit.
#
# `use_default_settings: true` means this file is an OVERLAY on the image's own
# settings, so it only has to say what differs. The one thing that must be here
# is the json format: stock SearXNG disables it, and without it every search
# she makes comes back 403.
use_default_settings: true

server:
  secret_key: "{secret}"
  limiter: false          # a single local user is not a rate-limiting problem
  image_proxy: true

search:
  formats:
    - html
    - json
"""


def runtime() -> str | None:
    """`docker`, `podman`, or None. Podman's CLI is docker-compatible for every
    verb used here, and somebody who has it usually has it on purpose."""
    for candidate in ("docker", "podman"):
        if shutil.which(candidate):
            return candidate
    return None


def usable(cmd: str | None = None) -> bool:
    """Is the daemon actually reachable? `docker` on PATH proves nothing — the
    service may be stopped, or the user may not be in the docker group, and both
    of those look exactly like "installed" until you ask it something."""
    cmd = cmd or runtime()
    if not cmd:
        return False
    try:
        return subprocess.run([cmd, "info"], capture_output=True,
                              timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _run(cmd: str, *args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run([cmd, *args], capture_output=True, text=True,
                          timeout=timeout)


def is_local(url: str) -> bool:
    """Does this URL name an instance on this machine — i.e. is it ours?

    A remote `SEARXNG_URL` is somebody's existing instance. Reporting on it is
    fine; starting, stopping or recreating a container over it is not.
    """
    host = (urlparse(url).hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0")


def port_of(url: str, default: int = 8080) -> int:
    parsed = urlparse(url)
    return parsed.port or (443 if parsed.scheme == "https" else default)


def state(cmd: str | None = None) -> str:
    """`running` | `exited` | `missing` | `no-docker`."""
    cmd = cmd or runtime()
    if not cmd or not usable(cmd):
        return "no-docker"
    try:
        out = _run(cmd, "inspect", "-f", "{{.State.Running}}", CONTAINER)
    except (OSError, subprocess.SubprocessError):
        return "no-docker"
    if out.returncode != 0:
        return "missing"
    return "running" if out.stdout.strip() == "true" else "exited"


def settings_path(root: Path) -> Path:
    return root / SETTINGS_DIR / "settings.yml"


def write_settings(root: Path) -> tuple[Path, bool]:
    """Create the settings file if it isn't there. Returns (path, created).

    Never overwrites: after the first install this file is the user's, and a
    rerun of the installer silently reverting their engine choices would be a
    bad surprise. The secret key is generated per install — it signs this
    instance's own session cookies and nothing else.
    """
    import secrets

    path = settings_path(root)
    if path.is_file():
        return path, False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SETTINGS_TEMPLATE.format(secret=secrets.token_hex(32)),
                    encoding="utf-8")
    return path, True


def create(root: Path, *, url: str, cmd: str | None = None,
           pull: bool = True) -> tuple[bool, str]:
    """Create (or recreate) the container. Returns (ok, message)."""
    cmd = cmd or runtime()
    if not cmd:
        return False, "no container runtime found (install Docker)"
    if not usable(cmd):
        return False, (f"{cmd} is installed but not usable — is the daemon "
                       f"running, and are you in the `{cmd}` group?")
    path, _created = write_settings(root)
    port = port_of(url, default=8080)
    try:
        if pull:
            pulled = _run(cmd, "pull", IMAGE, timeout=900)
            if pulled.returncode != 0:
                return False, f"couldn't pull {IMAGE}: {pulled.stderr.strip()[:200]}"
        _run(cmd, "rm", "-f", CONTAINER)       # idempotent: replace any old one
        made = _run(cmd, "run", "-d", "--name", CONTAINER,
                    "--restart", "unless-stopped",
                    "-p", f"{port}:8080",
                    "-v", f"{path.parent.resolve()}:/etc/searxng:ro",
                    IMAGE, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"{cmd} failed: {e}"
    if made.returncode != 0:
        return False, made.stderr.strip()[:300]
    return True, f"created {CONTAINER} on port {port}"


def start(cmd: str | None = None) -> tuple[bool, str]:
    """Start an existing, stopped container."""
    cmd = cmd or runtime()
    if not cmd:
        return False, "no container runtime found"
    try:
        out = _run(cmd, "start", CONTAINER, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    return (out.returncode == 0), (out.stderr.strip()[:200] or "started")


def probe(url: str, *, timeout: float = 5.0) -> tuple[bool, str]:
    """Ask the instance the one question that matters: does it answer JSON?

    A 200 on the front page proves the container is alive and proves nothing
    about whether she can search — those are different failures with the same
    "it's running!" symptom, which is exactly why this asks for `format=json`.
    """
    import httpx

    try:
        resp = httpx.get(f"{url.rstrip('/')}/search",
                         params={"q": "yurios", "format": "json"},
                         timeout=timeout)
    except Exception as e:
        return False, f"not reachable ({type(e).__name__})"
    if resp.status_code == 403:
        return False, ("reachable, but refusing JSON — add `json` to "
                       "`search.formats` in its settings.yml and restart it")
    if resp.status_code != 200:
        return False, f"answered {resp.status_code}"
    try:
        resp.json()
    except (json.JSONDecodeError, ValueError):
        return False, "answered 200 but not JSON"
    return True, "answering JSON"


def wait_ready(url: str, *, seconds: float = 60.0) -> tuple[bool, str]:
    """Poll until the instance answers JSON, or give up. A cold SearXNG takes a
    few seconds to bind, and the first `probe` after `start` will always fail."""
    deadline = time.monotonic() + seconds
    ok, why = False, "never answered"
    while time.monotonic() < deadline:
        ok, why = probe(url)
        if ok:
            return True, why
        time.sleep(2)
    return ok, why


def ensure_running(cfg, root: Path, *, wait: float = 45.0) -> tuple[bool, str]:
    """Bring her search instance up if it isn't. Used by `yurios start`.

    Never raises and never blocks the boot: every failure path returns False
    with a sentence worth printing, and the caller carries on without web hands.
    """
    url = getattr(cfg, "searxng_url", "")
    if getattr(cfg, "search_backend", "off") != "searxng" or not url:
        return True, ""                        # not configured — nothing to do

    # Already answering? Then there is nothing to start, whoever owns it. On
    # loopback a refused connection comes back instantly, so this costs nothing
    # in the case where we do have work to do.
    ok, why = probe(url)
    if ok:
        return True, ""
    if not is_local(url):
        return False, f"{url} is {why}"        # somebody else's instance, and down

    current = state()
    if current == "running":
        # Up but not answering — a cold start we caught mid-boot, or the JSON
        # format is off. Give it a moment before saying either.
        return wait_ready(url, seconds=min(wait, 20.0))
    if current == "no-docker":
        return False, ("her search instance needs Docker, which isn't usable "
                       "here — she'll run without web search")
    if current == "missing":
        return False, (f"no {CONTAINER} container — run ./install.sh to create "
                       "it, or set SEARCH_BACKEND=off in .env")
    ok, why = start()
    if not ok:
        return False, f"couldn't start {CONTAINER}: {why}"
    ready, why = wait_ready(url, seconds=wait)
    return ready, ("" if ready else f"{CONTAINER} started but {why}")


def status(cfg, root: Path) -> dict:
    """Everything `yurios doctor` wants to say about web search, in one call."""
    backend = getattr(cfg, "search_backend", "off")
    url = getattr(cfg, "searxng_url", "")
    info = {"backend": backend, "url": url, "runtime": runtime(),
            "container": "", "live": False, "detail": ""}
    if backend == "off":
        info["detail"] = "off — she has no web hands"
        return info
    if backend == "fake":
        info["live"] = True
        info["detail"] = "deterministic offline rows (tests and demos)"
        return info
    # Ask the instance first, always. "Can she search right now" is a question
    # only the instance can answer, and our container is one of several ways
    # somebody might be running one — compose, systemd, a container under
    # another name. Leading with container state would tell a user with a
    # perfectly good instance to go and create ours, which is both wrong and
    # the kind of wrong that costs an afternoon.
    info["live"], info["detail"] = probe(url)
    if not is_local(url):
        info["container"] = "not ours (remote instance)"
        return info

    info["container"] = state()
    if info["live"]:
        if info["container"] == "missing":
            # Answering, but not from a container we know about. Say so plainly
            # rather than claiming credit: `yurios start` won't manage this one.
            info["container"] = "not ours (something else is serving this port)"
        return info

    # It isn't answering. NOW the container state is the useful explanation.
    if info["container"] == "no-docker":
        info["detail"] = ("Docker isn't usable here, so the container can't "
                          "run — install Docker, or set SEARCH_BACKEND=off")
    elif info["container"] == "missing":
        info["detail"] = (f"no {CONTAINER} container — rerun ./install.sh "
                          "--web-search to create it")
    elif info["container"] == "exited":
        info["detail"] = (f"{CONTAINER} is stopped — `yurios start` brings "
                          "it up with her")
    return info


# ---------------------------------------------------------------------------
# A tiny CLI, so install.sh doesn't have to keep its own copy of the container
# arguments and the settings template. One source of truth; bash calls it.
#
#   python -m yurios.searxng setup  --url http://localhost:8080
#   python -m yurios.searxng status --url http://localhost:8080

def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="yurios.searxng",
                                     description="her SearXNG instance")
    parser.add_argument("action", choices=("setup", "status", "check"))
    parser.add_argument("--url", default="http://localhost:8080")
    parser.add_argument("--root", default=".")
    parser.add_argument("--no-pull", action="store_true")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()

    if args.action == "check":                 # "can we even do this?" — no output
        return 0 if usable() else 1

    if args.action == "status":
        ok, why = probe(args.url)
        print(f"{'ok' if ok else 'not working'}: {why}")
        return 0 if ok else 1

    ok, why = create(root, url=args.url, pull=not args.no_pull)
    if not ok:
        print(f"Could not set up her search instance: {why}")
        return 1
    print(why)
    print("Waiting for it to answer…")
    ready, why = wait_ready(args.url, seconds=120)
    print(f"  {args.url} — {why}")
    return 0 if ready else 1


if __name__ == "__main__":                     # pragma: no cover — the CLI entry
    raise SystemExit(main())
