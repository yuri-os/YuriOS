#!/usr/bin/env bash
# Does this thing still install?
#
# `pytest` runs against the working tree, so it answers a different question: does
# the code work. Everything between the code and a stranger's machine — the wheel's
# contents, the console script, the package data, and above all whether the declared
# dependency set still RESOLVES — is invisible to it. That gap is where this
# project's worst failures have lived, and every one of them was silent:
#
#   · litellm's Python ceiling, hit on a 3.14 venv: the resolver backtracked through
#     years of releases and either took forever or landed on something ancient.
#   · mcp 2.0.0 renaming `mcp.server.fastmcp`: `import mcp` still worked, so the
#     doctor said her tools were fine, and the only symptom was the spawned server
#     dying on its import line — "she has no hands this run".
#   · a package-data file dropped from the wheel: the tests read it off the working
#     tree and never notice it did not ship.
#
# None of those are visible from a checkout, and all of them are one command away
# from being visible here.
#
# Two depths, because the honest check is expensive:
#
#   (default)  build the wheel, read what is inside it, install it WITHOUT its
#              dependencies into a throwaway venv, and import it. Then ask the
#              resolver — without downloading any of them — whether the full
#              declared set can still be satisfied on this Python. Under a minute;
#              this is the one to run before a commit.
#   --full     actually install those dependencies and drive the console script.
#              Downloads gigabytes (torch, CPU build). Run before a release, or
#              after touching dependencies.
#
# The venv is thrown away on exit; nothing here touches the working tree, the repo's
# own .venv, or your .env — the checks run from the temp directory precisely so that
# `import yurios`'s dotenv read (yurios/__init__.py) finds no .env to read.
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FULL=false
KEEP=false
OFFLINE=false
# Empty = whichever Python uv picks. `--python 3.11` is the interesting one: it is
# the floor in requires-python, uv fetches it if it is not installed, and it is the
# version nobody develops on and half the distros ship.
PYTHON_SPEC=""

usage() {
    cat <<'EOF'
Usage: ./scripts/smoke_install.sh [options]

Build the wheel and check that it installs and imports.

  --full            Also install the real dependency set and run the console
                    script. Downloads gigabytes; takes minutes, not seconds.
  --python X.Y      Build and install against this Python (e.g. 3.11, the floor
                    in requires-python). uv fetches it if it is not installed.
  --offline         Skip the checks that need a package index (the resolve, and
                    --full's install), and run the rest from uv's cache alone.
                    Leaves the wheel's own contents, which is the half that
                    catches a packaging mistake.
  --keep            Leave the temp venv on disk and print where it is.
  -h, --help        Show this help
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --full)      FULL=true ;;
        --offline)   OFFLINE=true ;;
        --keep)      KEEP=true ;;
        --python)    [ $# -ge 2 ] || { printf 'Error: --python needs a version, e.g. --python 3.11\n' >&2; exit 1; }
                     PYTHON_SPEC="$2"; shift ;;
        --python=*)  PYTHON_SPEC="${1#*=}" ;;
        -h|--help)   usage; exit 0 ;;
        *)           printf 'Error: unknown option %s\n' "$1" >&2; usage >&2; exit 1 ;;
    esac
    shift
done

log()  { printf '\n==> %s\n' "$*"; }
fail() { printf 'Error: %s\n' "$*" >&2; exit 1; }

command -v uv >/dev/null 2>&1 \
    || fail "uv is not installed — see https://docs.astral.sh/uv/ (or: pip install uv)"

# --offline has to reach uv itself, not just the stages this script skips: building
# the wheel and installing it both resolve *something* (the build backend,
# python-dotenv), and would go to the index for it. UV_OFFLINE makes uv use only what
# it has cached, so an offline run either passes on the cache or says it cannot.
if [ "$OFFLINE" = true ]; then
    export UV_OFFLINE=1
fi

WORK="$(mktemp -d)"
cleanup() {
    if [ "$KEEP" = true ]; then
        printf '\nLeft behind: %s\n' "$WORK"
    else
        rm -rf "$WORK"
    fi
}
trap cleanup EXIT

PY_ARGS=()
if [ -n "$PYTHON_SPEC" ]; then
    PY_ARGS=(--python "$PYTHON_SPEC")
