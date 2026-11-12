"""The body library, what each character wears, and the window floating it (SPEC §6.5, §6.8).

The VRM check reads a GLB's header and JSON chunk and nothing else, so every model
here is a few hundred bytes of real GLB framing around a JSON document — enough
to be a VRM to `vrmbody`, and to three.js if it ever came to that. A Live2D rig
is a zip of a model3.json and the files it names; the bytes inside are never read.
"""
from __future__ import annotations

import io
import json
import struct
import zipfile

import pytest
from fastapi.testclient import TestClient

from yurios.characters import CharacterRegistry, bodylib, vrmbody
from yurios.characters.bodylib import BodyLibrary
from yurios.world.config import Config
from yurios.world.host import create_host_app

from tests.test_desktop_windows import Spawner
from tests.test_host import fake_character_app, record


def glb(doc: dict, *, magic: bytes = b"glTF", version: int = 2, pad: bytes = b"",
        lie_about_length: int = 0) -> bytes:
    """A binary glTF: header, one JSON chunk, one BIN chunk."""
    text = json.dumps(doc).encode()
    text += b" " * (-len(text) % 4)
    binary = b"\0" * 16
    body = (struct.pack("<II", len(text), 0x4E4F534A) + text
            + struct.pack("<II", len(binary), 0x004E4942) + binary)
    length = 12 + len(body) + lie_about_length
    return magic + struct.pack("<II", version, length) + body + pad


def vrm0(title="Aria") -> bytes:
    return glb({"asset": {"version": "2.0"}, "extensionsUsed": ["VRM"],
                "extensions": {"VRM": {"meta": {"title": title}}}})


def vrm1(name="Aria") -> bytes:
    return glb({"asset": {"version": "2.0"}, "extensionsUsed": ["VRMC_vrm"],
                "extensions": {"VRMC_vrm": {"meta": {"name": name}}}})


def check(data: bytes):
    return vrmbody.inspect(io.BytesIO(data), len(data))


# --- what counts as a VRM ------------------------------------------------------


def test_both_vrm_generations_read_with_their_own_names():
    old, new = check(vrm0("Aria")), check(vrm1("Mika"))
    assert (old.version, old.title) == ("0.x", "Aria")
    assert (new.version, new.title) == ("1.0", "Mika")
    assert old.size == len(vrm0("Aria"))


@pytest.mark.parametrize("data, says", [
    (b"\x89PNG\r\n\x1a\n" + b"\0" * 40, "not a VRM"),
    (glb({"asset": {"version": "2.0"}}), "not a VRM"),            # a plain .glb
    (glb({"extensions": {"VRM": {}}}, version=1), "version 1"),
    (glb({"extensions": {"VRM": {}}}, lie_about_length=64), "truncated"),
    (glb({"extensions": {"VRM": {}}}, pad=b"\0" * 8), "truncated"),
    (b"glTF" + struct.pack("<II", 2, 20) + struct.pack("<II", 4, 0x4E4F534A), "JSON"),
])
def test_anything_else_is_refused_with_the_reason(data, says):
    with pytest.raises(ValueError, match=says):
        check(data)


def test_a_refused_upload_leaves_her_old_body_alone(tmp_path):
    path = tmp_path / "body" / "body.vrm"
    vrmbody.store(io.BytesIO(vrm0("First")), path)

    with pytest.raises(ValueError):
        vrmbody.store(io.BytesIO(b"not a model at all, just words"), path)

    assert vrmbody.read_info(path).title == "First"
    assert [p.name for p in path.parent.iterdir()] == ["body.vrm"]   # no temp left behind


def test_a_body_over_the_ceiling_is_refused_before_it_is_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(vrmbody, "MAX_VRM_BYTES", 64)
    path = tmp_path / "body.vrm"
    with pytest.raises(ValueError, match="over the"):
        vrmbody.store(io.BytesIO(vrm0()), path)
    assert not path.exists()


def test_the_bundled_body_is_a_vrm_by_this_check():
    from yurios.world.main import WEB_DIR
    info = vrmbody.read_info(WEB_DIR / "models" / "avatar.vrm")
    assert info is not None and info.version == "0.x"


