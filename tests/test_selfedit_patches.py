"""Self-edit patches, ruled on later than they were made (SPEC §23.2, §23.3).

A `find`/`replace` proposal is realised into a whole file when she makes it,
and ruled on hours later. Two of them proposed in a row were each built
against the file without the other, so approving both wrote the first one back
out. These pin the fix — the patch is applied again at the ruling — and walk
the whole door once through the real tool server and the real loop.
"""
from __future__ import annotations

import json

import pytest

from yurios.app import vaultgit
from yurios.kernel.clock import VirtualClock
from yurios.mind.selfedit import SelfEdit, SoulShapeError
from yurios.mind.util import write_json
from yurios.mind.vaultio import MindVault

from .conftest import SIM_START, make_mind

SOUL_YAML = """\
name: "Test"
fields:
  description:
    - PERSONA.md#Appearance
  personality: PERSONA.md@personality
"""

PERSONA = """\
---
soul: persona
personality: "quiet"
---
# Persona

## Appearance

Dark hair.

## Manner

Calls him darling.
"""


@pytest.fixture
def door(tmp_path):
    vault_dir = tmp_path / "vault"
    (vault_dir / "soul").mkdir(parents=True)
    (vault_dir / "soul" / "CONSTITUTION.md").write_text("# who she is\n")
    (vault_dir / "soul" / "soul.yaml").write_text(SOUL_YAML)
    (vault_dir / "soul" / "PERSONA.md").write_text(PERSONA)
    vaultgit.ensure_repo(vault_dir)
    vault = MindVault(vault_dir)
    return SelfEdit(vault, VirtualClock(start=SIM_START.timestamp())), vault


def patched(find: str, replace: str, base: str = PERSONA) -> tuple[str, dict]:
    """What the tool server hands the host for a patch: the whole file, and
    the patch it came from."""
    assert base.count(find) == 1
    return base.replace(find, replace, 1), {"find": find, "replace": replace}


def propose(selfedit, find, replace, reason="a change"):
    content, patch = patched(find, replace)
    return selfedit.propose("soul/PERSONA.md", content, reason=reason, patch=patch)


# --- the ruling-time apply ---------------------------------------------------------

def test_two_patches_approved_in_a_row_both_land(door):
    """The live failure: each looked like a one-line change, and the second
    approval quietly undid the first."""
    selfedit, vault = door
    hair = propose(selfedit, "Dark hair.", "Short dark hair.")
    manner = propose(selfedit, "Calls him darling.", "Calls him by his name.")
    assert selfedit.decide(hair.id, approve=True).outcome == "applied"
    assert selfedit.decide(manner.id, approve=True).outcome == "applied"
    text = vault.read("soul/PERSONA.md")
    assert "Short dark hair." in text and "Calls him by his name." in text


def test_the_queue_shows_a_patch_as_it_would_land_now(door):
    """After the first is approved, the second's diff is its own one line —
    not its line plus the first one being reverted."""
    selfedit, _vault = door
    hair = propose(selfedit, "Dark hair.", "Short dark hair.")
    propose(selfedit, "Calls him darling.", "Calls him by his name.")
    selfedit.decide(hair.id, approve=True)
    (left,) = selfedit.pending_view()
    assert left["stale"] is None
    assert left["diff"] == [l for l in left["diff"] if "hair" not in l.lower()]
    assert "+Calls him by his name." in left["diff"]
    assert "Short dark hair." in left["content"]        # what approving writes
    assert left["draft"] == left["content"]


def test_a_patch_that_no_longer_lands_stays_queued_and_says_why(door):
    selfedit, vault = door
    first = propose(selfedit, "Dark hair.", "Short dark hair.")
    second = propose(selfedit, "Dark hair.", "Long dark hair.")
    selfedit.decide(first.id, approve=True)

    reason = selfedit.stale(second.id)
    assert "no longer in it" in reason
    assert selfedit.decide(second.id, approve=True) is None, "not applied as written"
    assert "Short dark hair." in vault.read("soul/PERSONA.md")
    (shown,) = selfedit.pending_view()
    assert shown["stale"] == reason
    assert shown["diff"] == ["-Dark hair.", "+Long dark hair."]   # what she asked
    assert shown["draft"] == vault.read("soul/PERSONA.md")       # yours starts here

    # …and the two rulings that are still yours to make both work
    mine = vault.read("soul/PERSONA.md").replace("Short", "Long")
    assert selfedit.decide(second.id, approve=True, content=mine).revised
    assert vault.read("soul/PERSONA.md") == mine
    third = propose(selfedit, "Calls him darling.", "x")      # built on PERSONA
    vault.write("soul/PERSONA.md", PERSONA.replace("Calls him darling.", "gone"),
                gate=True)
    assert selfedit.decide(third.id, approve=False).outcome == "rejected"
    assert selfedit.pending() == []


