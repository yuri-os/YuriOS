"""Her own selfie library — the per-character override (SPEC §7.6).

Three things have to hold together or the studio is lying about what her camera
knows: the rows survive a trip through the editor's model and back to yaml, the
forge composes from *her* file instead of the shipped one, and the `take_selfie`
description offers her rows rather than ours.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from yurios.characters import selfiebook
from yurios.forge.templates import SelfieBook
from yurios.world.config import Config
from yurios.world.selfies import SHIPPED_BOOK, book_path, build_forge


def test_the_editor_model_round_trips_a_library(tmp_path):
    """Rows in, yaml the loader accepts out. The mapping form is kept only for
    the entries that need it — a file of one-key mappings is unreadable, and
    both shapes mean the same thing to `SelfieBook.load`."""
    book = selfiebook.normalise({"tool_hint": "hers, in her words", "slots": {
        "scenes": [{"key": "garden", "prompt": "SCENE-garden"},
                   {"key": "", "prompt": "dropped — no key"},
                   {"key": "attic", "prompt": ""}],           # …and no prompt
        "wardrobe": [{"key": "everyday", "prompt": "WARDROBE-everyday"},
                     {"key": "gala", "prompt": "WARDROBE-gala",
                      "negative": "NEG-jeans", "pinned": True}],
    }})
    assert [row["key"] for row in book["slots"]["scenes"]] == ["garden"]

    path = selfiebook.write(tmp_path / "selfie.yaml", book, "Lumina")
    data = yaml.safe_load(path.read_text())
    assert data["scenes"]["garden"] == "SCENE-garden"         # plain stays plain
    assert data["wardrobe"]["gala"] == {"prompt": "WARDROBE-gala",
                                        "negative": "NEG-jeans", "pinned": True}
    assert selfiebook.read(path) == book                      # …and back again

    loaded = SelfieBook.load(path)
    assert loaded.tool_hint == "hers, in her words"
    _prompt, _chosen, negative = loaded.compose(wardrobe="gala")
    assert negative == "NEG-jeans"
    for seed in range(100):                                   # pinned survives
        assert loaded.compose(seed=seed, rotate=True)[1].get("wardrobe") != "gala"


def test_her_file_replaces_the_shipped_library_rather_than_merging(tmp_path):
    """The whole point of a book of her own. An overlay could only ever *add* to
    the shipped library, and the shipped library is one character's world down
    to the tail in half its scenes — a different character has to be able to
    take those rows away, not just bury them."""
    hers = tmp_path / "selfie.yaml"
    selfiebook.write(hers, selfiebook.normalise({"slots": {
        "scenes": [{"key": "garden", "prompt": "SCENE-garden"}],
        "wardrobe": [{"key": "everyday", "prompt": "WARDROBE-everyday"}],
    }}), "Lumina")

    assert book_path(str(hers)) == hers
    assert book_path(str(tmp_path / "nothing.yaml")) == SHIPPED_BOOK  # absence is fine
    assert book_path("") == SHIPPED_BOOK

    cfg = Config(_env_file=None, selfie_backend="mock",
                 selfie_dir=tmp_path / "out", selfie_templates=str(hers))
    forge, _status = build_forge(cfg)
    assert set(forge.book.scenes) == {"garden"}               # ours are gone
    assert "sanctuary" not in forge.book.scenes


def test_the_env_overlay_still_layers_over_her_own_book(tmp_path):
    """A house-wide register (SELFIE_TEMPLATES_EXTRA) keeps working for a
    character who has forked the library — it merges over whichever base won,
    not over the shipped one specifically."""
    hers = tmp_path / "selfie.yaml"
    selfiebook.write(hers, selfiebook.normalise({"slots": {
        "scenes": [{"key": "garden", "prompt": "SCENE-garden"}]}}), "Lumina")
    overlay = tmp_path / "extra.yaml"
    overlay.write_text(yaml.safe_dump({"moods": {"teasing": "MOOD-teasing"}}))

    cfg = Config(_env_file=None, selfie_backend="mock", selfie_dir=tmp_path / "out",
                 selfie_templates=str(hers), selfie_templates_extra=str(overlay))
    forge, _status = build_forge(cfg)
    assert set(forge.book.scenes) == {"garden"}
    assert "teasing" in forge.book.moods


def test_the_shipped_library_is_what_she_starts_from():
    """The studio shows ours until she has one of her own, so "reset" and "first
    open" are the same read — and it must be the real shipped file, not an
    approximation of it."""
    shipped = selfiebook.shipped()
    assert [row["key"] for row in shipped["slots"]["wardrobe"]] == [
        "signature", "everyday", "cozy", "dressy", "swim"]
    assert shipped["tool_hint"] == ""
    assert selfiebook.SHIPPED == Path(SHIPPED_BOOK)
