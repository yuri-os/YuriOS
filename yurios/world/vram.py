"""Parking the LLM for the local camera — the VRAM lender (§7.6's neighbour).

A resident chat model in LM Studio (~5 GiB) and a resident local render
pipeline (~11 GiB for SDXL; a Krea 2 transformer plus its text encoder is
bigger still) do not fit one 16 GB card at once, and the naive fixes are bad:
CPU-offload renders cost a minute, dropping the LLM costs her brain. So when a
local render is requested and the free VRAM won't hold a resident pipeline, the
LLM is *parked* — evicted from LM Studio for the duration of the render, then
re-pinned through the same `ensure_resident` the boot path uses. The card
briefly belongs to her camera, then goes back to her mind.

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

The mid-render race is narrowed and documented: the selfie lab waits for a
quiet moment before parking (no turn in flight — Runtime.wait_turns_idle),
because evicting while a turn is streaming kills that stream mid-reply and
her draft vanishes from the chat as if cancelled. What remains accepted: a
chat turn that *arrives* while she is parked JIT-loads the model back, which
can OOM the render. The window is tens of seconds; the render's OOM retry is
the backstop, and the restore runs regardless.
"""
from __future__ import annotations

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
                 resident_free_gib: Optional[float] = _DEFAULT_FLOOR_GIB):
        self.cfg = cfg
        self._free = free_probe or _torch_free_gib
        # None = the resolved backend keeps nothing resident on the card
        # (mock, openrouter, off) → never park. Passed in from the runtime,
        # which is the only place that knows which backend actually got built:
        # SELFIE_BACKEND=diffusers may resolve to either local backend.
        self.floor = resident_free_gib
        self._lock = threading.Lock()

    def _ids(self) -> list[str]:
        """Her LM Studio models, as the server names them (empty list when her
        brain is Ollama/OpenRouter/… — then there is nothing here to park)."""
        from yurios.app.main import _lmstudio_ids
        ids = _lmstudio_ids(self.cfg, chat=True, embed=True)
        return list(dict.fromkeys(ids))      # chat and utility are often one model

    def applicable(self) -> bool:
        """Is there anything to park, for a backend that benefits from it?"""
        return (getattr(self.cfg, "selfie_llm_park", False)
                and self.floor is not None
                and bool(self._ids()))

    def needs_park(self) -> bool:
        """Free VRAM below the resident floor → parking buys a fast render."""
        if self.floor is None:
            return False
        free = self._free()
        return free is not None and free < self.floor

    def _await_free(self, before: float) -> None:
        """Give LM Studio a moment to actually hand the VRAM back: poll until
        free memory stops growing (or we cross the floor), capped — a slow
        unload must never stall her camera for long."""
        deadline = time.monotonic() + _WAIT_BUDGET_S
        best = before
        while time.monotonic() < deadline:
            time.sleep(_WAIT_STEP_S)
            free = self._free()
            if free is None:
                return
            if self.floor is not None and free >= self.floor:
                log.info("park: %.1f GiB free — enough for a resident render", free)
                return
            if free > best + 0.2:           # still climbing; keep waiting
                best = free
                continue
            if free > before + 0.2:         # settled higher than we started
                return
        log.info("park: VRAM still below the floor after %.0fs — rendering "
                 "anyway (offload fallback)", _WAIT_BUDGET_S)

    @contextmanager
    def parked(self):
        """The render's wrapper: park if it buys speed, restore no matter what.

        Yields whether this render is running on borrowed VRAM — the caller
        must release any resident render pipeline BEFORE the `finally` below
        re-pins her brain, because the pipeline and the chat model are exactly
        the two things that don't fit on the card at once."""
        if not self.applicable() or not self.needs_park():
            yield False
            return
        with self._lock:
            from yurios.app.providers.lmstudio import ensure_resident, evict
            cfg = self.cfg
            ids = self._ids()
            before = self._free() or 0.0
            log.info("park: evicting %s from LM Studio for one render "
                     "(%.1f GiB free, floor %.0f)", ids, before, self.floor)
            evict(cfg.lmstudio_base_url, ids)
            self._await_free(before)
            try:
                yield True
            finally:
                # Her brain comes back even if the render died — and the reload
                # is the same pinned, TTL-free residency the boot path builds.
                log.info("park: restoring %s to LM Studio", ids)
                ensure_resident(cfg.lmstudio_base_url, ids,
                                context_length=getattr(cfg, "context_length", 0),
                                timeout=getattr(cfg, "lmstudio_load_timeout_s", 600.0))
