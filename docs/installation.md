# Installation

YuriOS runs on Linux, macOS, and Windows via WSL. Everything heavy is optional: the base install
carries no torch, no CUDA and no model weights, and each backend you actually select brings
exactly one extra with it.

## The script

```bash
cd YuriOS
./install.sh
source .venv/bin/activate
python -m yurios.world             # → http://localhost:8768
```

With no options this installs everything the shipped `.env.example` selects, so she runs as
configured out of the box: her body, brain, memory, MCP tools and text chat, plus her real voice
(faster-whisper ears, the kokoro voice, silero turn-taking) on the CPU-only torch wheel. ~1.6 GB,
no CUDA, and no model weights are downloaded at install time. Nothing needs a cloud key.

The script installs system packages, [`uv`](https://docs.astral.sh/uv/), Node, the venv, her
Vault and the web build — and finishes by running the doctor, which should come back with nothing
to do.

### Options

| Flag | What it does |
|---|---|
| `--thin` | Base runtime only (~280 MB, no torch). Her voice seams fall back to fakes and say so on startup |
| `--voice` | The local voice stack — already the default; kept so a rerun can name it |
| `--local-embed` | Adds sentence-transformers, for `EMBED_BACKEND=sentence_tf` |
| `--forge-local` | Adds the local camera (diffusers) for `SELFIE_BACKEND=diffusers` — an SDXL checkpoint rendered in-process |
| `--forge-krea2` | The same camera for a Krea 2 checkpoint (INT4, via comfy-kitchen) |
| `--gpu-voice` | Adds qwen3_tts, the designed voice — needs a CUDA GPU (and selects the GPU torch build) |
| `--cpu-torch` | The CPU-only torch + torchaudio wheels (~750 MB) |
| `--cuda-torch` | PyPI's matched CUDA torch + torchaudio pair (~4.5 GB) |
| `--desktop` | Adds the native transparent desktop-window dependencies (pywebview + Qt) |
| `--skip-system` | Don't install system packages |
| `--print-extras` | Print the extras the other flags resolve to and exit — a dry run that touches nothing |
| `--docker` | Build a Docker Compose setup instead of a host environment. Needs a `compose.yaml`, which this checkout does not ship — the flag is for distributions that add one |
| `-h`, `--help` | The same table, from the script |

On Linux/WSL with a terminal attached, the installer **asks** which torch build to install
whenever a torch consumer is selected (voice, embeddings, the local camera) and no
`--cpu-torch`/`--cuda-torch` flag settled it. Piped or unattended runs keep the CPU default.

Contradictions are refused rather than resolved by argument order: `--thin` cannot be combined
with `--voice`, `--gpu-voice`, `--forge-local` or `--forge-krea2`, and `--desktop` cannot be
combined with `--docker`.

Everything is additive and re-runnable — install `--thin` now, rerun without it later.

## Manual setup

Everything `./install.sh` does, as steps you can run yourself. Useful if you're packaging her,
don't want a script touching your system, or are working on YuriOS.

**Prerequisites.** Python **3.11–3.13** (3.12 is the tested one; the upper bound is real — see
the note in `pyproject.toml`), Node **20.19+ or 22+** for the frontend build, and `git`. For her
voice: `espeak-ng` (kokoro's phonemiser) and `libsndfile` — a text-only install needs neither.

Install `espeak-ng` as a **system** package even though a pip wheel bundles a copy: the bundled
phoneme data loses espeak-ng's own path-resolution race, and its answer to data it can't read is
to `exit(1)` the process. Kokoro checks for a working one in a child process and falls back to
the fake rather than take the server down with it.

```bash
# 1. a venv on a supported Python
python3.12 -m venv .venv && source .venv/bin/activate

# 2. YuriOS + everything .env.example selects: body, brain, memory, MCP tools,
#    text chat, her ears, her voice, turn-taking. Drop `,voice` for text only.
pip install -e ".[test,voice]"

# 3. her config. The defaults are local-first and need no cloud key. Without the
#    voice extra, set STT_BACKEND / TTS_BACKEND / VAD_BACKEND to `fake` in it.
cp .env.example .env

# 4. her mind: the Vault, seeded once from her SOUL source (./soul-src). Idempotent —
#    it refuses rather than overwriting a Vault that already exists.
python scripts/seed_vault.py

# 5. her body: three.js/three-vrm bundled by Vite → web/dist
(cd web && npm ci && npm run build)

# 6. go
python -m yurios.doctor            # what .env selects vs what's installed
python -m yurios.world             # → http://localhost:8768
```

## The extras — pay only for the backends you select

Every heavy backend is a lazy import behind a seam, so the base install carries none of them and
each extra installs exactly one. Missing deps are **not** a hard failure: the seam falls back to
its fake and logs the command that fixes it.

`./install.sh` installs `[test,voice]` — the row in bold — because that is what `.env.example`
selects. Sizes are **measured on disk** (Linux, Python 3.12, venv total — not deltas):

| Install | Adds | On disk |
| --- | --- | --- |
| `pip install -e ".[test]"` | body, brain, memory, MCP tools, text chat, pytest | 280 MB |
| `.[stt]` | her ears: faster-whisper — CTranslate2, **no torch** | 564 MB |
| `.[tts]` | her voice: kokoro — the CPU default, needs `espeak-ng` | 1.3 GB |
| `.[vad]` | turn-taking: silero-vad — torch, shared with `tts` | — |
| `.[test,voice]` | `stt` + `tts` + `vad`: **the default install** | **1.6 GB** |
| `.[local-embed]` | `EMBED_BACKEND=sentence_tf` — no LM Studio/Ollama needed | — |
| `.[all]` | `voice` + `local-embed` | 1.8 GB |
| `.[tts-sovits]` | `TTS_BACKEND=gpt_sovits` — client for a server you run | +2 MB |
| `.[forge-local]` | `SELFIE_BACKEND=diffusers` — local SDXL in-process, **wants CUDA**; checkpoint (~7 GB) is user-supplied | +0.1 GB |
| `.[forge-krea2]` | the same camera for a Krea 2 checkpoint (INT4, **wants CUDA**); also needs HF access to the gated `krea/Krea-2-Raw` | +40 MB |
| `.[voice,tts-qwen]` | `TTS_BACKEND=qwen3_tts` — the designed voice, **wants CUDA** | 2.1 GB |
| `.[desktop]` | `--window`: pywebview + Qt (QtWebEngine) | 798 MB |
| `.[gpu]` | genuinely everything: GPU voice and Qt, on CUDA torch | 6.4 GB |

### CPU vs CUDA torch

kokoro, silero-vad and sentence-transformers all depend on torch, and on Linux the default PyPI
torch wheel bundles CUDA:

| Wheel | On disk | Download |
|---|---|---|
| torch (default PyPI) | 4.5 GB | 2.73 GB, 23 CUDA packages |
| torch (`whl/cpu` index) | 747 MB | ~195 MB |

None of that CUDA runs for the default stack: kokoro is CPU-only and the GPU belongs to your LLM
anyway. Install the CPU build first and the extras reuse it. `./install.sh` does this for you
unless you ask for a GPU backend; by hand it is:

```bash
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu
pip install -e ".[test,voice]"
```

Install **both** packages from that index. Taking torch from `whl/cpu` and letting the extras pull
torchaudio from PyPI is a silent-voice install: kokoro and silero-vad die on `libcudart.so.13` and
fall back to fakes. `python -m yurios.doctor` spots that mismatched pair and prints the fix.

## Which backends is she actually using?

```bash
python -m yurios.doctor            # or: python -m yurios.world --check
```

It reads the same `.env` the server reads, checks each selected backend against what's
importable, and prints the exact install command for anything missing — plus the `.env` change
that avoids the download altogether where one exists (`EMBED_BACKEND=lm_studio` needs no torch at
all). It checks her ears, her voice, turn-taking, embeddings, the hands, the camera, and —
advisory, since it's a run-time choice rather than a config one — the desktop window.

`./install.sh` runs it as its last step. If you forgot to activate the venv, it says so by name.

## Running the tests

```bash
pip install -e ".[test]"           # nothing else — the suite runs against the fakes
pytest                             # offline, no GPU
```

That is the contract the seams buy you: every heavy backend has a fake, so the whole suite is
green on a machine with no torch, no CUDA and nothing downloaded — which is also why a thin
install is a *testable* install.

It runs with fake models on a `VirtualClock`, so **days of an always-on mind run in
milliseconds**. That's the only way the make-or-break component — the interrupt threshold —
ships tuned instead of vibed (the scenario battery: "the interview was Tuesday", "the dark
weekend", "the machine sleeps").

## WSL

Everything above works unchanged inside WSL, with one exception: the native desktop window has to
be drawn by Windows rather than the VM. See [Bodies → desktop mode on WSL](bodies.md#wsl).

Her ears are pinned to the CPU on WSL, where the GPU passthrough can't reliably load them.

## Upgrading

Re-run `./install.sh` — it is additive and idempotent. On the first start after upgrading from
0.1, your existing Vault and its neighbours are copied into a character root under `DATA_DIR`;
the originals stay put as a backup. See [Characters → migrating](characters.md#migrating-from-01).
