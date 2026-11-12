"""Her own VRM body, and the desktop window floating her own body (SPEC §6.5, §6.8).

The check reads a GLB's header and JSON chunk and nothing else, so every model
here is a few hundred bytes of real GLB framing around a JSON document — enough
to be a VRM to `vrmbody`, and to three.js if it ever came to that.
"""
from __future__ import annotations

import io
import json
import struct

import pytest
from fastapi.testclient import TestClient

from yurios.characters import CharacterRegistry, vrmbody
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


def upload(client, character_id, data, name="aria.vrm"):
    return client.post(f"/api/characters/{character_id}/body",
                       files={"file": (name, data, "application/octet-stream")})


def test_without_a_body_of_her_own_she_wears_the_bundled_one(house):
    app, _ = house
    with TestClient(app) as client:
        state = client.get("/api/characters/yuri/body").json()
        missing = client.get("/api/characters/yuri/body.vrm")

    assert state["vrm"] is None
    assert state["vrm_url"].startswith("/models/avatar.vrm")
    assert state["desktop_body"] == "live2d"          # the house's, until she has her own
    assert missing.status_code == 404


def test_an_upload_becomes_her_body_and_her_backend(house):
    app, registry = house
    with TestClient(app) as client:
        controller = Controller()
        app.state.host.runtime("yuri").controller = controller
        response = upload(client, "yuri", vrm1("Aria"))
        state = client.get("/api/characters/yuri/body").json()
        served = client.get(state["vrm_url"])

    assert response.status_code == 200
    assert state["vrm"]["title"] == "Aria" and state["vrm"]["version"] == "1.0"
    assert state["vrm_url"].startswith("/api/characters/yuri/body.vrm?v=")
    # Choosing a 3D body is choosing VRM — the desktop window follows at once.
    assert registry.require("yuri").body.backend == "vrm"
    assert state["desktop_body"] == "vrm"
    assert served.content == vrm1("Aria")
    assert served.headers["cache-control"] == "no-cache"
    # …and an open room changes body without a reload.
    assert controller.loaded == [response.json()["vrm_url"]]


def test_a_body_is_hers_alone(house):
    app, _ = house
    with TestClient(app) as client:
        app.state.host.runtime("yuri").controller = Controller()
        upload(client, "yuri", vrm0())
        mika = client.get("/api/characters/mika/body").json()

    assert mika["vrm"] is None and mika["desktop_body"] == "live2d"


def test_a_file_that_is_not_a_vrm_is_refused_and_changes_nothing(house):
    app, registry = house
    with TestClient(app) as client:
        response = upload(client, "yuri", b"\x89PNG not a model", name="face.png")

    assert response.status_code == 400
    assert "not a VRM" in response.json()["detail"]
    assert registry.require("yuri").body.backend == ""
    assert not registry.require("yuri").paths.body_vrm.exists()


def test_removing_her_body_puts_the_bundled_one_back_on(house):
    app, _ = house
    with TestClient(app) as client:
        controller = Controller()
        app.state.host.runtime("yuri").controller = controller
        upload(client, "yuri", vrm0())
        state = client.delete("/api/characters/yuri/body").json()

    assert state["vrm"] is None
    assert controller.loaded[-1] == state["vrm_url"]
    assert state["vrm_url"].startswith("/models/avatar.vrm")


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


def test_a_body_changed_in_the_board_is_the_next_windows(house):
    """Read at open, not at boot: no restart between the save and the float."""
    app, _ = house
    with TestClient(app) as client:
        assert app.state.host.desktop_body("yuri") == "live2d"
        client.patch("/api/characters/yuri/profile", json={"body_backend": "vrm"})
        assert app.state.host.desktop_body("yuri") == "vrm"
