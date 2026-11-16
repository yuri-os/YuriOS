"""Snapshots, backups and restores of a whole character (SPEC §29.8)."""
from __future__ import annotations

import io
import json
import os
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from yurios.characters import CharacterRegistry
from yurios.characters import backup
from yurios.characters.bodylib import BodyLibrary
from yurios.security import HTTPBodyLimitMiddleware
from yurios.world.config import Config
from yurios.world.host import create_host_app

from tests.test_bodies import rig, vrm1
from tests.test_host import fake_character_app, record


def lived_in(tmp_path: Path, character_id: str = "yuri", *, enabled: bool = False):
    """A character with the shapes a real one has: a git Vault with an empty
    refs folder, a selfie, a trace, and the derived cache a backup leaves out."""
    item = record(tmp_path, character_id, enabled=enabled)
    root = item.paths.root
    (root / "vault" / ".git" / "refs" / "tags").mkdir(parents=True)
    (root / "vault" / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (root / "vault" / "memory" / "episodic").mkdir(parents=True)
    (root / "vault" / "memory" / "episodic" / "2026-11-01.md").write_text("### 10:00  we met\n")
    (root / "selfies").mkdir()
    (root / "selfies" / "one.png").write_bytes(b"\x89PNG not really")
    (root / "corpus").mkdir()
    (root / "traces").mkdir()
    (root / "traces" / "ticks.jsonl").write_text('{"tick": 1}\n')
    (root / "cache" / "thumbs").mkdir(parents=True)
    (root / "cache" / "thumbs" / "one.webp").write_bytes(b"derived")
    item.models.chat = "lmstudio/her-own-model"
    item.loops.dream = False
    return item


def library_at(tmp_path: Path) -> BodyLibrary:
    from yurios.world.main import WEB_DIR
    return BodyLibrary(tmp_path / "bodies", live2d_vendor_dir=tmp_path / "no-vendor",
                       vrm_bundled=WEB_DIR / "models" / "avatar.vrm")


# ---- the format --------------------------------------------------------------


def test_a_backup_carries_her_tree_and_her_row_but_not_the_cache(tmp_path):
    registry = CharacterRegistry(tmp_path)
    item = lived_in(tmp_path)
    registry.add(item)
    dest = tmp_path / "out" / "yuri.zip"

    manifest = backup.write_backup(item, registry.data_root, dest)

    assert manifest.character_id == "yuri" and manifest.files >= 4
    names = set(zipfile.ZipFile(dest).namelist())
    assert "character/vault/memory/episodic/2026-11-01.md" in names
    assert "character/vault/.git/refs/tags/" in names        # empty, still there
    assert "character/selfies/one.png" in names
    assert not any(name.startswith("character/cache") for name in names)
    read = backup.read_manifest(dest)
    assert read.record["models"]["chat"] == "lmstudio/her-own-model"
    assert read.record["loops"]["dream"] is False


def test_a_link_in_her_tree_is_not_followed(tmp_path):
    registry = CharacterRegistry(tmp_path)
    item = lived_in(tmp_path)
    registry.add(item)
    secret = tmp_path / "outside.txt"
    secret.write_text("not hers")
    os.symlink(secret, item.paths.root / "vault" / "link.txt")
    dest = tmp_path / "yuri.zip"

    backup.write_backup(item, registry.data_root, dest)

    assert "character/vault/link.txt" not in zipfile.ZipFile(dest).namelist()


def test_a_restore_lands_as_a_new_character_with_her_bindings(tmp_path):
    registry = CharacterRegistry(tmp_path)
    item = lived_in(tmp_path)
    registry.add(item)
    dest = tmp_path / "yuri.zip"
    backup.write_backup(item, registry.data_root, dest)

    staged = backup.stage_restore(dest, registry, character_id="yuri_v2", trusted=True)
    assert registry.get("yuri_v2") is None, "nothing is visible before install"
    staged.install(registry)

    restored = registry.require("yuri_v2")
    root = tmp_path / "characters" / "yuri_v2"
    assert restored.paths.root == root.resolve()
    assert restored.models.chat == "lmstudio/her-own-model"
    assert restored.lifecycle.review_required is False
    assert (root / "vault" / "memory" / "episodic" / "2026-11-01.md").read_text() == \
        "### 10:00  we met\n"
    assert (root / "vault" / ".git" / "refs" / "tags").is_dir()
    assert not list((tmp_path / "characters").glob(".yuri_v2.restore.*"))


def test_a_backup_from_outside_lands_under_review(tmp_path):
    registry = CharacterRegistry(tmp_path)
    item = lived_in(tmp_path, enabled=True)
    registry.add(item)
    dest = tmp_path / "yuri.zip"
    backup.write_backup(item, registry.data_root, dest)

    staged = backup.stage_restore(dest, registry, character_id="other", trusted=False)

    assert staged.record.lifecycle.review_required is True
    assert staged.record.lifecycle.enabled is False
    assert staged.record.lifecycle.autostart is False
    staged.discard()


def _forged(path: Path, members: dict[str, bytes], *, version=backup.FORMAT_VERSION,
            symlink: str | None = None) -> Path:
    manifest = {"format": backup.FORMAT, "version": version,
                "character": {"id": "eve", "name": "Eve"},
                "record": {"id": "eve", "display": {"name": "Eve"},
                           "paths": {"root": "characters/eve"}}}
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(backup.MANIFEST_NAME, json.dumps(manifest))
        for name, data in members.items():
            archive.writestr(name, data)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (0o120777 << 16)
            archive.writestr(info, "/etc/passwd")
    return path


@pytest.mark.parametrize("members, symlink, says", [
    ({"character/../../escape.txt": b"x"}, None, "unsafe name"),
    ({"character//etc/x": b"x"}, None, "unsafe name"),
    ({"elsewhere/file.txt": b"x"}, None, "does not"),
    ({}, "character/vault/link", "symbolic link"),
])
def test_a_forged_backup_is_refused_and_leaves_nothing(tmp_path, members, symlink, says):
    registry = CharacterRegistry(tmp_path)
    forged = _forged(tmp_path / "eve.zip", members, symlink=symlink)

    with pytest.raises(backup.BackupError, match=says):
        backup.stage_restore(forged, registry, character_id="eve", trusted=False)

    assert not (tmp_path / "escape.txt").exists()
    assert list((tmp_path / "characters").iterdir()) == []


def test_a_backup_that_expands_past_its_budget_is_stopped_there(tmp_path):
    registry = CharacterRegistry(tmp_path)
    forged = _forged(tmp_path / "eve.zip", {"character/big.bin": b"\0" * 4096})
    tight = backup.BackupLimits(expanded_bytes=1024)

    with pytest.raises(backup.BackupError, match="expands past"):
        backup.stage_restore(forged, registry, character_id="eve", trusted=False,
                             limits=tight)
    assert list((tmp_path / "characters").iterdir()) == []


@pytest.mark.parametrize("make, says", [
    (lambda p: p.write_bytes(b"not a zip"), "not a zip"),
    (lambda p: _forged(p, {}, version=99), "version 99"),
    (lambda p: zipfile.ZipFile(p, "w").close(), "no manifest"),
])
def test_what_is_not_a_backup_this_build_restores_says_so(tmp_path, make, says):
    path = tmp_path / "file.zip"
    make(path)
    with pytest.raises(backup.BackupError, match=says):
        backup.read_manifest(path)


def test_her_uploaded_bodies_travel_with_her_to_another_node(tmp_path):
    here, there = tmp_path / "here", tmp_path / "there"
    registry = CharacterRegistry(here)
    library = library_at(here)
    vrm = library.add_vrm(io.BytesIO(vrm1("Aria")), "aria.vrm")
    live2d = library.add_live2d(io.BytesIO(rig("Mika")), "mika.zip")
    item = lived_in(here)
    item.body.vrm, item.body.model = vrm.id, live2d.id
    registry.add(item)
    dest = tmp_path / "yuri.zip"
    manifest = backup.write_backup(item, registry.data_root, dest, library=library)
    assert {b.kind for b in manifest.bodies} == {"vrm", "live2d"}

    elsewhere = CharacterRegistry(there)
    other_library = library_at(there)
    staged = backup.stage_restore(dest, elsewhere, character_id="yuri", trusted=False,
                                  library=other_library)
    staged.install(elsewhere)

    restored = elsewhere.require("yuri")
    worn_vrm = other_library.get("vrm", restored.body.vrm)
    worn_rig = other_library.get("live2d", restored.body.model)
    assert worn_vrm is not None and not worn_vrm.builtin and worn_vrm.title == "Aria"
    assert worn_rig is not None and not worn_rig.builtin


def test_a_restore_on_the_same_node_wears_the_body_already_there(tmp_path):
    registry = CharacterRegistry(tmp_path)
    library = library_at(tmp_path)
    vrm = library.add_vrm(io.BytesIO(vrm1("Aria")), "aria.vrm")
    item = lived_in(tmp_path)
    item.body.vrm = vrm.id
    registry.add(item)
    dest = tmp_path / "yuri.zip"
    backup.write_backup(item, registry.data_root, dest, library=library)

    staged = backup.stage_restore(dest, registry, character_id="yuri_v2", trusted=True,
                                  library=library)

    assert staged.record.body.vrm == vrm.id
    assert staged.added_bodies == []
    assert len([e for e in library.entries("vrm") if not e.builtin]) == 1
    staged.discard(library)


def test_snapshots_are_named_by_stamp_and_listed_newest_first(tmp_path):
    import datetime
    registry = CharacterRegistry(tmp_path)
    item = lived_in(tmp_path)
    registry.add(item)
    when = datetime.datetime(2026, 11, 16, 10, 0, 0)
    paths = []
    for _ in range(3):
        path = backup.new_snapshot_path(tmp_path, "yuri", now=when)
        backup.write_backup(item, registry.data_root, path, kind="snapshot")
        paths.append(path.name)
    later = backup.new_snapshot_path(tmp_path, "yuri", now=when.replace(hour=11))
    backup.write_backup(item, registry.data_root, later, kind="snapshot")

    assert paths == ["yuri-20261116-100000.zip", "yuri-20261116-100000-2.zip",
                     "yuri-20261116-100000-3.zip"]
    listed = [row["snapshot"] for row in backup.list_snapshots(tmp_path, "yuri")]
    assert listed == [later.name, paths[2], paths[1], paths[0]]


@pytest.mark.parametrize("character_id, name", [
    ("yuri", "../yuri-20261116-100000.zip"),
    ("yuri", "mika-20261116-100000.zip"),
    ("../x", "x-20261116-100000.zip"),
    ("yuri", "yuri-20261116-100000.zip.tmp"),
])
def test_a_snapshot_name_cannot_reach_outside_her_shelf(tmp_path, character_id, name):
    assert backup.snapshot_path(tmp_path, character_id, name) is None


# ---- the host ----------------------------------------------------------------


@pytest.fixture
def house(tmp_path, monkeypatch):
    registry = CharacterRegistry(tmp_path)
    registry.add(lived_in(tmp_path))
    monkeypatch.setattr("yurios.world.host.hosting.create_app", fake_character_app)
    app = create_host_app(Config(data_dir=tmp_path, _env_file=None), registry)
    with TestClient(app) as client:
        yield client, registry, tmp_path


def test_a_snapshot_is_kept_listed_and_restorable_as_a_copy(house):
    client, registry, root = house
    taken = client.post("/api/characters/yuri/snapshots")
    assert taken.status_code == 201
    name = taken.json()["snapshot"]["snapshot"]
    assert (root / "snapshots" / "yuri" / name).is_file()
    listed = client.get("/api/characters/yuri/snapshots").json()["snapshots"]
    assert [row["snapshot"] for row in listed] == [name]

    restored = client.post(f"/api/characters/yuri/snapshots/{name}/restore",
                           json={"mode": "new"})

    assert restored.status_code == 201
    copy = restored.json()["character"]
    assert copy["id"] != "yuri" and copy["name"] == "Yuri (restored)"
    assert registry.get("yuri") is not None
    assert registry.require(copy["id"]).models.chat == "lmstudio/her-own-model"


def test_rolling_back_archives_the_state_it_replaces(house):
    client, registry, root = house
    name = client.post("/api/characters/yuri/snapshots").json()["snapshot"]["snapshot"]
    memory = root / "characters" / "yuri" / "vault" / "memory" / "episodic" / "2026-11-01.md"
    memory.write_text("### 10:00  we met\n### 11:00  something to undo\n")

    rolled = client.post(f"/api/characters/yuri/snapshots/{name}/restore",
                         json={"mode": "replace"})

    assert rolled.status_code == 201
    body = rolled.json()
    assert body["character"]["id"] == "yuri"
    assert memory.read_text() == "### 10:00  we met\n"
    archived = root / "archives" / body["archived"]
    kept = archived / "vault" / "memory" / "episodic" / "2026-11-01.md"
    assert "something to undo" in kept.read_text()
    assert len(registry) == 1


def test_a_snapshot_that_will_not_unpack_leaves_her_where_she_is(house):
    client, registry, root = house
    name = client.post("/api/characters/yuri/snapshots").json()["snapshot"]["snapshot"]
    (root / "snapshots" / "yuri" / name).write_bytes(b"corrupted")

    failed = client.post(f"/api/characters/yuri/snapshots/{name}/restore",
                         json={"mode": "replace"})

    assert failed.status_code == 400
    assert registry.get("yuri") is not None
    assert (root / "characters" / "yuri" / "vault").is_dir()
    assert not (root / "archives").exists() or not list((root / "archives").iterdir())


def test_a_snapshot_can_be_downloaded_and_deleted(house):
    client, _registry, root = house
    name = client.post("/api/characters/yuri/snapshots").json()["snapshot"]["snapshot"]
    fetched = client.get(f"/api/characters/yuri/snapshots/{name}")
    assert fetched.status_code == 200
    assert zipfile.ZipFile(io.BytesIO(fetched.content)).getinfo(backup.MANIFEST_NAME)

    assert client.delete(f"/api/characters/yuri/snapshots/{name}").status_code == 200
    assert not (root / "snapshots" / "yuri" / name).exists()
    assert client.get(f"/api/characters/yuri/snapshots/{name}").status_code == 404


def test_a_backup_downloads_once_and_restores_elsewhere_under_review(house, tmp_path):
    client, registry, root = house
    prepared = client.post("/api/characters/yuri/backup")
    assert prepared.status_code == 200
    url = prepared.json()["url"]
    assert prepared.json()["filename"].startswith("yurios-yuri-")
    fetched = client.get(url)
    assert fetched.status_code == 200
    assert "attachment" in fetched.headers["content-disposition"]
    assert client.get(url).status_code == 404, "a prepared backup is fetched once"
    assert not list((root / ".backups" / "downloads").iterdir())

    uploaded = client.post("/api/backups/uploads", content=fetched.content,
                           headers={"content-type": "application/zip"})
    assert uploaded.status_code == 200
    body = uploaded.json()
    assert body["exists"] is True and body["existing_name"] == "Yuri"
    assert body["backup"]["name"] == "Yuri"
    assert body["backup"]["size"] == len(fetched.content)

    restored = client.post(f"/api/backups/uploads/{body['upload']}/restore",
                           json={"mode": "new", "name": "Yuri from backup"})

    assert restored.status_code == 201
    new = registry.require(restored.json()["character"]["id"])
    assert new.display.name == "Yuri from backup"
    assert new.lifecycle.review_required is True
    assert restored.json()["started"] is False
    assert not list((root / ".backups" / "uploads").iterdir())


def test_an_upload_that_is_not_a_backup_is_refused_and_not_kept(house):
    client, _registry, root = house
    refused = client.post("/api/backups/uploads", content=b"PK not really",
                          headers={"content-type": "application/zip"})
    assert refused.status_code == 400
    assert not list((root / ".backups" / "uploads").iterdir())


def test_a_snapshot_of_a_running_character_starts_her_again(tmp_path, monkeypatch):
    registry = CharacterRegistry(tmp_path)
    registry.add(lived_in(tmp_path, enabled=True))
    monkeypatch.setattr("yurios.world.host.hosting.create_app", fake_character_app)
    seen: list[bool] = []
    original = backup.write_backup

    def watched(*args, **kwargs):
        seen.append(app.state.host.runtime("yuri") is None)
        return original(*args, **kwargs)

    monkeypatch.setattr(backup, "write_backup", watched)
    app = create_host_app(Config(data_dir=tmp_path, _env_file=None), registry)
    with TestClient(app) as client:
        assert app.state.host.runtime("yuri") is not None
        taken = client.post("/api/characters/yuri/snapshots")
        assert taken.status_code == 201
        assert taken.json()["source_error"] is None
        assert seen == [True], "the copy is of a quiet tree"
        assert app.state.host.runtime("yuri") is not None


def test_only_the_upload_route_may_exceed_the_house_body_ceiling():
    async def ok(scope, receive, send):
        while (await receive()).get("more_body"):
            pass
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    from starlette.applications import Starlette
    from starlette.routing import Mount
    app = Starlette(routes=[Mount("/", app=ok)])
    app.add_middleware(HTTPBodyLimitMiddleware, maximum=16,
                       larger={"/api/backups/uploads": 1024})
    with TestClient(app) as client:
        assert client.post("/api/other", content=b"x" * 64).status_code == 413
        assert client.post("/api/backups/uploads", content=b"x" * 64).status_code == 200
        assert client.post("/api/backups/uploads", content=b"x" * 2048).status_code == 413
