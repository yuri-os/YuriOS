from __future__ import annotations

import io
import stat
import warnings
import zipfile
from pathlib import Path

import pytest

from scripts import fetch_live2d as fetch


ANCHOR = fetch.HIYORI_MODEL3


class Response:
    def __init__(self, body: bytes, content_length: str | None = None):
        self._body = io.BytesIO(body)
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = content_length
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return self._body.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


def make_zip(
    path: Path,
    entries: list[tuple[str | zipfile.ZipInfo, bytes]],
    compression: int = zipfile.ZIP_STORED,
) -> Path:
    with zipfile.ZipFile(path, "w", compression=compression) as archive:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            for name, body in entries:
                archive.writestr(name, body)
    return path


def valid_entries(prefix: str = "package/runtime") -> list[tuple[str, bytes]]:
    return [
        (f"{prefix}/{ANCHOR}", b'{"Version": 3}'),
        (f"{prefix}/textures/texture_00.png", b"texture"),
    ]


def special_info(name: str, kind: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name)
    info.create_system = 3
    info.external_attr = (kind | 0o755) << 16
    return info


def test_download_rejects_large_content_length_before_reading(tmp_path, monkeypatch):
    response = Response(b"not read", content_length="100")
    monkeypatch.setattr(fetch.urllib.request, "urlopen", lambda *_a, **_kw: response)

    with pytest.raises(RuntimeError, match="limit"):
        fetch._download("https://example.test/file", tmp_path, 10, 30)

    assert response.read_sizes == []
    assert list(tmp_path.iterdir()) == []


def test_download_stream_limit_preserves_atomic_destination(tmp_path, monkeypatch):
    response = Response(b"0123456789ABCDEF")
    monkeypatch.setattr(fetch.urllib.request, "urlopen", lambda *_a, **_kw: response)
    dest = tmp_path / "runtime.js"
    dest.write_bytes(b"prior")

    with pytest.raises(RuntimeError, match="exceeds"):
        fetch._download_atomic("https://example.test/file", dest, 8, 30)

    assert dest.read_bytes() == b"prior"
    assert response.read_sizes == [9]
    assert not list(tmp_path.glob(".live2d-download-*"))


def test_download_rejects_truncated_declared_body(tmp_path, monkeypatch):
    response = Response(b"short", content_length="10")
    monkeypatch.setattr(fetch.urllib.request, "urlopen", lambda *_a, **_kw: response)

    with pytest.raises(RuntimeError, match="incomplete"):
        fetch._download("https://example.test/file", tmp_path, 20, 30)

    assert not list(tmp_path.iterdir())


def test_install_selects_only_runtime_with_expected_anchor(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch, "VENDOR", tmp_path / "vendor")
    archive = make_zip(
        tmp_path / "model.zip",
        valid_entries("right/runtime")
        + [
            ("wrong/runtime/not-the-model.model3.json", b"wrong"),
            ("wrong/runtime/unrelated.bin", b"unrelated"),
            ("right/readme.txt", b"outside"),
        ],
    )

    fetch.install_model(archive)

    runtime = fetch.VENDOR / "hiyori" / "runtime"
    assert (runtime / ANCHOR).read_bytes() == b'{"Version": 3}'
    assert (runtime / "textures" / "texture_00.png").read_bytes() == b"texture"
    assert not (runtime / "unrelated.bin").exists()
    assert not (fetch.VENDOR / "hiyori" / "readme.txt").exists()


def test_extraction_streams_members_instead_of_using_zipfile_read(tmp_path, monkeypatch):
    archive = make_zip(tmp_path / "model.zip", valid_entries())
    dest = tmp_path / "vendor" / "hiyori"

    def buffered_read_is_forbidden(*_args, **_kwargs):
        raise AssertionError("ZipFile.read buffered a complete member")

    monkeypatch.setattr(zipfile.ZipFile, "read", buffered_read_is_forbidden)
    assert fetch._install_archive(archive, dest, ANCHOR) == 2
    assert (dest / "runtime" / ANCHOR).exists()


@pytest.mark.parametrize(
    "unsafe_name",
    [
        "../escape",
        "/absolute/escape",
        "package/runtime/../../escape",
        "package\\runtime\\escape",
        "C:/escape",
        "package/runtime/file:stream",
    ],
)
def test_zip_slip_paths_are_rejected(tmp_path, unsafe_name):
    archive = make_zip(
        tmp_path / "model.zip", valid_entries() + [(unsafe_name, b"bad")]
    )

    with pytest.raises(RuntimeError, match="unsafe ZIP member path"):
        fetch._install_archive(archive, tmp_path / "install", ANCHOR)

    assert not (tmp_path / "install").exists()
    assert not (tmp_path.parent / "escape").exists()


