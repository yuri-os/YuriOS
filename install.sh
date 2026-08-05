#!/usr/bin/env bash
set -Eeuo pipefail

PYTHON_VERSION="3.12"
NVM_VERSION="v0.40.3"
NODE_VERSION="22"
MODE="host"
INSTALL_DESKTOP=false
SKIP_SYSTEM=false
# The default install includes the local embeddings, voice stack, and Diffusers camera.
# A fresh checkout deliberately starts with no LLM or camera selected; the first web
# load or `yurios configure` asks for them, so installation never makes an unchosen
# model connection or downloads a checkpoint.
# (faster_whisper ears, kokoro voice, silero turn-taking). torch's build is the one
# choice the script asks about when it can: the CPU-only wheel (~750 MB) keeps the
# install lean; embeddings and the default voice are CPU-friendly. The CUDA pair (~4.5 GB) is what
# makes local selfies (SELFIE_BACKEND=diffusers) and GPU voice fast.
# Unattended runs keep the CPU default. During an interactive install, a working
# NVIDIA driver makes CUDA the preselected choice. --thin omits voice and local-camera
# packages but retains the local sentence-transformer embedder.
INSTALL_VOICE=true
INSTALL_THIN=false
INSTALL_GPU_VOICE=false
INSTALL_FORGE_LOCAL=true
INSTALL_FORGE_KREA2=false
FORGE_LOCAL_EXPLICIT=false
# Which torch build to install: cpu (the historic default — cheap, and the
# default voice is CPU-only) or cuda (fast local selfies via SELFIE_BACKEND=
# diffusers, GPU voice). Empty = not chosen yet: the script asks when it can,
# and falls back to cpu when nobody is there to answer (pipes, CI, dry runs).
TORCH_CHOICE=""
TORCH_EXPLICIT=false
VOICE_EXPLICIT=false
PRINT_EXTRAS=false

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    cat <<'EOF'
Usage: ./install.sh [options]

Set up YuriOS on WSL, native Linux, or macOS.

With no options this installs YuriOS's body, local memory, MCP tools, and real
voice — faster-whisper ears, the kokoro voice, silero turn-taking — plus the
local Diffusers camera runtime. It starts as a background daemon with no LLM or
camera selected; open the dashboard or run `yurios configure` to choose them. A
detected working NVIDIA driver preselects CUDA; otherwise the CPU-only torch
wheel is used. Nothing needs a cloud key.

Options:
  --thin         Base runtime without the voice or local-camera stacks: body, brain,
                  local memory, tools, and text chat. Those seams fall back to fakes
                  and say so on startup; rerun without --thin to add them later
  --voice        The local voice stack — already the default, kept so a rerun
                 can name it explicitly
  --forge-local  The local camera (diffusers), already installed by default; kept so
                  a rerun can name it explicitly. Wants the GPU torch build to be fast
  --forge-krea2  The same camera for a Krea 2 checkpoint (INT4, via
                 comfy-kitchen). Also needs Hugging Face access to the
                 gated krea/Krea-2-Raw for its text encoder + VAE
  --gpu-voice    Also install qwen-tts, the designed voice — needs a CUDA GPU
                 (selects the GPU torch build, since that one really uses it)
  --cpu-torch    Install the CPU-only torch + torchaudio wheels (~750 MB):
                 voice and embeddings run fine on CPU, but local selfies crawl
  --cuda-torch   Install PyPI's matched CUDA torch + torchaudio pair (~4.5 GB):
                 fast local selfies and GPU voice. Needs an NVIDIA GPU
  --docker       Build the Docker Compose setup instead of a host environment
  --desktop      Also install the native transparent desktop-window dependencies
  --print-extras Print the extras the other flags resolve to and exit — a dry run
                 that touches nothing (the test suite uses it)
  --skip-system  Do not install system packages (git, curl, and — unless --thin
                 — espeak-ng and libsndfile, which the voice needs)
  -h, --help     Show this help

On Linux/WSL with a terminal attached, the installer asks which Torch build to
install because local embeddings always need it. A detected working NVIDIA driver
preselects CUDA; otherwise, piped or unattended runs keep the CPU default.

