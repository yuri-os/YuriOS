"""Disposable parser entry point, never imported by the host (SPEC §11.6)."""
from __future__ import annotations

import base64
import io
import json
import sys
from pathlib import Path

MEMORY_BYTES = 1536 * 1024 * 1024
CPU_SECONDS = 10
OUTPUT_BYTES = 64 * 1024 * 1024
_job = None                       # keep the Windows job handle alive


def _limit_resources() -> None:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                        ("flags", wintypes.DWORD), ("min_working", ctypes.c_size_t),
                        ("max_working", ctypes.c_size_t), ("active", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                        ("scheduling", wintypes.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ("read_ops", "write_ops", "other_ops", "read", "write", "other")]

        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", IO), ("process_memory", ctypes.c_size_t),
                        ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t),
                        ("peak_job", ctypes.c_size_t)]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                   ctypes.c_void_p, wintypes.DWORD]
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        global _job
        _job = kernel.CreateJobObjectW(None, None)
        info = Extended()
        info.basic.flags = 0x100 | 0x2       # process memory + per-process CPU time
        info.process_memory = MEMORY_BYTES
        info.basic.process_time = CPU_SECONDS * 10_000_000
        if not _job or not kernel.SetInformationJobObject(_job, 9, ctypes.byref(info),
                                                         ctypes.sizeof(info)):
            raise RuntimeError("cannot limit the file parser")
        if not kernel.AssignProcessToJobObject(_job, kernel.GetCurrentProcess()):
            raise RuntimeError("cannot isolate the file parser")
    else:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES, MEMORY_BYTES))
        resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS + 1))
        resource.setrlimit(resource.RLIMIT_FSIZE, (OUTPUT_BYTES, OUTPUT_BYTES))


def _image(data: bytes, portrait: bool, options: dict) -> dict:
    from PIL import Image, ImageOps
    formats = ("PNG", "JPEG") if portrait else ("PNG", "JPEG", "WEBP", "GIF", "BMP")
    with Image.open(io.BytesIO(data), formats=formats) as image:
        if image.width * image.height > options["max_pixels"]:
            raise ValueError("that image is too large to open")
        if portrait and image.size != (options["width"], options["height"]):
            raise ValueError("portrait dimensions do not match its header")
        if portrait and image.format != options["source_format"]:
            raise ValueError("image format does not match its header")
        image.load()
        oriented = ImageOps.exif_transpose(image)
        alpha = oriented.mode in ("RGBA", "LA") or "transparency" in oriented.info
        oriented = oriented.convert("RGBA" if alpha else "RGB")
        if not portrait:
            oriented.thumbnail((options["max_px"], options["max_px"]), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        fmt = "PNG" if portrait or alpha else "JPEG"
        if fmt == "JPEG":
            oriented.save(output, fmt, quality=88, optimize=True)
        else:
            oriented.save(output, fmt, optimize=True)
        return {"image": base64.b64encode(output.getvalue()).decode("ascii"),
                "format": fmt, "source_format": image.format,
                "width": oriented.width, "height": oriented.height}


def main() -> None:
    kind, source, target, raw_options = sys.argv[1:]
    _limit_resources()
    try:
        data = Path(source).read_bytes()
        options = json.loads(raw_options)
        if kind == "pdf":
            from yurios.mind.pdftext import pdf_text
            result = {"text": pdf_text(data, title=options.get("title", ""))}
            if len(result["text"]) > 5_000_000:
                raise ValueError("PDF text exceeds the output size limit")
        elif kind in {"image", "portrait"}:
            result = _image(data, kind == "portrait", options)
        else:
            raise ValueError("unknown parser")
    except Exception as exc:
        result = {"error": str(exc)[:500] or "file could not be parsed"}
    encoded = json.dumps(result).encode("utf-8")
    if len(encoded) > OUTPUT_BYTES:
        encoded = b'{"error":"parsed file exceeds the output size limit"}'
    Path(target).write_bytes(encoded)


if __name__ == "__main__":
    main()
