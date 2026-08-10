"""Her desk and her skills (SPEC §34) — the sandbox, the ceilings, the catalog.

The sandbox tests are the load-bearing ones. `Workspace` is reached from the
tool server, which is a separate process running whatever path a 12B model
wrote into a JSON argument, so "can this be talked into opening a file outside
the workspace" is the only question that matters here.
"""
from __future__ import annotations

import pytest

from yurios.mind.workspace import (DeskFull, OutsideTheDesk, SkillStore,
                                   Workspace)


@pytest.fixture
def desk(tmp_path) -> Workspace:
    return Workspace(tmp_path / "vault" / "workspace")


@pytest.fixture
def skills(tmp_path) -> SkillStore:
    return SkillStore(tmp_path / "vault" / "skills")


# ------------------------------------------------------------------- the sandbox

@pytest.mark.parametrize("path", [
    "../soul/CONSTITUTION.md",       # the obvious climb
    "notes/../../soul/PERSONA.md",   # the climb that starts legally
    "/etc/passwd",                   # absolute
    ".git/config",                   # a dotdir inside the desk
    ".env",                          # a dotfile inside the desk
    "",                              # nothing at all
])
def test_the_desk_refuses_everything_outside_it(desk, path):
    with pytest.raises(OutsideTheDesk):
        desk.resolve(path)


def test_a_symlink_out_of_the_desk_is_caught_like_a_climb(desk, tmp_path):
    """The textual checks above would pass this one — `resolve()` follows the
    link first and then tests containment, which is the whole reason it does
    them in that order."""
    secret = tmp_path / "vault" / "soul" / "CONSTITUTION.md"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("the constitution")
    (desk.root / "innocent.md").symlink_to(secret)
    with pytest.raises(OutsideTheDesk):
        desk.resolve("innocent.md")


def test_writing_and_reading_a_note(desk):
    entry = desk.write("research/boards.md", "three brands, one of them fine")
    assert entry.path == "research/boards.md"
    assert "one of them fine" in desk.read("research/boards.md")
    assert [e.path for e in desk.list() if not e.is_dir] == ["research/boards.md"]


def test_read_lines_and_count_are_one_based_and_inclusive(desk):
    desk.write("report.md", "first\nsecond\nthird\n")
    text, first, last, count = desk.read_lines("report.md", start_line=2, end_line=3)
    assert (text, first, last, count) == ("second\nthird\n", 2, 3, 3)
    assert desk.line_count("report.md") == 3


def test_append_adds_a_line_without_rewriting(desk):
    desk.write("log.md", "first")
    desk.append("log.md", "second\n")
    assert desk.read("log.md") == "first\nsecond\n"


def test_edit_replaces_one_exact_passage_in_an_existing_note(desk):
    desk.write("research/boards.md", "# Boards\n\n- Alder: promising\n")
    desk.edit("research/boards.md", "Alder: promising", "Alder: unavailable")
    assert desk.read("research/boards.md") == "# Boards\n\n- Alder: unavailable\n"


def test_edit_finds_a_unique_heading_despite_its_level_or_number(desk):
    desk.write("research/report.md", "# Report\n\n### 2. Commercial Divide\n\nOld text\n")
    desk.edit("research/report.md", "## Commercial Divide",
              "## Commercial Divide\n\nNew text")
    assert desk.read("research/report.md") == (
        "# Report\n\n## Commercial Divide\n\nNew text\n\nOld text\n")


def test_edit_refuses_a_no_op_instead_of_claiming_the_note_changed(desk):
    desk.write("note.md", "unchanged")
    with pytest.raises(ValueError, match="identical"):
        desk.edit("note.md", "unchanged", "unchanged")


def test_edit_deletes_one_exact_duplicate_passage_with_empty_replacement(desk):
    desk.write("report.md", "Keep this.\n\nDuplicate text.\n")
    desk.edit("report.md", "Duplicate text.\n", "")
    assert desk.read("report.md") == "Keep this.\n\n"


def test_edit_deletes_the_later_of_two_identical_passages(desk):
    desk.write("report.md", "Before\n\nRepeated section\n\nAfter\n\n"
               "Repeated section\n")
    desk.edit("report.md", "Repeated section\n", "")
    assert desk.read("report.md") == "Before\n\nRepeated section\n\nAfter\n"


def test_edit_recovers_a_mixed_escaped_newline_passage(desk):
    desk.write("report.md", "## First\nOne.\n\n## Second\nTwo.\n\n## Third\nThree.\n")
    desk.edit("report.md", "## First\nOne.\n\n## Second\nTwo.\\n\\n## Third\nThree.\n",
              "## First\nOne.\n")
    assert desk.read("report.md") == "## First\nOne.\n"


def test_edit_recovers_a_stray_backslash_before_an_escaped_newline(desk):
    desk.write("report.md", "## First\nOne.\n\n## Second\nTwo.\n\n## Third\nThree.\n")
    desk.edit("report.md", "## First\nOne.\n\n## Second\nTwo.\\\n\\n## Third\nThree.\n",
              "## First\nOne.\n")
    assert desk.read("report.md") == "## First\nOne.\n"


def test_edit_lines_deletes_the_later_of_two_identical_sections(desk):
    desk.write("report.md", "## Keep\n\nFirst.\n\n## Copy\n\nSame.\n\n"
               "## Copy\n\nSame.\n")
    desk.edit_lines("report.md", start_line=9, end_line=11, new_text="")
    assert desk.read("report.md") == "## Keep\n\nFirst.\n\n## Copy\n\nSame.\n"