def test_a_patch_that_now_appears_twice_is_stale(door):
    selfedit, vault = door
    edit = propose(selfedit, "Calls him darling.", "Calls him darling, softly.")
    vault.write("soul/PERSONA.md", PERSONA + "\nCalls him darling.\n", gate=True)
    assert "appears 2 times" in selfedit.stale(edit.id)


def test_a_patch_whose_reapplication_breaks_the_shape_is_stale(door):
    """The file it now lands on is checked again: a person may have moved the
    heading her `find` ran up to, and the patch would now take it away."""
    selfedit, vault = door
    edit = propose(selfedit, "Calls him darling.", "")
    # moved elsewhere in the file, the passage still lands cleanly…
    vault.write("soul/PERSONA.md", PERSONA.replace(
        "Dark hair.", "Dark hair. Calls him darling.").replace(
        "\nCalls him darling.\n", "\nWarm.\n"), gate=True)
    assert selfedit.stale(edit.id) == ""
    # …but where it is now the value soul.yaml reads, removing it would empty it
    vault.write("soul/PERSONA.md", PERSONA.replace(
        'personality: "quiet"', 'personality: "Calls him darling."').replace(
        "\nCalls him darling.\n", "\nWarm.\n"), gate=True)
    assert "personality" in selfedit.stale(edit.id)
    assert selfedit.decide(edit.id, approve=True) is None


def test_a_whole_file_proposal_over_a_changed_file_is_stale(door):
    """A rewrite has no passage to apply again: approving it as written would
    put the old file back around her change, so it waits for your ruling."""
    selfedit, vault = door
    whole = selfedit.propose("soul/PERSONA.md", PERSONA.replace("quiet", "calm"),
                             reason="calmer")
    assert selfedit.stale(whole.id) == ""
    vault.write("soul/PERSONA.md", PERSONA.replace("Dark", "Red"), gate=True)
    assert "undo that change" in selfedit.stale(whole.id)
    assert selfedit.decide(whole.id, approve=True) is None
    (shown,) = selfedit.pending_view()
    assert shown["stale"] and shown["draft"] == shown["content"]   # hers, whole
    assert "Red hair." in vault.read("soul/PERSONA.md")


def test_an_entry_queued_before_base_was_recorded_is_taken_as_written(door):
    """Upgrading must not strand a queue: an old entry applies the way it
    always did."""
    selfedit, vault = door
    old = PERSONA.replace("Dark", "Old")
    write_json(selfedit.pending_path, [{"id": "e_old", "surface": "soul/PERSONA.md",
                                       "content": old, "reason": "before",
                                       "proposed_at": "2026-01-01T00:00:00"}])
    vault.write("soul/PERSONA.md", PERSONA.replace("Dark", "New"), gate=True)
    assert selfedit.stale("e_old") == ""
    assert selfedit.decide("e_old", approve=True).outcome == "applied"
    assert vault.read("soul/PERSONA.md") == old


def test_a_queued_entry_records_its_base_and_patch(door):
    selfedit, _vault = door
    edit = propose(selfedit, "Dark hair.", "Short dark hair.")
    (entry,) = selfedit.pending()
    assert entry["id"] == edit.id and len(entry["base"]) == 16
    assert entry["patch"] == {"find": "Dark hair.", "replace": "Short dark hair."}
    whole = selfedit.propose("soul/NOTES.md", "a note\n", reason="r",
                             patch={"find": 3})              # malformed: ignored
    assert "patch" not in next(p for p in selfedit.pending() if p["id"] == whole.id)
    with pytest.raises(KeyError):
        selfedit.stale("nope")


# --- the shape rule a patch made easy to break ---------------------------------------

def test_a_referenced_heading_twice_is_refused(door):
    """`split_sections` keeps the last section under a name, so a second
    `## Appearance` silently takes the first one out of her prompt."""
    selfedit, _vault = door
    twice = PERSONA.replace("## Manner", "## Appearance\n\nAnd more.\n\n## Manner")
    with pytest.raises(SoulShapeError) as exc:
        selfedit.propose("soul/PERSONA.md", twice, reason="more")
    assert "## Appearance more than once" in str(exc.value)


