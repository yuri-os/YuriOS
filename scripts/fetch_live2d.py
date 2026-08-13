#!/usr/bin/env python3
"""Populate web/live2d/vendor/ with the Live2D runtime + the Hiyori model.

Adapted from the book's `fetch_avatar.py` (SPEC §6.6): the Live2D client lives
under web/live2d/ here, next to the VRM frontend, so the destination and the
closing hint differ. See web/live2d/README.md.

None of this is committed to git — it is third-party, and two pieces are
proprietary-but-free-to-use, so the reference impl *fetches* them rather than
redistributing them (exactly how AIRI keeps them in a .cache). Run once:

    python scripts/fetch_live2d.py                 # model from the local AIRI checkout
    python scripts/fetch_live2d.py --model-zip path/to/hiyori_free_zh.zip

What lands in web/live2d/vendor/:
  live2dcubismcore.min.js   Live2D Cubism Core — PROPRIETARY, free under the
                            Live2D Proprietary Software License for businesses
                            under ¥10M JPY annual revenue; larger orgs need a
                            Cubism SDK Release License. (live2d.com)
  pixi.min.js               PixiJS v6 — MIT
  index.min.js              pixi-live2d-display — MIT
  hiyori/runtime/…          Hiyori Free — a Live2D sample model. Free for
                            individuals and small businesses under Live2D's
                            "Free Material" license; illustration by Kani Biimu,
                            model by Live2D. (live2d.com/en/learn/sample)

If a download fails (offline, CDN moved), the app still runs voice-only — the
avatar is skipped when vendor/ is empty (web/live2d/avatar.js says so out loud).
"""
from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

VENDOR = Path(__file__).resolve().parent.parent / "web" / "live2d" / "vendor"

# PixiJS v6 is required by pixi-live2d-display (it targets Pixi v6, not v7+).
# Use the Cubism-4-ONLY build (cubism4.min.js): the combined index.min.js bundle
# also demands the old Cubism 2 runtime (live2d.min.js) and throws at load without
# it, leaving PIXI.live2d.Live2DModel undefined. Hiyori is a Cubism 4 (.moc3) model,
# so cubism4.min.js needs only live2dcubismcore.min.js, which we fetch below.
JS = {
    "pixi.min.js": "https://cdnjs.cloudflare.com/ajax/libs/pixi.js/6.5.10/browser/pixi.min.js",
    "cubism4.min.js": "https://cdn.jsdelivr.net/npm/pixi-live2d-display@0.4.0/dist/cubism4.min.js",
    "live2dcubismcore.min.js": "https://cubism.live2d.com/sdk-web/cubismcore/live2dcubismcore.min.js",
}

# The user pointed at a local AIRI checkout as the model source (it ships the
# Hiyori Free sample). Default to it; override with --airi or --model-zip.
DEFAULT_AIRI = Path("/mnt/6870C6B170C68572/AI/airi")
AIRI_HIYORI_ZIP = "packages/stage-ui/src/assets/live2d/models/hiyori_free_zh.zip"

# AIRI also vendors the Cubism SDK for Web, whose Samples/Resources ship several
# free sample rigs. We copy them in as *alternative* bodies (pick one with
# AVATAR_MODEL in .env, → desktop/avatar_models.py). Each is a directory of a
# .moc3 + textures + motions; we mirror it to web/vendor/<key>/runtime/. These
# are Live2D sample material (free to use; see live2d.com/en/learn/sample), kept
# out of git like everything else in web/vendor/ (§8.2). key → SDK sample folder:
AIRI_SDK_SAMPLES = "apps/stage-web/.cache/assets/js/CubismSdkForWeb-5-r.3/Samples/Resources"
SAMPLE_MODELS = {
    "haru": "Haru",
    "mao": "Mao",
    "mark": "Mark",
    "natori": "Natori",
    "rice": "Rice",
    "wanko": "Wanko",
}

# The prettier, modern female rigs. These aren't in the SDK's Samples/Resources —
# they live in Live2D's "Sample Data" collection (live2d.com/en/learn/sample), and
# we pull each straight from Live2D's CDN so no local checkout is needed. Same
# Free Material license as Hiyori (commercial OK under ¥10M JPY annual revenue).
# Note the moc3 versions: Miara is v3 (Cubism 4) and renders on our pinned
# pixi-live2d-display; Kei (v5) and Ren (v6) are Cubism 5 and rely on the current
# live2dcubismcore.min.js we fetch above — newer than the display lib's era, so
# they may need a Core/lib bump on some setups. key → (CDN zip stem, model3.json).
CDN_BASE = "https://cubism.live2d.com/sample-data/bin/{stem}/{stem}_en.zip"
CDN_SAMPLES = {
    "miara": ("miara", "miara_pro_t03.model3.json"),
    "kei":   ("kei",   "kei_basic_free.model3.json"),
    "ren":   ("ren",   "ren.model3.json"),
}

