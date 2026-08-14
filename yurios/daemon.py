"""The `.yurios/` runtime: who is running, why she last stopped, and the
supervisor that puts her back up.

An always-on companion is only as always-on as the process holding her. Three
things make that claim true rather than aspirational:

**Identity.** The pid file is *held* — the running process keeps an exclusive
`flock` on it for its whole life. "Is she running?" is then answered by asking
whether anyone holds the lock, not by asking whether some number in a file
happens to name a live process. A recycled pid can't be mistaken for her, and
`yurios stop` can never signal a stranger who inherited it.

**A single start.** The same lock is the startup lock: two `yurios start`s
racing (an impatient hand, a login item, a shell alias) end with one daemon and
one honest "already running", never two servers fighting over the port and the
Vault.

**A reason.** When she goes down, the supervisor writes `last-exit.json` — exit
code or signal, whether it asked for that, and the tail of the log — then brings
her back with backoff, until a crash loop proves that restarting is not the
answer. `yurios status` reads that file, so "she's not running" always comes
with what happened.

Run directly (`python -m yurios.daemon`) this module *is* the supervisor; the
CLI launches it detached and never talks to the world server's pid itself.
"""
from __future__ import annotations

import datetime
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

try:                                  # POSIX only; Windows falls back below.
    import fcntl
except ImportError:                   # pragma: no cover - not a supported host
    fcntl = None                      # type: ignore[assignment]

_PID_RETRY_SECONDS = 0.05
_PID_RETRIES = 5
# A child that survives this long counts as a real run: the backoff resets and
# the crash-loop budget is refilled.
HEALTHY_SECONDS = 30.0
BACKOFF_SECONDS = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
MAX_FAST_FAILURES = 5
CHILD_STOP_TIMEOUT = 15.0
EXIT_TAIL_LINES = 20


def install_root() -> Path:
    """The installation these paths belong to, found without asking cwd.

    One installation is one `.env`, one Vault, one `.yurios/` lock — and the
    command that addresses it lives on `$PATH`, so it gets typed from wherever
    the shell happens to stand. Standing in the wrong place used to mean
    addressing a different, empty installation: no `.env` (so no model), a
    `./vault` that isn't there, and a boot that fails on data it was never going
    to find.

    So the executing package names the installation instead. `install.sh` links
    the launcher into the install's own venv and installs the project in place,
    which puts `yurios/` inside the project directory — the one above this file.
    A non-editable install has no project above it, and there the working
    directory is still the best answer available. `YURIOS_ROOT` overrides both,
    for a second checkout or a test.
    """
    override = os.environ.get("YURIOS_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    project = Path(__file__).resolve().parent.parent
    if (project / "pyproject.toml").exists():
        return project
    return Path.cwd()


def runtime_dir(root: Path) -> Path:
    return root / ".yurios"


def pid_path(root: Path) -> Path:
    return runtime_dir(root) / "yurios.pid"


def log_path(root: Path) -> Path:
    return runtime_dir(root) / "yurios.log"


def exit_path(root: Path) -> Path:
    return runtime_dir(root) / "last-exit.json"


class Lock:
    """An open pid file whose lock this process holds until it exits."""

    def __init__(self, fd: int, path: Path) -> None:
        self._fd = fd
        self.path = path

    def release(self) -> None:
        if self._fd < 0:
            return
        # Unlink first: while the lock is still held nobody can be mid-acquire,
        # so no other daemon's file can be removed by mistake.
        self.path.unlink(missing_ok=True)
        try:
            if fcntl is not None:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = -1


def acquire(root: Path) -> Lock | None:
    """Take the runtime lock and record this pid, or return None if she's up.

    The lock lives on the pid file itself and is released by the kernel however
    this process dies — crash, SIGKILL, power loss — so a stale file can never
    keep the next start out.
    """
    path = pid_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        elif running_pid(path) is not None:   # best effort without flock
            os.close(fd)
            return None
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        os.fsync(fd)
    except OSError:
        os.close(fd)
        return None
    return Lock(fd, path)


def running_pid(path: Path) -> int | None:
    """The pid of the live daemon, or None when nothing is holding the runtime.

    Held-lock, not pid-exists: the number in an abandoned file may belong to
    anyone by now, and signalling that stranger is the bug this closes.
    """
    if fcntl is None:                                    # pragma: no cover
        return _pid_if_alive(path)
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            pass                                         # someone holds it: she's up
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return None                                  # stale file, no owner
        # The holder writes its pid immediately after locking; a read that lands
        # inside that window sees an empty file rather than a missing daemon.
        for _ in range(_PID_RETRIES):
            try:
                pid = int(os.pread(fd, 32, 0).decode().strip())
            except ValueError:
                time.sleep(_PID_RETRY_SECONDS)
                continue
            return pid
        return None
    finally:
        os.close(fd)


def unlocked_pid(path: Path) -> int | None:
    """A daemon started before this file was a lock (an upgrade in place), or
    None. Signalling it is still safe: the pid has to name a live process that
    really is a YuriOS server, so a recycled number can't be mistaken for her.
    """
    pid = _pid_if_alive(path)
    if pid is None or pid == os.getpid():
        return None
    return pid if _looks_like_yurios(pid) else None


SERVER_TOKENS = ("yurios.world", "yurios.daemon", "yurios")


def _looks_like_yurios(pid: int) -> bool:
    """Whether this pid is running a YuriOS server, by its command line.

    Deliberately narrow — a token, not a substring: the installation path itself
    is full of the word, and "the directory is named YuriOS" is not grounds for
    sending anything SIGTERM.
    """
    try:
        command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
    except OSError:
        try:                               # macOS and anything else with ps(1)
            done = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                                  capture_output=True, timeout=5)
        except (OSError, subprocess.SubprocessError):    # pragma: no cover
            return False
        command = done.stdout
    tokens = command.decode("utf-8", "replace").split()
    return any(token in SERVER_TOKENS or token.rsplit("/", 1)[-1] in SERVER_TOKENS
               for token in tokens)