Everything is additive and re-runnable: install --thin now, rerun without it later.
`yurios doctor` reports what your .env selects vs what's installed.
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
        --forge-local) INSTALL_FORGE_LOCAL=true; FORGE_LOCAL_EXPLICIT=true ;;
        --forge-krea2) INSTALL_FORGE_KREA2=true ;;
        --gpu-voice) INSTALL_GPU_VOICE=true; INSTALL_VOICE=true
                     TORCH_CHOICE="cuda"; TORCH_EXPLICIT=true ;;
        --cpu-torch) TORCH_CHOICE="cpu"; TORCH_EXPLICIT=true ;;
        --cuda-torch) TORCH_CHOICE="cuda"; TORCH_EXPLICIT=true ;;
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
# saying out loud rather than resolving by argument order. --thin drops the default
# local camera too, while an explicit --forge-local/--forge-krea2 remains contradictory.
if [ "$INSTALL_THIN" = true ]; then
    if [ "$VOICE_EXPLICIT" = true ] || [ "$INSTALL_GPU_VOICE" = true ]; then
        fail "--thin cannot be combined with --voice or --gpu-voice; --thin is the no-voice install"
    fi
    if [ "$FORGE_LOCAL_EXPLICIT" = true ] || [ "$INSTALL_FORGE_KREA2" = true ]; then
        fail "--thin cannot be combined with --forge-local/--forge-krea2; --thin omits optional local backends"
    fi
    INSTALL_VOICE=false
    INSTALL_FORGE_LOCAL=false
fi

# One extra per backend (pyproject.toml), so the install cost tracks what the user
# actually asked for rather than the union of every backend that exists. Resolved here,
# up front, because it depends on nothing but the flags — which lets --print-extras be
# a real dry run (tests/test_doctor.py pins the default against .env.example with it).
EXTRAS="test,llm"
if [ "$INSTALL_VOICE" = true ]; then
    EXTRAS="$EXTRAS,voice"
fi
if [ "$INSTALL_GPU_VOICE" = true ]; then
    EXTRAS="$EXTRAS,tts-qwen"
fi
if [ "$INSTALL_FORGE_LOCAL" = true ]; then
    EXTRAS="$EXTRAS,forge-local"
fi
if [ "$INSTALL_FORGE_KREA2" = true ]; then
    EXTRAS="$EXTRAS,forge-krea2"
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

select_torch_build() {
    # Ask which torch build to install — but only when all of these hold:
    # the always-installed local embedder or another torch consumer needs it, the
    # platform has two real builds to choose between (macOS wheels are CPU-only),
    # no flag settled it, and someone is actually there to answer. Anything less
    # and the CPU default stands — unattended runs must never block on a prompt.
    [ "$TORCH_EXPLICIT" = false ] || return 0
    { [ "$PLATFORM" = "linux" ] || [ "$PLATFORM" = "wsl" ]; } || return 0
    [ -t 0 ] || return 0

    local default_choice="cpu" gpu_note=""
    if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
        gpu_note=" (NVIDIA GPU detected: $(nvidia-smi -L | head -1 | cut -d: -f2- | sed 's/^ //'))"
        default_choice="cuda"
    fi
    printf '\n==> Which torch build?%s\n' "$gpu_note" >&2
    printf '    [1] CPU  (~750 MB)  — voice and embeddings run fine on CPU; local\n' >&2
    printf '        selfies (SELFIE_BACKEND=diffusers) will crawl\n' >&2
    printf '    [2] CUDA (~4.5 GB)  — fast local selfies and GPU voice, on your NVIDIA GPU\n' >&2
    local answer=""
    local default_number=1
    [ "$default_choice" = "cuda" ] && default_number=2
    read -r -p "    Choose [1/2] (default $default_number): " answer || true
    case "$answer" in
        2|cuda|gpu|CUDA|GPU) TORCH_CHOICE="cuda" ;;
        1|cpu|CPU) TORCH_CHOICE="cpu" ;;
        *) TORCH_CHOICE="$default_choice" ;;
    esac
    log "torch build: $TORCH_CHOICE"
}

