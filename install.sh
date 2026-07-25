#!/usr/bin/env bash
set -Eeuo pipefail

PYTHON_VERSION="3.12"
NVM_VERSION="v0.40.3"
NODE_VERSION="22"
MODE="host"
INSTALL_DESKTOP=false
SKIP_SYSTEM=false
# Default to the thin install: ~270 MB, no torch, no CUDA, no model downloads. That
# is a complete YuriOS (body, brain, memory, tools, text chat) — the local voice
# backends are lazy imports that degrade to fakes with a printed install hint, so
# --voice is an upgrade, not a prerequisite. Opting everyone into the old
# "[all,test]" cost 6.2 GB, most of it a CUDA torch build nothing here runs.
INSTALL_VOICE=false
INSTALL_EMBED=false
INSTALL_GPU_VOICE=false
CPU_TORCH=true

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    cat <<'EOF'
Usage: ./install.sh [options]

Set up YuriOS on WSL, native Linux, or macOS.

By default this installs the base runtime only (~270 MB): her body, brain,
memory, MCP tools and text chat, with no torch, no CUDA and no model
downloads. Her voice seams fall back to fakes and say so on startup; add
them with --voice when you want them.

Options:
  --voice        Also install the local voice stack: faster-whisper ears,
                 the kokoro voice, silero turn-taking (CPU-only, ~700 MB)
  --local-embed  Also install sentence-transformers, for EMBED_BACKEND=sentence_tf
                 (not needed for the default LM Studio / Ollama embeddings)
  --gpu-voice    Also install qwen-tts, the designed voice — needs a CUDA GPU
                 (implies --voice, and skips the CPU-torch shortcut)
  --cuda-torch   Do not pre-install the CPU torch wheel on Linux; let the
                 extras pull PyPI's CUDA build (~4 GB more)
  --docker       Build the Docker Compose setup instead of a host environment
  --desktop      Also install the native transparent desktop-window dependencies
  --skip-system  Do not install system packages (git, curl, and — only with a
                 voice extra — espeak-ng and libsndfile)
  -h, --help     Show this help

Everything is additive and re-runnable: install thin now, rerun with --voice later.
`python -m yurios.doctor` reports what your .env selects vs what's installed.
EOF
}

log() {
    printf '\n==> %s\n' "$*"
}

fail() {
    printf 'Error: %s\n' "$*" >&2
    exit 1
}

for arg in "$@"; do
    case "$arg" in
        --docker) MODE="docker" ;;
        --desktop) INSTALL_DESKTOP=true ;;
        --voice) INSTALL_VOICE=true ;;
        --local-embed) INSTALL_EMBED=true ;;
        --gpu-voice) INSTALL_GPU_VOICE=true; INSTALL_VOICE=true; CPU_TORCH=false ;;
        --cuda-torch) CPU_TORCH=false ;;
        --skip-system) SKIP_SYSTEM=true ;;
        -h|--help) usage; exit 0 ;;
        *) fail "unknown option: $arg" ;;
    esac
done

if [ "$MODE" = "docker" ] && [ "$INSTALL_DESKTOP" = true ]; then
    fail "--desktop cannot be combined with --docker; the native window must run on the host"
fi

case "$(uname -s)" in
    Darwin)
        PLATFORM="macos"
        VENV_DIR="$ROOT_DIR/.venv"
        ;;
    Linux)
        if [ -r /proc/sys/kernel/osrelease ] && grep -qi microsoft /proc/sys/kernel/osrelease; then
            PLATFORM="wsl"
            VENV_DIR="$ROOT_DIR/.wvenv"
        else
            PLATFORM="linux"
            VENV_DIR="$ROOT_DIR/.venv"
        fi
        ;;
    *) fail "unsupported operating system: $(uname -s). On Windows, run this script inside WSL." ;;
esac

cd "$ROOT_DIR"
log "Detected $PLATFORM"

prepare_local_state() {
    if [ ! -f .env ]; then
        cp .env.example .env
        log "Created .env from .env.example"
    else
        log "Keeping existing .env"
    fi
}

