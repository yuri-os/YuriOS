"""Her camera's wording, per model (forge/prompts.py, SPEC §7.6).

The resolution rule is the whole feature, so most of this file is that rule
pinned case by case: later layers win outright, the narrowest cell wins within
a layer, and an empty field is never a value. The rest is the plumbing that
must actually use it — the forge, per render, and the studio's routes.
"""
from __future__ import annotations

import pytest

from tests.support.checkpoints import write_safetensors_header
from yurios.forge import Character, ImageForge, SelfieBook, make_backend
from yurios.forge.character import REGISTER_PATH
from yurios.forge.prompts import (CHARACTER_FILE, HOUSE_FILE, Profile, PromptSources,
                                  camera_of, load, parse, render_yaml, resolve, write)
from yurios.world.selfies import build_forge, camera_identity, wording_for


def values(resolved):
    return {key: entry["value"] for key, entry in resolved.items()}


# ---- parsing -------------------------------------------------------------------

def test_the_file_shape_and_the_api_shape_are_the_same_profile():
    from_file = parse({"quality_preamble": "PRE", "models": {"x.safetensors": {
        "quality_postfix": "POST"}}})
    from_api = parse({"default": {"quality_preamble": "PRE"}, "architectures": {},
                      "models": {"x.safetensors": {"quality_postfix": "POST"}}})
    assert from_file == from_api


def test_an_empty_field_is_inherit_not_a_value():
    profile = parse({"quality_preamble": "  ", "architectures": {"anima": {
        "base_negative": ""}}})
    assert profile.default == {}
    assert profile.architectures == {}           # an all-empty cell is no cell


def test_a_prompts_file_refuses_what_it_does_not_know():
    with pytest.raises(ValueError, match="unknown key"):
        parse({"quality_prefix": "typo"})
    with pytest.raises(ValueError, match="unknown field"):
        parse({"models": {"x": {"identity": "not wording"}}})
    with pytest.raises(ValueError, match="must be text"):
        parse({"base_negative": ["a", "b"]})


def test_an_appearance_file_is_read_leniently():
    # name and identity are the character's business, not wording's
    profile = parse({"name": "Yuri", "identity": "cat ears", "quality_preamble": "P"},
                    strict=False)
    assert profile.default == {"quality_preamble": "P"}


def test_folded_text_comes_back_as_one_line():
    assert parse({"quality_preamble": "a,\n  b,\n\n c"}).default == \
        {"quality_preamble": "a, b, c"}


def test_write_round_trips_and_an_empty_profile_removes_the_file(tmp_path):
    path = tmp_path / HOUSE_FILE
    profile = parse({"quality_postfix": "END",
                     "architectures": {"anima": {"base_negative": "score_1"}},
                     "models": {"m.safetensors": {"quality_preamble": "P"}}})
    write(path, profile)
    assert path.read_text().startswith("# How her camera")
    assert load(path) == profile

    write(path, Profile())
    assert not path.exists()


def test_render_yaml_of_nothing_is_just_the_header():
    assert render_yaml(Profile()).strip().startswith("#")


# ---- resolution ------------------------------------------------------------------

def layer(name, data):
    return (name, parse(data, strict=False))


def test_within_a_layer_the_narrowest_cell_wins():
    house = layer("house", {
        "quality_preamble": "EVERY",
        "architectures": {"anima": {"quality_preamble": "ANIMA"}},
        "models": {"a.safetensors": {"quality_preamble": "MODEL-A"}}})
    assert values(resolve([house], "anima", "a.safetensors"))["quality_preamble"] == "MODEL-A"
    assert values(resolve([house], "anima", "b.safetensors"))["quality_preamble"] == "ANIMA"
    assert values(resolve([house], "sdxl", "c.safetensors"))["quality_preamble"] == "EVERY"


def test_two_checkpoints_of_one_architecture_can_differ():
    house = layer("house", {"models": {"a.safetensors": {"quality_postfix": "A"},
                                       "b.safetensors": {"quality_postfix": "B"}}})
    assert values(resolve([house], "anima", "a.safetensors"))["quality_postfix"] == "A"
    assert values(resolve([house], "anima", "b.safetensors"))["quality_postfix"] == "B"


def test_a_later_layer_wins_outright_even_over_a_narrower_cell():
    # Hers beats the house's, everywhere: her every-model prefix wins over the
    # house's setting for this exact model.
    house = layer("house", {"models": {"a.safetensors": {"quality_preamble": "HOUSE-A"}}})
    her = layer("her", {"quality_preamble": "HERS"})
    out = resolve([house, her], "anima", "a.safetensors")
    assert out["quality_preamble"] == {"value": "HERS", "source": "her · every model"}


def test_fields_resolve_independently():
    shipped = layer("shipped", {"quality_preamble": "P0", "base_negative": "N0"})
    house = layer("house", {"architectures": {"anima": {"base_negative": "N-ANIMA"}}})
    out = values(resolve([shipped, house], "anima", ""))
    assert out["quality_preamble"] == "P0" and out["base_negative"] == "N-ANIMA"


