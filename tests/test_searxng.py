"""Her search instance as a managed container (SPEC §7.7).

The container is the one dependency in this project that can be *present but
wrong*, so most of these pin which sentence the user gets: a stopped container, a
missing one, and one that is up but refusing JSON are three different fixes, and
telling somebody the wrong one costs them an afternoon.

No Docker is invoked anywhere here — the runtime probes are patched, because a
test suite that needs a container daemon isn't offline (SPEC §27).
"""
from __future__ import annotations

import httpx
import pytest

from yurios import searxng


class Cfg:
    def __init__(self, backend="searxng", url="http://localhost:8080"):
        self.search_backend = backend
        self.searxng_url = url


@pytest.fixture
def nodocker(monkeypatch):
    monkeypatch.setattr(searxng, "runtime", lambda: None)
    monkeypatch.setattr(searxng, "usable", lambda cmd=None: False)


def fake_state(monkeypatch, value):
    monkeypatch.setattr(searxng, "state", lambda cmd=None: value)


def fake_probe(monkeypatch, ok, why="because"):
    monkeypatch.setattr(searxng, "probe", lambda url, **kw: (ok, why))


# ------------------------------------------------------------------- basics

@pytest.mark.parametrize("url,local", [
    ("http://localhost:8080", True),
    ("http://127.0.0.1:8080", True),
    ("http://[::1]:8080", True),
    ("https://searx.example.com", False),
    ("http://192.168.1.50:8080", False),      # the LAN is somebody else's box
])
def test_only_a_loopback_instance_is_ours_to_manage(url, local):
    assert searxng.is_local(url) is local


@pytest.mark.parametrize("url,port", [
    ("http://localhost:8080", 8080),
    ("http://localhost", 8080),
    ("https://searx.example.com", 443),
])
def test_port_of(url, port):
    assert searxng.port_of(url) == port


def test_settings_are_written_once_and_never_overwritten(tmp_path):
    path, created = searxng.write_settings(tmp_path)
    assert created and path.is_file()
    assert "formats:" in path.read_text() and "json" in path.read_text()

    path.write_text("# the user's own edits\n")
    path2, created2 = searxng.write_settings(tmp_path)
    # A rerun of the installer must not revert somebody's engine choices.
    assert path2 == path and created2 is False
    assert path.read_text() == "# the user's own edits\n"


def test_each_install_gets_its_own_secret(tmp_path):
    a, _ = searxng.write_settings(tmp_path / "a")
    b, _ = searxng.write_settings(tmp_path / "b")
    assert a.read_text() != b.read_text()


# -------------------------------------------------------------------- probe

def probing(monkeypatch, response):
    def get(url, params=None, timeout=None):
        if isinstance(response, Exception):
            raise response
        return response
    monkeypatch.setattr(httpx, "get", get)


def test_probe_names_the_disabled_json_format_on_403(monkeypatch):
    """The trap: HTML works, JSON is 403, and the status code points at auth."""
    probing(monkeypatch, httpx.Response(403))
    ok, why = searxng.probe("http://localhost:8080")
    assert ok is False and "search.formats" in why


def test_probe_is_happy_with_real_json(monkeypatch):
    probing(monkeypatch, httpx.Response(200, json={"results": []}))
    assert searxng.probe("http://localhost:8080") == (True, "answering JSON")


def test_probe_rejects_a_200_that_is_not_json(monkeypatch):
    """A captive portal or a reverse proxy's error page answers 200 cheerfully."""
    probing(monkeypatch, httpx.Response(200, text="<html>hi</html>"))
    ok, why = searxng.probe("http://localhost:8080")
    assert ok is False and "not JSON" in why


def test_probe_reports_an_unreachable_instance(monkeypatch):
    probing(monkeypatch, httpx.ConnectError("refused"))
    ok, why = searxng.probe("http://localhost:8080")
    assert ok is False and "not reachable" in why


# ------------------------------------------------------------------- status

def test_status_off_says_what_she_is_missing(tmp_path):
    info = searxng.status(Cfg(backend="off"), tmp_path)
    assert info["live"] is False and "no web hands" in info["detail"]


def test_status_fake_is_a_working_configuration(tmp_path):
    info = searxng.status(Cfg(backend="fake"), tmp_path)
    assert info["live"] is True