@pytest.mark.parametrize("old_text", ["", "missing", "same"])
def test_edit_refuses_an_empty_missing_or_ambiguous_passage(desk, old_text):
    desk.write("note.md", "same\nsame\n")
    with pytest.raises(ValueError):
        desk.edit("note.md", old_text, "replacement")


def test_delete_removes_the_file_and_says_whether_it_was_there(desk):
    desk.write("gone.md", "x")
    assert desk.delete("gone.md") is True
    assert desk.delete("gone.md") is False       # not an error; just nothing there


def test_the_ceilings_refuse_rather_than_fill_the_disk(tmp_path):
    small = Workspace(tmp_path / "desk", max_file_bytes=64, max_tree_bytes=200)
    with pytest.raises(DeskFull):
        small.write("big.md", "x" * 500)
    small.write("a.md", "x" * 60)
    small.write("b.md", "x" * 60)
    small.write("c.md", "x" * 60)
    with pytest.raises(DeskFull):
        small.write("d.md", "x" * 60)            # the tree ceiling, not the file one


def test_the_digest_is_an_index_not_the_contents(desk):
    desk.write("a.md", "the whole text of a")
    desk.write("b.md", "the whole text of b")
    digest = desk.digest()
    assert "a.md" in digest and "b.md" in digest
    assert "whole text" not in digest             # names and sizes only


def test_the_desk_ignores_itself_and_skills_do_not(desk, skills):
    """Scratch churns; a skill is a durable statement. The rule lives *inside*
    the desk folder so an existing vault gets it without a migration."""
    assert (desk.root / ".gitignore").read_text().endswith("*\n")
    assert not (skills.root / ".gitignore").exists()


def test_a_real_repo_carries_skills_and_not_the_desk(tmp_path):
    """The property the .gitignore is for, asserted against actual git rather
    than against the file's contents."""
    import subprocess

    from yurios.app import vaultgit
    vault = tmp_path / "vault"
    vault.mkdir()
    vaultgit.ensure_repo(vault)
    Workspace(vault / "workspace").write("diary/2026-08-06.md", "a quiet one")
    SkillStore(vault / "skills").save("tea-timer", description="steeping",
                                      body="Ask which tea first.")
    vaultgit.commit(vault, "a night's work")
    tracked = subprocess.run(["git", "-C", str(vault), "ls-files"],
                             capture_output=True, text=True).stdout.split()
    assert "skills/tea-timer/SKILL.md" in tracked
    assert not any(p.startswith("workspace/") for p in tracked)
    # …and the note is still right there on disk, just not in the history
    assert (vault / "workspace" / "diary" / "2026-08-06.md").is_file()


def test_dotfiles_are_skipped_by_list_not_just_refused_by_resolve(desk):
    """A listing that advertised a path no other method will open would be a
    worse answer than leaving it out."""
    (desk.root / ".hidden").write_text("x")
    assert [e.path for e in desk.list()] == []


# --------------------------------------------------------------------- skills

def test_a_saved_skill_round_trips_through_its_frontmatter(skills):
    skills.save("tea-timer", description="when they ask to steep something",
                body="Ask which tea first, then set the timer.")
    skill = skills.get("tea-timer")
    assert skill.name == "tea-timer"
    assert skill.description == "when they ask to steep something"
    assert "Ask which tea first" in skill.body
    assert skill.author == "her"


def test_the_catalog_is_one_line_per_skill_and_never_the_body(skills):
    """The whole economics of §34.3: twenty skills cost twenty lines until one
    of them is actually opened."""
    skills.save("tea-timer", description="when they ask to steep something",
                body="A LONG METHOD " * 200)
    skills.save("bass-talk", description="when the conversation turns to music",
                body="A LONG METHOD " * 200)
    catalog = skills.catalog()
    assert catalog.count("\n") == 1               # two skills, two lines
    assert "when they ask to steep" in catalog
    assert "LONG METHOD" not in catalog


def test_a_disabled_skill_stays_on_disk_and_leaves_the_catalog(skills):
    skills.save("old", description="something she used to do", body="...")
    path = skills.root / "old" / "SKILL.md"
    path.write_text(path.read_text().replace("enabled: true", "enabled: false"))
    assert skills.catalog() == ""
    assert skills.get("old") is not None          # readable, just not offered


def test_a_skill_needs_a_usable_name_and_a_description(skills):
    with pytest.raises(ValueError):
        skills.save("Tea Timer!", description="x", body="y")
    with pytest.raises(ValueError):
        skills.save("tea-timer", description="   ", body="y")


def test_mangled_frontmatter_degrades_to_a_bodyless_skill(skills):
    """A hand-edited SKILL.md with broken YAML must not take down the catalog
    for every other skill — she loses one entry, not the block."""
    folder = skills.root / "broken"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: [unclosed\n---\n\nbody\n")
    skills.save("fine", description="a working one", body="ok")
    assert "a working one" in skills.catalog()
    assert skills.get("broken").name == "broken"  # falls back to the folder name


def test_removing_a_skill_takes_its_supporting_files_with_it(skills):
    skills.save("tea-timer", description="steeping", body="...")
    (skills.root / "tea-timer" / "chart.md").write_text("oolong: 3min")
    assert skills.remove("tea-timer") is True
    assert not (skills.root / "tea-timer").exists()
    assert skills.remove("tea-timer") is False