def _pid_if_alive(path: Path) -> int | None:
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return pid


def tail(path: Path, lines: int) -> str:
    """The last `lines` lines of a file, read from the end.

    Her log is append-only for the life of an installation; `yurios log` used to
    load all of it into memory to print the last screenful.
    """
    if lines <= 0:
        return ""
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        end = fh.tell()
        found = 0
        pos = end
        while pos > 0 and found <= lines:
            step = min(8192, pos)
            pos -= step
            fh.seek(pos)
            found += fh.read(step).count(b"\n")
        fh.seek(pos)
        data = fh.read(end - pos)
    text = data.decode("utf-8", "replace")
    return "".join(text.splitlines(keepends=True)[-lines:])


def _now() -> str:
    # Local time with its offset: these lines are read by a person looking at a
    # machine, next to log lines that are already local.
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def describe_exit(code: int) -> str:
    """`Popen.returncode` as something a person can read."""
    if code < 0:
        try:
            name = signal.Signals(-code).name
        except ValueError:                     # pragma: no cover - unknown signal
            name = f"signal {-code}"
        return f"killed by {name}"
    return f"exit status {code}" if code else "exited cleanly"


def record_exit(root: Path, *, pid: int, code: int, requested: bool,
                restarting: bool, detail: str = "") -> dict:
    """Persist why she went down, with enough log to act on it."""
    record = {
        "at": _now(),
        "pid": pid,
        "code": code,
        "reason": describe_exit(code),
        "requested": requested,
        "restarting": restarting,
        "detail": detail,
        "log_tail": tail(log_path(root), EXIT_TAIL_LINES) if log_path(root).exists() else "",
    }
    path = exit_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record