HIYORI_MODEL3 = "hiyori_free_t08.model3.json"

# Downloads and archives are untrusted input. These limits are deliberately well
# above the current runtime and sample sizes, but low enough to bound a bad CDN or
# a deceptively small ZIP before it fills the installation disk.
DOWNLOAD_CHUNK_SIZE = 64 * 1024
MAX_JS_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 4096
MAX_MEMBER_BYTES = 128 * 1024 * 1024
MAX_EXTRACTED_BYTES = 512 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200


def _content_length(response) -> int | None:
    value = response.headers.get("Content-Length")
    if value is None:
        return None
    try:
        length = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"invalid Content-Length: {value!r}") from exc
    if length < 0:
        raise RuntimeError(f"invalid Content-Length: {value!r}")
    return length


def _download(url: str, directory: Path, max_bytes: int, timeout: int) -> Path:
    """Stream *url* to a temporary file in *directory*, bounded by metadata and bytes."""
    directory.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    temp_path: Path | None = None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            declared = _content_length(response)
            if declared is not None and declared > max_bytes:
                raise RuntimeError(
                    f"download is {declared} bytes; limit is {max_bytes} bytes"
                )
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=".live2d-download-", dir=directory, delete=False
            ) as target:
                temp_path = Path(target.name)
                received = 0
                while chunk := response.read(
                    min(DOWNLOAD_CHUNK_SIZE, max_bytes - received + 1)
                ):
                    received += len(chunk)
                    if received > max_bytes:
                        raise RuntimeError(f"download exceeds {max_bytes} byte limit")
                    target.write(chunk)
            if declared is not None and received != declared:
                raise RuntimeError(
                    f"incomplete download: expected {declared} bytes, received {received}"
                )
        return temp_path
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise


def _download_atomic(url: str, dest: Path, max_bytes: int, timeout: int) -> None:
    temp_path = _download(url, dest.parent, max_bytes, timeout)
    try:
        os.replace(temp_path, dest)
    finally:
        temp_path.unlink(missing_ok=True)