fi

# --- 1. the wheel -----------------------------------------------------------------
# The wheel lands in the temp dir, not the repo's dist/, so a run of this script does
# not quietly become the artifact somebody ships. (setuptools still uses its usual
# build/ scratch directory inside the checkout — gitignored, and the same one every
# other build here uses.) The build log is kept and only printed if it failed.
log "Building the wheel"
uv build --wheel --out-dir "$WORK/dist" "${PY_ARGS[@]}" "$ROOT_DIR" >"$WORK/build.log" 2>&1 \
    || { cat "$WORK/build.log" >&2; fail "the wheel would not build"; }
WHEEL="$(find "$WORK/dist" -name '*.whl' | head -1)"
[ -n "$WHEEL" ] || fail "no wheel came out of the build"
printf '    %s\n' "$(basename "$WHEEL")"

log "Making a throwaway venv"
uv venv "${PY_ARGS[@]}" "$WORK/venv" >/dev/null 2>&1 || fail "could not create the venv"
VENV_PY="$WORK/venv/bin/python"
[ -x "$VENV_PY" ] || VENV_PY="$WORK/venv/Scripts/python.exe"   # Git Bash on Windows
[ -x "$VENV_PY" ] || fail "the venv has no python in it"
printf '    %s\n' "$("$VENV_PY" --version)"

# --- 2. what is inside the wheel --------------------------------------------------
# Package data is declared in [tool.setuptools.package-data] and read off the working
# tree by every test, so a file dropped from that list is invisible until an
# installed copy goes looking for it. These four have no fallback: her voice's
# cloning clip, her look, the selfie templates, and the portrait a first-run
# character starts with. (Read with Python's zipfile, not unzip, which is not
# installed everywhere this has to run.)
log "Reading the wheel"
"$VENV_PY" - "$WHEEL" <<'PY' || fail "the wheel is not shaped like an install"
import sys, zipfile

names = set(zipfile.ZipFile(sys.argv[1]).namelist())
missing = [n for n in [
    "yurios/desktop/voice/assets/designed.wav",
    "yurios/forge/characters/yuri.yaml",
    "yurios/forge/templates/selfie.yaml",
    "yurios/characters/assets/default-portrait.png",
] if n not in names]
if missing:
    sys.exit("missing from the wheel (check [tool.setuptools.package-data]): "
             + ", ".join(missing))
if not any(n.endswith("dist-info/entry_points.txt") for n in names):
    sys.exit("the wheel declares no console script")
print("    package data and the console script are in the wheel")
PY

# --- 3. does it import ------------------------------------------------------------
# --no-deps, so this stays seconds rather than gigabytes. python-dotenv is the one
# import yurios/__init__.py itself makes, and what it does with it is the check
# below: the local-first defaults (litellm's price-map fetch, HF's telemetry) are set
# in the package every entry point imports, and a stranger's install is exactly where
# nobody would notice they had stopped being set.
log "Installing the wheel (no dependencies)"
VIRTUAL_ENV="$WORK/venv" uv pip install --quiet --no-deps "$WHEEL" python-dotenv \
    || fail "the wheel would not install"

# Run from $WORK, so the dotenv read in yurios/__init__.py finds no .env — the
# developer's own keys must not be what makes this pass.
( cd "$WORK" && "$VENV_PY" - <<'PY' ) || fail "the installed package does not import"
import os
from pathlib import Path

import yurios

assert os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] == "True", "litellm would fetch its price map on import"
assert os.environ["HF_HUB_DISABLE_TELEMETRY"] == "1", "Hugging Face downloads would carry telemetry"

# The same four files, this time on disk where an installed copy will look for them.
# Walked as paths rather than through importlib.resources, which would have to import
# yurios.forge and yurios.desktop — and those pull yaml, httpx, fastapi, none of which
# are installed here. This stage is about the wheel's contents, not its dependencies.
installed = Path(yurios.__file__).parent
for rel in ["desktop/voice/assets/designed.wav",
            "forge/characters/yuri.yaml",
            "forge/templates/selfie.yaml",
            "characters/assets/default-portrait.png"]:
    if not (installed / rel).is_file():
        raise SystemExit(f"yurios/{rel} did not survive the install")

