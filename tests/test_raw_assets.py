"""Raw scripts carry their content in their URL (SPEC §32.1).

`no-cache` only reaches a copy fetched after it shipped. A browser that already
held `/shared/settings.js` without one kept drawing an old model picker — one
with no OpenAI-compatible provider in it — and nothing the server sent could
reach that copy. The pages are revalidated on every load, so the address they
ask for has to change when the file does.
"""
from __future__ import annotations

import re

import pytest

pytest.importorskip("fastapi")
from starlette.testclient import TestClient           # noqa: E402

from yurios.characters import CharacterRegistry       # noqa: E402
from yurios.world import raw_assets                    # noqa: E402
from yurios.world.config import Config                 # noqa: E402
from yurios.world.host import create_host_app          # noqa: E402
from yurios.world.main import WEB_DIR                  # noqa: E402

from tests.test_host import fake_character_app, record  # noqa: E402

PAGE = ('<link rel="stylesheet" href="/shared/panel.css">'
        '<script src="/shared/panel.js"></script>'
        '<script src="/js/chat.js"></script>'
        '<script src="/shared/panel.js?v=pinned"></script>'
        '<script src="/shared/gone.js"></script>'
        '<script src="/assets/entry-abc.js"></script>')


def _web(tmp_path):
    web = tmp_path / "web"
    (web / "shared").mkdir(parents=True)
    (web / "js").mkdir()
    (web / "shared" / "panel.js").write_text("// one", encoding="utf-8")
    (web / "shared" / "panel.css").write_text("/* one */", encoding="utf-8")
    (web / "js" / "chat.js").write_text("// chat", encoding="utf-8")
    return web


def _stamp(html, ref):
    found = re.search(re.escape(ref) + r'\?v=([0-9a-f]+)"', html)
    return found and found.group(1)


def test_an_edit_is_a_new_address(tmp_path):
    web = _web(tmp_path)
    before = raw_assets.stamp_refs(PAGE, web)
    assert _stamp(before, "/shared/panel.js") and _stamp(before, "/shared/panel.css")
    assert _stamp(before, "/js/chat.js")

    (web / "shared" / "panel.js").write_text("// two — with a provider added",
                                             encoding="utf-8")
    after = raw_assets.stamp_refs(PAGE, web)
    assert _stamp(after, "/shared/panel.js") != _stamp(before, "/shared/panel.js")
    # …and only the file that changed moves
    assert _stamp(after, "/js/chat.js") == _stamp(before, "/js/chat.js")


def test_what_it_cannot_stamp_is_left_as_written(tmp_path):
    html = raw_assets.stamp_refs(PAGE, _web(tmp_path))
    assert 'src="/shared/panel.js?v=pinned"' in html      # a query already there
    assert 'src="/shared/gone.js"' in html                 # nothing on disk to hash
    assert 'src="/assets/entry-abc.js"' in html            # hashed by Vite already


def test_the_house_settings_page_asks_for_the_picker_by_its_content(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    (dist / "settings").mkdir(parents=True)
    (dist / "settings" / "index.html").write_text(
        '<script src="/shared/settings.js"></script>', encoding="utf-8")
    monkeypatch.setattr("yurios.world.host.hosting.create_app", fake_character_app)
    monkeypatch.setattr("yurios.world.host.app.DIST_DIR", dist)
    monkeypatch.setattr("yurios.world.host.pages.DIST_DIR", dist)
    registry = CharacterRegistry(tmp_path)
    registry.add(record(tmp_path, enabled=False))

    with TestClient(create_host_app(Config(data_dir=tmp_path), registry)) as client:
        page = client.get("/settings/")
        assert page.headers["cache-control"] == "no-cache"
        stamp = _stamp(page.text, "/shared/settings.js")
        assert stamp
        script = client.get(f"/shared/settings.js?v={stamp}")
        assert script.status_code == 200
        assert script.text == (WEB_DIR / "shared" / "settings.js").read_text(encoding="utf-8")


def test_the_raw_live2d_page_is_stamped_too(tmp_path):
    """The one page Vite never builds — so the server is the only place to do it."""
    from yurios.desktop.voice.backends.fakes import FakeBrain
    from yurios.world.main import create_app

    cfg = Config(data_dir=tmp_path, tools_backend="off", mind_enabled=False)
    app = create_app(cfg, brain=FakeBrain())
    with TestClient(app, base_url="http://127.0.0.1:8768",
                    client=("127.0.0.1", 5555)) as client:
        page = client.get("/live2d/")
        assert page.status_code == 200
        assert page.headers["cache-control"] == "no-cache"
        assert _stamp(page.text, "/shared/settings.js")
        assert _stamp(page.text, "/shared/runtime.js")
