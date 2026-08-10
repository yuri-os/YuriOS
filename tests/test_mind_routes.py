"""/api/mind (SPEC §24.3) — the inner-life surface, wired through the app.

The routes read *through* the mind's own stores; the one write path (a
self-edit decision) is only a signal the loop consumes on its next tick.
"""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from starlette.testclient import TestClient            # noqa: E402

from yurios.desktop.voice.backends.fakes import FakeBrain     # noqa: E402
from yurios.world.main import create_app                      # noqa: E402

from .conftest import make_mind                        # noqa: E402


@pytest.fixture
def client_with_mind(cfg, seeded_vault):
    """A served app (FakeBrain keeps voice cheap) with a REAL mind attached —
    the same object shape start_async builds over the real brain."""
    app_cfg = cfg.model_copy(update={"tools_backend": "off",
                                     "mind_enabled": False})
    app = create_app(app_cfg, brain=FakeBrain())
    rig = make_mind(cfg, seeded_vault)
    with TestClient(app) as c:
        rt = c.app.state.rt
        rt.mind = rig.mind
        rt.signals = rig.mind.bus
        yield c, rig


def test_api_mind_snapshot(client_with_mind):
    c, rig = client_with_mind
    snap = c.get("/api/mind").json()
    assert snap["state"] in ("ENGAGED", "IDLE", "DORMANT", "DREAM")
    assert "budget" in snap and snap["budget"]["daily_tokens"] > 0
    assert "pending_edits" in snap and "goals" in snap


async def test_api_journal_serves_her_day(client_with_mind):
    c, rig = client_with_mind
    rig.mind.journal.write("reorganised the shelf")
    days = c.get("/api/mind/journal?days=2").json()["days"]
    assert days and days[0]["entries"]
    entry = days[0]["entries"][-1]
    assert entry["hers"] is True and "reorganised the shelf" in entry["text"]


async def test_edit_decision_rides_the_signal_bus(client_with_mind):
    c, rig = client_with_mind
    edit = rig.mind.selfedit.propose("soul/PERSONA.md", "v2\n", reason="grown")
    assert edit.outcome == "queued"
    r = c.post(f"/api/mind/edits/{edit.id}", json={"approve": True})
    assert r.json()["queued"] is True
    # not applied yet — the loop consumes the decision on its next tick
    assert rig.mind.selfedit.pending()
    await rig.mind.tick()
    assert rig.mind.selfedit.pending() == []
    assert rig.mind.vault.read("soul/PERSONA.md") == "v2\n"
    # and the decision itself is journaled
    day_files = list((rig.mind.vault.vault / "memory" / "episodic").glob("*.md"))
    assert any("you applied my edit" in p.read_text() for p in day_files)


def test_unknown_edit_is_404(client_with_mind):
    c, _ = client_with_mind
    assert c.post("/api/mind/edits/nope", json={"approve": True}).status_code == 404


# --- the DREAM roster and its trigger (SPEC §21.3) --------------------------
# The one part of the mind surface that *acts* rather than reporting. It runs
# inline, unlike the self-edit decision above, and deliberately: a decision
# belongs to the loop's next tick, but a test you are watching has to answer you.


def test_dream_status_lists_the_night(client_with_mind):
    c, _rig = client_with_mind
    data = c.get("/api/mind/dream").json()
    names = [j["name"] for j in data["jobs"]]
    assert names[0] == "consolidate"                # priority order, as the loop runs it
    assert {"diary", "strategy"} <= set(names)
    assert data["window"] == [2, 6]
    assert all("backlog" in j and "enabled" in j for j in data["jobs"])


async def test_dream_run_answers_with_the_prompts_it_sent(client_with_mind):
    """The whole point of the button: the exact system message, the exact
    input and the raw completion, without waiting until 3am to see them."""
    c, rig = client_with_mind
    day = rig.mind.vault.vault / "memory" / "episodic" / "2026-07-04.md"
    day.parent.mkdir(parents=True, exist_ok=True)
    day.write_text("# Journal — 2026-07-04\n\n### 10:01  user: the rain kept up\n")
    body = c.post("/api/mind/dream/run",
                  json={"job": "diary", "day": "2026-07-04", "dry_run": True}).json()
    assert body["dry_run"] is True
    assert body["writes"] == ["diary/2026-07-04.md"]
    exchange = body["exchanges"][0]
    assert "diary entry" in exchange["system"]
    assert "the rain kept up" in exchange["user"]
    assert exchange["completion"]
    # dry: nothing on disk, nothing marked done
    assert not (rig.mind.vault.vault / "workspace" / "diary").exists()
    assert "2026-07-04" in rig.mind.dreams.backlog()


