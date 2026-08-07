"""Parking the LLM for the local camera — the VRAM lender (§7.6's neighbour).

A resident chat model (~5 GiB) and a resident local render pipeline (~11 GiB
for SDXL; a Krea 2 transformer plus its text encoder is bigger still) do not
fit one 16 GB card at once, and the naive fixes are bad: CPU-offload renders
cost a minute, dropping the LLM costs her brain. So when a local render is
requested and the free VRAM won't hold a resident pipeline, the LLM is *parked*
for the duration of the render, then brought back. The card briefly belongs to
her camera, then goes back to her mind. Two shapes of brain, one loan each:

  - **LM Studio** (`lm_studio/…` ids): evicted over its developer REST API,
    then re-pinned through the same `ensure_resident` the boot path uses.
  - **Direct GGUF** (`gguf/…` ids, llama.cpp in this process): her contexts
    are closed in place by `providers/gguf.park()` and reloaded by `unpark()`
    — no server to ask, so the loan happens where the weights live.

Three rules, learned from the failure log:

  - **Park only when it buys something.** Free VRAM above the resident floor →
    the render fits alongside her brain; nobody gets evicted. Below it →
    parking turns a 70-second offload crawl into a 15-second resident render.
  - **Restore in `finally`, always.** A failed render (or a cancelled task)
    must never leave her brain unloaded. `ensure_resident` is idempotent and
    best-effort, so a botched park self-heals on the next render or restart.
  - **Never break the render to save the LLM.** Every measurement and HTTP
    call here is optional: no torch, no CUDA, no server, an old LM Studio —
    all mean "don't park", and the render falls back to its own offload path.

The mid-render race is closed from both sides. The selfie lab waits for a
quiet moment before parking (no turn in flight — Runtime.wait_turns_idle),
because evicting while a turn is streaming kills that stream mid-reply and
her draft vanishes from the chat as if cancelled. And a turn that *arrives*
while she is parked now queues at the `ParkGate` below instead of JIT-loading
the model straight back onto the card the render is mid-way through claiming
— which is the OOM this module used to list as accepted, and which duly
happened: evict at :01:47, a chat turn at :01:49, 4.4 GiB of chat model back
on a 16 GiB card, render dead at :02:16 with 53 MiB free.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from contextlib import contextmanager
from typing import Callable, Optional

log = logging.getLogger("world.vram")

# The floor is the *backend's* — each local camera knows how much free VRAM its
# own resident weights need (ImageBackend.RESIDENT_FREE_GIB; ~11 GiB for SDXL,
# more for Krea 2's INT4 transformer plus its text encoder), and parking exists
# to get ABOVE it. A backend that isn't a resident local model says None, and
# the parker stays out of the way entirely.
from yurios.forge.backends.diffusers import DiffusersBackend

_DEFAULT_FLOOR_GIB = DiffusersBackend.RESIDENT_FREE_GIB
_WAIT_STEP_S = 0.5
_WAIT_BUDGET_S = 25.0     # LM Studio unloads are fast; don't stall a selfie
# Room a parked brain needs to come back to, beside a warm render pipeline.
# A 7–8B Q4 with a long context is ~5.5 GiB on the card; this is that plus a
# little, because being wrong low here strands the card and being wrong high
# only costs one pipeline reload.
_BRAIN_HEADROOM_GIB = 6.0
# How many quiet polls mean an unload has really finished. One is not enough:
# llama.cpp hands VRAM back in stages, and a single flat reading mid-release
# used to end the wait with the card still full.
_SETTLE_POLLS = 3
# How long a turn will hold at the gate before going through anyway. Covers a
# whole park window (evict ≤25 s + render + re-pin ≈8 s) with room to spare;
# past that something is wedged, and a late reply beats a mute companion.
_GATE_WAIT_S = 90.0


class ParkGate:
    """The park window, made waitable — the missing half of the quiet gate.

    `wait_turns_idle` stops a park from starting *on top of* a live turn. This
    stops the mirror case: a turn arriving *during* the park, whose first
    completion call JIT-loads the very model the parker just evicted, into the
    VRAM the render is still claiming. Turns wait at the door instead.

    Open by default and open whenever no park is running, so every path that
    never parks — hosted backends, a card with headroom, tests — walks through
    without touching an event loop at all.

    Threading: the parker runs on a render worker thread (`asyncio.to_thread`),
    so mutations from off-loop hop to the loop; waiters are ordinary
    coroutines. Callers must wait *before* `turn_started`, or the park's quiet
    gate ends up waiting for a turn that is waiting for the park.
    """

    def __init__(self, *, timeout_s: float = _GATE_WAIT_S) -> None:
        self.timeout_s = timeout_s
        self._open = asyncio.Event()      # loop-agnostic since 3.10
        self._open.set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        """Remember the loop the waiters live on (Runtime.start_async)."""
        self._loop = loop

    async def wait(self) -> bool:
        """Hold this turn until the render gives her brain back. True = the way
        was clear (or cleared in time); False = the cap blew and the caller is
        going through anyway."""
        if self._open.is_set():
            return True
        log.info("park: a turn is waiting for the render to give the LLM back")
        try:
            await asyncio.wait_for(self._open.wait(), self.timeout_s)
            return True
        except asyncio.TimeoutError:
            log.warning("park: still parked after %.0fs — letting the turn "
                        "through (it may load the model mid-render)",
                        self.timeout_s)
            return False

    def close(self) -> None:
        """Shut the door: arriving turns queue until `open`."""
        self._hop(False)

    def open(self) -> None:
        """Let them through. Idempotent, and safe to call when never closed —
        every path out of a park runs it, including the ones that didn't park."""
        self._hop(True)

    def _hop(self, opened: bool) -> None:
        """Apply the flag on the loop that owns the waiters, from either side.
        No loop (tests, pre-startup, a closed loop) → set it directly; the
        Event is only ever *awaited* from the loop thread anyway."""
        def apply() -> None:
            self._open.set() if opened else self._open.clear()

        loop = self._loop
        try:
            if asyncio.get_running_loop() is loop:      # already on it
                apply()
                return
        except RuntimeError:                           # not on any loop
            pass
        if loop is None or not loop.is_running():
            apply()
            return
        try:
            loop.call_soon_threadsafe(apply)
        except RuntimeError:                           # loop closed under us
            apply()


