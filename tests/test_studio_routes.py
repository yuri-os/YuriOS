"""The studio's HTTP surface: create, edit, preview, export, and the refusals."""
from __future__ import annotations

import base64
import io
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image
from starlette.testclient import TestClient

from tests.conftest import FakeEmbedder
from tests.support.cards import card_data, png_card, preferred, wrapper
from yurios.characters import CharacterImporter, CharacterRegistry
from yurios.characters.creator import template_draft
from yurios.characters.exporter import estimate_tokens
from yurios.characters.studio import Draft, apply_draft, read_draft
from yurios.world.config import Config
from yurios.world.host import create_host_app


@pytest.fixture
def node(tmp_path):
    cfg = Config(_env_file=None, data_dir=tmp_path / "data",
                 telegram_bot_token="", telegram_chat_id="")
    registry = CharacterRegistry(tmp_path / "data")
    CharacterImporter(registry, initialize_git=True).import_card(
        png_card(wrapper(card_data(), native=True)), character_id="subject")
    # Her own embedder, not a cold torch model: these tests are about the
    # studio's HTTP surface, and building a character on this node otherwise
    # loads sentence-transformers for real — twenty seconds, once per worker,
    # to index memory nothing here reads back.
    with TestClient(create_host_app(cfg, registry, embedder=FakeEmbedder())) as client:
        yield client, registry


def test_template_is_a_working_starting_point(node):
    client, _ = node
    payload = client.get("/api/studio/template").json()

    assert payload["draft"]["name"]              # soul-src is installed in-repo
    assert [s["field"] for s in payload["sections"]] == \
        ["identity", "history", "appearance", "manner"]
    assert "system_prompt" in payload["constitution_fields"]


def test_studio_get_returns_draft_provenance_and_images(node):
    client, _ = node
    payload = client.get("/api/characters/subject/studio").json()

    assert payload["draft"]["name"] == "Card Person"
    assert payload["draft"]["personality"] == "dry, observant, kind"
    assert payload["draft"]["description"].startswith("A complete identity")
    assert payload["provenance"]["manner"]["origin"] in ("seed", "you", "her", "unknown")
    assert payload["images"] == {"portrait": True, "selfies": []}


def test_a_studio_save_reaches_the_soul_and_the_registry(node):
    client, registry = node
    draft = client.get("/api/characters/subject/studio").json()["draft"]
    draft["manner"] = "She waits by the window, and does not say so."
    draft["alternate_greetings"] = ["Oh — it's you.", "Late again. Sit."]

    response = client.patch("/api/characters/subject/studio", json={"draft": draft})

    assert response.status_code == 200
    assert "PERSONA.md" in response.json()["touched"]
    again = client.get("/api/characters/subject/studio").json()["draft"]
    assert again["manner"] == "She waits by the window, and does not say so."
    assert again["alternate_greetings"] == ["Oh — it's you.", "Late again. Sit."]
    assert "waits by the window" in registry.require("subject").display.description


def test_removing_a_greeting_keeps_the_manifest_loadable(node):
    """The manifest's greeting refs must track the blocks, or the next load
    fails on a section that is no longer there."""
    from yurios.app.core.soul import SoulLoader

    client, registry = node
    draft = client.get("/api/characters/subject/studio").json()["draft"]
    draft["alternate_greetings"] = ["Only one now."]
    client.patch("/api/characters/subject/studio", json={"draft": draft})

    soul = SoulLoader(registry.require("subject").paths.vault / "soul").load()
    assert soul.return_greetings == ["Only one now."]


def test_preview_returns_the_report_without_writing_a_file(node):
    client, _ = node
    payload = client.post("/api/characters/subject/studio/preview", json={}).json()

    assert payload["bytes"] == 0
    assert payload["card"]["data"]["name"] == "Card Person"
    assert payload["privacy"]["stays"]
    assert payload["privacy"]["assay"]["ran_on"] == ["json"]
    assert {row["field"] for row in payload["report"]} >= {"description", "first_mes"}


def test_the_one_click_export_goes_through_the_pipeline(node):
    client, _ = node
    response = client.get("/api/characters/subject/export")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert 'filename="card-person.png"' in response.headers["content-disposition"]
    assert preferred(response.content)["name"] == "Card Person"


