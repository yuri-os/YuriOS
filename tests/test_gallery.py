"""Her gallery (SPEC §7.6) — the shelf as a paged reader, and the score sidecar.

The reader half runs on files alone: a directory of PNGs and the forge's own
`generations.jsonl` beside them, which is exactly what `world/selfies.py`
leaves behind. The route half runs the same reader through the app, because the
one thing a panel must never do is disagree with the files.
"""
from __future__ import annotations

import json

import pytest

from yurios.world import gallery

pytest.importorskip("fastapi")
from starlette.testclient import TestClient                   # noqa: E402

from yurios.desktop.voice.backends.fakes import FakeBrain     # noqa: E402
from yurios.world.main import create_app                      # noqa: E402


def shelve(directory, name: str, **meta) -> dict:
    """One saved render: the PNG, its sidecar, and the appended ledger line —
    the three things `_write_provenance` leaves on disk."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_bytes(b"\x89PNG\r\n\x1a\n" + name.encode())
    row = {"image": name, "backend": "mock", "model": "mock/sdxl",
           "created_at": "2026-08-20T21:00:00", "seed": 7,
           "prompt": "a prompt", "negative": "", **meta}
    (directory / name).with_suffix(".json").write_text(
        json.dumps(row) + "\n", encoding="utf-8")
    with (directory / gallery.LEDGER).open("a", encoding="utf-8") as ledger:
        ledger.write(json.dumps(row) + "\n")
    return row


@pytest.fixture
def shelf(tmp_path):
    directory = tmp_path / "selfies"
    for n in range(5):
        shelve(directory, f"{1000 + n}-shot{n}.png", seed=n,
               template={"scene": "window", "mood": "quiet"})
    return directory


# ---- the reader --------------------------------------------------------------

def test_an_empty_shelf_is_an_empty_page_not_an_error(tmp_path):
    page = gallery.page(tmp_path / "never-rendered")
    assert page == {"items": [], "page": 0, "limit": gallery.DEFAULT_LIMIT,
                    "has_more": False, "total": 0, "rated": 0}


def test_pages_are_newest_first_and_say_when_there_is_more(shelf):
    first = gallery.page(shelf, limit=2)
    assert [item["name"] for item in first["items"]] == \
        ["1004-shot4.png", "1003-shot3.png"]
    assert first["has_more"] and first["total"] == 5

    second = gallery.page(shelf, page=1, limit=2)
    assert [item["name"] for item in second["items"]] == \
        ["1002-shot2.png", "1001-shot1.png"]
    assert second["has_more"]

    last = gallery.page(shelf, page=2, limit=2)
    assert [item["name"] for item in last["items"]] == ["1000-shot0.png"]
    assert not last["has_more"]
    assert gallery.page(shelf, page=3, limit=2)["items"] == []


def test_a_tile_carries_the_provenance_and_her_words_for_the_shot(shelf):
    shelve(shelf, "2000-picture.png", template={"look": "the rain, from above"})
    tile = gallery.page(shelf, limit=1)["items"][0]
    assert tile["name"] == "2000-picture.png"
    assert tile["url"] == "/selfies/2000-picture.png"      # the page scopes it
    assert tile["caption"] == "the rain, from above"       # her words win
    assert tile["backend"] == "mock" and tile["seed"] == 7
    assert tile["prompt"] == "a prompt" and tile["bytes"] > 0
    assert tile["score"] is None and tile["rated_at"] is None
    # a shot she never described falls back to the slots she named, in the
    # order selfies.py announces them
    assert gallery.page(shelf, page=1, limit=1)["items"][0]["caption"] == \
        "window, quiet"


def test_a_deleted_image_leaves_no_hole_in_the_page(shelf):
    (shelf / "1003-shot3.png").unlink()
    page = gallery.page(shelf, limit=2)
    # filtered inside the pager: still a full page, and the count follows disk
    assert [item["name"] for item in page["items"]] == \
        ["1004-shot4.png", "1002-shot2.png"]
    assert page["total"] == 4


def test_the_limit_is_bounded_and_the_page_never_goes_negative(shelf):
    assert gallery.page(shelf, limit=10_000)["limit"] == gallery.MAX_LIMIT
    assert gallery.page(shelf, limit=0)["limit"] == 1
    assert gallery.page(shelf, page=-3)["page"] == 0


# ---- the score sidecar -------------------------------------------------------

def test_a_score_is_appended_and_the_last_line_wins(shelf):
    gallery.rate(shelf, "1000-shot0.png", 4)
    gallery.rate(shelf, "1000-shot0.png", 9)
    assert gallery.ratings(shelf)["1000-shot0.png"]["score"] == 9
    # provenance is untouched: opinion never edits the record of the render
    ledger = (shelf / gallery.LEDGER).read_text(encoding="utf-8")
    assert "score" not in ledger
    assert len((shelf / gallery.RATINGS).read_text(
        encoding="utf-8").strip().splitlines()) == 2


def test_null_takes_a_rating_back(shelf):
    gallery.rate(shelf, "1001-shot1.png", 8)
    gallery.rate(shelf, "1001-shot1.png", None)
    assert gallery.ratings(shelf) == {}
    assert gallery.page(shelf, limit=5)["rated"] == 0


def test_scores_land_on_the_tiles_they_belong_to(shelf):
    gallery.rate(shelf, "1004-shot4.png", 10)
    page = gallery.page(shelf, limit=2)
    assert page["items"][0]["score"] == 10
    assert page["items"][0]["rated_at"]
    assert page["items"][1]["score"] is None
    assert page["rated"] == 1


@pytest.mark.parametrize("score", [0, 11, -2])
def test_a_score_outside_one_to_ten_is_refused(shelf, score):
    with pytest.raises(ValueError):
        gallery.rate(shelf, "1000-shot0.png", score)
    assert not (shelf / gallery.RATINGS).exists()


@pytest.mark.parametrize("name", ["", "nope.png", "../secret.png",
                                  "sub/1000-shot0.png"])
def test_only_a_file_on_the_flat_shelf_can_be_rated(shelf, name):
    (shelf.parent / "secret.png").write_bytes(b"not hers")
    with pytest.raises(gallery.UnknownShot):
        gallery.rate(shelf, name, 5)


def test_a_hand_broken_sidecar_line_unrates_rather_than_crashing(shelf):
    gallery.rate(shelf, "1002-shot2.png", 6)
    with (shelf / gallery.RATINGS).open("a", encoding="utf-8") as sidecar:
        sidecar.write('{"image": "1002-shot2.png", "score": "great"}\n')
        sidecar.write("{not json at all\n")
    assert gallery.ratings(shelf) == {}


# ---- the routes, over the real app -------------------------------------------

@pytest.fixture
def client(cfg):
    app = create_app(cfg.model_copy(update={"tools_backend": "off"}),
                     brain=FakeBrain())
    with TestClient(app) as served:
        served.app = app
        yield served


def test_the_route_pages_the_shelf_and_the_bytes_still_come_from_selfies(client, cfg):
    for n in range(3):
        shelve(cfg.selfie_dir, f"{1000 + n}-shot{n}.png")
    body = client.get("/api/gallery", params={"limit": 2}).json()
    assert [item["name"] for item in body["items"]] == \
        ["1002-shot2.png", "1001-shot1.png"]
    assert body["has_more"] and body["total"] == 3
    # the tile's url is the transcript's own selfie route, and it serves
    picture = client.get(body["items"][0]["url"])
    assert picture.status_code == 200 and picture.content.startswith(b"\x89PNG")


def test_rating_over_the_route_lands_on_the_next_page_and_the_bus(
        client, cfg, monkeypatch):
    shelve(cfg.selfie_dir, "1000-shot0.png")
    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(client.app.state.rt.hub, "publish",
                        lambda type_, payload: published.append((type_, payload)))

    saved = client.post("/api/gallery/rate",
                        json={"name": "1000-shot0.png", "score": 8})
    assert saved.status_code == 200
    assert saved.json()["rating"]["score"] == 8
    # the other open room hears about it (world/hub.py), the way a desk write does
    assert published == [("gallery", {"action": "rate", **saved.json()["rating"]})]
    assert client.get("/api/gallery").json()["items"][0]["score"] == 8

    cleared = client.post("/api/gallery/rate",
                          json={"name": "1000-shot0.png", "score": None})
    assert cleared.status_code == 200
    assert client.get("/api/gallery").json()["items"][0]["score"] is None


def test_the_route_refuses_a_score_for_a_picture_that_is_not_there(client, cfg):
    cfg.selfie_dir.mkdir(parents=True, exist_ok=True)
    gone = client.post("/api/gallery/rate", json={"name": "ghost.png", "score": 5})
    assert gone.status_code == 404


@pytest.mark.parametrize("score", [0, 11, "9", True])
def test_the_route_refuses_a_score_that_is_not_one_to_ten(client, cfg, score):
    shelve(cfg.selfie_dir, "1000-shot0.png")
    refused = client.post("/api/gallery/rate",
                          json={"name": "1000-shot0.png", "score": score})
    assert refused.status_code == 422
    assert not (cfg.selfie_dir / gallery.RATINGS).exists()
