"""The selfie lab (SPEC §7.6) — her camera, realised host-side, start-don't-await.

`take_selfie` is the fourth hand, and it teaches the one tool pattern the other
three couldn't: **a slow tool must not sit inside the turn.** A hosted render
takes 10–30 s; parking the tool loop on it would mean dead air after her lead-in
sentence, against a per-call timeout sized for hands that answer at once. So
this follows the
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
#: The house library, and the fallback for every character with no book of her own.
SHIPPED_BOOK = FORGE_DIR / "templates" / "selfie.yaml"


def book_path(own: str | Path | None) -> Path:
    """Which library the camera composes from — hers, or the house's.

    A character's own `selfie.yaml` *replaces* the shipped book rather than
    merging over it (characters/selfiebook.py): the shipped one describes one
    character's world down to the tail in half its scenes, and an overlay can
    add rows to that but can never take them back out. No file — which is every
    character until somebody edits her library — means the shipped defaults,
    unchanged. Absence is the normal case and is not worth a warning; the
    env-level overlay still layers on top of whichever base wins.
    """
    if own and Path(own).is_file():
        return Path(own)
    return SHIPPED_BOOK

# the announce cue (§8.3): spoken only if she's free, through the ambient seam.
# Two words for two cameras — "the selfie you just took" is a strange thing to
# say about a photo of the rain, and she should sound like she knows which of
# the two she reached for.
ANNOUNCE_CUE = (
    "((The {noun} you just took is ready — it's visible in the chat now "
    "({detail}). Say one short, warm line about it, nothing else.))")


def _lightweight(exc: Exception) -> Exception:
    """The same failure, without the frames that pin a pipeline's VRAM.

    Re-raising the original would carry its traceback — and on the OOM path that
    traceback is holding the gigabytes the caller is about to try to hand back.
    Same class, same message (the chat line and the ledger both read
    ``type(e).__name__``), no frames. A class with an exotic constructor is not
    worth failing over: it degrades to a RuntimeError that still says the name.
    """
    try:
        return type(exc)(str(exc))
    except Exception:
        return RuntimeError(f"{type(exc).__name__}: {exc}")


def _noun(kind: str) -> str:
    """What to call the thing she just made. `kind` comes off the tool contract;
    a contract from before there were two cameras has none, and a selfie is the
    right thing to assume."""
    return "picture" if kind == "picture" else "selfie"


def _unprompted(contract: dict) -> bool:
    """Did she take this one unasked? — the `proactive` marking the photo is
    committed with (§15.5).

    The render outlives the turn that started it, so the lab cannot look at the
    conversation to answer this; whoever built the contract stamps `_proactive`
    while the turn is still in scope (world/brain.py). A contract without it is
    one nobody was talking during — a dream's picture, a host call — and that
    is her speaking first, which is also the old behaviour.
    """
    return bool(contract.get("_proactive", True))


def _identity(cfg):
    """Whose face the camera renders (SPEC §7.6).

    `SELFIE_CHARACTER` unset means the house's own character — the shipped Yuri
    — which is right for a single-character install and is what every existing
    .env expects. A character runtime sets it to her `appearance.yaml`, and if
    that file is missing she gets the neutral stand-in rather than inheriting
    whoever happens to be shipped: a photo of the wrong person is worse than a
    photo of no one, and it is the failure that let Lumina's selfies come back
    with Yuri's cat ears and Yuri's name in the provenance sidecar.
    """
    from yurios.forge import Character

    path = Path(cfg.selfie_character) if cfg.selfie_character else None
    if path is None:
        return Character.load(FORGE_DIR / "characters" / "yuri.yaml")
    if path.is_file():
        return Character.load(path, defaults=Character.register())
    log.warning(
        "selfies: %s has no appearance file at %s — rendering a neutral "
        "stand-in rather than another character's likeness. Run "
        "`python -m yurios.characters appearance <id>` to derive one from "
        "her card.", getattr(cfg, "companion_name", "this character"), path)
    return Character.neutral(getattr(cfg, "companion_name", "") or "her")


def build_forge(cfg) -> tuple[object, str]:
    """The forge behind the lab, from config. Returns (forge, status) where
    status is what /api/health reports: "openrouter" | "mock" | "mock (…)"."""
    from yurios.forge import Character, ImageForge, SelfieBook, make_backend

    character = _identity(cfg)
    overlays = []
    if cfg.selfie_templates_extra:             # personal registers (user file)
        extra = Path(cfg.selfie_templates_extra)
        if extra.is_file():
            overlays.append(extra)
        else:
            log.warning("selfies: SELFIE_TEMPLATES_EXTRA points at %s, which "
                        "doesn't exist — using the shipped library alone", extra)
    base = book_path(cfg.selfie_templates)
    if base != SHIPPED_BOOK:
        log.info("selfies: composing from %s's own template library (%s)",
                 getattr(cfg, "companion_name", "this character"), base)
    book = SelfieBook.load(base, overlays=overlays)

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
    elif name in ("diffusers", "krea2"):
        # One knob, two architectures: SELFIE_LOCAL_MODEL may be an SDXL UNet
        # or a Krea 2 transformer, and they need entirely different loaders.
        # The checkpoint says which it is (its safetensors header, read without
        # torch), so the user doesn't have to — an explicit SELFIE_BACKEND=krea2
        # still wins, for a file whose header is unhelpful.
        from yurios.forge.backends.sniff import sniff_local_checkpoint_architecture
        if name == "diffusers":
            arch = sniff_local_checkpoint_architecture(cfg.selfie_local_model)
            if arch == "krea2":
                log.info("selfies: %s is a Krea 2 checkpoint — using the krea2 "
                         "backend (SELFIE_BACKEND=diffusers picks the loader "
                         "from the file).", Path(cfg.selfie_local_model).name)
                name = status = "krea2"

        if name == "krea2":
            backend = make_backend(
                "krea2", model_path=cfg.selfie_local_model,
                device=cfg.selfie_local_device, steps=cfg.selfie_krea2_steps,
                cfg=cfg.selfie_krea2_cfg,
                cpu_offload=cfg.selfie_local_cpu_offload)
            hint = ("Fix: pip install -e \".[forge-krea2]\" and point "
                    "SELFIE_LOCAL_MODEL in .env at a Krea 2 .safetensors "
                    "checkpoint (see .env.example).")
        else:
            backend = make_backend(
                "diffusers", model_path=cfg.selfie_local_model,
                device=cfg.selfie_local_device, steps=cfg.selfie_local_steps,
                cfg=cfg.selfie_local_cfg, hires=cfg.selfie_local_hires,
                hires_scale=cfg.selfie_local_hires_scale,
                hires_denoise=cfg.selfie_local_hires_denoise,
                cpu_offload=cfg.selfie_local_cpu_offload)
            hint = ("Fix: pip install -e \".[forge-local]\" and point "
                    "SELFIE_LOCAL_MODEL in .env at an SDXL .safetensors "
                    "checkpoint (e.g. a Pie Model from Civitai — see "
                    ".env.example).")

        if not backend.health():               # no deps/checkpoint → degrade loudly
            log.warning("selfies: the %s backend can't run — degrading to the "
                        "mock backend (placeholder cards). %s", name, hint)
            backend, status = make_backend("mock"), f"mock ({name} unavailable — placeholder)"
    else:
        backend = make_backend(name)

    return (ImageForge(character, book, backend,
                       out_dir=cfg.selfie_dir, provenance_mode="strip"),
            status)


class SelfieLab:
    """Owns the render tasks. `start()` is the §7.5 host-side realisation."""

    def __init__(self, forge, *, clock: Clock,
                 post: Callable[..., dict],
                 speak: Callable[[str], Awaitable[bool]],
                 notify: Optional[Callable[[str, dict], None]] = None,
                 parker=None,
                 quiet: Optional[Callable[[], Awaitable[None]]] = None,
                 situation: Optional[Callable[[], str]] = None):
        self.forge = forge
        self.clock = clock
        self.post = post                       # Runtime.post_message
        self.speak = speak                     # Runtime.speak_ambient (§8.4)
        self.notify = notify                   # EventHub.publish, when hosted
        self.parker = parker                   # LLMParker | None (world/vram.py)
        self.quiet = quiet                     # Runtime.wait_turns_idle | None
        self.situation = situation             # () -> visual facts about now
        self._tasks: set[asyncio.Task] = set()
        self._task_ids: dict[asyncio.Task, str] = {}
        self._contracts: dict[asyncio.Task, dict] = {}
        # One camera, one VRAM loan and one boolean ParkGate. Serialisation keeps
        # one job from reopening the gate under another job's active render.
        self._render_lock = asyncio.Lock()

    def _compose(self, kind: str, kw: dict):
        """Which of the two cameras this contract asked for (§7.6).

        Both end in the same backend, the same register and the same provenance
        ledger; the only difference is whether her likeness is in the frame.
        Keeping the choice here — rather than in two parallel jobs — means the
        parking, the gate, the cancellation dance and the announce path are
        written once and cannot drift apart.
        """
        if kind == "picture":
            return self.forge.picture(kw.get("subject", ""),
                                      avoid=kw.get("avoid", ""), save=False)
        return self.forge.selfie(**kw)

    def _render(self, kind: str = "selfie", **kw):
        """One render, borrowing the LLM's VRAM when the card needs it: the
        parker unloads her models (LM Studio's over HTTP, direct-GGUF's in
        process) for the render's duration and brings them back after
        (finally — a failed render never strands her brain). A no-op context
        when no parker is wired (tests, hosted backends).

        When the loan happened, the render pipeline is released BEFORE the
        restore: the cached pipeline and the re-pinning chat model are the two
        things that don't fit on the card at once, so keeping the pipeline
        warm would make the restore fail with the card still full. In a
        `finally`, because a render that *died* strands its weights just as
        surely as one that finished — and OOM, the likeliest way to get here,
        is precisely the case where the card can least afford it.

        It is released after a render that *didn't* park too, whenever the card
        has no room left for her brain to come home beside it. That case is
        why selfies "fail randomly": the render that keeps the pipeline warm is
        never the one that fails. The next turn reloads her brain next to it,
        the card fills, and the render after that parks — which frees her brain
        and nothing else, and cannot reach a floor that assumes an empty card.
        One selfie after a restart works and the rest die of OOM.

        A render that *died* needs one more thing, and it is the difference
        between "this selfie failed" and "every selfie fails until you restart":
        the exception must be dead before the release. An OOM traceback holds a
        frame for every call down into the UNet, and those frames hold `pipe`
        and the hires pass's `i2i` — so a `_release()` running while it is still
        alive drops the backend's handle to a pipeline the traceback is still
        pinning, and hands back nothing. The card then sits at 14.9 of 15.5 GiB
        with nothing rendering, and the next render has no room to fail in
        either. So the failure is caught HERE, inside the loan: the traceback
        dies with the `except` clause, the release actually frees, and only then
        does `parked()` bring her brain home. What reaches the caller is a light
        copy that still answers to the same name."""
        if self.parker is None:
            return self._compose(kind, kw)
        result = error = None
        with self.parker.parked() as borrowed:
            try:
                result = self._compose(kind, kw)
            except Exception as exc:           # not BaseException: cancellation
                log.exception("%s render failed on the card", kind)
                error = _lightweight(exc)
            finally:
                # A failed render always gives the pipeline back, whatever the
                # card looks like: the likeliest way to get here is OOM, and a
                # half-run pipeline is the last thing worth keeping warm.
                if error is not None or borrowed or not self._can_stay_warm():
                    self._release()
        if error is not None:
            raise error
        return result

    def _can_stay_warm(self) -> bool:
        """Ask the parker whether a warm pipeline still leaves room for her
        brain. A parker that doesn't answer (a stand-in, a test double) keeps
        the old behaviour — the same duck typing `_release` uses."""
        ask = getattr(self.parker, "can_keep_pipeline_warm", None)
        if not callable(ask):
            return True
        try:
            return bool(ask())
        except Exception:
            log.exception("selfie: couldn't measure the card — releasing the "
                          "pipeline, which is the safe way to be wrong")
            return False

    def _situation(self) -> str:
        """The visual facts about right now, or "" when nobody wired any.

        Never fatal and never loud: a selfie that lost its context is a slightly
        more generic photo, which is not worth failing a render over.
        """
        if self.situation is None:
            return ""
        try:
            return (self.situation() or "").strip()
        except Exception:
            log.exception("selfie: couldn't read the situation — rendering "
                          "without it")
            return ""

    def _gate(self):
        """The parker's ParkGate, or None when there isn't one — a stand-in
        parker (tests, a hosted camera) parks nothing and gates nothing, the
        same duck-typed tolerance `_release` gives the backend seam."""
        return getattr(self.parker, "gate", None)

    def _release(self) -> None:
        """Hand back whatever VRAM the backend is holding between renders.

        The local cameras keep their pipeline resident on purpose — a warm
        pipeline is the difference between a 15-second selfie and a 40-second
        one — so this is deliberate, not routine cleanup. Backends with nothing
        resident (mock, hosted) have no `_teardown` and this does nothing."""
        teardown = getattr(getattr(self.forge, "backend", None),
                           "_teardown", None)
        if callable(teardown):
            teardown()

    def start(self, contract: dict) -> None:
        """Spawn one render from the tool's validated contract. Never blocks,
        never raises — the turn that asked is already moving on."""
        task = asyncio.create_task(self._job(dict(contract)),
                                   name=f"selfie-{contract.get('id', '?')}")
        self._tasks.add(task)
        self._task_ids[task] = str(contract.get("id", ""))
        self._contracts[task] = contract
        self._status(contract, "started")
        task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task) -> None:
        selfie_id = self._task_ids.pop(task, "")
        contract = self._contracts.pop(task, {"id": selfie_id})
        self._tasks.discard(task)
        if task.cancelled():
            self._status(contract, "cancelled")

    def _status(self, contract: dict, state: str) -> None:
        if self.notify is None:
            return
        event = {"id": str(contract.get("id", "")), "state": state}
        if contract.get("_client_id"):
            event["client_id"] = contract["_client_id"]
        self.notify("selfie_status", event)

    def active_ids(self, client_id: str | None = None) -> list[str]:
        """IDs still rendering, optionally restricted to one submitted turn."""
        ids = []
        for task in self._tasks:
            contract = self._contracts.get(task)
            if client_id is None or (contract or {}).get("_client_id") == client_id:
                ids.append(self._task_ids.get(task, ""))
        return [selfie_id for selfie_id in ids if selfie_id]

    async def cancel(self, ids: list[str] | None = None, *,
                     client_id: str | None = None) -> list[str]:
        """Cancel correlated renders; supplied IDs cannot cross request owners."""
        wanted = set(ids or ())
        cancelled = []
        tasks = []
        for task in list(self._tasks):
            selfie_id = self._task_ids.get(task, "")
            contract = self._contracts.get(task) or {}
            if client_id is not None and contract.get("_client_id") != client_id:
                continue
            if wanted and selfie_id not in wanted:
                continue
            cancelled.append(selfie_id)
            tasks.append(task)
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)                  # run cancellation callbacks/status
        return [selfie_id for selfie_id in cancelled if selfie_id]

    async def _job(self, c: dict) -> None:
        async with self._render_lock:
            await self._serial_job(c)

    async def _serial_job(self, c: dict) -> None:
        kind = str(c.get("kind") or "selfie")
        noun = _noun(kind)
        if kind == "picture":
            # Nothing to fill in and nothing to roll: she wrote the subject, and
            # a picture of something else is entirely hers to describe. The
            # situation stays out for the same reason it stays out of a selfie
            # she placed herself — "it is night, rain on the window" appended to
            # her sunlit meadow is worse than adding nothing at all.
            kw = {"subject": c.get("subject") or "", "avoid": c.get("avoid") or ""}
        else:
            scene, mood = c.get("scene") or None, c.get("mood") or None
            framing, lighting = c.get("framing") or None, c.get("lighting") or None
            look, avoid = c.get("look") or "", c.get("avoid") or ""
            wardrobe = c.get("wardrobe") or "everyday"   # the tier she asked the
            # tool for; unprompted shots stay in the everyday default (→ ch. 11:
            # the yaml gates nothing — whether a tier renders is the backend's call)
            # What she didn't say, the world says (§7.6): the hour, the weather,
            # the room she is actually in this minute. Read at render time rather
            # than at ask time — a few seconds either way changes nothing, and it
            # keeps the tool contract free of host state.
            kw = {"look": look, "scene": scene, "mood": mood, "wardrobe": wardrobe,
                  "framing": framing, "lighting": lighting, "avoid": avoid,
                  "situation": self._situation()}
        # A parked render unloads her LLM — never while a turn is still
        # streaming from it (killing the model kills that stream mid-reply
        # and the draft vanishes from the chat). start-don't-await means the
        # turn that asked is exactly the turn in flight right now, so wait
        # for a quiet moment first. Only a park needs this: a render that
        # fits alongside her brain starts at once.
        failed = False
        gate = self._gate()
        try:
            if (self.parker is not None and self.quiet is not None
                    and self.parker.applicable() and self.parker.needs_park()):
                log.info("selfie: parking needs the GPU — waiting for a quiet "
                         "moment before the render")
                # The cleanup scope starts before this close: cancellation while
                # draining the current turn must never strand the gate shut.
                if gate is not None:
                    gate.close()
                await self.quiet()
            worker = asyncio.create_task(asyncio.to_thread(
                self._render, kind, save=False, **kw))
            try:
                result = await asyncio.shield(worker)
            except asyncio.CancelledError:
                # A Python worker thread cannot be killed. Keep the VRAM gate
                # closed until it actually exits, even if Stop is pressed twice.
                while not worker.done():
                    try:
                        await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                await asyncio.gather(worker, return_exceptions=True)
                raise
            stamp = int(self.clock.now())
            name = f"{stamp}-{c.get('id', 'x')}.png"
            path = Path(self.forge.out_dir) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(result.data)
            # Carry the turn's identity into the ledger. A render finishes long
            # after the sentence that asked for it, so without these the only
            # way back to "who wanted this photo, and why" is the clock.
            if c.get("_corr_id"):
                result.meta["corr_id"] = c["_corr_id"]
            if c.get("id"):
                result.meta["selfie_id"] = str(c["id"])
            self.forge._write_provenance(path, result.meta)   # the ledger (→ ch. 26)
        except Exception as e:                 # render failed: say so, quietly
            failed = True
            log.exception("%s render failed", noun)
            post_kw = {"proactive": _unprompted(c)}
            if c.get("_channel"):
                post_kw["channel"] = c["_channel"]
            if c.get("_client_id"):
                post_kw["client_id"] = c["_client_id"]
            post_kw["selfie_id"] = str(c.get("id", ""))
            self.post("assistant",
                      f"(the {noun} didn't come out — {type(e).__name__})",
                      **post_kw)
            self._status(c, "error")
        finally:
            # `parked()` reopens the gate on every path it runs, but a cancel
            # between the close above and the render never reaches it — and a
            # gate stuck shut is a companion who stops answering.
            if gate is not None:
                gate.open()
        if failed:
            # Only now: while the `except` above was running, the live
            # exception's traceback still pinned the dead pipeline's frames,
            # and with them the gigabytes this is trying to hand back. (Same
            # rule the backends' OOM retry follows — see diffusers.py.)
            await asyncio.to_thread(self._release)
            return

        chosen = result.meta.get("template", {})
        # Her own words first — they describe the shot better than two slot
        # names ever did, and it is her line about her own photo.
        detail = chosen.get("look") or ", ".join(
            v for v in (chosen.get("scene"), chosen.get("mood")) if v)
        post_kw = {"image_url": f"/selfies/{name}",
                   "proactive": _unprompted(c)}
        if c.get("_channel"):
            post_kw["channel"] = c["_channel"]
        if c.get("_client_id"):
            post_kw["client_id"] = c["_client_id"]
        post_kw["selfie_id"] = str(c.get("id", ""))
        self.post("assistant", "", **post_kw)
        self._status(c, "done")
        # one soft line about it, only if she's free — a drop is fine (§8.3):
        # unlike a timer, the photo itself already landed.
        try:
            await self.speak(ANNOUNCE_CUE.format(noun=noun,
                                                 detail=detail or "a new shot"))
        except Exception:
            log.exception("%s announce failed", noun)

    async def close(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