def test_configured_export_honours_its_options(node):
    client, _ = node
    response = client.post("/api/characters/subject/export",
                           json={"spec": "v2", "include_soul": False,
                                 "attribution": False, "timestamps": False})

    data = preferred(response.content)
    assert response.status_code == 200
    assert "yurios" not in data["tags"]
    assert "soul" not in data["extensions"]["yurios"]


def test_a_refusal_is_a_422_with_something_to_act_on(node):
    client, registry = node
    record = registry.require("subject")
    secret = "He lives on the coast and takes his coffee black without fail."
    (record.paths.vault / "soul" / "USER.md").write_text(f"# U\n\n## W\n\n{secret}\n")
    persona = record.paths.vault / "soul" / "PERSONA.md"
    persona.write_text(persona.read_text() + f"\n\n{secret}\n")

    response = client.get("/api/characters/subject/export")

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "review_required"
    assert detail["overlaps"]
    assert client.post("/api/characters/subject/export",
                       json={"acknowledged": True}).status_code == 200


def test_creating_a_character_starts_her(node):
    client, registry = node
    draft = {**template_draft().to_dict(), "name": "Halden"}

    response = client.post("/api/characters", json={"draft": draft})

    assert response.status_code == 201
    character = response.json()["character"]
    assert character["id"] == "halden"
    assert not character["review_required"]
    assert registry.require("halden").paths.vault.is_dir()
    assert not registry.require("halden").paths.source_card.exists()
    assert client.get("/api/characters/halden/export").status_code == 200


def test_creating_without_a_name_is_refused(node):
    client, _ = node
    response = client.post("/api/characters", json={"draft": {"name": "  "}})
    assert response.status_code == 400


def test_a_created_character_is_not_marked_as_imported(node):
    client, _ = node
    client.post("/api/characters", json={
        **{"draft": {**template_draft().to_dict(), "name": "Halden"}}})
    payload = client.post("/api/characters/halden/studio/preview", json={}).json()
    assert payload["card"]["data"]["extensions"]["yurios"]["lineage"]["canon"] == "original"


def test_the_selfie_list_is_registry_scoped(node):
    client, registry = node
    record = registry.require("subject")
    record.paths.selfies.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (10, 10)).save(record.paths.selfies / "one.png", "PNG")

    selfies = client.get("/api/characters/subject/selfies").json()["selfies"]

    assert [s["name"] for s in selfies] == ["one.png"]
    assert selfies[0]["url"] == "/api/characters/subject/selfies/one.png"
    assert client.get("/api/characters/nobody/selfies").status_code == 404


def test_setting_a_portrait_sanitises_it(node):
    client, registry = node
    buffer = io.BytesIO()
    Image.new("RGB", (40, 60), (90, 40, 60)).save(buffer, "PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode()

    response = client.post("/api/characters/subject/portrait",
                           json={"image": f"data:image/png;base64,{encoded}"})

    assert response.status_code == 200
    assert Image.open(registry.require("subject").paths.portrait).size == (40, 60)


def test_adopting_a_selfie_becomes_her_portrait(node):
    """The studio's face picker rides this route. It used to only set an export
    option that lived in the browser tab: the exported card wore the selfie and
    her portrait, her dashboard tile and the settings modal all still wore the
    old face."""
    client, registry = node
    record = registry.require("subject")
    record.paths.selfies.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (30, 45), (12, 200, 90)).save(record.paths.selfies / "desk.png")

    response = client.post("/api/characters/subject/portrait",
                           json={"selfie": "desk.png"})

    assert response.status_code == 200
    adopted = Image.open(record.paths.portrait)
    assert adopted.size == (30, 45)
    assert adopted.getpixel((0, 0))[:3] == (12, 200, 90)


def test_a_portrait_from_a_selfie_is_path_checked(node):
    client, _ = node
    response = client.post("/api/characters/subject/portrait",
                           json={"selfie": "../../../etc/hostname"})
    assert response.status_code == 404


def test_unknown_characters_are_404_everywhere(node):
    client, _ = node
    assert client.get("/api/characters/nobody/studio").status_code == 404
    assert client.get("/api/characters/nobody/export").status_code == 404
    assert client.post("/api/characters/nobody/studio/preview", json={}).status_code == 404


