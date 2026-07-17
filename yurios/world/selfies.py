"""The selfie lab (SPEC §7.6) — her camera, realised host-side, start-don't-await.

`take_selfie` is the fourth hand, and it teaches the one tool pattern the other
three couldn't: **a slow tool must not sit inside the turn.** A hosted render
takes 10–30 s; parking the tool loop on it would mean dead air after her lead-in
sentence and a per-call timeout tuned for weather lookups. So this follows the
YuriOS `ExecEffector.spawn` rule — *ACT starts work; it never awaits it*:

    pass 1:   "hold on, let me take one~ [[take_selfie {"scene": "window"}]]"
    server:   validates the args, returns {"status": "started", …}  (§7.2 contract)
    pass 2:   she finishes the turn knowing the shot is coming — no dead air
    (async):  the lab renders off-turn → saves the PNG → posts a `message` with
              `image_url` on the hub (the chat shows the photo) → best-effort
              ambient cue so she says one line about it if she's free (§8.3);
              if she's mid-conversation the cue is dropped — the image is
              already in the chat, and she never talks over you.

The generator behind it is the forge (./forge, → ch. 26): the locked
register + the selfie template library + provenance, with the backend swappable.
Default is `openrouter` on a cheap image model (seedream — the GPU stays free
for her voice; point SELFIE_MODEL at riverflow for the brand-art register);
`mock` renders deterministic placeholder cards for tests and keyless machines.
A missing key degrades to mock with one loud WARNING, the same
graceful-fallback rule as the voice stack (B2 §3).

A failed render is a quiet `message` in the chat and an audit-style log line —
never a crash, and never silence about a promise she made.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .clock import Clock

log = logging.getLogger("world.selfies")

FORGE_DIR = Path(__file__).resolve().parent.parent / "forge"

# the announce cue (§8.3): spoken only if she's free, through the ambient seam
ANNOUNCE_CUE = (
    "((The selfie you just took is ready — it's visible in the chat now "
    "({detail}). Say one short, warm line about it, nothing else.))")


def build_forge(cfg) -> tuple[object, str]:
    """The forge behind the lab, from config. Returns (forge, status) where
    status is what /api/health reports: "openrouter" | "mock" | "mock (…)"."""
    from yurios.forge import Character, ImageForge, SelfieBook, make_backend

    character = Character.load(FORGE_DIR / "characters" / "yuri.yaml")
    book = SelfieBook.load(FORGE_DIR / "templates" / "selfie.yaml")

    name, status = cfg.selfie_backend, cfg.selfie_backend
    if name == "openrouter":
        backend = make_backend("openrouter", model=cfg.selfie_model,
                               api_key=cfg.openrouter_api_key)
        if not backend.health():               # no key anywhere → degrade loudly
            log.warning(
                "selfies: no OpenRouter key found — degrading to the mock "
                "backend (placeholder cards). Set OPENROUTER_API_KEY in .env "
                "to give her a real camera.")
            backend, status = make_backend("mock"), "mock (no key — placeholder)"
    else:
        backend = make_backend(name)

    return (ImageForge(character, book, backend,
                       out_dir=cfg.selfie_dir, provenance_mode="strip"),
            status)


class SelfieLab:
    """Owns the render tasks. `start()` is the §7.5 host-side realisation."""

    def __init__(self, forge, *, clock: Clock,
                 post: Callable[..., dict],
                 speak: Callable[[str], Awaitable[bool]]):
        self.forge = forge
        self.clock = clock
        self.post = post                       # Runtime.post_message
        self.speak = speak                     # Runtime.speak_ambient (§8.4)
        self._tasks: set[asyncio.Task] = set()

    def start(self, contract: dict) -> None:
        """Spawn one render from the tool's validated contract. Never blocks,
        never raises — the turn that asked is already moving on."""
        task = asyncio.create_task(self._job(dict(contract)),
                                   name=f"selfie-{contract.get('id', '?')}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _job(self, c: dict) -> None:
        scene, mood = c.get("scene") or None, c.get("mood") or None
        wardrobe = c.get("wardrobe") or "everyday"   # the tier she asked the
        # tool for; unprompted shots stay in the everyday default (→ ch. 11:
        # the yaml gates nothing — whether a tier renders is the backend's call)
        try:
            result = await asyncio.to_thread(
                self.forge.selfie, scene=scene, mood=mood, wardrobe=wardrobe,
                save=False)
            stamp = int(self.clock.now())
            name = f"{stamp}-{c.get('id', 'x')}.png"
            path = Path(self.forge.out_dir) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(result.data)
            self.forge._write_provenance(path, result.meta)   # the ledger (→ ch. 26)
        except Exception as e:                 # render failed: say so, quietly
            log.exception("selfie render failed")
            self.post("assistant",
                      f"(the selfie didn't come out — {type(e).__name__})",
                      proactive=True)
            return

        chosen = result.meta.get("template", {})
        detail = ", ".join(v for v in (chosen.get("scene"), chosen.get("mood")) if v)
        self.post("assistant", "", image_url=f"/selfies/{name}", proactive=True)
        # one soft line about it, only if she's free — a drop is fine (§8.3):
        # unlike a timer, the photo itself already landed.
        try:
            await self.speak(ANNOUNCE_CUE.format(detail=detail or "a new shot"))
        except Exception:
            log.exception("selfie announce failed")

    async def close(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