def test_every_field_is_present_even_when_nothing_sets_it():
    out = resolve([], "sdxl", "x")
    assert out["quality_postfix"] == {"value": "", "source": ""}


def test_sources_name_the_layer_and_the_cell():
    house = layer("house", {"architectures": {"anima": {"base_negative": "N"}}})
    assert resolve([house], "anima", "m")["base_negative"]["source"] == \
        "house · architecture anima"


# ---- the camera's identity ---------------------------------------------------------

@pytest.mark.parametrize("name,opts,want", [
    ("diffusers", {"model_path": "/drive/AI/pie.safetensors"}, ("sdxl", "pie.safetensors")),
    ("anima", {"model_path": "/x/a.safetensors"}, ("anima", "a.safetensors")),
    ("krea2", {"model_path": "/x/k.safetensors"}, ("krea2", "k.safetensors")),
    ("openrouter", {"model": "bytedance-seed/seedream-4.5", "api_key": "k"},
     ("openrouter", "bytedance-seed/seedream-4.5")),
    ("mock", {}, ("mock", "")),
])
def test_camera_of_names_a_checkpoint_by_file_and_a_hosted_model_by_id(name, opts, want):
    assert camera_of(make_backend(name, **opts)) == want


def test_camera_identity_matches_what_build_forge_builds(cfg, tmp_path, monkeypatch):
    from yurios.forge.backends.anima import AnimaBackend
    monkeypatch.setattr(AnimaBackend, "deps_available", staticmethod(lambda: True))
    ckpt = write_safetensors_header(tmp_path / "a.safetensors",
                                    ["model.diffusion_model.llm_adapter.embed.weight"])
    cfg.selfie_backend, cfg.selfie_local_model = "diffusers", str(ckpt)
    forge, _status = build_forge(cfg)
    assert camera_identity(cfg) == camera_of(forge.backend) == ("anima", "a.safetensors")


# ---- the layers on disk -------------------------------------------------------------

def test_an_appearance_that_restates_the_register_does_not_override_it(tmp_path):
    """The shipped yuri.yaml carries the register's prefix word for word.
    Counted as hers, it would sit above every house and per-model setting."""
    register = load(REGISTER_PATH, strict=False)
    appearance = tmp_path / "appearance.yaml"
    appearance.write_text(
        "name: Her\nidentity: someone\n"
        f"quality_preamble: \"{register.default['quality_preamble']}\"\n"
        "base_negative: her own negative\n")
    house = tmp_path / HOUSE_FILE
    write(house, parse({"architectures": {"anima": {"quality_preamble": "HOUSE-ANIMA"}}}))
    sources = PromptSources(register=REGISTER_PATH, house=house, appearance=appearance)

    out = values(resolve(sources.layers(), "anima", "x"))
    assert out["quality_preamble"] == "HOUSE-ANIMA"
    assert out["base_negative"] == "her own negative"   # a real override stays one


def test_an_unreadable_file_costs_its_wording_not_the_camera(tmp_path, caplog):
    broken = tmp_path / CHARACTER_FILE
    broken.write_text("quality_preamble: [unclosed\n")
    sources = PromptSources(register=REGISTER_PATH, character=broken)
    with caplog.at_level("WARNING"):
        names = [name for name, _ in sources.layers()]
    assert names == ["shipped", "house", "her appearance"]
    assert "ignoring the her prompt wording" in caplog.text


def test_the_shipped_register_words_anima_its_own_way():
    out = values(resolve([("shipped", load(REGISTER_PATH, strict=False))], "anima", "x"))
    assert "score_7" in out["quality_preamble"]
    assert "score_1" in out["base_negative"]
    sdxl = values(resolve([("shipped", load(REGISTER_PATH, strict=False))], "sdxl", "x"))
    assert "score_7" not in sdxl["quality_preamble"]


# ---- the forge uses it, per render --------------------------------------------------

@pytest.fixture
def forge(tmp_path):
    character = Character(name="Her", identity="IDENTITY", quality_preamble="BOOT-PRE")
    # Two folders, as on disk: the two file names are the same name.
    sources = PromptSources(register=None, house=tmp_path / "data" / HOUSE_FILE,
                            character=tmp_path / "her" / CHARACTER_FILE)
    book = SelfieBook(scenes={"room": "SCENE"}, framings={}, lighting={}, moods={},
                      wardrobe={})
    return ImageForge(character, book, make_backend("mock"), out_dir=tmp_path / "out",
                      wording=sources)


def test_postfix_closes_the_prompt_after_the_scene():
    character = Character(name="Her", identity="ID", quality_preamble="PRE",
                          quality_postfix="POST")
    positive, _negative = character.assemble("SCENE")
    assert positive == "PRE ID SCENE POST"


