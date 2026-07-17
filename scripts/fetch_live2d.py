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
import io
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

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


def fetch_js() -> None:
    VENDOR.mkdir(parents=True, exist_ok=True)
    for name, url in JS.items():
        dest = VENDOR / name
        try:
            print(f"↓ {name}  ({url})")
            # a real UA — Live2D's CDN 403s the default urllib agent
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                dest.write_bytes(r.read())
        except Exception as e:
            print(f"  ! failed: {e} — the app will run voice-only until this exists",
                  file=sys.stderr)


def install_model(zip_path: Path) -> None:
    """Extract hiyori_free_zh/runtime/* → web/vendor/hiyori/runtime/*."""
    dest = VENDOR / "hiyori"
    if dest.exists():
        shutil.rmtree(dest)
    with zipfile.ZipFile(zip_path) as z:
        members = [m for m in z.namelist() if "/runtime/" in m and not m.endswith("/")]
        if not members:
            raise RuntimeError(f"no runtime/ files in {zip_path}")
        for m in members:
            rel = m.split("/runtime/", 1)[1]           # strip the leading pkg dir
            out = dest / "runtime" / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(z.read(m))
    print(f"✓ Hiyori installed → {dest}/runtime "
          f"({len(members)} files) — model3: hiyori_free_t08.model3.json")


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
        dest = VENDOR / key / "runtime"
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest)
        print(f"✓ {folder} installed → {dest}  (AVATAR_MODEL={key})")


def install_cdn_samples() -> None:
    """Download the modern female rigs from Live2D's Sample Data CDN → vendor/<key>."""
    for key, (stem, model3) in CDN_SAMPLES.items():
        url = CDN_BASE.format(stem=stem)
        try:
            print(f"↓ {key}  ({url})")
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                blob = r.read()
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                # find the runtime/ folder that holds this model's model3.json
                anchor = next((m for m in z.namelist()
                               if m.endswith("/runtime/" + model3)), None)
                if anchor is None:
                    print(f"  ! {model3} not found in {stem} zip — skipping.",
                          file=sys.stderr)
                    continue
                prefix = anchor.rsplit("/runtime/", 1)[0] + "/runtime/"
                dest = VENDOR / key
                if dest.exists():
                    shutil.rmtree(dest)
                n = 0
                for m in z.namelist():
                    if not m.startswith(prefix) or m.endswith("/"):
                        continue
                    out = dest / "runtime" / m[len(prefix):]
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_bytes(z.read(m))
                    n += 1
                print(f"✓ {key} installed → {dest}/runtime  ({n} files, "
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