@pytest.mark.parametrize("kind", [stat.S_IFLNK, stat.S_IFIFO, stat.S_IFCHR])
def test_symlinks_and_special_files_are_rejected(tmp_path, kind):
    archive = make_zip(
        tmp_path / "model.zip",
        valid_entries() + [(special_info("package/runtime/device", kind), b"target")],
    )

    with pytest.raises(RuntimeError, match="symlink or special"):
        fetch._install_archive(archive, tmp_path / "install", ANCHOR)


def test_duplicate_members_are_rejected(tmp_path):
    entries = valid_entries() + [(f"package/runtime/{ANCHOR}", b"second")]
    archive = make_zip(tmp_path / "model.zip", entries)

    with pytest.raises(RuntimeError, match="duplicate ZIP member"):
        fetch._install_archive(archive, tmp_path / "install", ANCHOR)


@pytest.mark.parametrize(
    "extra, message",
    [
        ([('PACKAGE/runtime/other.bin', b'x')], "case-colliding"),
        ([('package/runtime/textures', b'file')], "file/directory collision"),
    ],
)
def test_portable_path_collisions_are_rejected(tmp_path, extra, message):
    archive = make_zip(tmp_path / "model.zip", valid_entries() + extra)

    with pytest.raises(RuntimeError, match=message):
        fetch._install_archive(archive, tmp_path / "install", ANCHOR)


def test_member_count_is_bounded(tmp_path, monkeypatch):
    archive = make_zip(tmp_path / "model.zip", valid_entries())
    monkeypatch.setattr(fetch, "MAX_ARCHIVE_MEMBERS", 1)

    with pytest.raises(RuntimeError, match="members; limit"):
        fetch._install_archive(archive, tmp_path / "install", ANCHOR)


def test_each_member_size_is_bounded(tmp_path, monkeypatch):
    archive = make_zip(tmp_path / "model.zip", valid_entries())
    monkeypatch.setattr(fetch, "MAX_MEMBER_BYTES", 4)

    with pytest.raises(RuntimeError, match="member.*limit"):
        fetch._install_archive(archive, tmp_path / "install", ANCHOR)


def test_aggregate_expansion_is_bounded(tmp_path, monkeypatch):
    archive = make_zip(tmp_path / "model.zip", valid_entries())
    monkeypatch.setattr(fetch, "MAX_MEMBER_BYTES", 100)
    monkeypatch.setattr(fetch, "MAX_EXTRACTED_BYTES", 20)

    with pytest.raises(RuntimeError, match="aggregate limit"):
        fetch._install_archive(archive, tmp_path / "install", ANCHOR)


def test_compression_ratio_is_bounded(tmp_path, monkeypatch):
    archive = make_zip(
        tmp_path / "model.zip",
        valid_entries() + [("package/runtime/bomb.bin", b"A" * 10_000)],
        compression=zipfile.ZIP_DEFLATED,
    )
    monkeypatch.setattr(fetch, "MAX_COMPRESSION_RATIO", 10)

    with pytest.raises(RuntimeError, match="compression ratio"):
        fetch._install_archive(archive, tmp_path / "install", ANCHOR)


def test_ambiguous_expected_anchors_are_rejected(tmp_path):
    archive = make_zip(
        tmp_path / "model.zip",
        valid_entries("first/runtime") + valid_entries("second/runtime"),
    )

    with pytest.raises(RuntimeError, match="exactly one.*found 2"):
        fetch._install_archive(archive, tmp_path / "install", ANCHOR)


def test_failed_install_preserves_previous_tree(tmp_path):
    dest = tmp_path / "vendor" / "hiyori"
    dest.mkdir(parents=True)
    (dest / "prior.txt").write_bytes(b"keep me")
    archive = make_zip(
        tmp_path / "bad.zip", valid_entries() + [("../escape", b"bad")]
    )

    with pytest.raises(RuntimeError):
        fetch._install_archive(archive, dest, ANCHOR)

    assert (dest / "prior.txt").read_bytes() == b"keep me"
    assert not list(dest.parent.glob(".hiyori-install-*"))


def test_successful_install_atomically_replaces_previous_tree(tmp_path):
    dest = tmp_path / "vendor" / "hiyori"
    dest.mkdir(parents=True)
    (dest / "stale.txt").write_bytes(b"remove me")
    archive = make_zip(tmp_path / "good.zip", valid_entries())

    fetch._install_archive(archive, dest, ANCHOR)

    assert not (dest / "stale.txt").exists()
    assert (dest / "runtime" / ANCHOR).exists()
    assert not list(dest.parent.glob(".hiyori-install-*"))