# --- the library ----------------------------------------------------------------


def rig(name="Mika", *, moc=True, textures=("Mika.2048/texture_00.png",),
        folder="Mika/runtime/", extra=None) -> bytes:
    """A zipped Cubism 3 rig: the model3.json and (unless told otherwise) what it names."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        model = {"Version": 3, "FileReferences": {"Moc": f"{name}.moc3",
                                                   "Textures": list(textures)}}
        archive.writestr(f"{folder}{name}.model3.json", json.dumps(model))
        if moc:
            archive.writestr(f"{folder}{name}.moc3", b"MOC3")
        for texture in textures:
            archive.writestr(f"{folder}{texture}", b"PNG")
        for path, data in (extra or {}).items():
            archive.writestr(path, data)
    return buffer.getvalue()


@pytest.fixture
def library(tmp_path):
    from yurios.world.main import WEB_DIR
    return BodyLibrary(tmp_path / "bodies", live2d_vendor_dir=tmp_path / "no-vendor",
                       vrm_bundled=WEB_DIR / "models" / "avatar.vrm")


def test_the_bundled_body_leads_the_list_and_cannot_be_removed(library):
    [bundled] = library.entries("vrm")
    assert (bundled.id, bundled.builtin, bundled.url) == ("bundled", True, bodylib.BUNDLED_VRM_URL)
    with pytest.raises(ValueError, match="ships with YuriOS"):
        library.remove("vrm", "bundled")


def test_an_added_vrm_is_listed_served_and_removable(library):
    entry = library.add_vrm(io.BytesIO(vrm1("Mika")), "mika.vrm")
    assert (entry.title, entry.detail, entry.builtin) == ("Mika", "VRM 1.0", False)
    assert entry.url.startswith(f"/api/bodies/vrm/{entry.id}.vrm?v=")
    assert library.vrm_file(entry.id).read_bytes() == vrm1("Mika")
    assert [e.id for e in library.entries("vrm")] == ["bundled", entry.id]

    library.remove("vrm", entry.id)
    assert library.get("vrm", entry.id) is None
    assert library.resolve("vrm", entry.id).id == "bundled"     # whoever wore it: the default


def test_a_refused_vrm_leaves_nothing_in_the_library(library):
    with pytest.raises(ValueError, match="not a VRM"):
        library.add_vrm(io.BytesIO(b"\x89PNG not a model"), "face.png")
    assert [e.id for e in library.entries("vrm")] == ["bundled"]
    assert not any(p.name.startswith(".") for p in (library.root / "vrm").iterdir())


def test_a_zipped_rig_unpacks_into_the_library_and_serves_its_files(library):
    entry = library.add_live2d(io.BytesIO(rig("Mika")), "mika.zip")
    assert (entry.title, entry.builtin) == ("Mika", False)
    assert entry.url == f"/api/bodies/live2d/{entry.id}/files/Mika/runtime/Mika.model3.json"
    assert library.live2d_file(entry.id, "Mika/runtime/Mika.moc3").read_bytes() == b"MOC3"
    # …and nothing outside the rig's own folder, however the path is spelled.
    assert library.live2d_file(entry.id, "../../vrm/x.json") is None
    assert library.resolve("live2d", "").id == entry.id      # no vendor rig: the first upload


@pytest.mark.parametrize("data, says", [
    (b"PK not really", "not a zip"),
    (rig(moc=False), "not in the zip"),
    (rig(extra={"../escape.txt": b"x"}), "outside itself"),
    (rig(folder="", extra={"/abs.txt": b"x"}), "outside itself"),
])
def test_a_rig_that_is_not_one_is_refused_with_the_reason(library, data, says):
    with pytest.raises(ValueError, match=says):
        library.add_live2d(io.BytesIO(data), "rig.zip")
    assert library.entries("live2d") == []
    assert not any((library.root / "live2d").iterdir())        # no staging left behind


def test_a_cubism_2_rig_is_told_what_it_is(library):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("old/old.model.json", "{}")
    with pytest.raises(ValueError, match="Cubism 2"):
        library.add_live2d(io.BytesIO(buffer.getvalue()), "old.zip")


def test_a_zip_bigger_than_the_ceiling_is_refused(library, monkeypatch):
    monkeypatch.setattr(bodylib, "MAX_LIVE2D_ZIP_BYTES", 64)
    with pytest.raises(ValueError, match="over the"):
        library.add_live2d(io.BytesIO(rig()), "rig.zip")


def test_installed_vendor_rigs_are_builtin(tmp_path):
    from yurios.desktop.avatar_models import MODELS
    vendor = tmp_path / "live2d"
    (vendor / MODELS["hiyori"]).parent.mkdir(parents=True)
    (vendor / MODELS["hiyori"]).write_text("{}")
    library = BodyLibrary(tmp_path / "bodies", live2d_vendor_dir=vendor,
                          vrm_bundled=tmp_path / "none.vrm")
    [hiyori] = library.entries("live2d")
    assert (hiyori.id, hiyori.builtin) == ("hiyori", True)
    assert hiyori.url == "/live2d/" + MODELS["hiyori"]
    assert library.resolve("live2d", "nonsense").id == "hiyori"


# --- the host routes ------------------------------------------------------------


class Controller:
    def __init__(self):
        self.loaded: list[str] = []

    def load_model(self, url):
        self.loaded.append(url)


@pytest.fixture
def house(tmp_path, monkeypatch):
    monkeypatch.setattr("yurios.world.host.hosting.create_app", fake_character_app)
    registry = CharacterRegistry(tmp_path)
    registry.add(record(tmp_path, "yuri"))
    registry.add(record(tmp_path, "mika"))
    app = create_host_app(Config(data_dir=tmp_path, tray_enabled=False,
                                 desktop_body="live2d"), registry)
    return app, registry


def add(client, kind, data, name):
    return client.post(f"/api/bodies/{kind}",
                       files={"file": (name, data, "application/octet-stream")})


def test_without_a_choice_she_wears_the_defaults(house):
    app, _ = house
    with TestClient(app) as client:
        state = client.get("/api/characters/yuri/body").json()

    assert state["vrm"]["id"] == "bundled"
    assert state["vrm_url"] == bodylib.BUNDLED_VRM_URL
    assert state["desktop_body"] == "live2d"          # the house's, until she has her own


def test_a_body_is_added_once_and_worn_by_whoever_chooses_it(house):
    app, registry = house
    with TestClient(app) as client:
        controller = Controller()
        app.state.host.runtime("yuri").controller = controller
        added = add(client, "vrm", vrm1("Aria"), "aria.vrm")
        body_id = added.json()["body"]["id"]
        state = client.put("/api/characters/yuri/body",
                           json={"vrm": body_id, "backend": "vrm"}).json()
        served = client.get(state["vrm_url"])
        listing = client.get("/api/bodies").json()
        mika = client.get("/api/characters/mika/body").json()

    assert added.status_code == 200
    assert registry.require("yuri").body.vrm == body_id
    assert state["vrm"]["title"] == "Aria" and state["desktop_body"] == "vrm"
    assert served.content == vrm1("Aria")
    assert served.headers["cache-control"] == "no-cache"
    # …an open room changes body without a reload,
    assert controller.loaded == [state["vrm_url"]]
    # …the library says who wears what, and the other character is untouched.
    worn = {e["id"]: [w["id"] for w in e["worn_by"]] for e in listing["vrm"]}
    assert worn == {"bundled": ["mika"], body_id: ["yuri"]}
    assert mika["vrm"]["id"] == "bundled"


def test_a_file_that_is_not_a_vrm_is_refused_and_changes_nothing(house):
    app, _ = house
    with TestClient(app) as client:
        response = add(client, "vrm", b"\x89PNG not a model", "face.png")
        listing = client.get("/api/bodies").json()

    assert response.status_code == 400
    assert "not a VRM" in response.json()["detail"]
    assert [e["id"] for e in listing["vrm"]] == ["bundled"]


def test_an_unknown_body_cannot_be_chosen(house):
    app, registry = house
    with TestClient(app) as client:
        response = client.put("/api/characters/yuri/body", json={"vrm": "nobody-123456"})

    assert response.status_code == 400
    assert registry.require("yuri").body.vrm == ""


def test_removing_a_body_puts_its_wearers_back_in_the_default(house):
    app, registry = house
    with TestClient(app) as client:
        controller = Controller()
        app.state.host.runtime("yuri").controller = controller
        body_id = add(client, "vrm", vrm0(), "a.vrm").json()["body"]["id"]
        client.put("/api/characters/yuri/body", json={"vrm": body_id})
        listing = client.delete(f"/api/bodies/vrm/{body_id}").json()

    assert [e["id"] for e in listing["vrm"]] == ["bundled"]
    assert registry.require("yuri").body.vrm == ""
    assert controller.loaded[-1] == bodylib.BUNDLED_VRM_URL


def test_the_live2d_room_asks_the_host_which_rig(house):
    """Answered from her record at request time — no rebuild between the choice
    and the room (SPEC §6.6, §31.4)."""
    app, _ = house
    with TestClient(app) as client:
        body_id = add(client, "live2d", rig("Mika"), "mika.zip").json()["body"]["id"]
        client.put("/api/characters/yuri/body", json={"live2d": body_id})
        config = client.get("/api/characters/yuri/config").json()
        model = client.get(config["avatar_model_url"])

    assert config["avatar_model"] == body_id
    assert json.loads(model.content)["FileReferences"]["Moc"] == "Mika.moc3"


def test_a_body_from_the_old_layout_is_filed_into_the_library(tmp_path, monkeypatch):
    """Bodies used to live in each character's folder; the first start files them."""
    monkeypatch.setattr("yurios.world.host.hosting.create_app", fake_character_app)
    registry = CharacterRegistry(tmp_path)
    yuri = record(tmp_path, "yuri")
    registry.add(yuri)
    yuri.paths.body_vrm.parent.mkdir(parents=True)
    yuri.paths.body_vrm.write_bytes(vrm0("Old"))

    app = create_host_app(Config(data_dir=tmp_path, tray_enabled=False), registry)

    chosen = registry.require("yuri").body.vrm
    assert chosen and app.state.bodies.get("vrm", chosen).title == "Old"
    assert not yuri.paths.body_vrm.parent.exists()


