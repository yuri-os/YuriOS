# Installation

YuriOS runs on Linux, macOS, and Windows via WSL. The base install includes
sentence-transformers so local memory works without an embedding server. It pulls Torch but no
CUDA is required and no model weights are downloaded until the embedder first starts; each optional
voice, camera, or desktop backend is still an extra.

## The script

```bash
cd YuriOS
./install.sh
yurios status                      # → http://localhost:8768
```

With no options this installs her body, local memory, MCP tools and real voice (faster-whisper ears,
the kokoro voice, silero turn-taking), then starts YuriOS as a background daemon. On a fresh install,
its model setting is `NONE`: it makes no LLM connection until the first dashboard load or `yurios configure`
chooses one. Reruns preserve the existing `.env` and its model choice. Selecting the current direct GGUF
recommendation downloads its Q4_K_M model automatically.
A detected working NVIDIA driver preselects CUDA; otherwise the CPU-only Torch wheel is used.
Nothing needs a cloud key.

The installer links `~/.local/bin/yurios` to this installation's isolated virtual
environment. You do not need to activate `.venv`; open a new terminal if
`~/.local/bin` was not already on your `PATH`.

The script installs system packages, [`uv`](https://docs.astral.sh/uv/), Node, the venv, her
Vault and the web build — and finishes by running the doctor, which should come back with nothing
to do.

## Uninstall

Run this from the YuriOS project directory to stop the daemon and remove the global
`~/.local/bin/yurios` launcher plus this project's `.venv`:

```bash
yurios uninstall
```

Confirm the prompt, or use `yurios uninstall --yes` for an unattended uninstall. The command
first waits for the daemon process and its health endpoint to stop; it refuses to remove anything
while YuriOS is still serving requests. It only removes the launcher installed by YuriOS when it
points at this project's virtual environment; it refuses to remove another `yurios` command or
environment. It preserves the checkout, `.env`, `.yurios/`, logs, downloaded models, Vault, and
all other local data. Re-run `./install.sh` later to recreate the virtual environment and launcher.

### Options

| Flag | What it does |
|---|---|
| `--thin` | Omits the voice stack. On a newly created `.env`, selects fake voice backends; an existing `.env` is preserved, so set its voice backends to `fake` yourself for a text-only setup |
| `--voice` | The local voice stack — already the default; kept so a rerun can name it |
| `--forge-local` | Adds the local camera (diffusers) for `SELFIE_BACKEND=diffusers` — an SDXL checkpoint rendered in-process |
| `--forge-krea2` | The same camera for a Krea 2 checkpoint (INT4, via comfy-kitchen) |
| `--gpu-voice` | Adds qwen3_tts, the designed voice — needs a CUDA GPU (and selects the GPU torch build) |
| `--cpu-torch` | The CPU-only torch + torchaudio wheels (~750 MB) |
| `--cuda-torch` | PyPI's matched CUDA torch + torchaudio pair (~4.5 GB) |
| `--web-search` | Gives her the web (`web_search`, `read_page`, `research`): pulls, configures and starts a SearXNG container, then points `.env` at it. Needs Docker |
| `--no-web-search` | Leaves web search off — the default for unattended runs |
| `--desktop` | Adds the native transparent desktop-window dependencies (pywebview + Qt) |
| `--skip-system` | Don't install system packages |
| `--print-extras` | Print the extras the other flags resolve to and exit — a dry run that touches nothing |
| `--docker` | Build a Docker Compose setup instead of a host environment. Needs a `compose.yaml`, which this checkout does not ship — the flag is for distributions that add one |
| `-h`, `--help` | The same table, from the script |

On Linux/WSL with a terminal attached, the installer **asks** which Torch build to install unless
`--cpu-torch` or `--cuda-torch` already settled it. When a working NVIDIA driver is detected,
CUDA is preselected; otherwise, piped or unattended runs keep the CPU default.

With a terminal attached it also **asks whether she should be able to search the web**, saying up
front what that needs (Docker, a ~500 MB SearXNG image, a service on port 8080) and whether Docker
is actually usable on this machine. Say yes and it does the whole setup; say no and she simply has
no web hands. Piped or unattended runs leave it off — standing up a service for somebody who isn't
there to see it is not a reasonable default. If Docker turns out to be unusable, the installer says
so and continues with web search off rather than failing over an optional capability. See
[Tools](tools.md#web_search-read_page-and-research).

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

# 2. YuriOS + everything .env.example selects: body, brain, direct GGUF runtime,
#    local embeddings, MCP tools, text chat, her ears, her voice, turn-taking.
#    Drop `,voice` for no voice.
pip install -e ".[test,llm,voice]"

# 3. her config. The defaults are local-first and need no cloud key. Without the
#    voice extra, set STT_BACKEND / TTS_BACKEND / VAD_BACKEND to `fake` in it.
cp .env.example .env

# 4. her mind: the Vault, seeded once from her SOUL source (./soul-src). Idempotent —
#    it refuses rather than overwriting a Vault that already exists.
python scripts/seed_vault.py

# 5. her body: three.js/three-vrm bundled by Vite → web/dist
(cd web && npm ci && npm run build)

# 6. go
yurios doctor                      # what .env selects vs what's installed
yurios start                       # background daemon → http://localhost:8768
```

## Optional extras

The base install includes the local sentence-transformer embedder. Every other heavy backend is a
lazy import behind a seam, so each extra installs exactly one. Missing optional deps are **not** a
hard failure: the seam falls back to its fake and logs the command that fixes it.

`./install.sh` installs `[test,llm,voice]`: the test/runtime dependencies, direct GGUF runtime,
and the voice stack that the defaults select. The base dependency set already includes
sentence-transformers; model weights download only on first use. Sizes below are additive guidance rather than an exact total because Torch wheels differ by
platform and selected CPU/CUDA build:

| Install | Adds | On disk |
| --- | --- | --- |
| `pip install -e ".[test]"` | body, brain, local embeddings, MCP tools, text chat, pytest | includes Torch + sentence-transformers |
| `.[stt]` | her ears: faster-whisper — CTranslate2, **no torch** | 564 MB |
| `.[tts]` | her voice: kokoro — the CPU default, needs `espeak-ng` | 1.3 GB |
| `.[vad]` | turn-taking: silero-vad — torch, shared with `tts` | — |
| `.[test,llm,voice]` | direct GGUF runtime plus `stt` + `tts` + `vad`: **the default install** | local embeddings included |
| `.[all]` | all non-GPU voice backends | local embeddings included |
| `.[tts-sovits]` | `TTS_BACKEND=gpt_sovits` — client for a server you run | +2 MB |
| `.[forge-local]` | `SELFIE_BACKEND=diffusers` — local SDXL in-process, **wants CUDA**; checkpoint (~7 GB) is user-supplied | +0.1 GB |
| `.[forge-krea2]` | the same camera for a Krea 2 checkpoint (INT4, **wants CUDA**); also needs HF access to the gated `krea/Krea-2-Raw` | +40 MB |
| `.[llm]` | direct llama.cpp fallback for unavailable LM Studio; downloads the selected Q4_K_M GGUF on first use | platform-dependent |
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

None of that CUDA runs for the default stack: sentence-transformers and kokoro are CPU-friendly,
and the GPU belongs to your LLM anyway. Install the CPU build first and YuriOS reuses it.
`./install.sh` does this for you unless you ask for a GPU backend; by hand it is:

```bash
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu
pip install -e ".[test,llm,voice]"
```

Install **both** packages from that index. Taking torch from `whl/cpu` and letting the extras pull
torchaudio from PyPI is a silent-voice install: kokoro and silero-vad die on `libcudart.so.13` and
fall back to fakes. `yurios doctor` spots that mismatched pair and prints the fix.

## Which backends is she actually using?

```bash
yurios doctor                      # or: python -m yurios.world --check
```

It reads the same `.env` the server reads, checks each selected backend against what's
importable, and prints the exact install command for anything missing — plus the `.env` change
that avoids an embedding server where one exists (`EMBED_BACKEND=sentence_tf` is built in). It
checks her ears, her voice, turn-taking, embeddings, the hands, the camera, and —
advisory, since it's a run-time choice rather than a config one — the desktop window.

`./install.sh` runs it as its last step. If you forgot to activate the venv, it says so by name.

## Running the tests

```bash
pip install -e ".[test]"           # nothing else — the suite runs against the fakes
pytest                             # offline, no GPU
```

That is the contract the optional seams buy you: voice, camera, and desktop backends have fakes,
so the whole suite is green without loading a model or requiring CUDA — which is also why a thin
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
