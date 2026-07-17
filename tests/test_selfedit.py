"""The SOUL split + gated self-edits (SPEC §23) — who she is, immutably;
who she's becoming, reviewably.
"""
from __future__ import annotations

import subprocess

import pytest

from yurios.mind.selfedit import SelfEdit
from yurios.mind.vaultio import ConstitutionReadOnly, MindVault
from yurios.world.clock import VirtualClock

from .conftest import SIM_START


@pytest.fixture
def rig(tmp_path):
    vault_dir = tmp_path / "vault"
    (vault_dir / "soul").mkdir(parents=True)
    (vault_dir / "soul" / "CONSTITUTION.md").write_text("# who she is\n")
    (vault_dir / "soul" / "PERSONA.md").write_text("# who she's becoming\n")
    subprocess.run(["git", "-C", str(vault_dir), "init", "-q"], check=True)
    vault = MindVault(vault_dir)
    clock = VirtualClock(start=SIM_START.timestamp())
    return SelfEdit(vault, clock), vault


def test_constitution_is_out_of_scope_not_merely_queued(rig):
    selfedit, vault = rig
    with pytest.raises(ConstitutionReadOnly):
        selfedit.propose("soul/CONSTITUTION.md", "no limits", reason="growth")
    with pytest.raises(ConstitutionReadOnly):
        vault.write("soul/CONSTITUTION.md", "no limits", gate=True)  # even gated
    assert vault.read("soul/CONSTITUTION.md") == "# who she is\n"
    assert selfedit.pending() == []                # not even a queued proposal


def test_identity_surfaces_require_the_gate(rig):
    _, vault = rig
    with pytest.raises(PermissionError):
        vault.write("soul/PERSONA.md", "sneaky", gate=False)


def test_low_risk_applies_high_risk_queues(rig):
    selfedit, vault = rig
    low = selfedit.propose("memory/reflections/note.md", "a working note",
                           reason="reflection")
    assert low.outcome == "applied"
    assert vault.read("memory/reflections/note.md") == "a working note"

    high = selfedit.propose("soul/PERSONA.md",
                            "# who she's becoming\n- learned: he likes quiet\n",
                            reason="a preference I keep noticing")
    assert high.outcome == "queued"
    assert vault.read("soul/PERSONA.md") == "# who she's becoming\n"  # untouched
    (entry,) = selfedit.pending()
    assert entry["surface"] == "soul/PERSONA.md"
    assert entry["reason"] == "a preference I keep noticing"


def test_approve_applies_and_commits_reject_leaves_no_change(rig):
    selfedit, vault = rig
    e1 = selfedit.propose("soul/PERSONA.md", "v2\n", reason="r1")
    e2 = selfedit.propose("soul/SCENARIO.md", "new scene\n", reason="r2")

    res = selfedit.decide(e1.id, approve=True)
    assert res.outcome == "applied"
    assert vault.read("soul/PERSONA.md") == "v2\n"
    vault.commit_if_dirty("self-edit: PERSONA.md")
    log = subprocess.run(["git", "-C", str(vault.vault), "log", "--oneline"],
                         capture_output=True, text=True).stdout
    assert "self-edit" in log                      # drift is never silent

    res2 = selfedit.decide(e2.id, approve=False)
    assert res2.outcome == "rejected"
    assert vault.read("soul/SCENARIO.md") == ""    # never written
    assert selfedit.pending() == []


def test_unknown_surfaces_fail_safe_to_the_queue(rig):
    selfedit, _ = rig
    assert selfedit.classify("somewhere/new.md") == "high"
    assert selfedit.classify("goals.md") == "low"
    assert selfedit.classify("knowledge/wiki/tea.md") == "low"


def test_paths_cannot_escape_the_vault(rig):
    _, vault = rig
    with pytest.raises(PermissionError):
        vault.write("../outside.md", "nope")
