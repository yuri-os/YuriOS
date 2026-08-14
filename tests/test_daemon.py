"""The always-on process: who holds the runtime, what happens when she dies,
and why `yurios status` can always say what went wrong (yurios/daemon.py)."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from argparse import Namespace
from pathlib import Path

import pytest

from yurios import cli, daemon


@pytest.fixture
def restore_signals():
    """Supervisor.run installs handlers in whatever process runs it."""
    saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    yield
    for sig, handler in saved.items():
        signal.signal(sig, handler)


class FakeHandle:
    def close(self) -> None:
        pass


class FakeChild:
    """A world server that exits with `code` the moment it is waited on."""

    def __init__(self, pid: int, code: int, on_wait=None) -> None:
        self.pid = pid
        self.returncode = None
        self._code = code
        self._on_wait = on_wait
        self.terminated = False
        self.killed = False

    def wait(self, timeout=None):
        if self._on_wait is not None:
            self._on_wait(self)
        self.returncode = self._code
        return self._code

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def _fake_children(supervisor, children):
    spawned = []

    def spawn():
        child = children[len(spawned)]
        spawned.append(child)
        return child, FakeHandle()

    supervisor._spawn = spawn
    return spawned


# ---- which installation ----------------------------------------------------

def test_the_installation_is_found_from_the_package_not_the_shell(tmp_path, monkeypatch):
    """`yurios` is on $PATH and gets typed from anywhere; the installation it
    controls is the one it was installed from, not the directory it was typed in."""
    monkeypatch.delenv("YURIOS_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)

    assert daemon.install_root() == Path(daemon.__file__).resolve().parent.parent


def test_yurios_root_names_a_second_installation(tmp_path, monkeypatch):
    monkeypatch.setenv("YURIOS_ROOT", str(tmp_path))

    assert daemon.install_root() == tmp_path.resolve()


def test_a_command_typed_anywhere_runs_inside_the_installation(tmp_path, monkeypatch):
    """Finding the installation is not the same as standing in it: `.env` and the
    relative roots it holds are read against the working directory."""
    elsewhere = (tmp_path / "elsewhere").resolve()
    install = (tmp_path / "install").resolve()
    elsewhere.mkdir()
    install.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(cli, "_root", lambda: install)
    seen = []
    monkeypatch.setattr(cli, "command_status", lambda args: seen.append(Path.cwd()) or 0)

    assert cli.main(["status"]) == 0

    assert seen == [install]


# ---- identity: the lock, not the number ------------------------------------

def test_a_pid_file_nobody_holds_names_nobody(tmp_path):
    """The recycled-pid bug: an abandoned file whose number now belongs to a
    live stranger (this test process) must not read as "she is running"."""
    path = daemon.pid_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(f"{os.getpid()}\n", encoding="utf-8")

    assert daemon.running_pid(path) is None


def test_stop_refuses_to_signal_a_process_that_only_inherited_her_pid(
        tmp_path, monkeypatch, capsys):
    path = daemon.pid_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_root", lambda: tmp_path)
    sent, real_kill = [], os.kill
    # `os.kill(pid, 0)` is the liveness probe; anything else is a real signal.
    monkeypatch.setattr(cli.os, "kill",
                        lambda pid, sig: real_kill(pid, sig) if sig == 0
                        else sent.append((pid, sig)))

    assert cli.command_stop(Namespace()) == 0

    assert sent == []
    assert "not running" in capsys.readouterr().out
    assert not path.exists()


def test_a_live_holder_is_found_and_releases_the_runtime_when_it_dies(tmp_path):
    repo = str(Path(daemon.__file__).resolve().parent.parent)
    holder = subprocess.Popen([sys.executable, "-c", (
        f"import sys, time; sys.path.insert(0, {repo!r})\n"
        "from pathlib import Path\n"
        "from yurios import daemon\n"
        f"daemon.acquire(Path({str(tmp_path)!r}))\n"
        "time.sleep(30)\n")])
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if daemon.running_pid(daemon.pid_path(tmp_path)) == holder.pid:
                break
            time.sleep(0.05)
        assert daemon.running_pid(daemon.pid_path(tmp_path)) == holder.pid
    finally:
        holder.terminate()
        holder.wait(timeout=15)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and daemon.running_pid(daemon.pid_path(tmp_path)):
        time.sleep(0.05)

    assert daemon.running_pid(daemon.pid_path(tmp_path)) is None


def test_an_upgraded_installation_can_still_stop_the_daemon_it_started(tmp_path, monkeypatch):
    """A daemon from before the lock holds nothing, but is hers to stop once its
    command line proves it — the pid alone never is."""
    path = daemon.pid_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("4242\n", encoding="utf-8")
    monkeypatch.setattr(daemon, "_pid_if_alive", lambda _: 4242)

    monkeypatch.setattr(daemon, "_looks_like_yurios", lambda pid: False)
    assert daemon.unlocked_pid(path) is None           # a stranger with her old pid

    monkeypatch.setattr(daemon, "_looks_like_yurios", lambda pid: True)
    assert daemon.unlocked_pid(path) == 4242


def test_a_process_is_only_hers_when_its_command_line_says_so(tmp_path):
    """The installation directory is full of the word; that is not consent."""
    assert daemon._looks_like_yurios(os.getpid()) is False   # this pytest run


def test_the_runtime_lock_admits_one_start(tmp_path):
    first = daemon.acquire(tmp_path)
    try:
        assert first is not None
        assert daemon.acquire(tmp_path) is None            # the second `yurios start`
        assert daemon.running_pid(daemon.pid_path(tmp_path)) == os.getpid()
    finally:
        first.release()

    assert daemon.acquire(tmp_path) is not None            # released, so free again


# ---- restarts and the reason she stopped -----------------------------------

def test_the_supervisor_restarts_her_and_records_why(tmp_path, monkeypatch, restore_signals):
    monkeypatch.setattr(daemon, "BACKOFF_SECONDS", (0.0,) * 6)
    supervisor = daemon.Supervisor(tmp_path)
    crashed = FakeChild(101, -signal.SIGSEGV)
    stopped = FakeChild(102, 0, on_wait=lambda child: supervisor._handle_stop(
        signal.SIGTERM, None))
    spawned = _fake_children(supervisor, [crashed, stopped])

    assert supervisor.run() == 0

    assert [c.pid for c in spawned] == [101, 102]          # she was put back up
    assert supervisor.restarts == 1
    assert stopped.terminated                              # the stop was forwarded
    record = daemon.last_exit(tmp_path)
    assert record["requested"] and record["pid"] == 102
    assert not daemon.pid_path(tmp_path).exists()          # runtime handed back


def test_a_crash_loop_stops_restarting_and_says_so(tmp_path, monkeypatch, restore_signals):
    monkeypatch.setattr(daemon, "BACKOFF_SECONDS", (0.0,) * 6)
    monkeypatch.setattr(daemon, "MAX_FAST_FAILURES", 2)
    supervisor = daemon.Supervisor(tmp_path)
    spawned = _fake_children(supervisor, [FakeChild(200 + n, 1) for n in range(5)])

    assert supervisor.run() == 1

    assert len(spawned) == 3                               # budget + the one that spent it
    record = daemon.last_exit(tmp_path)
    assert record["restarting"] is False and record["requested"] is False
    assert record["reason"] == "exit status 1"
    assert "not restarting" in record["detail"]
    assert daemon.exit_summary(record).startswith("exit status 1; ")


def test_a_run_that_stayed_up_refills_the_crash_budget(tmp_path, monkeypatch, restore_signals):
    monkeypatch.setattr(daemon, "BACKOFF_SECONDS", (0.0,) * 6)
    monkeypatch.setattr(daemon, "MAX_FAST_FAILURES", 1)
    monkeypatch.setattr(daemon, "HEALTHY_SECONDS", 0.0)    # every run counts as real
    supervisor = daemon.Supervisor(tmp_path)
    children = [FakeChild(300 + n, 1) for n in range(4)]
    children[-1] = FakeChild(399, 0, on_wait=lambda child: supervisor._handle_stop(
        signal.SIGTERM, None))
    spawned = _fake_children(supervisor, children)

    assert supervisor.run() == 0

    assert len(spawned) == 4                               # no crash loop declared


def test_status_reports_the_reason_she_is_not_running(tmp_path, monkeypatch, capsys):
    (tmp_path / ".env").write_text("CHAT_MODEL=ollama/qwen3\n", encoding="utf-8")
    daemon.record_exit(tmp_path, pid=7, code=-9, requested=False, restarting=False,
                       detail="3 starts in a row died within 30s; not restarting")
    monkeypatch.setattr(cli, "_root", lambda: tmp_path)

    def unreachable(*args, **kwargs):
        raise cli.httpx.ConnectError("no daemon")

    monkeypatch.setattr(cli.httpx, "get", unreachable)

    assert cli.command_status(Namespace()) == 1

    output = capsys.readouterr().out
    assert "Daemon     stopped" in output
    assert "Last exit  killed by SIGKILL; 3 starts in a row died" in output


def test_the_exit_record_carries_the_end_of_the_log(tmp_path):
    log = daemon.log_path(tmp_path)
    log.parent.mkdir(parents=True)
    log.write_text("".join(f"line {n}\n" for n in range(200)), encoding="utf-8")

    record = daemon.record_exit(tmp_path, pid=5, code=1, requested=False, restarting=True)

    assert record["log_tail"].endswith("line 199\n")
    assert record["log_tail"].count("\n") == daemon.EXIT_TAIL_LINES


# ---- the log --------------------------------------------------------------

def test_log_prints_the_end_of_a_long_log_not_all_of_it(tmp_path, monkeypatch, capsys):
    log = daemon.log_path(tmp_path)
    log.parent.mkdir(parents=True)
    log.write_text("".join(f"line {n}\n" for n in range(5000)), encoding="utf-8")
    monkeypatch.setattr(cli, "_root", lambda: tmp_path)

    assert cli.command_log(Namespace(lines=10, all=False, follow=False)) == 0

    output = capsys.readouterr().out
    assert output.splitlines() == [f"line {n}" for n in range(4990, 5000)]

    assert cli.command_log(Namespace(lines=10, all=True, follow=False)) == 0
    assert len(capsys.readouterr().out.splitlines()) == 5000


def test_tail_of_a_log_shorter_than_the_window(tmp_path):
    log = tmp_path / "yurios.log"
    log.write_text("one\ntwo\n", encoding="utf-8")

    assert daemon.tail(log, 200) == "one\ntwo\n"
    assert daemon.tail(log, 1) == "two\n"
    assert daemon.tail(log, 0) == ""


# ---- "ok" means working, not reachable -------------------------------------

def _health(cfg, **overrides):
    from starlette.testclient import TestClient

    from yurios.desktop.voice.backends.fakes import FakeBrain
    from yurios.world.main import create_app

    app = create_app(cfg.model_copy(update={"tools_backend": "off",
                                            "mind_enabled": False}),
                     brain=FakeBrain())
    with TestClient(app) as client:
        for name, value in overrides.items():
            setattr(client.app.state.rt, name, value)
        return client.get("/api/health").json()


def test_health_is_not_ok_without_a_model(cfg):
    # An injected brain counts as configured (main.py), so say what a fresh
    # install is: reachable, answering, and with nothing to think with.
    health = _health(cfg, model_configured=False)

    assert health["ok"] is False
    assert health["degraded"] == ["no language model configured"]


def test_health_names_the_seams_that_failed(cfg):
    health = _health(cfg, model_configured=True,
                     channels_status="telegram failed: bad token",
                     tools_status="failed: no such server")

    assert health["ok"] is False
    assert health["degraded"] == ["channels: telegram failed: bad token",
                                  "tools: failed: no such server"]


def test_a_working_companion_on_fallbacks_is_still_ok(cfg):
    """Degrading loudly is her design (§3): a fake voice or a mock selfie lab is
    working, and must not read as broken."""
    health = _health(cfg, model_configured=True, selfies_status="mock (no key)",
                     research_status="fake")

    assert health["ok"] is True and health["degraded"] == []
