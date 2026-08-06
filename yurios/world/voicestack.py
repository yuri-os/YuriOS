"""Her ears and her voice, resident only while somebody is in the room (SPEC §9.9).

Kokoro, faster-whisper and silero are the heaviest things this process holds —
measured on the shipped CPU defaults, a warm stack is **~2.2 GB of RSS** and
most of a minute — and building them at startup was fine when a node ran one
companion. It stopped being fine with a registry: `CharacterHost.start_all`
brings up every autostart character at boot (world/host.py), so a dashboard of
six characters warmed six voice stacks that nobody was listening to, and the
boot log crawled behind them.

What each half is worth, honestly, because the two are not equal. *Not loading*
is the whole 2.2 GB, for every character who never gets a visitor — that is the
win, and it is why this exists. *Unloading* is the smaller half: measured over
five enter/leave cycles on the CPU defaults, RSS falls ~2.3 GB → ~2.1 GB and the
warm peak settles at ~2.9 GB, because most of the footprint is torch's own
allocator arena rather than anything a `del` can reach — the process keeps it,
re-uses it, and the numbers plateau instead of climbing. On a CUDA backend
(`qwen3_tts` on the GPU) it is worth far more: `empty_cache` hands the VRAM back
to the LLM and the image forge, which is the memory that is genuinely scarce
there. Reclaiming the CPU arena as well would mean putting the stack in a
subprocess and streaming PCM over a pipe — a bigger seam than this one, and this
one is what the dashboard needed.

Nothing outside `/ws/voice` needs any of it. The text room, the chat route, the
channels (§10.5) and the mind all run on the brain alone; her voice is only ever
wanted by a client that opened the audio socket. So this holds it behind a
count of *listeners*: the first connection into a room warms the stack and waits
for it, every later one joins it already warm, and when the last one leaves the
weights go — after `VOICE_UNLOAD_AFTER_S`, because a page reload is a
disconnect too and dropping a minute of model loading over an F5 is worse than
holding it a little longer.

`VOICE_PRELOAD=1` restores the old behaviour (warm at boot, off-thread) for a
single-companion install that would rather spend the memory than the wait.
"""
from __future__ import annotations

import asyncio
import ctypes
import gc
import logging
import sys
import threading
import time

from yurios.desktop.main import build_stt, build_tts, build_vad
from yurios.desktop.voice.fillers import FillerBank

from .boot import BootBoard, FAILED, PENDING, READY, SKIPPED

log = logging.getLogger("world.voice")

UNLOADED = "unloaded"          # what /api/health calls a stage nobody is holding


