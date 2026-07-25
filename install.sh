#!/usr/bin/env bash
set -Eeuo pipefail

PYTHON_VERSION="3.12"
NVM_VERSION="v0.40.3"
NODE_VERSION="22"
MODE="host"
INSTALL_DESKTOP=false
SKIP_SYSTEM=false
# The default install is exactly what .env.example selects, so a fresh checkout runs
# as configured with no second step: base runtime + the local voice stack
# (faster_whisper ears, kokoro voice, silero turn-taking) on the CPU-only torch
# wheel. ~1.6 GB. The old "[all,test]" default cost 6.2 GB — that was PyPI's CUDA
# torch, 3.8 GB of nvidia-* nothing here executes; the CPU wheel is where the saving
# came from, not from shipping a config whose voice seams fall back to fakes.
# --thin drops back to the ~280 MB base for anyone who wants text-only.
INSTALL_VOICE=true
INSTALL_THIN=false
INSTALL_EMBED=false
INSTALL_GPU_VOICE=false
CPU_TORCH=true
VOICE_EXPLICIT=false
PRINT_EXTRAS=false

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    cat <<'EOF'
Usage: ./install.sh [options]

Set up YuriOS on WSL, native Linux, or macOS.

With no options this installs everything the shipped .env.example selects, so
she runs as configured out of the box: her body, brain, memory, MCP tools and
text chat, plus her real voice — faster-whisper ears, the kokoro voice, silero
turn-taking — on the CPU-only torch wheel. ~1.6 GB, no CUDA, and no model
weights are downloaded at install time. Nothing needs a cloud key.

Options:
  --thin         Base runtime only (~280 MB, no torch): body, brain, memory,
                 tools, text chat. Her voice seams fall back to fakes and say
                 so on startup; rerun without --thin to add them later
  --voice        The local voice stack — already the default, kept so a rerun
                 can name it explicitly
  --local-embed  Also install sentence-transformers, for EMBED_BACKEND=sentence_tf
                 (not needed for the default LM Studio / Ollama embeddings)
  --gpu-voice    Also install qwen-tts, the designed voice — needs a CUDA GPU
                 (skips the CPU-torch shortcut, since that one really uses it)
  --cuda-torch   Do not pre-install the CPU torch wheel on Linux; let the
                 extras pull PyPI's CUDA build (~3.8 GB more)
  --docker       Build the Docker Compose setup instead of a host environment
  --desktop      Also install the native transparent desktop-window dependencies
  --print-extras Print the extras the other flags resolve to and exit — a dry run
                 that touches nothing (the test suite uses it)
  --skip-system  Do not install system packages (git, curl, and — unless --thin
                 — espeak-ng and libsndfile, which the voice needs)
  -h, --help     Show this help

Everything is additive and re-runnable: install --thin now, rerun without it later.
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
        --voice) INSTALL_VOICE=true; VOICE_EXPLICIT=true ;;
        --thin|--no-voice) INSTALL_THIN=true ;;
        --local-embed) INSTALL_EMBED=true ;;
        --gpu-voice) INSTALL_GPU_VOICE=true; INSTALL_VOICE=true; CPU_TORCH=false ;;
        --cuda-torch) CPU_TORCH=false ;;
        --print-extras) PRINT_EXTRAS=true ;;
        --skip-system) SKIP_SYSTEM=true ;;
        -h|--help) usage; exit 0 ;;
        *) fail "unknown option: $arg" ;;
    esac
done

if [ "$MODE" = "docker" ] && [ "$INSTALL_DESKTOP" = true ]; then
    fail "--desktop cannot be combined with --docker; the native window must run on the host"
fi

# --thin is the opposite of the default, so asking for both is a contradiction worth
# saying out loud rather than resolving by argument order.
if [ "$INSTALL_THIN" = true ]; then
    if [ "$VOICE_EXPLICIT" = true ] || [ "$INSTALL_GPU_VOICE" = true ]; then
        fail "--thin cannot be combined with --voice or --gpu-voice; --thin is the no-voice install"
    fi
    INSTALL_VOICE=false
fi

# One extra per backend (pyproject.toml), so the install cost tracks what the user
# actually asked for rather than the union of every backend that exists. Resolved here,
# up front, because it depends on nothing but the flags — which lets --print-extras be
# a real dry run (tests/test_doctor.py pins the default against .env.example with it).
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

if [ "$PRINT_EXTRAS" = true ]; then
    printf '%s\n' "$EXTRAS"
    exit 0
fi

VENV_DIR="$ROOT_DIR/.venv"
case "$(uname -s)" in
    Darwin)
        PLATFORM="macos"
        ;;
    Linux)
        if [ -r /proc/sys/kernel/osrelease ] && grep -qi microsoft /proc/sys/kernel/osrelease; then
            PLATFORM="wsl"
        else
            PLATFORM="linux"
        fi
        ;;
    *) fail "unsupported operating system: $(uname -s). On Windows, run this script inside WSL." ;;