def test_the_draft_survives_a_full_python_round_trip(node):
    """`read_draft(apply_draft(d)) == d` for everything the studio edits."""
    client, registry = node
    record = registry.require("subject")
    original, _ = read_draft(record)
    original.manner = "Quieter than she was."
    original.examples = ["{{user}}: hi\n{{char}}: mm."]
    original.alternate_greetings = ["one", "two"]
    original.lorebook = {"scan_depth": 6, "token_budget": 400,
                         "recursive_scanning": False,
                         "entries": [{"name": "Pier", "keys": ["pier"],
                                      "content": "Low tide.", "constant": False,
                                      "use_regex": False, "case_sensitive": False}]}

    apply_draft(record, original)
    again, _ = read_draft(record)

    for field in ("name", "manner", "appearance", "personality", "scenario",
                  "system_prompt", "post_history_instructions",
                  "alternate_greetings", "examples", "tags", "character_version"):
        assert getattr(again, field) == getattr(original, field), field
    assert again.lorebook["entries"][0]["keys"] == ["pier"]
    assert again.lorebook["scan_depth"] == 6


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "drafts" / "backbone.json"


def test_the_server_derives_the_backbone_as_the_fixture_says():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    draft = Draft.from_dict(payload["draft"])

    assert draft.description == payload["expected_description"]
    assert draft.to_dict()["description"] == payload["expected_description"]
    assert draft.lorebook["entries"][0]["keys"] == ["window", "light"]


def test_the_browser_derives_it_the_same_way():
    """One fixture, two implementations of the same shape.

    `web/studio/draft.js` re-derives `description` from the four backbone
    sections so the preview can update without a round trip, and estimates
    tokens so the budget panel does not need one either. If the two ever
    disagree the studio shows a card that is not the card that ships — which is
    the one thing the preview exists to prevent. Skips where node is absent;
    the suite's contract is that it runs offline, not that it runs without npm.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    root = Path(__file__).resolve().parents[1]
    script = f"""
      import {{ normalise, description, tokens }} from "{root}/web/studio/draft.js";
      import {{ readFileSync }} from "node:fs";
      const fixture = JSON.parse(readFileSync("{FIXTURE}", "utf8"));
      const draft = normalise(fixture.draft);
      const out = {{ description: description(draft),
                    tokens: Object.fromEntries(
                      Object.keys(fixture.expected_tokens).map((k) =>
                        [k, tokens(k === "description" ? description(draft) : draft[k])])) }};
      process.stdout.write(JSON.stringify(out));
    """
    result = subprocess.run([node, "--input-type=module", "-e", script],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr

    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    produced = json.loads(result.stdout)
    draft = Draft.from_dict(payload["draft"])

    # against the fixture, and against the server — the second is the real
    # assertion, because it is what stops the two drifting apart silently.
    assert produced["description"] == payload["expected_description"]
    assert produced["description"] == draft.description
    assert produced["tokens"] == payload["expected_tokens"]
    assert produced["tokens"] == {
        "description": estimate_tokens(draft.description),
        "personality": estimate_tokens(draft.personality),
        "scenario": estimate_tokens(draft.scenario),
        "first_mes": estimate_tokens(draft.first_mes),
    }


# ---- the retired bootstrap (SPEC §5.4) --------------------------------------

def retire(registry, character_id: str = "subject") -> Path:
    """What a greeting does once she has met you: move the bootstrap aside."""
    soul = Path(registry.require(character_id).paths.vault) / "soul"
    done = soul / "onboarded" / "BOOTSTRAP.done.md"
    done.parent.mkdir(parents=True, exist_ok=True)
    (soul / "BOOTSTRAP.md").rename(done)
    return soul


def test_a_retired_cold_open_is_still_readable_and_editable(node):
    """Consumed is not gone. The studio used to show an empty box for every
    character anyone had ever spoken to, because the field's file had moved."""
    client, registry = node
    soul = retire(registry)

    draft = client.get("/api/characters/subject/studio").json()["draft"]
    assert draft["first_mes"].strip(), "her cold open followed the file into retirement"

    draft["first_mes"] = "*The rain stops.* You came back after all."
    response = client.patch("/api/characters/subject/studio", json={"draft": draft})
    assert response.status_code == 200

    # written where it lives — and NOT resurrected under soul/, which is the flag
    # the runtime reads as "she has never met you"
    assert not (soul / "BOOTSTRAP.md").exists()
    assert "You came back after all." in (
        soul / "onboarded" / "BOOTSTRAP.done.md").read_text(encoding="utf-8")
    again = client.get("/api/characters/subject/studio").json()["draft"]
    assert again["first_mes"] == "*The rain stops.* You came back after all."


