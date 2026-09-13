"""The relationship-evolution scorer, offline.

The GLM-5.2 13-week run is a script (`scripts/relationship_bench/run.py`).
These pin that the score function actually fails the live-vault soup and
passes the compacted one — otherwise the bench could green on a broken model.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "relationship_bench"))

from score import score_user_md  # noqa: E402
from yurios.app.memory import partner  # noqa: E402
from tests.test_partner import SOUP, SOUP_LABELS  # noqa: E402

SOUL_SRC = ROOT / "soul-src"


def test_live_vault_soup_fails_the_evolution_score():
    scored = score_user_md(SOUP, week=13)
    names = {c.name: c.ok for c in scored.checks}
    assert names["phase is not frozen early"] is False
    assert names["no extractor provenance"] is False
    assert names["calming-sam is not a contradicting pair"] is False
    assert names["yandere-lite is in What helps"] is False
    assert not scored.ok


def test_compacted_soup_clears_the_evolution_failures():
    new_md, delta = partner.compact_user_md(SOUP, days_together=30,
                                            classified=SOUP_LABELS)
    scored = score_user_md(new_md, week=8)  # soup has yandere + reach-out, no americano
    names = {c.name: c.ok for c in scored.checks}
    assert names["phase is not frozen early"] is True
    assert names["no extractor provenance"] is True
    assert names["calming-sam is not a contradicting pair"] is True
    assert names["yandere-lite is in What helps"] is True
    assert names["reach out / no five-day silences"] is True
    assert delta is not None
    assert any("yandere" in line.lower() for line in delta.lines)


def test_a_fresh_seed_is_honestly_empty_not_a_fake_pass():
    seed = (SOUL_SRC / "USER.md").read_text(encoding="utf-8")
    scored = score_user_md(seed, week=0)
    assert scored.ok, scored.as_text()
    late = score_user_md(seed, week=13)
    assert not late.ok, "an empty seed must not pass a month-three score"
