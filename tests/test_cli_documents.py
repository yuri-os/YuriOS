"""Shelf and desk CLI (SPEC §20.1, §34.6, §36.6): hand her documents from the terminal."""
from __future__ import annotations

import json

import httpx

from yurios.cli import main as cli_main
from yurios.ctl.client import HostClient
from yurios.world.config import Config


def install_host(monkeypatch, handler, root):
    (root / ".env").write_text("CHAT_MODEL=NONE\n", encoding="utf-8")
    monkeypatch.setattr("yurios.cli._root", lambda: root)
    transport = httpx.MockTransport(handler)

    def connect(cfg=None, **kwargs):
        return HostClient(cfg or Config(_env_file=None), transport=transport)
    monkeypatch.setattr("yurios.ctl.documents.connect", connect)


def _content_type(request: httpx.Request) -> str:
    head = request.content.partition(b"\r\n\r\n")[0]
    return head.split(b"Content-Type: ", 1)[1].split(b"\r\n", 1)[0].decode()


def _uploaded(request: httpx.Request) -> tuple[str, bytes]:
    """The filename and bytes of a one-file multipart body."""
    body = request.content
    head, _, rest = body.partition(b"\r\n\r\n")
    name = head.split(b'filename="', 1)[1].split(b'"', 1)[0].decode()
    return name, rest.rsplit(b"\r\n--", 1)[0]


def test_shelf_add_uploads_a_path_relative_to_where_you_typed_it(
        tmp_path, monkeypatch, capsys):
    """`yurios` changes into the installation before it dispatches, so a
    relative path has to be resolved while it still means what you meant."""
    root, mine = tmp_path / "install", tmp_path / "papers"
    root.mkdir()
    mine.mkdir()
    (mine / "tea.md").write_text("# Tea\n", encoding="utf-8")
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/characters/yuri/mind/research"
        seen.append(_uploaded(request))
        return httpx.Response(200, json={"name": "tea.md", "calls": 2,
                                         "replaced": False, "unchanged": False})

    install_host(monkeypatch, handler, root)
    monkeypatch.chdir(mine)
    assert cli_main(["shelf", "add", "yuri", "tea.md"]) == 0
    assert seen == [("tea.md", b"# Tea\n")]
    out = capsys.readouterr().out
    assert "tea.md  shelved · about 2 model calls" in out


def test_one_refused_file_does_not_cost_the_rest(tmp_path, monkeypatch, capsys):
    (tmp_path / "paper.pdf").write_bytes(b"%PDF")
    (tmp_path / "notes.txt").write_text("words", encoding="utf-8")
    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        name, _ = _uploaded(request)
        posted.append(name)
        if name.endswith(".pdf"):
            return httpx.Response(415, json={"detail": "she reads .md and .txt files"})
        return httpx.Response(200, json={"name": name, "calls": 1})

    install_host(monkeypatch, handler, tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli_main(["shelf", "add", "yuri", "paper.pdf", "notes.txt",
                     "missing.md"]) == 1
    assert posted == ["paper.pdf", "notes.txt"]
    captured = capsys.readouterr()
    assert "notes.txt  shelved · about 1 model call\n" in captured.out
    assert "paper.pdf: she reads .md and .txt files (HTTP 415)" in captured.err
    assert "missing.md" in captured.err


def test_shelf_add_says_when_she_is_not_running(tmp_path, monkeypatch, capsys):
    (tmp_path / "notes.md").write_text("words", encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    install_host(monkeypatch, handler, tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli_main(["shelf", "add", "yuri", "notes.md"]) == 1
    assert "yurios start" in capsys.readouterr().err


def test_shelf_list_prints_the_shelf(tmp_path, monkeypatch, capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/characters/yuri/mind/research"
        return httpx.Response(200, json={"files": [
            {"name": "tea.md", "bytes": 1234, "mtime": 1_700_000_000}]})

    install_host(monkeypatch, handler, tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli_main(["shelf", "list", "yuri"]) == 0
    assert "1,234  tea.md" in capsys.readouterr().out
    assert cli_main(["shelf", "list", "yuri", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["name"] == "tea.md"


def test_a_pdf_is_sent_as_one_and_its_shelf_name_is_shown(tmp_path, monkeypatch, capsys):
    (tmp_path / "Tea Notes.pdf").write_bytes(b"%PDF-1.4")
    kinds = []

    def handler(request: httpx.Request) -> httpx.Response:
        kinds.append(_content_type(request))
        return httpx.Response(200, json={"name": "Tea_Notes.md", "calls": 3})

    install_host(monkeypatch, handler, tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli_main(["shelf", "add", "yuri", "Tea Notes.pdf"]) == 0
    assert kinds == ["application/pdf"]
    assert "Tea Notes.pdf → Tea_Notes.md  shelved" in capsys.readouterr().out


def test_desk_add_hands_it_to_her_inbox_and_says_when_she_reads_it(
        tmp_path, monkeypatch, capsys):
    (tmp_path / "Q3 numbers.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "later.md").write_text("words", encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/characters/yuri/mind/workspace/inbox"
        name, _ = _uploaded(request)
        quiet = name == "later.md"
        path = "inbox/later.md" if quiet else "inbox/Q3_numbers.md"
        return httpx.Response(200, json={"path": path, "noticed": True,
                                         "wakes": not quiet})

    install_host(monkeypatch, handler, tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli_main(["desk", "add", "yuri", "Q3 numbers.pdf", "later.md"]) == 0
    out = capsys.readouterr().out
    assert "Q3 numbers.pdf → inbox/Q3_numbers.md  she'll read it now" in out
    assert "later.md → inbox/later.md  she'll read it when you're back" in out


def test_desk_list_prints_files_not_folders(tmp_path, monkeypatch, capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/characters/yuri/mind/workspace"
        return httpx.Response(200, json={"files": [
            {"path": "inbox", "dir": True, "bytes": 0, "mtime": 1_700_000_000},
            {"path": "inbox/q3.md", "bytes": 40, "mtime": 1_700_000_100}]})

    install_host(monkeypatch, handler, tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli_main(["desk", "list", "yuri"]) == 0
    out = capsys.readouterr().out
    assert "inbox/q3.md" in out and "inbox\n" not in out