print("    imported yurios, and its local-first defaults are set")
PY

# --- 4. does the declared set still resolve ---------------------------------------
# The check the pyproject comments were written for. No download and no install —
# just: given these declarations, on this Python, is there a solution at all? This is
# what would have caught litellm's ceiling and mcp's 2.0 rename where they were
# introduced, rather than on a stranger's machine.
if [ "$OFFLINE" = true ]; then
    log "Skipping the resolve (--offline)"
else
    log "Resolving the declared dependency set"
    if [ -f "$ROOT_DIR/constraints.txt" ]; then
        # If the pins exist, resolve WITH them: a constraints file that no longer
        # admits a solution is worse than none, because it fails quietly — install.sh
        # simply stops using it.
        uv pip compile --quiet "${PY_ARGS[@]}" -c "$ROOT_DIR/constraints.txt" \
            -o "$WORK/resolved.txt" "$ROOT_DIR/pyproject.toml" \
            || fail "the base dependencies do not resolve against constraints.txt — regenerate it: ./scripts/pin_deps.sh"
        printf '    base dependencies resolve, and constraints.txt still admits them\n'
    else
        uv pip compile --quiet "${PY_ARGS[@]}" \
            -o "$WORK/resolved.txt" "$ROOT_DIR/pyproject.toml" \
            || fail "the base dependencies do not resolve"
        printf '    base dependencies resolve\n'
    fi
    # What a default ./install.sh actually resolves to, and where the caps earn their
    # keep (numba/numpy, litellm's ceiling, mcp's).
    uv pip compile --quiet "${PY_ARGS[@]}" \
        --extra test --extra llm --extra voice --extra forge-local \
        -o "$WORK/resolved-default.txt" "$ROOT_DIR/pyproject.toml" \
        || fail "the default install's extras do not resolve: [test,llm,voice,forge-local]"
    printf '    the default install'"'"'s extras resolve\n'
fi

# --- 5. the real thing ------------------------------------------------------------
if [ "$FULL" = true ] && [ "$OFFLINE" = false ]; then
    log "Installing the dependencies for real (this pulls torch — minutes, and gigabytes)"
    # CPU torch first, so what follows reuses it: 747 MB instead of 4.5 GB. The same
    # order pyproject documents and install.sh follows. torchaudio has to be on this
    # line too — PyPI's is built against CUDA torch, and without a matching build the
    # voice seams degrade to fakes while pip calls the install fine.
    VIRTUAL_ENV="$WORK/venv" uv pip install --quiet torch torchaudio \
        --index-url https://download.pytorch.org/whl/cpu \
        || fail "CPU torch would not install"
    # With the pins if this checkout has them — the same install --pinned describes.
    PIN_ARGS=()
    if [ -f "$ROOT_DIR/constraints.txt" ]; then
        PIN_ARGS=(--constraints "$ROOT_DIR/constraints.txt")
    fi
    VIRTUAL_ENV="$WORK/venv" uv pip install --quiet "${PIN_ARGS[@]}" "$WHEEL" \
        || fail "the dependencies would not install"

    log "Driving the console script"
    YURIOS_BIN="$WORK/venv/bin/yurios"
    [ -x "$YURIOS_BIN" ] || YURIOS_BIN="$WORK/venv/Scripts/yurios.exe"
    ( cd "$WORK" && "$YURIOS_BIN" --help >/dev/null ) || fail "\`yurios --help\` does not run"
    printf '    yurios --help runs\n'

    # The imports a start actually makes. Each of these sits behind a seam that may
    # fall back to a fake at RUNTIME — but not to being unimportable.
    ( cd "$WORK" && "$VENV_PY" - <<'PY' ) || fail "an installed module does not import"
import importlib

for name in ["yurios.cli", "yurios.doctor", "yurios.world.host", "yurios.world.main",
             "yurios.mind.loop", "yurios.world.tools.client", "yurios.desktop.main",
             "yurios.app.memory.store", "yurios.forge"]:
    importlib.import_module(name)
    print(f"    {name}")
PY
elif [ "$FULL" = true ]; then
    log "Skipping the real install (--offline)"
fi

log "The wheel installs and imports."
