"""The boot board + /api/boot (SPEC §6.4) — the wake-up log the enter gate polls.

The board is what turns a minute of cold-model loading into visible progress
instead of a blank gate; these pin its state machine and the endpoint shape.
"""
from __future__ import annotations

import pytest

from yurios.world.boot import BootBoard

pytest.importorskip("fastapi")
from starlette.testclient import TestClient           # noqa: E402

from yurios.desktop.voice.backends.fakes import FakeBrain    # noqa: E402
from yurios.world.main import create_app                     # noqa: E402


# ---- the board itself ------------------------------------------------------

def test_pending_service_keeps_boot_unfinished():
    b = BootBoard()
    b.declare("tts", "voice · TTS")
    b.declare("selfies", "camera", state="ready", detail="mock")
    snap = b.snapshot()
    assert snap["done"] is False                       # tts still pending
    assert [s["key"] for s in snap["services"]] == ["tts", "selfies"]  # order kept


def test_stage_records_state_and_timing():
    ticks = iter([0.0, 0.0, 2.5, 2.5])                 # t0, start, done, snapshot
    b = BootBoard(clock=lambda: next(ticks))
    b.declare("tts", "voice · TTS")
    b.start("tts", detail="kokoro")
    b.done("tts", detail="kokoro")
    (svc,) = b.snapshot()["services"]
    assert svc["state"] == "ready" and svc["detail"] == "kokoro"
    assert svc["seconds"] == 2.5
    assert "_start" not in svc                          # internals never serialised


def test_done_true_only_when_every_service_is_terminal():
    b = BootBoard()
    b.declare("a", "a"); b.declare("b", "b")
    b.done("a", state="ready")
    assert b.snapshot()["done"] is False
    b.done("b", state="failed")                         # failed still counts as settled
    assert b.snapshot()["done"] is True


def test_unresolved_finds_only_pending_or_loading():
    b = BootBoard()
    b.declare("tts", "t"); b.declare("stt", "s"); b.declare("vad", "v")
    b.start("stt"); b.done("vad", state="ready")
    assert b.unresolved(("tts", "stt", "vad", "missing")) == ["tts", "stt"]


def test_a_non_blocking_service_keeps_its_line_but_not_the_gate():
    """The embedder's weights are mostly a module import, and it runs several
    times slower while the rest of the node builds around it — a room held shut
    for it turned a one-minute boot into a two-and-a-half-minute one. A recall
    without it is an empty Vault, so the gate opens and it catches up (§6.4)."""
    b = BootBoard()
    b.declare("embed", "memory · embedding model", blocking=False)
    b.declare("tools", "hands · tool server")
    b.start("embed")
    b.start("tools")
    assert b.snapshot()["done"] is False           # the gate waits on the hands

    b.done("tools", detail="mcp · 17 tools")
    snap = b.snapshot()
    assert snap["done"] is True                    # …and not on the weights
    states = {s["key"]: s["state"] for s in snap["services"]}
    assert states == {"embed": "loading", "tools": "ready"}   # still on the board

    b.done("embed", detail="BAAI/bge-small-en-v1.5 · 384d")
    assert b.snapshot()["done"] is True


def test_a_board_of_nothing_but_non_blocking_services_is_not_done():
    """`done` on an empty set is vacuously true, which would open the gate on a
    board that has not started. The blocking set is the one that must be there."""
    b = BootBoard()
    b.declare("embed", "memory · embedding model", blocking=False)
    assert b.snapshot()["done"] is False


def test_empty_board_is_not_done():
    assert BootBoard().snapshot()["done"] is False


def test_every_move_is_narrated_to_the_log(caplog):
    """The panel only exists once the port is open, and a cold LM Studio
    load still happens before that — so the log is the only witness, and
    `yurios start` reads it to tell a slow wake from a wedged one."""
    ticks = iter([0.0, 0.0, 41.0])                     # t0, start, done
    b = BootBoard(clock=lambda: next(ticks))
    b.declare("embed", "memory · embedding model")
    with caplog.at_level("INFO", logger="world.boot"):
        b.start("embed", detail="BAAI/bge-small-en-v1.5")
        b.done("embed", detail="BAAI/bge-small-en-v1.5 · 384d")

    started, finished = [r.message for r in caplog.records]
    assert started == "boot: memory · embedding model… (BAAI/bge-small-en-v1.5)"
    assert finished == ("boot: memory · embedding model ready in 41s — "
                        "BAAI/bge-small-en-v1.5 · 384d")


def test_the_narration_names_the_character_in_a_house_of_them(caplog):
    """Four characters boot into one log file; four unlabelled "embedding model
    ready" lines record nothing anyone can act on."""
    b = BootBoard(who="virelle")
    b.declare("tools", "hands · tool server")
    with caplog.at_level("INFO", logger="world.boot"):
        b.start("tools")
        b.done("tools", detail="mcp · 17 tools")

    assert caplog.records[-1].message.startswith("boot: virelle · hands · tool server ready")