def _safe_member_path(info: zipfile.ZipInfo) -> tuple[str, ...]:
    name = info.filename
    if not name or "\x00" in name or "\\" in name:
        raise RuntimeError(f"unsafe ZIP member path: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        raise RuntimeError(f"unsafe ZIP member path: {name!r}")
    parts = tuple(part for part in path.parts if part not in ("", "."))
    if not parts or any(":" in part for part in parts):
        raise RuntimeError(f"unsafe ZIP member path: {name!r}")
    return parts


def _member_is_dir(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    kind = stat.S_IFMT(mode)
    if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
        raise RuntimeError(f"ZIP member is a symlink or special file: {info.filename!r}")
    is_dir = info.is_dir() or kind == stat.S_IFDIR
    if is_dir and kind == stat.S_IFREG:
        raise RuntimeError(f"ZIP member has conflicting file type: {info.filename!r}")
    if not is_dir and kind == stat.S_IFDIR:
        raise RuntimeError(f"ZIP member has conflicting file type: {info.filename!r}")
    return is_dir


def _preflight_archive(
    archive: zipfile.ZipFile, expected_anchor: str
) -> list[tuple[zipfile.ZipInfo, tuple[str, ...]]]:
    infos = archive.infolist()
    if len(infos) > MAX_ARCHIVE_MEMBERS:
        raise RuntimeError(
            f"ZIP has {len(infos)} members; limit is {MAX_ARCHIVE_MEMBERS}"
        )

    seen: dict[str, tuple[str, ...]] = {}
    spellings: dict[str, tuple[str, ...]] = {}
    files: set[str] = set()
    directories: set[str] = set()
    checked: list[tuple[zipfile.ZipInfo, tuple[str, ...], bool]] = []
    total = 0
    for info in infos:
        parts = _safe_member_path(info)
        folded = "/".join(parts).casefold()
        for size in range(1, len(parts) + 1):
            spelling = parts[:size]
            spelling_key = "/".join(spelling).casefold()
            previous_spelling = spellings.get(spelling_key)
            if previous_spelling is not None and previous_spelling != spelling:
                raise RuntimeError(
                    f"case-colliding ZIP paths: {'/'.join(previous_spelling)!r} "
                    f"and {'/'.join(spelling)!r}"
                )
            spellings[spelling_key] = spelling
        previous = seen.get(folded)
        if previous is not None:
            if previous == parts:
                raise RuntimeError(f"duplicate ZIP member: {info.filename!r}")
            raise RuntimeError(
                f"case-colliding ZIP members: {'/'.join(previous)!r} and {info.filename!r}"
            )
        seen[folded] = parts

        is_dir = _member_is_dir(info)
        parent_keys = ["/".join(parts[:i]).casefold() for i in range(1, len(parts))]
        if any(parent in files for parent in parent_keys):
            raise RuntimeError(f"ZIP file/directory collision at {info.filename!r}")
        if is_dir:
            if folded in files:
                raise RuntimeError(f"ZIP file/directory collision at {info.filename!r}")
            directories.add(folded)
        else:
            if folded in directories:
                raise RuntimeError(f"ZIP file/directory collision at {info.filename!r}")
            files.add(folded)
            directories.update(parent_keys)
            if info.file_size > MAX_MEMBER_BYTES:
                raise RuntimeError(
                    f"ZIP member {info.filename!r} is {info.file_size} bytes; "
                    f"limit is {MAX_MEMBER_BYTES}"
                )
            total += info.file_size
            if total > MAX_EXTRACTED_BYTES:
                raise RuntimeError(
                    f"ZIP expands beyond {MAX_EXTRACTED_BYTES} byte aggregate limit"
                )
            ratio = info.file_size / max(info.compress_size, 1)
            if ratio > MAX_COMPRESSION_RATIO:
                raise RuntimeError(
                    f"ZIP member {info.filename!r} has compression ratio {ratio:.1f}; "
                    f"limit is {MAX_COMPRESSION_RATIO}"
                )
        checked.append((info, parts, is_dir))

    anchors = [
        parts
        for _info, parts, is_dir in checked
        if not is_dir and len(parts) >= 2
        and parts[-2] == "runtime" and parts[-1] == expected_anchor
    ]
    if len(anchors) != 1:
        raise RuntimeError(
            f"expected exactly one runtime/{expected_anchor} anchor in ZIP; "
            f"found {len(anchors)}"
        )
    prefix = anchors[0][:-1]
    selected = [
        (info, parts[len(prefix):])
        for info, parts, is_dir in checked
        if not is_dir and len(parts) > len(prefix) and parts[:len(prefix)] == prefix
    ]
    if not selected:
        raise RuntimeError(f"no files under selected runtime/{expected_anchor} anchor")
    return selected


def _atomic_replace_tree(staged: Path, dest: Path, work: Path) -> None:
    previous = work / "previous"
    had_previous = dest.exists() or dest.is_symlink()
    if had_previous:
        os.replace(dest, previous)
    try:
        os.replace(staged, dest)
    except Exception:
        if had_previous:
            os.replace(previous, dest)
        raise


def _install_archive(zip_path: Path, dest: Path, expected_anchor: str) -> int:
    if zip_path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise RuntimeError(
            f"ZIP is {zip_path.stat().st_size} bytes; limit is {MAX_ARCHIVE_BYTES}"
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f".{dest.name}-install-", dir=dest.parent))
    try:
        staged = work / "new"
        runtime = staged / "runtime"
        with zipfile.ZipFile(zip_path) as archive:
            selected = _preflight_archive(archive, expected_anchor)
            written_total = 0
            for info, relative_parts in selected:
                out = runtime.joinpath(*relative_parts)
                out.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                with archive.open(info) as source, out.open("xb") as target:
                    while chunk := source.read(DOWNLOAD_CHUNK_SIZE):
                        written += len(chunk)
                        written_total += len(chunk)
                        if written > MAX_MEMBER_BYTES or written_total > MAX_EXTRACTED_BYTES:
                            raise RuntimeError("ZIP exceeded extraction limits while streaming")
                        target.write(chunk)
                if written != info.file_size:
                    raise RuntimeError(
                        f"ZIP member {info.filename!r} size changed during extraction"
                    )
        _atomic_replace_tree(staged, dest, work)
        return len(selected)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def fetch_js() -> None:
    VENDOR.mkdir(parents=True, exist_ok=True)
    for name, url in JS.items():
        dest = VENDOR / name
        try:
            print(f"↓ {name}  ({url})")
            _download_atomic(url, dest, MAX_JS_BYTES, timeout=30)
        except Exception as e:
            print(f"  ! failed: {e} — the app will run voice-only until this exists",
                  file=sys.stderr)


def install_model(zip_path: Path) -> None:
    """Extract hiyori_free_zh/runtime/* → web/vendor/hiyori/runtime/*."""
    dest = VENDOR / "hiyori"
    count = _install_archive(zip_path, dest, HIYORI_MODEL3)
    print(f"✓ Hiyori installed → {dest}/runtime "
          f"({count} files) — model3: {HIYORI_MODEL3}")


def install_samples(airi: Path) -> None:
    """Mirror the Cubism SDK sample rigs → web/vendor/<key>/runtime/ (alt bodies)."""
    src_root = airi / AIRI_SDK_SAMPLES
    if not src_root.exists():
        print(f"  · no Cubism SDK samples at {src_root} — skipping alt models "
              f"(only Hiyori will be installed).", file=sys.stderr)
        return
    for key, folder in SAMPLE_MODELS.items():
        src = src_root / folder
        if not src.is_dir():
            print(f"  · sample {folder!r} missing — skipping.", file=sys.stderr)
            continue
        dest = VENDOR / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix=f".{key}-install-", dir=dest.parent))
        try:
            staged = work / "new"
            shutil.copytree(src, staged / "runtime")
            _atomic_replace_tree(staged, dest, work)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        print(f"✓ {folder} installed → {dest}/runtime  (AVATAR_MODEL={key})")