install_docker() {
    [ -f compose.yaml ] || fail "no compose.yaml in this checkout — the Docker setup isn't part of it. Run ./install.sh without --docker for a host install."
    command -v docker >/dev/null 2>&1 || fail "Docker is not installed. Install Docker Desktop (WSL/macOS) or Docker Engine with the Compose plugin (Linux)."
    docker compose version >/dev/null 2>&1 || fail "the Docker Compose plugin is not available"

    prepare_local_state
    mkdir -p vault corpus traces tool-logs selfies
    log "Building the YuriOS Docker image"
    docker compose build
    printf '\nDocker setup is ready. Start YuriOS with:\n\n  docker compose up -d\n\nThen open http://localhost:8768. Follow logs with `docker compose logs -f`.\n'
}

if [ "$MODE" = "docker" ]; then
    install_docker
    exit 0
fi

run_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    else
        fail "sudo is required to install system packages"
    fi
}

install_system_packages() {
    # espeak-ng (kokoro's phonemiser) and libsndfile (soundfile's decoder) are voice
    # deps, so the thin install doesn't drag them in — a base setup needs only git
    # and curl. Both stay empty unless a voice extra was requested, and an empty
    # var word-splits to nothing, so the package simply drops off the command.
    local ESPEAK="" SNDFILE=""
    if [ "$INSTALL_VOICE" = true ] || [ "$INSTALL_GPU_VOICE" = true ]; then
        ESPEAK="espeak-ng"
        SNDFILE="libsndfile"
    fi

    if [ "$PLATFORM" = "macos" ]; then
        if [ -x /opt/homebrew/bin/brew ]; then
            eval "$(/opt/homebrew/bin/brew shellenv)"
        elif [ -x /usr/local/bin/brew ]; then
            eval "$(/usr/local/bin/brew shellenv)"
        fi
        if ! command -v brew >/dev/null 2>&1; then
            log "Installing Homebrew"
            NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
            if [ -x /opt/homebrew/bin/brew ]; then
                eval "$(/opt/homebrew/bin/brew shellenv)"
            elif [ -x /usr/local/bin/brew ]; then
                eval "$(/usr/local/bin/brew shellenv)"
            fi
        fi
        log "Installing macOS system packages"
        # shellcheck disable=SC2086  # word-splitting is the point: empty = omit
        brew install git $ESPEAK $SNDFILE
        return
    fi

    if command -v apt-get >/dev/null 2>&1; then
        log "Installing Debian/Ubuntu system packages"
        run_root apt-get update
        # shellcheck disable=SC2086
        run_root apt-get install -y git curl ca-certificates $ESPEAK ${SNDFILE:+libsndfile1}
    elif command -v dnf >/dev/null 2>&1; then
        log "Installing Fedora/RHEL system packages"
        # shellcheck disable=SC2086
        run_root dnf install -y git curl ca-certificates $ESPEAK $SNDFILE
    elif command -v pacman >/dev/null 2>&1; then
        log "Installing Arch system packages"
        # shellcheck disable=SC2086
        run_root pacman -Sy --needed --noconfirm git curl ca-certificates $ESPEAK $SNDFILE
    elif command -v zypper >/dev/null 2>&1; then
        log "Installing openSUSE system packages"
        # shellcheck disable=SC2086
        run_root zypper --non-interactive install git curl ca-certificates $ESPEAK ${SNDFILE:+libsndfile1}
    else
        fail "unsupported Linux package manager; install git, curl, espeak-ng, and libsndfile, then rerun with --skip-system"
    fi
}

node_is_supported() {
    command -v node >/dev/null 2>&1 || return 1
    node -e 'const [major, minor] = process.versions.node.split(".").map(Number); process.exit((major === 20 && minor >= 19) || (major >= 22) ? 0 : 1)'
}

install_node() {
    if node_is_supported && command -v npm >/dev/null 2>&1; then
        log "Using Node.js $(node --version)"
        return
    fi

    export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
    if [ ! -s "$NVM_DIR/nvm.sh" ]; then
        command -v curl >/dev/null 2>&1 || fail "curl is required to install Node.js"
        log "Installing nvm $NVM_VERSION"
        PROFILE=/dev/null bash -c "$(curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/$NVM_VERSION/install.sh)"
    fi
    # shellcheck source=/dev/null
    . "$NVM_DIR/nvm.sh"
    log "Installing Node.js $NODE_VERSION"
    nvm install "$NODE_VERSION"
    nvm use "$NODE_VERSION"
}