def test_a_failed_stage_is_narrated_as_a_warning(caplog):
    b = BootBoard()
    b.declare("tools", "hands · tool server")
    with caplog.at_level("INFO", logger="world.boot"):
        b.start("tools")
        b.done("tools", state="failed", detail="no such server")

    assert caplog.records[-1].levelname == "WARNING"
    assert "hands · tool server failed" in caplog.records[-1].message


# ---- the endpoint, wired through the real Runtime --------------------------

def test_api_boot_reaches_done_with_every_service_settled(cfg):
    """Nothing is left pending once the runtime is up (SPEC §6.4). Her voice is
    not among the waiting: it loads when someone opens /ws/voice (§9.9), so the
    gate must not sit on three stages nobody is going to warm."""
    cfg = cfg.model_copy(update={"tools_backend": "off", "mind_enabled": False})
    app = create_app(cfg, brain=FakeBrain())
    with TestClient(app) as c:
        snap = c.get("/api/boot").json()
        assert snap["done"] is True
        states = {s["key"]: s["state"] for s in snap["services"]}
        assert states["tts"] == states["stt"] == states["vad"] == "skipped"
        details = {s["key"]: s["detail"] for s in snap["services"]}
        assert details["tts"] == "on demand"
        assert states["tools"] == "skipped"            # backend off
        assert not any(s["state"] in ("pending", "loading")
                       for s in snap["services"])


def test_voice_preload_warms_the_stages_at_boot(cfg):
    """VOICE_PRELOAD=1 is the old behaviour, kept: warm off-thread at startup,
    and the panel narrates all three stages as it goes."""
    cfg = cfg.model_copy(update={"tools_backend": "off", "mind_enabled": False,
                                 "voice_preload": True})
    app = create_app(cfg, brain=FakeBrain())
    with TestClient(app) as c:
        assert c.app.state.rt.voice_ready.wait(timeout=10)  # warm thread finished
        snap = c.get("/api/boot").json()
        assert snap["done"] is True
        states = {s["key"]: s["state"] for s in snap["services"]}
        assert states["tts"] == states["stt"] == states["vad"] == "ready"


async def test_start_async_does_not_wait_for_the_tool_server(cfg, seeded_vault):
    """Spawning MCP is 2–3s per character and used to sit in front of her mind
    and the host's port. Discovery now runs on the loop without holding the
    rest of boot (SPEC §7.2)."""
    import asyncio

    from yurios.world.tools.fakes import FakeToolRunner

    from .conftest import CannedChat, FakeEmbedder, FakeUtility

    began = asyncio.Event()
    release = asyncio.Event()

    class Slow(FakeToolRunner):
        async def start(self):
            began.set()
            await release.wait()
            return await super().start()

    app_cfg = cfg.model_copy(update={
        "mind_enabled": False, "vault_dir": seeded_vault,
        "embed_dim": FakeEmbedder.dim,
        "corpus_dir": seeded_vault.parent / "corpus",
        "trace_dir": seeded_vault.parent / "traces"})
    app = create_app(app_cfg, chat_model=CannedChat(),
                     utility_model=FakeUtility(), embedder=FakeEmbedder(),
                     tool_runner=Slow(), manage_lifespan=False)
    rt = app.state.rt
    await rt.start_async()
    await asyncio.sleep(0)
    assert began.is_set()
    tools = next(s for s in rt.boot.snapshot()["services"] if s["key"] == "tools")
    assert tools["state"] == "loading"
    assert rt.tools_status == "loading"     # …and /api/health says so, not "off"
    assert rt.tool_count == 0
    assert not rt.tools_settled.is_set()    # her mind's first tick waits on this
    release.set()
    for _ in range(50):
        if rt.tool_count:
            break
        await asyncio.sleep(0.02)
    assert rt.tool_count > 0
    assert rt.tools_settled.is_set()
    tools = next(s for s in rt.boot.snapshot()["services"] if s["key"] == "tools")
    assert tools["state"] == "ready"
    await rt.stop_async()


async def test_a_failed_tool_server_still_settles_the_wait(cfg, seeded_vault):
    """Her mind's first tick waits for discovery to *answer* (SPEC §26.3), and
    a spawn that died is an answer — not thirty seconds of nothing."""
    import asyncio

    from yurios.world.tools.fakes import FakeToolRunner

    from .conftest import CannedChat, FakeEmbedder, FakeUtility

    class Dead(FakeToolRunner):
        async def start(self):
            raise RuntimeError("no such server")

    app_cfg = cfg.model_copy(update={
        "mind_enabled": False, "vault_dir": seeded_vault,
        "embed_dim": FakeEmbedder.dim,
        "corpus_dir": seeded_vault.parent / "corpus",
        "trace_dir": seeded_vault.parent / "traces"})
    app = create_app(app_cfg, chat_model=CannedChat(),
                     utility_model=FakeUtility(), embedder=FakeEmbedder(),
                     tool_runner=Dead(), manage_lifespan=False)
    rt = app.state.rt
    await rt.start_async()
    await asyncio.wait_for(rt.tools_settled.wait(), timeout=5)
    assert rt.tools_status.startswith("failed")
    await rt.stop_async()