def test_status_asks_the_instance_before_it_blames_the_container(monkeypatch, tmp_path):
    """Somebody running SearXNG via compose, systemd, or a differently-named
    container has a perfectly good instance. Telling them to rerun install.sh
    would be both wrong and expensive."""
    fake_probe(monkeypatch, True, "answering JSON")
    fake_state(monkeypatch, "missing")
    info = searxng.status(Cfg(), tmp_path)
    assert info["live"] is True
    assert info["container"].startswith("not ours")


def test_status_blames_the_container_only_when_it_is_not_answering(monkeypatch, tmp_path):
    fake_probe(monkeypatch, False, "not reachable (ConnectError)")
    fake_state(monkeypatch, "exited")
    info = searxng.status(Cfg(), tmp_path)
    assert info["live"] is False
    assert "yurios start" in info["detail"]


def test_status_missing_container_points_at_the_installer(monkeypatch, tmp_path):
    fake_probe(monkeypatch, False, "not reachable")
    fake_state(monkeypatch, "missing")
    info = searxng.status(Cfg(), tmp_path)
    assert "install.sh" in info["detail"]


def test_status_no_docker_points_at_docker(monkeypatch, tmp_path):
    fake_probe(monkeypatch, False, "not reachable")
    fake_state(monkeypatch, "no-docker")
    info = searxng.status(Cfg(), tmp_path)
    assert "Docker" in info["detail"]


def test_a_remote_instance_is_reported_never_managed(monkeypatch, tmp_path):
    fake_probe(monkeypatch, True, "answering JSON")
    monkeypatch.setattr(searxng, "state",
                        lambda cmd=None: pytest.fail("touched the container"))
    info = searxng.status(Cfg(url="https://searx.example.com"), tmp_path)
    assert info["live"] is True and info["container"] == "not ours (remote instance)"


# ------------------------------------------------------------ ensure_running

def test_ensure_does_nothing_when_search_is_off(tmp_path, monkeypatch):
    monkeypatch.setattr(searxng, "probe",
                        lambda *a, **k: pytest.fail("probed a disabled backend"))
    assert searxng.ensure_running(Cfg(backend="off"), tmp_path) == (True, "")


def test_ensure_is_a_no_op_when_it_is_already_answering(monkeypatch, tmp_path):
    fake_probe(monkeypatch, True)
    monkeypatch.setattr(searxng, "start",
                        lambda cmd=None: pytest.fail("started a running instance"))
    assert searxng.ensure_running(Cfg(), tmp_path) == (True, "")


def test_ensure_starts_a_stopped_container(monkeypatch, tmp_path):
    started = []
    fake_probe(monkeypatch, False, "not reachable")
    fake_state(monkeypatch, "exited")
    monkeypatch.setattr(searxng, "start",
                        lambda cmd=None: (started.append(1), (True, "started"))[1])
    monkeypatch.setattr(searxng, "wait_ready",
                        lambda url, seconds=0: (True, "answering JSON"))
    ok, _why = searxng.ensure_running(Cfg(), tmp_path)
    assert ok is True and started == [1]


def test_ensure_never_makes_a_missing_container_fatal(monkeypatch, tmp_path):
    """A search instance that won't come up must not stop her booting — the
    voice stack's rule, applied to a service."""
    fake_probe(monkeypatch, False, "not reachable")
    fake_state(monkeypatch, "missing")
    ok, why = searxng.ensure_running(Cfg(), tmp_path)
    assert ok is False and "install.sh" in why      # a sentence, not an exception


def test_ensure_reports_a_dead_remote_instance_without_touching_docker(
        monkeypatch, tmp_path):
    fake_probe(monkeypatch, False, "not reachable (ConnectError)")
    monkeypatch.setattr(searxng, "state",
                        lambda cmd=None: pytest.fail("touched the container"))
    ok, why = searxng.ensure_running(Cfg(url="https://searx.example.com"), tmp_path)
    assert ok is False and "searx.example.com" in why


def test_a_missing_runtime_is_a_message_not_a_traceback(nodocker, tmp_path):
    assert searxng.state() == "no-docker"
    ok, why = searxng.create(tmp_path, url="http://localhost:8080")
    assert ok is False and "Docker" in why
