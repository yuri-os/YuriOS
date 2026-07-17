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