async def test_a_wet_dream_run_writes_and_journals(client_with_mind):
    c, rig = client_with_mind
    day = rig.mind.vault.vault / "memory" / "episodic" / "2026-07-04.md"
    day.parent.mkdir(parents=True, exist_ok=True)
    day.write_text("# Journal — 2026-07-04\n\n### 10:01  user: the rain kept up\n")
    body = c.post("/api/mind/dream/run", json={"job": "diary", "day": "2026-07-04"}).json()
    assert body["dry_run"] is False
    entry = rig.mind.vault.vault / "workspace" / "diary" / "2026-07-04.md"
    assert entry.is_file()
    day_files = list((rig.mind.vault.vault / "memory" / "episodic").glob("*.md"))
    assert any("wrote a diary entry" in p.read_text() for p in day_files)


def test_running_the_night_does_not_move_the_activity_ladder(client_with_mind):
    """A night you asked for is not evidence she drifted into one — and a DREAM
    state written by a button is a lie the timeline then shows you forever."""
    c, rig = client_with_mind
    before = rig.mind.activity.state
    c.post("/api/mind/dream/run", json={"dry_run": True})
    assert rig.mind.activity.state == before


def test_an_unknown_dream_job_is_404(client_with_mind):
    c, _rig = client_with_mind
    r = c.post("/api/mind/dream/run", json={"job": "nonesuch"})
    assert r.status_code == 404


def test_start_async_builds_the_mind_over_the_real_brain(cfg, seeded_vault):
    """The `python -m yurios.world` path: create_app with the real brain
    (fake models) boots the mind on the server's event loop (SPEC §15)."""
    from .conftest import CannedChat, FakeEmbedder, FakeUtility
    app_cfg = cfg.model_copy(update={
        "tools_backend": "off", "vault_dir": seeded_vault,
        "embed_dim": FakeEmbedder.dim,
        "corpus_dir": seeded_vault.parent / "corpus",
        "trace_dir": seeded_vault.parent / "traces"})
    app = create_app(app_cfg, chat_model=CannedChat(),
                     utility_model=FakeUtility(), embedder=FakeEmbedder())
    with TestClient(app) as c:
        health = c.get("/api/health").json()
        assert health["mind"] == "running"
        assert health["activity"] in ("ENGAGED", "IDLE", "DORMANT", "DREAM")
        assert c.get("/api/mind").status_code == 200
        boot = c.get("/api/boot").json()
        states = {s["key"]: s["state"] for s in boot["services"]}
        assert states["mind"] == "ready"
        # the situation seam swapped: the brain's prompts now carry the store's
        # stage (presence line included), not just the host rendering
        rt = c.app.state.rt
        assert rt.brain.world is rt.mind.world


def test_mindless_app_reports_503(cfg):
    app = create_app(cfg.model_copy(update={"tools_backend": "off",
                                            "mind_enabled": False}),
                     brain=FakeBrain())
    with TestClient(app) as c:
        assert c.get("/api/mind").status_code == 503
        health = c.get("/api/health").json()
        assert health["mind"] == "disabled"            # the truth, not a guess
        assert health["tool_count"] == 0                # no hands were configured


# ---------------------------------------------- /api/mind/reading (SPEC §24.3)
#
# The panel behind the inner-life tab's reading block: what she is reading, what
# it will cost in model calls, and the two buttons that stop it without losing
# the document.

def test_reading_answers_even_when_she_is_reading_nothing(client_with_mind):
    """A panel that 503s tells you nothing about whether anything is happening."""
    c, _rig = client_with_mind
    body = c.get("/api/mind/reading").json()
    assert body["mind"] is True
    assert body["reading"] is None
    assert body["runs"] == [] and body["held"] == []


def test_reading_shows_what_is_held_and_what_finishing_it_costs(client_with_mind):
    c, rig = client_with_mind
    doc = rig.mind.knowledge.park("web-a-long-page.md",
                                  text="A paragraph.\n\n" * 400)
    (held,) = c.get("/api/mind/reading").json()["held"]
    assert held["doc"] == doc and held["done"] == 0
    assert held["passages"] > 1 and held["remaining_calls"] >= held["passages"]
    assert "stopped it" in held["reason"]