class VoiceStack:
    """The TTS/STT/VAD trio and the filler bank, loaded on demand.

    Everything here is safe to call from the event loop, but `load` and `unload`
    are blocking (the whole point is that they're slow) — `acquire` runs them on
    a worker thread. The lock is a plain `threading` one for the same reason:
    the warm-up genuinely runs off-loop, exactly as it did when it was a thread
    started in `Runtime.__init__`.
    """

    #: the boot-panel services this stack owns (world/boot.py)
    KEYS = ("tts", "stt", "vad", "fillers")

    def __init__(self, cfg, boot: BootBoard):
        self.cfg = cfg
        self.boot = boot
        self.tts = self.stt = self.vad = None
        self.filler_bank: FillerBank | None = None
        self.tts_name = self.stt_name = self.vad_name = UNLOADED
        # Set while the stack is usable. A connection waits on `acquire`, not on
        # this, but /api/health reads it and the desktop route's shape is kept.
        self.ready = threading.Event()
        self.listeners = 0             # open /ws/voice connections holding it up
        self.loads = 0                 # warms this run — a health/test readout
        self._loaded = False
        # Two locks, and the split matters: `_lock` is held for the whole cold
        # load (up to a minute), so anything the event loop touches — the
        # listener count above all — must never wait on it. A second client
        # arriving mid-warm would otherwise freeze the server until the models
        # landed. `_count_lock` is only ever held for an increment.
        self._lock = threading.RLock()
        self._count_lock = threading.Lock()
        self._unload_task: asyncio.Task | None = None
        self._closed = False
        self._declare()
        if cfg.voice_preload:
            threading.Thread(target=self.load, daemon=True,
                             name="voice-warmup").start()

    # ---- the boot panel -----------------------------------------------------

    def _keys(self) -> tuple[str, ...]:
        """The services actually declared: fillers only exist with masking on."""
        return self.KEYS if self.cfg.mask_latency else self.KEYS[:-1]

    def _declare(self) -> None:
        """Put the voice stages on the boot board (SPEC §6.4).

        Declared terminal when nothing is going to load them: the enter gate
        polls until every service settles, and three stages that only warm on
        the first connection would leave it saying "waking her up…" forever.
        They still move loading → ready when a listener does arrive — the panel
        is live, so a page opened during a warm shows it.
        """
        state, detail = ((PENDING, "") if self.cfg.voice_preload
                         else (SKIPPED, "on demand"))
        self.boot.declare("tts", "voice · speech synthesis", state=state, detail=detail)
        self.boot.declare("stt", "voice · speech recognition", state=state, detail=detail)
        self.boot.declare("vad", "voice · voice activity", state=state, detail=detail)
        if self.cfg.mask_latency:
            self.boot.declare("fillers", "voice · filler phrases",
                              state=state, detail=detail)

    # ---- loading ------------------------------------------------------------

    @property
    def loaded(self) -> bool:
        return self._loaded

    def _stage(self, key: str, what: str, backend: str, build):
        log.info("voice: loading %s (%s)…", what, backend)
        self.boot.start(key, detail=backend)
        start = time.perf_counter()
        try:
            obj, name = build()
        except Exception as e:                 # a failed stage marks its own
            self.boot.done(key, state=FAILED, detail=str(e)[:80])
            raise
        log.info("voice: %s ready [%s] in %.1fs", what, name,
                 time.perf_counter() - start)
        self.boot.done(key, state=READY, detail=name)
        return obj, name

    def load(self) -> None:
        """Warm every stage. Blocking — cold torch models take ~20 s+ on a CPU,
        which is why this runs on a thread and the socket says so meanwhile."""
        with self._lock:
            if self._loaded or self._closed:
                return
            t0 = time.perf_counter()
            log.info("voice: warming up (loading local models — this is the slow part)…")
            landed = False
            try:
                self.tts, self.tts_name = self._stage(
                    "tts", "TTS", self.cfg.tts_backend, lambda: build_tts(self.cfg))
                self.stt, self.stt_name = self._stage(
                    "stt", "STT", self.cfg.stt_backend, lambda: build_stt(self.cfg))
                self.vad, self.vad_name = self._stage(
                    "vad", "VAD", self.cfg.vad_backend, lambda: build_vad(self.cfg))
                if self.cfg.mask_latency:
                    log.info("voice: priming filler phrases (MASK_LATENCY)…")
                    self.boot.start("fillers")
                    filler_bank = FillerBank(tts=self.tts)
                    try:
                        filler_bank.prime()    # pre-render, off the hot path (§5)
                        self.filler_bank = filler_bank
                        log.info("voice: fillers primed")
                        self.boot.done("fillers", detail="primed")
                    except Exception:
                        log.exception("filler prime failed; masking disabled this run")
                        self.boot.done("fillers", state=FAILED, detail="prime failed")
                landed = True
            finally:
                # an earlier stage that raised leaves the later ones un-run; don't
                # let them hang the boot panel — settle any that never resolved.
                for key in self.boot.unresolved(self._keys()):
                    self.boot.done(key, state=FAILED, detail="not reached")
                # Only a stack that actually built counts as loaded. Marking a
                # failed warm `_loaded` made `acquire` short-circuit ever after,
                # handing the next connection a stack whose tts/stt are None
                # instead of retrying the build — a permanent mute from one bad
                # warm. A failure resets to cold; the next listener tries again.
                self._loaded = landed
                if landed:
                    self.loads += 1
                    self.ready.set()           # never leave a connection hanging
                    log.info("voice: ready — she can hear and speak (%.1fs total)",
                             time.perf_counter() - t0)
                else:
                    self.tts = self.stt = self.vad = None
                    self.filler_bank = None
                    self.tts_name = self.stt_name = self.vad_name = UNLOADED
                    self.ready.clear()
                    log.error("voice: warm-up failed after %.1fs — she has no "
                              "voice this connection; the next one retries",
                              time.perf_counter() - t0)

    def unload(self) -> None:
        """Drop the weights. A no-op while anyone is still listening — the check
        is inside the lock so a connection that arrived during the grace period
        can never have its stack pulled out from under it."""
        with self._lock:
            with self._count_lock:
                if self.listeners:
                    return
            if not self._loaded:
                return
            self.tts = self.stt = self.vad = None
            self.filler_bank = None
            self.tts_name = self.stt_name = self.vad_name = UNLOADED
            self._loaded = False
            self.ready.clear()
            for key in self._keys():
                self.boot.done(key, state=SKIPPED, detail=UNLOADED)
        # outside the lock: freeing a torch graph is not instant, and nothing
        # else may touch the stack until it's actually gone anyway.
        gc.collect()
        _release_torch_cache()
        _return_heap_to_os()
        log.info("voice: unloaded — nobody is in the room")

    # ---- the room's door ----------------------------------------------------

    async def acquire(self) -> None:
        """A client is in the room: hold the stack up, warming it if it's cold.

        Concurrent callers all wait, on a worker thread each: the second one's
        `load` blocks on the lock the first is holding and returns the moment
        it's done, which is the old "several connections park in the voice-warm
        wait" behaviour, kept.
        """
        with self._count_lock:
            self.listeners += 1
        self._cancel_unload()
        if not self._loaded:
            await asyncio.to_thread(self.load)

    def release(self) -> None:
        """…and left. The last one out schedules the unload."""
        with self._count_lock:
            self.listeners = max(0, self.listeners - 1)
            empty = self.listeners == 0
        if empty and self._loaded:
            self._schedule_unload()

    def _schedule_unload(self) -> None:
        delay = self.cfg.voice_unload_after_s
        if delay < 0:                          # negative = keep her loaded
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:                   # released off-loop (a test): now
            self.unload()
            return
        self._cancel_unload()
        self._unload_task = loop.create_task(self._unload_later(delay),
                                             name="voice-unload")

    async def _unload_later(self, delay: float) -> None:
        if delay:
            await asyncio.sleep(delay)         # a reload is a disconnect too
        if self.listeners:
            return
        await asyncio.to_thread(self.unload)

    def _cancel_unload(self) -> None:
        task, self._unload_task = self._unload_task, None
        if task is not None and not task.done():
            task.cancel()

    async def close(self) -> None:
        """Shutdown: no more warms, and let go of whatever is resident.

        Deliberately takes no lock on this thread — a warm in flight holds
        `_lock` for as long as the models take, and shutdown must not wait on
        it from the event loop. Both writes are single stores; the freeing
        itself goes to a worker, where waiting for that warm is fine."""
        self._cancel_unload()
        self._closed = True
        self.listeners = 0
        await asyncio.to_thread(self.unload)

    # ---- what /api/health says ---------------------------------------------

    def status(self) -> dict:
        return {
            "ready": self.ready.is_set(),
            "loaded": self._loaded,
            "listeners": self.listeners,       # open /ws/voice connections (§9.9)
            "loads": self.loads,
            "stt": self.stt_name,
            "tts": self.tts_name,
            "vad": self.vad_name,
        }


