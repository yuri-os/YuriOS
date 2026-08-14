#!/usr/bin/env bash
# Everything that can say no before a commit does, in one command.
#
#   ./scripts/check.sh              lint, typecheck, the Python suite, the web suite
#   ./scripts/check.sh --fast       …without the Python suite (it takes ~9 minutes)
#   ./scripts/check.sh --release    …plus the install smoke test, on the 3.11 floor
#
# There is no CI behind this. That is a choice, not an omission: YuriOS is installed
# from a checkout onto one machine, its test suite drives a voice stack and a GPU
# through fakes, and the thing most worth checking — that the wheel installs and its
# dependencies still resolve — is exactly what a hosted runner would have to fake
# hardest. So the gate runs here, on the machine that has the venv, and it runs all
# of it rather than a sampling.
#
# Every stage runs even if an earlier one failed, because "lint failed" should not
# hide "and eleven tests are broken". The exit code is the count of stages that
# failed, and the summary at the end names them.
set -uo pipefail   # NOT -e: a failing stage is data, not the end of the run

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR" || exit 1

FAST=false
RELEASE=false

usage() {
    cat <<'EOF'
Usage: ./scripts/check.sh [options]

  --fast      Skip the Python test suite (~9 minutes). Lint, typecheck and the
              web suite still run — seconds, and they catch most of what a
              half-finished edit does.
  --release   Also run scripts/smoke_install.sh against Python 3.11, the floor in
              requires-python: build the wheel, install it, resolve the declared
              dependency set. Run before tagging, or after touching dependencies.
  -h, --help  Show this help

Needs the dev tools: pip install -e ".[dev]"  (and `cd web && npm install`).
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --fast)    FAST=true ;;
        --release) RELEASE=true ;;
        -h|--help) usage; exit 0 ;;
        *)         printf 'Error: unknown option %s\n' "$1" >&2; usage >&2; exit 1 ;;
    esac
    shift
done

# The repo's own venv if there is one — the tools live there, and a global `ruff`
# would be checking this project with another project's version.
PY="$ROOT_DIR/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3 || command -v python)"
[ -n "$PY" ] || { printf 'Error: no python found\n' >&2; exit 1; }

FAILED=()
STAGE=""

start() { STAGE="$1"; printf '\n\033[1m==> %s\033[0m\n' "$1"; }
verdict() {
    if [ "$1" -eq 0 ]; then
        printf '    ok\n'
    else
        printf '    FAILED\n'
        FAILED+=("$STAGE")
    fi
}
# A tool that is not installed is not a pass. It is its own kind of failure, and
# saying which one it is beats a stage that silently did nothing.
missing() {
    printf '    NOT INSTALLED — %s\n' "$1"
    FAILED+=("$STAGE (not installed)")
}

# --- lint -------------------------------------------------------------------------
start "ruff"
if "$PY" -m ruff --version >/dev/null 2>&1; then
    "$PY" -m ruff check .
    verdict $?
elif command -v ruff >/dev/null 2>&1; then
    ruff check .
    verdict $?
else
    missing 'pip install -e ".[dev]"'
fi

# --- typecheck --------------------------------------------------------------------
# Green today only because [tool.mypy]'s overrides list the modules that predate it —
# see the comment there. New code is checked; that list may only shrink.
start "mypy"
if "$PY" -m mypy --version >/dev/null 2>&1; then
    "$PY" -m mypy
    verdict $?
else
    missing 'pip install -e ".[dev]"'
fi

# --- the Python suite -------------------------------------------------------------
# Offline by construction (SPEC §13): fake voice backends, a fake tool runner, an
# in-memory MCP session, a VirtualClock. It needs no model, no GPU and no network.
if [ "$FAST" = true ]; then
    printf '\n\033[1m==> pytest\033[0m\n    skipped (--fast)\n'
else
    start "pytest"
    "$PY" -m pytest -q
    verdict $?
fi

# --- the web suite ----------------------------------------------------------------
start "web tests"
if [ ! -d web/node_modules ]; then
    missing 'cd web && npm install'
elif ! command -v npm >/dev/null 2>&1; then
    missing 'install Node (install.sh does this via nvm)'
else
    ( cd web && npm test --silent )
    verdict $?
fi

# --- does it install --------------------------------------------------------------
if [ "$RELEASE" = true ]; then
    start "install smoke test"
    ./scripts/smoke_install.sh --python 3.11
    verdict $?
fi

# --- the verdict ------------------------------------------------------------------
printf '\n'
if [ ${#FAILED[@]} -eq 0 ]; then
    printf '\033[1mAll checks passed.\033[0m\n'
    [ "$FAST" = true ] && printf 'Note: the Python suite did not run (--fast).\n'
    [ "$RELEASE" = false ] && printf 'Before a release, run with --release for the install smoke test.\n'
    exit 0
fi
printf '\033[1m%d failed:\033[0m %s\n' "${#FAILED[@]}" "$(printf '%s; ' "${FAILED[@]}")"
exit "${#FAILED[@]}"