def test_resuming_a_held_doc_puts_it_back_in_her_way(client_with_mind):
    c, rig = client_with_mind
    doc = rig.mind.knowledge.park("web-a-long-page.md",
                                  text="A paragraph.\n\n" * 400)
    assert rig.mind.knowledge.pending_docs() == []

    assert c.post("/api/mind/reading/resume", json={"doc": doc}).status_code == 200
    assert rig.mind.knowledge.pending_docs() == [doc], "the tick can have it now"
    assert c.get("/api/mind/reading").json()["held"] == []


def test_resuming_something_she_never_stopped_is_a_404(client_with_mind):
    c, _rig = client_with_mind
    assert c.post("/api/mind/reading/resume",
                  json={"doc": "nothing.md"}).status_code == 404


def test_stopping_when_she_is_not_reading_says_so(client_with_mind):
    c, _rig = client_with_mind
    assert c.post("/api/mind/reading/stop", json={}).status_code == 409
    assert c.post("/api/mind/reading/stop",
                  json={"run": "nope"}).status_code == 404


async def test_stopping_a_live_run_through_the_route(client_with_mind, clock):
    """The button, end to end: the route reaches the runner the tool loop uses,
    and the run winds down through its own ending rather than being killed."""
    from yurios.world.research import Researcher
    from yurios.world.tools.fetch import FakeFetcher
    from yurios.world.tools.search import FakeSearch

    c, rig = client_with_mind
    r = Researcher(FakeSearch(), FakeFetcher(), clock=clock,
                   post=lambda *a, **k: {}, speak=None,
                   knowledge=lambda: rig.mind.knowledge)
    c.app.state.rt.research = r
    r.start({"id": "r1", "topic": "tea", "depth": 2})

    body = c.get("/api/mind/reading").json()
    assert [run["stage"] for run in body["runs"]] == ["searching"]

    assert c.post("/api/mind/reading/stop", json={"run": "r1"}).json()["stopped"]
    assert r.runs()[0]["stage"] == "stopping"
    assert c.post("/api/mind/reading/stop", json={"run": "r1"}).status_code == 200


async def test_watching_a_long_read_and_stopping_it_from_the_panel(
        client_with_mind):
    """The whole feature in one test: a document long enough to watch, the
    numbers the panel shows while it is being read, the stop button, and the
    guarantee that what's left is not read again until you resume it."""
    import asyncio

    c, rig = client_with_mind
    store = rig.mind.knowledge
    real = store._contextualize

    async def unhurried(doc, chunk):          # a local model is not instant
        await asyncio.sleep(0.02)
        return await real(doc, chunk)

    store._contextualize = unhurried
    store.vault.write("knowledge/reference/long.md", "A paragraph.\n\n" * 600)

    task = asyncio.create_task(store.ingest("long.md"))
    live = None
    try:
        for _ in range(200):                  # wait for the read to be visible
            await asyncio.sleep(0.02)
            live = c.get("/api/mind/reading").json()["reading"]
            if live and live["done"] >= 2:
                break
        assert live and live["doc"] == "long.md"
        assert 0 < live["done"] < live["passages"]
        assert live["calls"] == live["passages"] * live["calls_each"]
        assert live["calls_done"] == live["done"] * live["calls_each"]

        assert live["stopping"] is False
        assert c.post("/api/mind/reading/stop", json={}).json()["stopped"]
        # the gap the panel's "busy pausing" button lives in: asked for, and
        # still reading, until the passage in flight is finished and paid for
        asked = c.get("/api/mind/reading").json()["reading"]
        assert asked is None or asked["stopping"] is True
        result = await asyncio.wait_for(task, timeout=10)
    finally:
        task.cancel()

    assert result.held and 0 < result.chunks < live["passages"]
    body = c.get("/api/mind/reading").json()
    assert body["reading"] is None
    (held,) = body["held"]
    assert held["doc"] == "long.md" and held["done"] == result.chunks
    assert store.pending_docs() == [], "held is held: no tick will touch it"

    # …and it is exactly as resumable as the panel's button claims
    assert c.post("/api/mind/reading/resume", json={"doc": "long.md"}).is_success
    assert store.pending_docs() == ["long.md"]
    assert store._resume_point("long.md") == result.chunks
