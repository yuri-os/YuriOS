"""Exercise real parser process boundaries without a model or a live service."""
import io
import os
import sys

import pytest
from PIL import Image

from yurios import parsing
from yurios.parsing import ParseRejected, parse_file


def test_unsupported_decoder_is_refused(tmp_path):
    from yurios.world.uploads import UploadRejected, Uploads
    raw = io.BytesIO()
    Image.new("RGB", (4, 4)).save(raw, "TIFF")
    shelf = tmp_path / "shelf"
    with pytest.raises(UploadRejected):
        Uploads(shelf).save(raw.getvalue())
    assert not shelf.exists()


def test_timeout_kills_reaps_and_cleans_worker(monkeypatch, tmp_path):
    pid_file = tmp_path / "pid"
    scratch = []

    def command(kind, source, target, options):
        scratch.append(source.parent)
        return [sys.executable, "-c", "import os,time; from pathlib import Path; "
                f"Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(60)"]

    monkeypatch.setattr(parsing, "_command", command)
    monkeypatch.setattr(parsing, "WALL_TIMEOUT", 1)
    with pytest.raises(ParseRejected, match="time limit"):
        parse_file("pdf", b"malicious file")
    pid = int(pid_file.read_text())
    if sys.platform != "win32":
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    assert not scratch[0].exists()
    # A timed out worker must also release its admission slot.
    assert parsing._slots.acquire(blocking=False)
    parsing._slots.release()


@pytest.mark.parametrize("payload,limit", [(b"[]", 10), (b"x" * 11, 10),
                                          (b"\xff", 10)])
def test_invalid_or_oversized_worker_result_is_refused(monkeypatch, payload, limit):
    def command(kind, source, target, options):
        return [sys.executable, "-c", "from pathlib import Path; "
                f"Path({str(target)!r}).write_bytes({payload!r})"]

    monkeypatch.setattr(parsing, "_command", command)
    monkeypatch.setattr(parsing, "MAX_RESULT_BYTES", limit)
    with pytest.raises(ParseRejected):
        parse_file("pdf", b"input")


def test_busy_parsers_refuse_without_starting_another_process(monkeypatch):
    import threading
    monkeypatch.setattr(parsing, "_slots", threading.BoundedSemaphore(0))
    with pytest.raises(ParseRejected, match="busy"):
        parse_file("pdf", b"input")


@pytest.mark.skipif(sys.platform == "win32", reason="Unix resource limit probe")
def test_worker_enforces_memory_and_output_limits():
    import subprocess
    result = subprocess.run([sys.executable, "-c", '''
import resource
from yurios.parse_worker import _limit_resources, MEMORY_BYTES, OUTPUT_BYTES
_limit_resources()
assert resource.getrlimit(resource.RLIMIT_AS) == (MEMORY_BYTES, MEMORY_BYTES)
assert resource.getrlimit(resource.RLIMIT_FSIZE) == (OUTPUT_BYTES, OUTPUT_BYTES)
try:
    bytearray(MEMORY_BYTES + 1)
except MemoryError:
    pass
else:
    raise AssertionError("memory limit was not enforced")
'''], capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode()