install_uv() {
    if command -v uv >/dev/null 2>&1; then
        return
    fi
    if [ -x "$HOME/.local/bin/uv" ]; then
        export PATH="$HOME/.local/bin:$PATH"
        return
    fi
    command -v curl >/dev/null 2>&1 || fail "curl is required to install uv"
    log "Installing uv"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    command -v uv >/dev/null 2>&1 || fail "uv installed but was not found on PATH"
}

prepare_venv() {
    local recreate=false
    if [ -x "$VENV_DIR/bin/python" ]; then
        if ! "$VENV_DIR/bin/python" -c 'import sys; raise SystemExit(sys.version_info[:2] != (3, 12))'; then
            recreate=true
        fi
    elif [ -e "$VENV_DIR" ]; then
        recreate=true
    fi

    if [ "$recreate" = true ]; then
        log "Recreating $(basename "$VENV_DIR") with Python $PYTHON_VERSION"
        rm -rf "$VENV_DIR"
    fi

    uv python install "$PYTHON_VERSION"
    if [ ! -x "$VENV_DIR/bin/python" ]; then
        uv venv --python "$PYTHON_VERSION" --seed "$VENV_DIR"
    fi
}

if [ "$SKIP_SYSTEM" = false ]; then
    install_system_packages
fi
install_node
install_uv
prepare_venv

PYTHON="$VENV_DIR/bin/python"
# One extra per backend (pyproject.toml), so the install cost tracks what the user
# actually asked for rather than the union of every backend that exists.
EXTRAS="test"
if [ "$INSTALL_VOICE" = true ]; then
    EXTRAS="$EXTRAS,voice"
fi
if [ "$INSTALL_GPU_VOICE" = true ]; then
    EXTRAS="$EXTRAS,tts-qwen"
fi
if [ "$INSTALL_EMBED" = true ]; then
    EXTRAS="$EXTRAS,local-embed"
fi
if [ "$INSTALL_DESKTOP" = true ]; then
    EXTRAS="$EXTRAS,desktop"
fi

# Editable metadata can retain unusable Windows ACLs when a checkout is shared
# between Windows and WSL. It is generated, so make it writable before pip runs.
if [ -d yurios.egg-info ]; then
    chmod -R u+rwX yurios.egg-info 2>/dev/null || true
fi

log "Installing YuriOS with Python $($PYTHON --version 2>&1)"
# kokoro, silero-vad and sentence-transformers all depend on torch, and PyPI's LINUX
# torch wheel bundles CUDA: nvidia-* (2.7 GB) + triton (691 MB) + torch (1.1 GB). The
# default voice (kokoro) is CPU-only and the GPU belongs to the LLM, so that is ~4 GB
# nothing here executes. Installing the CPU build first satisfies the requirement and
# the extras reuse it. Windows/macOS wheels are CPU-only already, hence the guard.
if [ "$CPU_TORCH" = true ] && [ "$PLATFORM" != "macos" ] \
   && { [ "$INSTALL_VOICE" = true ] || [ "$INSTALL_EMBED" = true ]; }; then
    log "Installing the CPU-only torch wheel (skips ~4 GB of unused CUDA; --cuda-torch to opt out)"
    uv pip install --python "$PYTHON" torch \
        --index-url https://download.pytorch.org/whl/cpu
fi
uv pip install --python "$PYTHON" -e ".[$EXTRAS]"

prepare_local_state
if [ ! -f vault/soul/soul.yaml ]; then
    log "Seeding the Vault"
    "$PYTHON" scripts/seed_vault.py
else
    log "Keeping existing Vault"
fi

log "Building the web application"
(
    cd web
    npm ci
    npm run build
)

log "Checking which backends your .env selects"
# Advisory: exits non-zero when a selected backend isn't installed, which is a
# legitimate state (thin install + fakes), so don't let it kill a `set -e` script.
"$PYTHON" -m yurios.doctor || true

cat <<EOF

YuriOS setup is complete (extras: $EXTRAS).

Activate the environment:
  source ${VENV_DIR#$ROOT_DIR/}/bin/activate

Start YuriOS:
  python -m yurios.world

Then open http://localhost:8768.

Installed thin? Add her real voice whenever you like — this script is re-runnable:
  ./install.sh --voice
EOF