def _torch_free_gib() -> Optional[float]:
    """Free VRAM in GiB, or None when there's no torch/CUDA to ask — which the
    parker reads as "can't measure, don't touch anything"."""
    try:
        import torch
        if not torch.cuda.is_available():
            return None
        return torch.cuda.mem_get_info()[0] / 1024**3
    except Exception:
        return None


class LLMParker:
    """Decides when a render should borrow the LLM's VRAM, and runs the loan.

    One instance per runtime, shared by every SelfieLab render. The lock
    serialises renders through the park window: two overlapping selfies must
    not interleave park/render/restore (a restore mid-render is exactly the
    OOM this exists to prevent). Renderers queue; they don't race.
    """

    def __init__(self, cfg, *, free_probe: Callable[[], Optional[float]] = None,
                 resident_free_gib: Optional[float] = _DEFAULT_FLOOR_GIB,
                 gate: Optional[ParkGate] = None):
        self.cfg = cfg
        self._free = free_probe or _torch_free_gib
        # Shared with the Runtime, which is where turns wait on it. Owning one
        # by default keeps the parker usable standalone (tests, scripts).
        self.gate = gate or ParkGate()
        # None = the resolved backend keeps nothing resident on the card
        # (mock, openrouter, off) → never park. Passed in from the runtime,
        # which is the only place that knows which backend actually got built:
        # SELFIE_BACKEND=diffusers may resolve to either local backend.
        self.floor = resident_free_gib
        # What her brain needs to come home to after a render (GiB). Compared
        # against free VRAM *with the pipeline still resident*, so it answers
        # "can these two live together?" — see can_keep_pipeline_warm.
        self.brain_headroom = float(
            getattr(cfg, "selfie_warm_headroom_gib", _BRAIN_HEADROOM_GIB))
        self._lock = threading.Lock()

    def _ids(self) -> list[str]:
        """Her LM Studio models, as the server names them (empty list when her
        brain doesn't touch that server)."""
        from yurios.app.main import _lmstudio_ids
        ids = _lmstudio_ids(self.cfg, chat=True, embed=True)
        return list(dict.fromkeys(ids))      # chat and utility are often one model

    def _gguf_resident(self) -> int:
        """Her in-process llama.cpp contexts — the direct gguf/ route, direct
        or reached through the LM-Studio-down fallback. 0 when her brain lives
        entirely elsewhere (Ollama/OpenRouter/…), or when nothing has loaded
        yet — then there is nothing here to park."""
        try:
            from yurios.app.providers import gguf
            return gguf.resident_count()
        except Exception:
            return 0

    def applicable(self) -> bool:
        """Is there anything to park, for a backend that benefits from it?"""
        return (getattr(self.cfg, "selfie_llm_park", False)
                and self.floor is not None
                and (bool(self._ids()) or self._gguf_resident() > 0))

    def needs_park(self) -> bool:
        """Free VRAM below the resident floor → parking buys a fast render."""
        if self.floor is None:
            return False
        free = self._free()
        return free is not None and free < self.floor

    def can_keep_pipeline_warm(self) -> bool:
        """May the camera hold its pipeline on the card until the next render?

        The warm pipeline is worth 25 seconds a selfie, and on a card that fits
        both it costs nothing. On a card that does not, it is the bug this
        method exists to stop — and it is a *quiet* bug, because it makes the
        NEXT render fail, not this one:

          1. a render finds room, doesn't park, and keeps ~9 GiB warm;
          2. the next turn reloads her brain beside it — the card is now full;
          3. the render after that parks, which frees her brain and nothing
             else, and can never reach a floor that assumes an empty card;
          4. it renders into what's left and dies of OOM.

        Which reads exactly like "selfies fail randomly": the first one after a
        restart works, and then they don't. So the pipeline stays warm only
        while there is still room for her brain to come home beside it.
        """
        if not self.applicable():
            return True                  # nothing else is competing for this card
        free = self._free()
        if free is None:
            return True                  # can't measure → don't take the speed away
        return free >= self.brain_headroom

    def _await_free(self, before: float) -> bool:
        """Give the unload a moment to actually hand the VRAM back: poll until
        free memory stops growing (or we cross the floor), capped — a slow
        unload must never stall her camera for long.

        Returns whether the card came up to the floor. Two things here were
        learned from the failure log, and both are about *not lying quietly*:

        - Giving up needed more than one flat reading. llama.cpp releases in
          stages, so a single poll where free memory didn't move is a pause,
          not the end — and the old single-poll exit returned after ~1 second
          with the card still full.
        - Every exit says what it saw. The silent "settled higher" return was
          the reason a failing park looked identical in the log to one that
          never ran: "lending the GPU", then nothing, then OOM.
        """
        deadline = time.monotonic() + _WAIT_BUDGET_S
        best = before
        stalls = 0
        free = before
        while time.monotonic() < deadline:
            time.sleep(_WAIT_STEP_S)
            free = self._free()
            if free is None:
                return True                 # can't measure → don't block the render
            if self.floor is not None and free >= self.floor:
                log.info("park: %.1f GiB free — enough for a resident render", free)
                return True
            if free > best + 0.2:           # still climbing; keep waiting
                best, stalls = free, 0
                continue
            stalls += 1
            if stalls >= _SETTLE_POLLS:
                break
        log.warning("park: the unload settled at %.1f GiB free, below the %.0f "
                    "GiB floor — this render will offload. If it keeps "
                    "happening, something else is holding the card.",
                    free, self.floor or 0.0)
        return False

    @contextmanager
    def parked(self):
        """The render's wrapper: park if it buys speed, restore no matter what.

        Yields whether this render is running on borrowed VRAM — the caller
        must release any resident render pipeline BEFORE the `finally` below
        re-pins her brain, because the pipeline and the chat model are exactly
        the two things that don't fit on the card at once."""
        if not self.applicable() or not self.needs_park():
            # The lab shuts the gate before it waits for a quiet moment, and
            # the card can have freed up in between — a decision not to park
            # must never leave turns queued behind a door nobody will open.
            self.gate.open()
            yield False
            return
        with self._lock:
            from yurios.app.providers.lmstudio import ensure_resident, evict
            from yurios.app.providers import gguf
            cfg = self.cfg
            ids = self._ids()
            before = self._free() or 0.0
            log.info("park: lending the GPU to one render — LM Studio %s, "
                     "%d in-process GGUF (%.1f GiB free, floor %.0f)",
                     ids, self._gguf_resident(), before, self.floor)
            self.gate.close()        # usually already shut by the lab; idempotent
            if ids:
                evict(cfg.lmstudio_base_url, ids)
            # Also runs with nothing resident: it drops the in-process load
            # gate for the whole render window, so no turn or mind tick can
            # start loading her brain onto the card the render is filling.
            handles = gguf.park()
            self._await_free(before)
            try:
                yield True
            finally:
                # Her brain comes back even if the render died — and the reload
                # is the same pinned, TTL-free residency the boot path builds.
                log.info("park: restoring her brain (LM Studio %s, %d GGUF)",
                         ids, len(handles))
                try:
                    if ids:
                        ensure_resident(
                            cfg.lmstudio_base_url, ids,
                            context_length=getattr(cfg, "context_length", 0),
                            timeout=getattr(cfg, "lmstudio_load_timeout_s", 600.0))
                finally:
                    try:
                        gguf.unpark(handles)
                    finally:
                        # Even a failed restore opens the door: the next turn
                        # reloads her (LM Studio's JIT, gguf's lazy _load) —
                        # the fallback the gate exists to postpone, not prevent.
                        self.gate.open()