def test_a_saved_wording_reaches_the_very_next_render(forge, tmp_path):
    first = forge.generate("SCENE", save=False)
    assert first.meta["prompt"].startswith("IDENTITY")   # no layer sets a prefix

    write(forge.wording.house, parse({"architectures": {"mock": {
        "quality_preamble": "MOCK-PRE", "base_negative": "MOCK-NEG"}}}))
    second = forge.generate("SCENE", save=False)
    assert second.meta["prompt"] == "MOCK-PRE IDENTITY SCENE"
    assert second.meta["negative"] == "MOCK-NEG"

    write(forge.wording.character, parse({"quality_postfix": "HER-POST"}))
    third = forge.generate("SCENE", save=False)
    assert third.meta["prompt"] == "MOCK-PRE IDENTITY SCENE HER-POST"


def test_scenery_leaves_the_person_guard_out_whatever_the_wording(forge, tmp_path):
    write(forge.wording.house, parse({"base_negative": "BASE",
                                        "character_negative": "PERSON"}))
    assert forge.scenery("a street", save=False).meta["negative"] == "BASE"
    assert forge.generate("SCENE", save=False).meta["negative"] == "BASE PERSON"


def test_build_forge_reads_the_house_file_under_data_dir(cfg, tmp_path):
    house = cfg.data_dir / HOUSE_FILE
    write(house, parse({"architectures": {"mock": {"quality_postfix": "HOUSE-POST"}}}))
    forge, _status = build_forge(cfg)
    assert forge.wording.house == house
    assert forge.generate("SCENE", save=False).meta["prompt"].endswith("HOUSE-POST")


def test_a_characters_own_file_sits_beside_her_appearance(cfg, tmp_path):
    appearance = tmp_path / "her" / "appearance.yaml"
    appearance.parent.mkdir()
    appearance.write_text("name: Her\nidentity: someone\n")
    cfg.selfie_character = str(appearance)
    sources = wording_for(cfg)
    assert sources.appearance == appearance
    assert sources.character == appearance.parent / CHARACTER_FILE


# ---- the studio's routes ----------------------------------------------------------

@pytest.fixture
def node(tmp_path):
    from starlette.testclient import TestClient

    from tests.conftest import FakeEmbedder
    from tests.support.cards import card_data, png_card, wrapper
    from yurios.characters import CharacterImporter, CharacterRegistry
    from yurios.world.config import Config
    from yurios.world.host import create_host_app

    cfg = Config(_env_file=None, data_dir=tmp_path / "data", selfie_backend="mock",
                 telegram_bot_token="", telegram_chat_id="")
    registry = CharacterRegistry(tmp_path / "data")
    CharacterImporter(registry, initialize_git=True).import_card(
        png_card(wrapper(card_data(), native=True)), character_id="subject")
    with TestClient(create_host_app(cfg, registry, embedder=FakeEmbedder())) as client:
        yield client, registry


def test_the_route_reports_her_camera_and_every_layer(node):
    client, _registry = node
    payload = client.get("/api/characters/subject/selfie-prompts").json()
    assert payload["camera"] == {"architecture": "mock", "model": ""}
    assert [layer["name"] for layer in payload["layers"]] == \
        ["shipped", "house", "her appearance", "her"]
    assert [layer["editable"] for layer in payload["layers"]] == [False, True, False, True]
    assert {f["key"] for f in payload["fields"]} >= {"quality_postfix", "base_negative"}
    assert any(a["key"] == "anima" for a in payload["architectures"])


def test_saving_her_scope_writes_her_file_and_beats_the_house(node):
    client, registry = node
    record = registry.require("subject")
    url = "/api/characters/subject/selfie-prompts"

    house = client.put(url, json={"scope": "house", "profile": {
        "architectures": {"mock": {"quality_postfix": "HOUSE"}}}})
    assert house.status_code == 200
    assert house.json()["effective"]["quality_postfix"]["value"] == "HOUSE"
    assert (registry.data_root / HOUSE_FILE).is_file()

    her = client.put(url, json={"scope": "her", "profile": {
        "default": {"quality_postfix": "HERS"}}})
    assert her.json()["effective"]["quality_postfix"] == \
        {"value": "HERS", "source": "her · every model"}
    assert record.paths.selfie_prompts.is_file()

    gone = client.delete(f"{url}?scope=her")
    assert gone.json()["effective"]["quality_postfix"]["value"] == "HOUSE"
    assert not record.paths.selfie_prompts.exists()


def test_the_route_refuses_a_bad_profile_or_scope(node):
    client, registry = node
    url = "/api/characters/subject/selfie-prompts"
    assert client.put(url, json={"scope": "her", "profile": {
        "default": {"identity": "no"}}}).status_code == 422
    assert client.put(url, json={"scope": "everyone", "profile": {}}).status_code == 400
    assert not registry.require("subject").paths.selfie_prompts.exists()