def test_a_retired_cold_open_still_ships_as_the_cards_first_message(node):
    """A stranger importing her has not met her: the card carries the cold open
    she was written with, not the return greeting she uses on you."""
    from yurios.characters.exporter import ExportOptions, build_export

    client, registry = node
    retire(registry)

    result = build_export(registry.require("subject"), ExportOptions(acknowledged=True))
    fields = result.card["data"]

    assert "Cold open" not in fields["first_mes"]
    assert fields["first_mes"].strip()
    assert fields["first_mes"] not in fields["alternate_greetings"]
    assert not [w for w in result.warnings if w.code == "bootstrap_consumed"]


# ------------------------------------------- her selfie library (SPEC §7.6)

def test_the_selfie_library_starts_as_ours_and_forks_on_the_first_save(node):
    """Until she has a file of her own the studio shows the shipped library, so
    there is always somewhere to start; saving writes *her* book, and from then
    on it replaces ours outright rather than merging over it."""
    from yurios.characters import selfiebook

    client, registry = node
    record = registry.require("subject")

    payload = client.get("/api/characters/subject/selfie-templates").json()
    assert payload["source"] == "shipped"
    assert [slot["key"] for slot in payload["slots"]] == list(selfiebook.SLOT_NAMES)
    assert not record.paths.selfie_templates.exists()

    book = payload["book"]
    book["tool_hint"] = "she lives in a lighthouse, not a tower"
    book["slots"]["scenes"] = [{"key": "lamp room", "prompt": "SCENE-lamp",
                                "negative": "", "pinned": False}]
    book["slots"]["wardrobe"].append({"key": "oilskin", "prompt": "WARDROBE-oilskin",
                                      "negative": "NEG-silk", "pinned": True})
    saved = client.put("/api/characters/subject/selfie-templates", json={"book": book})
    assert saved.status_code == 200
    assert saved.json()["source"] == "character"

    again = client.get("/api/characters/subject/selfie-templates").json()
    assert again["source"] == "character"
    assert [row["key"] for row in again["book"]["slots"]["scenes"]] == ["lamp room"]
    assert again["book"]["tool_hint"] == "she lives in a lighthouse, not a tower"
    oilskin = next(row for row in again["book"]["slots"]["wardrobe"]
                   if row["key"] == "oilskin")
    assert oilskin["negative"] == "NEG-silk" and oilskin["pinned"] is True

    # the file the camera actually loads, not just what the page echoes back
    from yurios.forge import SelfieBook
    loaded = SelfieBook.load(record.paths.selfie_templates)
    assert set(loaded.scenes) == {"lamp room"}
    assert loaded.compose(wardrobe="oilskin")[2] == "NEG-silk"
    # …and the shipped one is still there for whoever hasn't forked it
    assert "sanctuary" in SelfieBook.load(selfiebook.SHIPPED).scenes


def test_an_empty_selfie_library_is_refused_and_delete_is_the_way_back(node):
    """Every slot empty is a camera with no words, and it is far more likely to
    be a page mid-edit than an intention. Deleting is how you say "use ours"."""
    client, registry = node
    record = registry.require("subject")

    blank = {"tool_hint": "", "slots": {name: [] for name in
                                        ("scenes", "framings", "lighting",
                                         "moods", "wardrobe")}}
    assert client.put("/api/characters/subject/selfie-templates",
                      json={"book": blank}).status_code == 400
    assert not record.paths.selfie_templates.exists()

    book = client.get("/api/characters/subject/selfie-templates").json()["book"]
    client.put("/api/characters/subject/selfie-templates", json={"book": book})
    assert record.paths.selfie_templates.is_file()

    gone = client.delete("/api/characters/subject/selfie-templates")
    assert gone.status_code == 200 and gone.json()["source"] == "shipped"
    assert not record.paths.selfie_templates.exists()
    assert client.get("/api/characters/subject/selfie-templates").json()["source"] \
        == "shipped"
