"""The selfie template library (forge/templates.py) — named slots, seeded
rotation, and the free-form pass-through: an ask that names no library entry
is used verbatim, never refused (→ ch. 11, no enforcement posture; the
backend decides what renders, not the library). Offline, no backend involved.
"""
from __future__ import annotations

from yurios.forge.templates import SelfieBook

BOOK = SelfieBook(
    scenes={"window": "SCENE-window", "bed": "SCENE-bed"},
    framings={"close": "FRAMING-close"},
    lighting={"neon": "LIGHT-neon"},
    moods={"happy": "MOOD-happy"},
    wardrobe={"everyday": "WARDROBE-everyday", "signature": "WARDROBE-signature"},
)


def test_named_slots_compose_from_the_library():
    prompt, chosen, negative = BOOK.compose(scene="window", mood="happy",
                                            wardrobe="signature", framing="close",
                                            lighting="neon")
    assert "SCENE-window" in prompt and "MOOD-happy" in prompt
    assert "WARDROBE-signature" in prompt
    assert chosen == {"scene": "window", "framing": "close",
                      "wardrobe": "signature", "lighting": "neon", "mood": "happy"}
    assert negative == ""                        # plain string tiers carry none


def test_empty_slots_rotate_in_seeded():
    _, a, _ = BOOK.compose(seed=42)
    _, b, _ = BOOK.compose(seed=42)
    assert a == b                                 # a seed reproduces the shot
    assert set(a) == {"scene", "framing", "wardrobe", "lighting", "mood"}


def test_freeform_text_passes_through_verbatim():
    """The fix for the silent refusal: off-menu asks used to die as KeyError /
    tool-error. Now her own words ARE the fragment — exactly as she put them."""
    prompt, chosen, negative = BOOK.compose(
        scene="on a balcony above the rain, hair whipping in the wind",
        mood="a look she has no template for",
        wardrobe="something the library never imagined",
        framing="close", lighting="neon")
    assert "on a balcony above the rain, hair whipping in the wind" in prompt
    assert "a look she has no template for" in prompt
    assert "something the library never imagined" in prompt
    # named slots still come from the library beside the free-form ones
    assert "FRAMING-close" in prompt and "LIGHT-neon" in prompt
    # provenance records exactly what she asked, not a nearest match
    assert chosen["mood"] == "a look she has no template for"
    assert chosen["wardrobe"] == "something the library never imagined"
    assert negative == ""                        # free-form words speak for themselves


def test_freeform_still_works_when_a_table_is_empty():
    book = SelfieBook(scenes={}, framings={}, lighting={}, moods={}, wardrobe={})
    prompt, chosen, _ = book.compose(scene="anywhere at all", mood="anything")
    assert "anywhere at all" in prompt and "anything" in prompt
    # the wardrobe default ("everyday") rides the same pass-through when the
    # table has no such entry — the word itself, no crash, no refusal
    prompt2, chosen2, _ = book.compose(seed=1)
    assert chosen2 == {"wardrobe": "everyday"} and "everyday" in prompt2


def test_mapping_entries_carry_a_negative_and_pinning(tmp_path):
    """A tier can be a mapping {prompt, negative, pinned}: the negative is the
    mechanism that makes the tier render (a tag-model dresses a figure unless
    clothing is actively negated), and a pinned tier is a named ask only —
    the rotation never volunteers it."""
    import yaml
    (tmp_path / "book.yaml").write_text(yaml.safe_dump({
        "scenes": {"room": "SCENE-room"},
        "framings": {"mid": "FRAMING-mid"},
        "lighting": {"lamplit": "LIGHT-lamplit"},
        "moods": {"tender": "MOOD-tender"},
        "wardrobe": {
            "everyday": "WARDROBE-everyday",
            "bare": {"prompt": "WARDROBE-bare",
                     "negative": "NEG-clothing",
                     "pinned": True}},
    }))
    book = SelfieBook.load(tmp_path / "book.yaml")

    prompt, chosen, negative = book.compose(wardrobe="bare", seed=1)
    assert "WARDROBE-bare" in prompt and negative == "NEG-clothing"
    assert chosen["wardrobe"] == "bare"

    # named plain tiers contribute no negative…
    _, _, negative = book.compose(wardrobe="everyday", seed=1)
    assert negative == ""

    # …and no seed ever rotates the pinned tier in unprompted
    for seed in range(200):
        _, chosen, negative = book.compose(seed=seed)
        assert chosen.get("wardrobe") != "bare"
        assert negative == ""


def test_the_shipped_library_stays_in_the_everyday_register(tmp_path):
    """The repo ships only the everyday register — personal tiers arrive as an
    overlay file outside the repo (SELFIE_TEMPLATES_EXTRA), merged key-by-key
    with their negatives and pinning intact. What a tier renders stays the
    backend's call; the shipped file simply takes no position."""
    import yaml
    from pathlib import Path
    shipped = SelfieBook.load(Path(__file__).resolve().parents[1]
                              / "yurios/forge/templates/selfie.yaml")
    assert set(shipped.wardrobe) == {
        "signature", "everyday", "cozy", "dressy", "swim"}
    assert not shipped.negatives and not shipped.pinned
    assert not shipped.tool_hint               # the shipped description stands alone

    overlay = tmp_path / "extra.yaml"
    overlay.write_text(yaml.safe_dump({
        "tool_hint": "HINT from the overlay",
        "wardrobe": {
            "intimate": "WARDROBE-intimate",
            "bare": {"prompt": "WARDROBE-bare",
                     "negative": "NEG-clothing", "pinned": True}}}))
    book = SelfieBook.load(
        Path(__file__).resolve().parents[1] / "yurios/forge/templates/selfie.yaml",
        overlays=[overlay, tmp_path / "missing.yaml"])   # gaps skip quietly
    assert set(book.wardrobe) == {"signature", "everyday", "cozy", "dressy",
                                  "swim", "intimate", "bare"}
    assert book.tool_hint == "HINT from the overlay"     # her register, her words
    prompt, chosen, negative = book.compose(wardrobe="bare", scene="bed", seed=1)
    assert "WARDROBE-bare" in prompt and negative == "NEG-clothing"
    for seed in range(200):                    # pinned even when overlaid in
        assert book.compose(seed=seed)[1].get("wardrobe") != "bare"

    # an overlay without a hint never clobbers one already set
    other = tmp_path / "other.yaml"
    other.write_text(yaml.safe_dump({"moods": {"teasing": "MOOD-teasing"}}))
    again = SelfieBook.load(
        Path(__file__).resolve().parents[1] / "yurios/forge/templates/selfie.yaml",
        overlays=[overlay, other])
    assert again.tool_hint == "HINT from the overlay"
    assert "teasing" in again.moods