def last_exit(root: Path) -> dict | None:
    try:
        record = json.loads(exit_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def exit_summary(record: dict | None) -> str | None:
    """One line for `yurios status` / a failed `yurios start`."""
    if not record:
        return None
    reason = str(record.get("reason") or "stopped")
    when = str(record.get("at") or "")
    if record.get("requested"):
        # uvicorn re-raises the signal it shut down on, so a clean stop still
        # reports as "killed by SIGTERM" — don't alarm anyone with it.
        graceful = record.get("code") in (0, -signal.SIGTERM, -signal.SIGINT)
        line = "stopped on request" if graceful else f"stopped on request ({reason})"
    else:
        line = reason
    # "restarting in 4s" was true when it was written and is stale by the time
    # anyone reads it; the standing reasons (a crash loop she gave up on) aren't.
    if record.get("detail") and not record.get("restarting"):
        line += f"; {record['detail']}"
    return f"{line} at {when}" if when else line


class Supervisor:
    """Keeps `python -m yurios.world` running, and says why when it doesn't.

    Restarting is the right answer to a segfault, an OOM kill, or a provider
    that took the process down with it — and the wrong answer to a config that
    can never boot. So: exponential backoff, and after `MAX_FAST_FAILURES`
    deaths that never reached `HEALTHY_SECONDS`, it stops and leaves the reason
    on disk instead of burning the machine restarting forever.
    """

    def __init__(self, root: Path, argv: list[str] | None = None) -> None:
        self.root = root
        self.argv = list(argv or [])
        self.child: subprocess.Popen | None = None
        self.stopping = False
        self.restarts = 0

    def _log(self, message: str) -> None:
        line = f"{_now()} yurios.daemon INFO: {message}\n"
        try:
            with log_path(self.root).open("a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError:                        # pragma: no cover - unwritable log
            pass

    def _handle_stop(self, signum, frame) -> None:  # noqa: ARG002 - signal API
        self.stopping = True
        child = self.child
        if child is not None and child.poll() is None:
            self._log(f"{signal.Signals(signum).name} received; stopping her")
            try:
                child.terminate()
            except OSError:                    # pragma: no cover - already gone
                pass

    def _spawn(self):
        log = log_path(self.root)
        log.parent.mkdir(parents=True, exist_ok=True)
        handle = log.open("a", encoding="utf-8")
        try:
            return subprocess.Popen(
                [sys.executable, "-m", "yurios.world", *self.argv], cwd=self.root,
                stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT), handle
        except OSError:
            handle.close()
            raise

    def _wait(self, child: subprocess.Popen) -> int:
        # Polled rather than blocking: a SIGTERM the child ignores has to end in
        # SIGKILL, and that deadline only exists if the supervisor keeps looking.
        deadline: float | None = None
        while True:
            try:
                return child.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                pass
            except KeyboardInterrupt:          # pragma: no cover - attached run
                self._handle_stop(signal.SIGINT, None)
            if not self.stopping:
                continue
            if deadline is None:
                deadline = time.monotonic() + CHILD_STOP_TIMEOUT
            elif time.monotonic() > deadline:
                self._log("she did not stop in time; killing her")
                child.kill()
                return child.wait()

    def run(self) -> int:
        lock = acquire(self.root)
        if lock is None:
            pid = running_pid(pid_path(self.root))
            print(f"YuriOS is already running{f' (pid {pid})' if pid else ''}.",
                  file=sys.stderr)
            return 1
        signal.signal(signal.SIGTERM, self._handle_stop)
        signal.signal(signal.SIGINT, self._handle_stop)
        fast_failures = 0
        try:
            while True:
                child, handle = self._spawn()
                self.child = child
                started = time.monotonic()
                self._log(f"started her (pid {child.pid})")
                code = self._wait(child)
                handle.close()
                self.child = None
                lived = time.monotonic() - started
                if self.stopping:
                    record_exit(self.root, pid=child.pid, code=code, requested=True,
                                restarting=False)
                    self._log(f"she stopped ({describe_exit(code)})")
                    return 0
                if lived >= HEALTHY_SECONDS:
                    fast_failures = 0
                else:
                    fast_failures += 1
                if fast_failures > MAX_FAST_FAILURES:
                    detail = (f"{fast_failures} starts in a row died within "
                              f"{HEALTHY_SECONDS:.0f}s; not restarting")
                    record_exit(self.root, pid=child.pid, code=code, requested=False,
                                restarting=False, detail=detail)
                    self._log(f"she keeps dying: {detail}. See the log above.")
                    return 1
                delay = BACKOFF_SECONDS[min(fast_failures, len(BACKOFF_SECONDS) - 1)] \
                    if fast_failures else 0.0
                record_exit(self.root, pid=child.pid, code=code, requested=False,
                            restarting=True,
                            detail=f"restarting in {delay:.0f}s" if delay else "restarting")
                self.restarts += 1
                self._log(f"she went down ({describe_exit(code)}); "
                          f"restart {self.restarts} in {delay:.0f}s")
                end = time.monotonic() + delay
                while not self.stopping and time.monotonic() < end:
                    time.sleep(min(0.25, max(0.0, end - time.monotonic())))
                if self.stopping:
                    return 0
        finally:
            lock.release()


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="python -m yurios.daemon",
        description="supervise `python -m yurios.world`, restarting it when it dies")
    ap.add_argument("--root", default=None,
                    help="installation directory (default: the installation this "
                         "package belongs to)")
    ap.add_argument("server", nargs="*", help="arguments passed through to yurios.world")
    args = ap.parse_args(argv)
    root = Path(args.root).resolve() if args.root else install_root()
    return Supervisor(root, args.server).run()


if __name__ == "__main__":
    raise SystemExit(main())