esac

cd "$ROOT_DIR"
log "Detected $PLATFORM"

ENV_CREATED=false

prepare_local_state() {
    if [ ! -f .env ]; then
        cp .env.example .env
        ENV_CREATED=true
        log "Created .env from .env.example"
    else
        log "Keeping existing .env"
    fi
}

configure_embeddings() {
    # .env.example ships EMBED_BACKEND=lm_studio, which needs nothing installed — so
    # the thin default boots out of the box. --local-embed asks for the in-process
    # embedder instead, which is only useful if .env actually selects it.
    #
    # Only ever rewrite a .env this run created: silently editing the user's own
    # config is not ours to do, and switching EMBED_* on a live Vault costs a reindex.
    [ "$INSTALL_EMBED" = true ] || return 0
    if [ "$ENV_CREATED" != true ]; then
        log "Keeping your EMBED_BACKEND as-is — set EMBED_BACKEND=sentence_tf, EMBED_MODEL=BAAI/bge-small-en-v1.5, EMBED_DIM=384 in .env to use the local embedder"
        return 0
    fi
    log "Pointing .env at the in-process embedder (EMBED_BACKEND=sentence_tf)"
    # -i.bak + rm works on both GNU and BSD/macOS sed, where bare -i needs an arg.
    sed -i.bak \
        -e 's|^EMBED_BACKEND=.*|EMBED_BACKEND=sentence_tf|' \
        -e 's|^EMBED_MODEL=.*|EMBED_MODEL=BAAI/bge-small-en-v1.5|' \
        -e 's|^EMBED_DIM=.*|EMBED_DIM=384|' .env
    rm -f .env.bak
}

configure_voice() {
    # Keep .env and the install in agreement in both directions. .env.example selects
    # her real voice and the default install provides it, so the only gap to close is
    # --thin: left alone, those three seams would fall back to fakes and warn on every
    # boot. Same rule as configure_embeddings — only ever rewrite a .env this run
    # created, and keep each line's trailing comment ([^#]* stops at the hash).
    if [ "$INSTALL_VOICE" = true ]; then
        if [ "$ENV_CREATED" != true ] && grep -q '^STT_BACKEND=fake' .env 2>/dev/null; then
            log "Your .env still selects the voice fakes — set STT_BACKEND=faster_whisper, TTS_BACKEND=kokoro, VAD_BACKEND=silero to use the voice this run installed"
        fi
        return 0
    fi
    [ "$ENV_CREATED" = true ] || return 0
    log "Thin install: pointing .env at the voice fakes — rerun ./install.sh without --thin for her real ears and voice"
    sed -i.bak \
        -e 's|^STT_BACKEND=[^#]*|STT_BACKEND=fake                 |' \
        -e 's|^TTS_BACKEND=[^#]*|TTS_BACKEND=fake                 |' \
        -e 's|^VAD_BACKEND=[^#]*|VAD_BACKEND=fake                 |' .env
    rm -f .env.bak
}