install_torch() {
    # sentence-transformers needs torch. When voice is installed, torchaudio must
    # come from the same index: kokoro and silero-vad load its C++ extension, and a
    # mismatched pair dies with "libcudart.so.13: cannot open shared object file"
    # while pip and the doctor's find_spec both report a fine install. The CPU pair
    # comes from whl/cpu (747 MB vs 4.5 GB); the CUDA pair is PyPI's default Linux
    # build, which is already matched to itself. Switching builds on a rerun needs
    # --reinstall: an installed torch satisfies the requirement, so uv keeps it.
    [ "$PLATFORM" != "macos" ] || return 0     # macOS wheels are CPU-only anyway

    local packages=(torch)
    if [ "$INSTALL_VOICE" = true ]; then
        packages+=(torchaudio)
    fi
    local current="none"
    if [ -x "$PYTHON" ]; then
        current="$("$PYTHON" -c 'import torch; print("cuda" if torch.version.cuda else "cpu")' \
            2>/dev/null || printf 'none')"
    fi
    # No choice made (unattended run)? Keep whatever build is already there —
    # a piped rerun must never downgrade a GPU user's torch to CPU. Fresh
    # installs still land on the cheap CPU default.
    if [ -z "$TORCH_CHOICE" ]; then
        if [ "$current" != "none" ]; then
            TORCH_CHOICE="$current"
        else
            TORCH_CHOICE="cpu"
        fi
    fi
    if [ "$current" = "$TORCH_CHOICE" ]; then
        log "torch ($TORCH_CHOICE build) is already installed"
        return 0
    fi

    local reinstall=()
    if [ "$current" != "none" ]; then
        log "Switching torch from the $current build to $TORCH_CHOICE"
        reinstall=(--reinstall-package torch)
        if [ "$INSTALL_VOICE" = true ]; then
            reinstall+=(--reinstall-package torchaudio)
        fi
    fi
    if [ "$TORCH_CHOICE" = "cuda" ]; then
        log "Installing the CUDA ${packages[*]} build (~4.5 GB; fast local selfies and GPU voice)"
        uv pip install --python "$PYTHON" "${reinstall[@]}" "${packages[@]}"
    else
        log "Installing the CPU-only ${packages[*]} wheels (skips ~3.8 GB of unused CUDA; --cuda-torch to opt out)"
        uv pip install --python "$PYTHON" "${reinstall[@]}" "${packages[@]}" \
            --index-url https://download.pytorch.org/whl/cpu
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

install_launcher() {
    # Keep the project dependencies isolated, but expose its console command from
    # any terminal. A symlink means upgrades keep using the same project venv and
    # never require `source .venv/bin/activate`.
    local user_bin="$HOME/.local/bin"
    local launcher="$user_bin/yurios"
    mkdir -p "$user_bin"
    if [ -e "$launcher" ] && [ ! -L "$launcher" ]; then
        fail "$launcher already exists and is not a YuriOS launcher; move it aside, then rerun the installer"
    fi
    ln -sfn "$VENV_DIR/bin/yurios" "$launcher"

    case "${SHELL##*/}" in
        bash) local profile="$HOME/.bashrc" ;;
        zsh) local profile="$HOME/.zshrc" ;;
        *)
            log "Installed $launcher (add $user_bin to PATH for your shell)"
            return
            ;;
    esac
    if [[ ":$PATH:" != *":$user_bin:"* ]] && ! grep -Fq 'YuriOS command launcher' "$profile" 2>/dev/null; then
        {
            printf '\n# YuriOS command launcher\n'
            printf 'export PATH="$HOME/.local/bin:$PATH"\n'
        } >> "$profile"
        log "Added $user_bin to PATH in $profile (open a new terminal to use yurios)"
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
select_torch_build
install_torch
uv pip install --python "$PYTHON" -e ".[$EXTRAS]"
install_launcher

prepare_local_state
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

log "Starting YuriOS daemon"
# The daemon command never needs installer input. Detach stdin explicitly so a
# terminal's pending input/read state cannot make the final install step appear to wait.
"$HOME/.local/bin/yurios" start </dev/null

printf '\nYuriOS setup is complete (extras: %s).\n' "$EXTRAS"
cat <<'EOF'

YuriOS is running as a background daemon:
  yurios status
  yurios stop
  yurios start                 # starts it again after a stop
  yurios restart               # reload settings saved in .env
  yurios start --foreground    # keep logs in this terminal

Then open http://localhost:8768.

The first dashboard load asks you to choose a language model. `yurios configure`
offers the same choice in a terminal; selecting a gguf/ model automatically
downloads its matching Q4_K_M GGUF before it is used. The default
sentence-transformer embedder also downloads its small local model on first startup.
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

if [ "$INSTALL_FORGE_LOCAL" = true ]; then
    cat <<'EOF'

Her local camera is installed. To use it, point .env at a checkpoint:
  SELFIE_BACKEND=diffusers
  SELFIE_LOCAL_MODEL=/path/to/your-model.safetensors
Any Illustrious-lineage SDXL base works (e.g. a Pie Model from Civitai — the
download instructions are in .env.example's SELFIE_LOCAL_* block). On the CUDA
torch build a selfie takes seconds; on the CPU build, minutes.
EOF
fi

if [ "$INSTALL_FORGE_KREA2" = true ]; then
    cat <<'EOF'

Her local camera is installed, Krea 2 flavour. Point .env at a checkpoint:
  SELFIE_BACKEND=diffusers
  SELFIE_LOCAL_MODEL=/path/to/your-krea2-model.safetensors
(diffusers is right — a Krea 2 file is recognised and loaded as one.) These
checkpoints carry no text encoder or VAE, so one more step, once:
  1. accept the licence at https://huggingface.co/krea/Krea-2-Raw (signed in)
  2. huggingface-cli login
Until then she falls back to placeholder cards and says so in the log.
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