def _release_torch_cache() -> None:
    """Hand the allocator's arena back as well, when torch is the one holding it.

    Dropping the references frees the python objects; the CUDA caching allocator
    keeps its blocks reserved until it's asked not to, and on a machine where the
    LLM and the image forge want the same card that reservation is the whole
    point of unloading. Never imports torch — a fake or CPU-only stack hasn't got
    it, and importing it here would cost more than it frees.
    """
    torch = sys.modules.get("torch")
    if torch is None:
        return
    try:
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:                          # a driver blip must not fail a release
        log.debug("could not empty the CUDA cache", exc_info=True)


def _return_heap_to_os() -> None:
    """…and ask glibc for the freed arenas back.

    Dropping a couple of gigabytes of weights does not lower the process's RSS
    on its own: glibc holds the freed pages in its arenas for the next
    allocation. `malloc_trim` is what actually hands them back. It recovers less
    than the load cost — see the honest accounting in `docs/voice.md`; most of a
    CPU stack's footprint is torch's own allocator, which keeps and *reuses* its
    arena, so a reload costs nothing extra rather than compounding. Free to
    call, so worth calling. glibc-only; anywhere else this is a no-op.
    """
    if sys.platform != "linux":
        return
    try:
        ctypes.CDLL(None).malloc_trim(0)
    except (OSError, AttributeError):          # musl, or no libc symbols here
        pass