configure_wsl_lmstudio() {
    [ "$PLATFORM" = "wsl" ] || return 0

    local default_url="http://localhost:1234/v1"
    local current_line current_url gateway interface subnet target_url
    current_line="$(grep '^LMSTUDIO_BASE_URL=' .env 2>/dev/null)"
    current_url="$(printf '%s' "${current_line#*=}" | cut -d' ' -f1)"
    if [ "$current_url" != "$default_url" ] \
        && [[ "$current_line" != *"# managed by install.sh for WSL" ]]; then
        log "Keeping your LMSTUDIO_BASE_URL as-is"
        return 0
    fi

    # Mirrored-network WSL can reach Windows localhost directly, so keep the
    # portable default when it already works.
    if curl -fsS --connect-timeout 1 "$default_url/models" >/dev/null 2>&1; then
        return 0
    fi
    command -v powershell.exe >/dev/null 2>&1 \
        || fail "powershell.exe is required to connect WSL to LM Studio on Windows"

    read -r _ _ gateway _ interface _ < <(ip -4 route show default | sed -n '1p')
    read -r subnet _ < <(ip -4 route show dev "$interface" proto kernel scope link | sed -n '1p')
    if [[ ! "$gateway" =~ ^[0-9]+(\.[0-9]+){3}$ ]] \
        || [[ ! "$subnet" =~ ^[0-9]+(\.[0-9]+){3}/[0-9]+$ ]]; then
        fail "could not detect the Windows gateway and WSL subnet"
    fi

    # LM Studio may already listen on the Windows-facing adapter. Prefer that
    # over changing the host when it is reachable.
    if curl -fsS --connect-timeout 1 "http://$gateway:1234/v1/models" >/dev/null 2>&1; then
        target_url="http://$gateway:1234/v1"
    else
        log "Configuring the Windows LM Studio bridge (approve the UAC prompt)"
        # Windows 10 cannot route WSL back to a localhost-only server. A scoped
        # portproxy preserves LM Studio's safe loopback bind and exposes only its
        # API port to this WSL subnet. Both addresses are discovered, never fixed.
        if ! powershell.exe -NoProfile -NonInteractive -Command \
            "\$p = Start-Process powershell.exe -Verb RunAs -ArgumentList '-NoProfile -NonInteractive -Command \"netsh interface portproxy delete v4tov4 listenaddress=$gateway listenport=1235; netsh interface portproxy add v4tov4 listenaddress=$gateway listenport=1235 connectaddress=127.0.0.1 connectport=1234; Remove-NetFirewallRule -DisplayName YuriOS-LMStudio-WSL -ErrorAction SilentlyContinue; New-NetFirewallRule -DisplayName YuriOS-LMStudio-WSL -Direction Inbound -Action Allow -Protocol TCP -LocalAddress $gateway -LocalPort 1235 -RemoteAddress $subnet -Profile Any\"' -Wait -PassThru; exit \$p.ExitCode"; then
            fail "Windows declined the LM Studio bridge; rerun the installer and approve its UAC prompt"
        fi
        target_url="http://$gateway:1235/v1"
    fi

    log "Pointing WSL at LM Studio through $target_url"
    sed -i.bak "s|^LMSTUDIO_BASE_URL=.*|LMSTUDIO_BASE_URL=$target_url # managed by install.sh for WSL|" .env
    rm -f .env.bak
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

sync_wsl_clock() {
    local windows_epoch linux_epoch drift

    if ! command -v powershell.exe >/dev/null 2>&1; then
        log "Cannot check the Windows clock because powershell.exe is unavailable; if apt reports 'not valid yet', run 'wsl --shutdown' in Windows and retry"
        return
    fi
    if ! windows_epoch="$(powershell.exe -NoProfile -NonInteractive \
        -Command '[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()')"; then
        log "Cannot read the Windows clock; if apt reports 'not valid yet', run 'wsl --shutdown' in Windows and retry"
        return
    fi
    windows_epoch="${windows_epoch//$'\r'/}"
    if [[ ! "$windows_epoch" =~ ^[0-9]+$ ]]; then
        log "Windows returned an invalid clock value; if apt reports 'not valid yet', run 'wsl --shutdown' in Windows and retry"
        return
    fi

    linux_epoch="$(date -u +%s)"
    drift=$((windows_epoch - linux_epoch))
    if (( drift < 0 )); then
        drift=$((-drift))
    fi
    if (( drift > 60 )); then
        log "Synchronizing the WSL clock with Windows (${drift}s drift)"
        run_root date -u --set="@$windows_epoch" >/dev/null
    fi
}

install_system_packages() {
    # espeak-ng (kokoro's phonemiser) and libsndfile (soundfile's decoder) are voice
    # deps — needed by the default install, but not by --thin, which gets away with
    # git and curl. Both stay empty when no voice extra was requested, and an empty
    # var word-splits to nothing, so the package simply drops off the command.
    local ESPEAK="" SNDFILE="" DESKTOP_APT=""
    if [ "$INSTALL_VOICE" = true ] || [ "$INSTALL_GPU_VOICE" = true ]; then
        ESPEAK="espeak-ng"
        SNDFILE="libsndfile"
    fi
    if [ "$INSTALL_DESKTOP" = true ] && [ "$PLATFORM" != "wsl" ]; then
        # PyQt's wheels contain Qt itself, but not Chromium's NSS runtime or the
        # libraries used by Qt's XCB platform plugin. Minimal Ubuntu images do not
        # carry these, so the Python packages install cleanly and --window then
        # dies while importing QtWebEngine. WSL is excluded on purpose: there
        # --window never touches Qt (world/window.py hands the window to a
        # Windows-side browser instead), so this would be a dozen packages and a
        # sudo prompt spent on a code path that cannot run.
        DESKTOP_APT="libnspr4 libnss3 libxkbfile1 libxkbcommon-x11-0 libxcb-cursor0 libxcb-icccm4 libxcb-util1 libxcb-image0 libxcb-keysyms1 libxcb-render-util0 libxcb-shape0 libxcb-xkb1"
    fi

    if [ "$PLATFORM" = "wsl" ]; then
        sync_wsl_clock
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
        # A typical WSL distro already has these packages after the first run. Avoid
        # asking for the Windows user's sudo password again when there is no work to do.
        if [ "$PLATFORM" = "wsl" ]; then
            local pkg all_installed=true
            for pkg in git curl ca-certificates $ESPEAK ${SNDFILE:+libsndfile1} $DESKTOP_APT; do
                if ! dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null \
                    | grep -q '^install ok installed$'; then
                    all_installed=false
                    break
                fi
            done
            if [ "$all_installed" = true ]; then
                log "WSL system packages are already installed"
                return
            fi
        fi
        log "Installing Debian/Ubuntu system packages"
        run_root apt-get update
        # shellcheck disable=SC2086
        run_root apt-get install -y git curl ca-certificates $ESPEAK ${SNDFILE:+libsndfile1} $DESKTOP_APT
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

# Editable metadata can retain unusable Windows ACLs when a checkout is shared
# between Windows and WSL. It is generated, so make it writable before pip runs.
if [ -d yurios.egg-info ]; then
    chmod -R u+rwX yurios.egg-info 2>/dev/null || true
fi

log "Installing YuriOS with Python $($PYTHON --version 2>&1)"
# kokoro, silero-vad and sentence-transformers all depend on torch, and PyPI's LINUX
# torch wheel bundles CUDA. Measured in an otherwise-empty venv: 4.5 GB on disk for
# the default wheel against 747 MB for whl/cpu. The default voice (kokoro) is CPU-only
# and the GPU belongs to the LLM, so that is ~3.8 GB nothing here executes. Installing
# the CPU build first satisfies the requirement and the extras reuse it — which is what
# makes shipping the voice by default affordable at all. Windows/macOS wheels are
# CPU-only already, hence the guard.
if [ "$CPU_TORCH" = true ] && [ "$PLATFORM" != "macos" ] \
   && { [ "$INSTALL_VOICE" = true ] || [ "$INSTALL_EMBED" = true ]; }; then
    # torchAUDIO has to come from the same index, not just torch. kokoro and silero-vad
    # both load its C++ extension, and PyPI's torchaudio is built against CUDA torch:
    # against a CPU-only torch it dies with "libcudart.so.13: cannot open shared object
    # file", both seams fall back to their fakes, and she is silent — with `pip list`
    # and the doctor's find_spec probe both reporting a perfectly good install.
    log "Installing the CPU-only torch + torchaudio wheels (skips ~3.8 GB of unused CUDA; --cuda-torch to opt out)"
    uv pip install --python "$PYTHON" torch torchaudio \
        --index-url https://download.pytorch.org/whl/cpu
fi
uv pip install --python "$PYTHON" -e ".[$EXTRAS]"

prepare_local_state
configure_embeddings
configure_voice
configure_wsl_lmstudio
# seed_vault.py refuses to overwrite a seeded Vault (it is her mind, and re-seeding
# would reset the relationship to zero), so guard on the same file it checks — that
# keeps a re-run idempotent instead of erroring out under `set -e`.
if [ ! -f vault/soul/soul.yaml ]; then
    log "Seeding the Vault from ./soul-src"
    "$PYTHON" scripts/seed_vault.py
else
    log "Keeping existing Vault (already seeded)"
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

Her brain is the one thing this script can't install: .env points CHAT_MODEL at a
local LM Studio on :1234. Start its server and load the two models it names —
  HauhauCS/Gemma-4-E4B-Uncensored-HauhauCS-Aggressive   (chat + utility)
  text-embedding-nomic-embed-text-v1.5                  (her memory)
— or point CHAT_MODEL/EMBED_BACKEND at Ollama or OpenRouter instead.
EOF

if [ "$INSTALL_VOICE" = true ]; then
    cat <<'EOF'

Her voice models (kokoro, faster-whisper base.en, silero) download themselves the
first time she speaks or listens — one time, a few hundred MB, then offline.
EOF
else
    cat <<'EOF'

Installed --thin, so .env selects the voice fakes: she is silent and doesn't
transcribe. Add her real ears and voice whenever you like (and flip the three
*_BACKEND knobs back) — this script is re-runnable:
  ./install.sh
EOF
fi

# The one piece a WSL install cannot finish from in here: --window has to be
# drawn by Windows, and Electron's binary is a Windows one, so Windows' own node
# must fetch it. Without it she still gets a window — an opaque browser one.
if [ "$PLATFORM" = "wsl" ] && [ "$INSTALL_DESKTOP" = true ]; then
    cat <<EOF

One more step for the transparent desktop window, run from WINDOWS (not in here —
it installs a Windows binary), in PowerShell:
  cd $(wslpath -w "$ROOT_DIR" 2>/dev/null || printf '%s' "$ROOT_DIR")\\desktop-shell
  npm install
Then \`python -m yurios.world --window\` finds it. Details: desktop-shell/README.md
EOF
fi