# --- the desktop window floats her own body (SPEC §6.5) -------------------------


def test_each_window_floats_the_body_she_was_given(house):
    """The host's own windows (not a stand-in): her backend, else the house's."""
    app, registry = house
    yuri = registry.require("yuri")
    yuri.body.backend = "vrm"
    registry.upsert(yuri)
    spawn = Spawner()
    windows = app.state.desktop_windows
    windows._spawn, windows._check = spawn, (lambda: None)

    with TestClient(app) as client:
        client.post("/api/characters/yuri/desktop")
        client.post("/api/characters/mika/desktop")

    argv = {proc.argv[4]: proc.argv for proc in spawn.procs}
    assert argv["yuri"][-2:] == ["--body", "vrm"]
    assert argv["mika"][-2:] == ["--body", "live2d"]      # no body of her own: the house's


def test_a_body_changed_in_the_studio_is_the_next_windows(house):
    """Read at open, not at boot: no restart between the save and the float."""
    app, _ = house
    with TestClient(app) as client:
        assert app.state.host.desktop_body("yuri") == "live2d"
        client.put("/api/characters/yuri/body", json={"backend": "vrm"})
        assert app.state.host.desktop_body("yuri") == "vrm"


def test_the_live2d_preview_page_may_be_framed_by_this_house_only(house):
    """The studio frames it so Pixi's `unsafe-eval` stays off the studio's own
    policy (SPEC §6.8): framed by this origin, by nobody else."""
    app, _ = house
    with TestClient(app) as client:
        preview = client.get("/live2d/preview/")
        studio = client.get("/studio/")

    policy = preview.headers["content-security-policy"]
    assert preview.status_code == 200
    assert "frame-ancestors 'self'" in policy and "'unsafe-eval'" in policy
    assert preview.headers["x-frame-options"] == "SAMEORIGIN"
    if studio.status_code == 200:                    # only when the web build exists
        assert "'unsafe-eval'" not in studio.headers["content-security-policy"]
        assert "frame-ancestors 'none'" in studio.headers["content-security-policy"]