def test_a_file_that_already_repeats_one_stays_editable(door):
    selfedit, vault = door
    twice = PERSONA.replace("## Manner", "## Appearance\n\nAnd more.\n\n## Manner")
    vault.write("soul/PERSONA.md", twice, gate=True)
    assert selfedit.propose("soul/PERSONA.md", twice.replace("quiet", "calm"),
                            reason="calmer").outcome == "queued"


# --- the whole door, once: her real tool server, the real loop ------------------------

class ServerRunner:
    """Her hands as the host reaches them, minus the subprocess: the real
    FastMCP server over an in-memory session."""

    def __init__(self, session):
        self.session = session

    async def call(self, tool: str, args: dict) -> str:
        from yurios.world.tools.client import result_text
        r = await self.session.call_tool(tool, args)
        text = result_text(r)
        if r.isError:
            raise RuntimeError(text)
        return text


async def test_two_patches_through_her_real_server_and_the_loop(
        cfg, seeded_vault, monkeypatch):
    """The scenario end to end: she reads her persona through `read_note`,
    proposes two one-line changes through `propose_edit`, you approve both
    through the signal the route posts, and the file has both. Then a third
    that collides with the first: it stays queued, refused as written."""
    pytest.importorskip("mcp")
    from mcp.shared.memory import create_connected_server_and_client_session

    from yurios.world.tools.server import build_server
    from yurios.world.tooltags import ToolCall

    monkeypatch.setenv("VAULT_DIR", str(seeded_vault))
    server = build_server(selfedit=True, selfies=False)
    rig = make_mind(cfg, seeded_vault)
    brain = rig.mind.brain
    for tool in ("read_note", "propose_edit", "write_note"):
        brain.guard.allow(tool, 60)

    async with create_connected_server_and_client_session(server._mcp_server) as s:
        brain.set_tools(ServerRunner(s), [])
        page = json.loads(await brain._execute(
            ToolCall("read_note", {"path": "soul/PERSONA.md"})))
        persona = (seeded_vault / "soul" / "PERSONA.md").read_text()
        assert page["text"] == persona[:len(page["text"])]
        first_line = next(l for l in persona.splitlines()
                          if l.startswith("Female-presenting"))
        manner_line = next(l for l in persona.splitlines()
                           if l.startswith("Warm first, always"))

        for find, replace, why in (
                (first_line, first_line.replace("Female-presenting",
                                                "Short-haired now, female-presenting"),
                 "I cut it"),
                (manner_line, manner_line.replace("Warm first, always",
                                                  "Steady first. Warm always"),
                 "I've settled")):
            said = await brain._execute(ToolCall("propose_edit", {
                "surface": "PERSONA.md", "find": find, "replace": replace,
                "reason": why}))
            # what she reads is the guard's 600 characters of it, cut inside
            # her persona — so "queued" has to come before `content` (§23.3)
            assert len(said) <= 600 and said.endswith("…")
            assert '"queued": true' in said and '"content"' in said, said
            assert said.index('"queued"') < said.index('"content"')
        collide = await brain._execute(ToolCall("propose_edit", {
            "surface": "PERSONA.md", "find": first_line,
            "replace": "Something else entirely.", "reason": "second thoughts"}))
        assert "proposed" in collide

        # a desk note under soul/ is refused rather than shadowing her soul
        refused = await brain._execute(ToolCall(
            "write_note", {"path": "soul/PERSONA.md", "text": "x"}))
        assert "not a place on your desk" in refused

    first, second, third = rig.mind.selfedit.pending()
    for edit in (first, second):
        rig.mind.bus.post("selfedit_decision", {"id": edit["id"], "approve": True},
                          source="user")
    await rig.mind.tick()
    text = (seeded_vault / "soul" / "PERSONA.md").read_text()
    assert "Short-haired now, female" in text and "Steady first. Warm" in text
    assert "Something else entirely." not in text

    (left,) = rig.mind.selfedit.pending_view()
    assert left["id"] == third["id"] and "no longer in it" in left["stale"]
    rig.mind.bus.post("selfedit_decision", {"id": third["id"], "approve": True},
                      source="user")
    await rig.mind.tick()
    assert rig.mind.selfedit.pending(), "a stale approval leaves it queued"
    assert (seeded_vault / "soul" / "PERSONA.md").read_text() == text
    assert not (seeded_vault / "workspace" / "soul").exists()