def install_cdn_samples() -> None:
    """Download the modern female rigs from Live2D's Sample Data CDN → vendor/<key>."""
    for key, (stem, model3) in CDN_SAMPLES.items():
        url = CDN_BASE.format(stem=stem)
        try:
            print(f"↓ {key}  ({url})")
            archive_path = _download(url, VENDOR, MAX_ARCHIVE_BYTES, timeout=60)
            try:
                count = _install_archive(archive_path, VENDOR / key, model3)
            finally:
                archive_path.unlink(missing_ok=True)
            print(f"✓ {key} installed → {VENDOR / key}/runtime  ({count} files, "
                  f"AVATAR_MODEL={key})")
        except Exception as e:
            print(f"  ! {key} failed: {e} — skipping (other rigs still install)",
                  file=sys.stderr)


def resolve_zip(args) -> Path | None:
    if args.model_zip:
        return Path(args.model_zip)
    cand = Path(args.airi) / AIRI_HIYORI_ZIP
    if cand.exists():
        return cand
    print(f"  ! Hiyori zip not found at {cand}.\n"
          f"    Point --model-zip at a hiyori_free_zh.zip, or download the Hiyori\n"
          f"    sample from https://www.live2d.com/en/learn/sample/ (Free Material).",
          file=sys.stderr)
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description="Fetch the Live2D avatar assets.")
    ap.add_argument("--airi", default=str(DEFAULT_AIRI),
                    help="path to a local AIRI checkout that ships the Hiyori sample")
    ap.add_argument("--model-zip", help="explicit path to a hiyori_free_zh.zip")
    ap.add_argument("--skip-js", action="store_true", help="only (re)install the model")
    ap.add_argument("--skip-samples", action="store_true",
                    help="install only Hiyori, not the Cubism SDK sample rigs")
    ap.add_argument("--skip-cdn-samples", action="store_true",
                    help="don't download the modern female rigs (miara/kei/ren)")
    args = ap.parse_args()

    if not args.skip_js:
        fetch_js()
    zip_path = resolve_zip(args)
    if zip_path and zip_path.exists():
        install_model(zip_path)
    if not args.skip_samples:
        install_samples(Path(args.airi))
    if not args.skip_cdn_samples:
        install_cdn_samples()
    print("\nDone. `python -m yurios.world` → open /live2d/ in a browser, or float her:\n"
          "`python -m yurios.world --window --body live2d`.\n"
          "Switch rigs with AVATAR_MODEL in .env (hiyori | miara | kei | ren | "
          "haru | mao | mark | natori | rice | wanko).\n"
          "Prettier female rigs: miara (safest), kei, ren — see avatar_models.py.\n"
          "Licenses: Cubism Core is Live2D-proprietary (free under ¥10M JPY revenue); "
          "Hiyori is a Live2D Free-Material sample. Neither is committed to git (§8.2).")


if __name__ == "__main__":
    main()
