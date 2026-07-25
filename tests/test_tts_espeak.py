"""The espeak-ng guard in front of the default voice (SPEC §3.3).

kokoro phonemises through misaki → phonemizer → espeak-ng, and espeak-ng answers a
phoneme-data directory it can't read by printing one line and calling exit(1). That is
the one failure the `_graceful` seam cannot catch: there is no exception, so the server
does not degrade to the fake — it stops, mid-boot, with the page never served.

It is not hypothetical. A fresh `./install.sh` hits it: the `espeakng-loader` wheel
hands espeak-ng a path that loses its own resolution race, and the library falls back
to an absolute path baked in at build time — a CI checkout under /home/runner. So the
backend probes espeak in a child process first and repairs or refuses. These pin that
behaviour without kokoro, espeak-ng or a subprocess anywhere near the suite.
"""
from __future__ import annotations

import subprocess

import pytest

from yurios.desktop.voice.backends import tts_kokoro


def test_a_working_espeak_is_left_completely_alone(monkeypatch):
    """The common case: probe passes, so nothing is set and nothing is logged. The
    environment belongs to the user, and their working install must stay theirs."""
    monkeypatch.delenv("ESPEAK_DATA_PATH", raising=False)
    monkeypatch.setattr(tts_kokoro, "_espeak_ok", lambda data_dir=None: True)
    monkeypatch.setattr(tts_kokoro, "_system_espeak_data",
                        lambda: pytest.fail("must not look for a fix when nothing is broken"))

    tts_kokoro.ensure_espeak()
    assert "ESPEAK_DATA_PATH" not in tts_kokoro.os.environ


def test_a_broken_espeak_is_repaired_from_the_system_install(monkeypatch, caplog, tmp_path):
    """The install.sh case: the wheel's data doesn't resolve, the apt/brew package's
    does. Repairing it beats a silent Yuri, and it says so at WARNING."""
    monkeypatch.delenv("ESPEAK_DATA_PATH", raising=False)
    system = tmp_path / "espeak-ng-data"
    system.mkdir()
    monkeypatch.setattr(tts_kokoro, "_system_espeak_data", lambda: system)
    # Only the second probe — the one carrying the system dir — succeeds.
    monkeypatch.setattr(tts_kokoro, "_espeak_ok", lambda data_dir=None: data_dir == system)

    with caplog.at_level("WARNING"):
        tts_kokoro.ensure_espeak()
    assert tts_kokoro.os.environ["ESPEAK_DATA_PATH"] == str(system)
    assert "espeak-ng" in caplog.text


def test_no_usable_espeak_refuses_instead_of_dying(monkeypatch):
    """The point of the whole guard: raise, so `_graceful` swaps in the fake and the
    server boots silent-but-alive. Returning normally here would hand the process to
    espeak-ng's exit(1)."""
    monkeypatch.setattr(tts_kokoro, "_espeak_ok", lambda data_dir=None: False)
    monkeypatch.setattr(tts_kokoro, "_system_espeak_data", lambda: None)

    with pytest.raises(RuntimeError, match="espeak-ng"):
        tts_kokoro.ensure_espeak()


def test_an_explicit_data_path_that_works_is_not_second_guessed(monkeypatch):
    """ESPEAK_DATA_PATH is a documented knob. If the probe passes with it, it stands —
    even when a system install exists that we'd otherwise prefer."""
    monkeypatch.setenv("ESPEAK_DATA_PATH", "/somewhere/of/my/own")
    monkeypatch.setattr(tts_kokoro, "_espeak_ok", lambda data_dir=None: data_dir is None)
    monkeypatch.setattr(tts_kokoro, "_system_espeak_data", lambda: None)

    tts_kokoro.ensure_espeak()
    assert tts_kokoro.os.environ["ESPEAK_DATA_PATH"] == "/somewhere/of/my/own"


def test_the_system_data_dir_comes_from_espeak_ngs_own_answer(monkeypatch, tmp_path):
    """`espeak-ng --version` prints the directory it uses, which beats guessing at
    /usr/lib/<triple>/ or /usr/share across Debian, Fedora, Arch and Homebrew. A
    directory without `phontab` is not one, whatever the binary claims."""
    data = tmp_path / "espeak-ng-data"
    data.mkdir()
    version_line = f"eSpeak NG text-to-speech: 1.51  Data at: {data}\n"
    monkeypatch.setattr(tts_kokoro.shutil, "which",
                        lambda exe: "/usr/bin/espeak-ng" if exe == "espeak-ng" else None)
    monkeypatch.setattr(tts_kokoro.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, version_line, ""))

    assert tts_kokoro._system_espeak_data() is None      # no phontab yet
    (data / "phontab").write_bytes(b"\0")
    assert tts_kokoro._system_espeak_data() == data

    monkeypatch.setattr(tts_kokoro.shutil, "which", lambda exe: None)
    assert tts_kokoro._system_espeak_data() is None      # espeak-ng isn't installed


def test_the_probe_runs_espeak_in_a_child_process(monkeypatch):
    """Because in-process there is nothing to catch. The probe must be a real
    subprocess, must pass the environment (that's how ESPEAK_DATA_PATH reaches it),
    and a non-zero exit — espeak-ng's way of failing — must read as "not ok"."""
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"], seen["env"] = cmd, kwargs.get("env", {})
        return subprocess.CompletedProcess(
            cmd, 0 if seen["env"].get("ESPEAK_DATA_PATH") else 1)

    monkeypatch.setattr(tts_kokoro.subprocess, "run", fake_run)
    assert tts_kokoro._espeak_ok(data_dir="/data") is True
    assert seen["cmd"][0] == tts_kokoro.sys.executable and seen["cmd"][1] == "-c"
    assert "EspeakWrapper()" in seen["cmd"][2]
    assert seen["env"]["ESPEAK_DATA_PATH"] == "/data"

    monkeypatch.delenv("ESPEAK_DATA_PATH", raising=False)
    assert tts_kokoro._espeak_ok() is False              # exit(1) is not "fine"
