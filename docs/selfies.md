# Selfies — her camera

`take_selfie` is the fourth of her [hands](tools.md), and the one that teaches the lesson the
others can't: **a slow tool must not sit inside the turn.** A render takes 10–30 seconds; dead air
after her lead-in would read as a hang.

So the tool starts work and never awaits it:

```
pass 1   "hold on, let me take one~ [[take_selfie {"scene": "window"}]]"
server   validates the arguments, returns {"status": "started"} immediately
pass 2   she finishes the turn knowing the shot is coming — no dead air
(async)  the lab renders off-turn → saves the PNG + a provenance sidecar →
         posts it into the chat → offers one spoken line about it, if she's free
```

If she's mid-conversation when the photo lands, the spoken line is dropped: the image is already
in the chat, and she never talks over you. A failed render becomes a quiet message in the chat —
never a crash, and never silence about a promise she made.

## Choosing a backend

```ini
SELFIE_BACKEND=openrouter         # openrouter | diffusers | krea2 | mock | off
```

| Backend | Where it renders | Needs |
|---|---|---|
| **`openrouter`** *(default)* | hosted — the GPU stays free for her voice | `OPENROUTER_API_KEY` |
| `diffusers` | **your** GPU, in-process: an SDXL checkpoint | `.[forge-local]` + a checkpoint |
| `krea2` | **your** GPU, in-process: a Krea 2 transformer, INT4 | `.[forge-krea2]` + a checkpoint + HF access |
| `mock` | deterministic placeholder cards | nothing — keyless, offline |
| `off` | no camera at all | — |

With `SELFIE_BACKEND=off` the tool isn't advertised to the model: no hand, rather than a dead one.

**Any backend that can't run degrades to `mock` with one loud WARNING** — no key, missing
dependencies, a missing checkpoint, or no access to a gated repo. She keeps working and
`/api/health` reports the truth (`"mock (no key — placeholder)"`).

## OpenRouter (the default)

```ini
SELFIE_BACKEND=openrouter
OPENROUTER_API_KEY=sk-or-…
SELFIE_MODEL=bytedance-seed/seedream-4.5     # the cheap everyday one
# SELFIE_MODEL=sourceful/riverflow-v2.5-pro  # the brand-art register
```

Any OpenRouter image route works — the backend asks for `["image"]` modalities and retries once
with `["image","text"]`, so no per-vendor registry is needed. Requests carry the project's
attribution headers, so selfie spend lands on the same app page as the chat path.

## Local SDXL (`diffusers`)

Her own camera, on your own GPU, with no third party. The model is loaded **in-process** — there's
no A1111 or ComfyUI to install and run alongside.

```bash
pip install -e ".[forge-local]"      # or ./install.sh --forge-local
```

Then get a checkpoint. The reference pick is an Illustrious-lineage SDXL `.safetensors` from
Civitai — a [Pie Model](https://civitai.com/models/1593793/pie-models) (Lemon = general anime,
Nutella = sharp linework, Blueberry = semi-realistic). ~7 GB; a free Civitai account may be
required; the model's own license applies. Any Illustrious-family base works, and swapping models
later is swapping one path. **LoRA is not wired in this build** — base checkpoint only.

```ini
SELFIE_BACKEND=diffusers
SELFIE_LOCAL_MODEL=/path/to/checkpoint.safetensors
SELFIE_LOCAL_DEVICE=cuda          # cpu is for emergencies — it crawls
SELFIE_LOCAL_STEPS=30
SELFIE_LOCAL_CFG=5.0
SELFIE_LOCAL_HIRES=true           # upscale 1.5× + low-denoise img2img second pass
SELFIE_LOCAL_HIRES_SCALE=1.5
SELFIE_LOCAL_HIRES_DENOISE=0.35
SELFIE_LOCAL_CPU_OFFLOAD=false    # true = ~2× slower, much less VRAM
```

Those defaults are the Pie author's own (DPM++ 2M / Karras / 30 steps / CFG 5 / hires fix on).
VRAM: ~7 GB fp16 next to a ~6.7 GB LLM fits a 16 GB card; turn on `SELFIE_LOCAL_CPU_OFFLOAD` if
you need the headroom.

The shipped `yuri.yaml` already carries the booru-style quality preamble these bases expect, so
prompts need no changes.

## Krea 2 (`krea2`)

Same knob, different architecture. If `SELFIE_LOCAL_MODEL` is a Krea 2 export rather than SDXL,
`SELFIE_BACKEND=diffusers` **notices** — it reads the file's safetensors header without loading a
byte of tensor data — and routes to the krea2 backend. Nothing to change.
`SELFIE_BACKEND=krea2` forces it, for a file whose header doesn't say.

That path needs two extra things:

1. `pip install -e ".[forge-krea2]"` (adds comfy-kitchen), and
2. Hugging Face access to the **text encoder + VAE**, which these single-file checkpoints don't
   carry: accept the licence at <https://huggingface.co/krea/Krea-2-Raw> while signed in, then
   `huggingface-cli login` (or set `HF_TOKEN`). Fetched once and cached; the repo's own
   transformer weights are skipped — yours replace them.

```ini
SELFIE_KREA2_STEPS=0              # 0 = read it off the checkpoint
SELFIE_KREA2_CFG=-1               # -1 = read it off the checkpoint
```

The weights are **never dequantized**: a Krea 2 transformer is ~12.5 B parameters (~23 GB at
bf16, which fits nothing), so the INT4 file runs as-is at ~6.4 GB resident — smaller *and* faster
than unpacking it. Sampling differs from SDXL's, which is why it gets its own two knobs — roughly
8 steps at guidance 0.0 for a turbo/TDM export, 28 at 4.5 for a base one. Set them only to
override what the checkpoint says.

### VRAM lending

```ini
SELFIE_LLM_PARK=true
```

When a local render is requested and free VRAM won't hold the resident pipeline (~11 GiB) next to
your LM Studio LLM, the LLM is **parked** — evicted for the render's duration, then re-pinned.
The render drops from a ~70 s offload crawl to ~15 s resident, and her brain always comes back,
even if the render fails.

A park never happens mid-reply: evicting the model while a turn is streaming would kill that
stream and the draft would vanish from the chat, so a render that needs a park waits for a quiet
moment first. Set `SELFIE_LLM_PARK=false` to never touch LM Studio (renders use offload instead).

## What she can ask for

The tool takes three optional slots — `scene`, `mood`, `wardrobe` — and the shot is composed as
*scene + framing + wardrobe + lighting + mood*. Name a slot to pin it; leave it out and the
service rotates one in (seeded, so a seed reproduces a shot).

Shipped keys (`yurios/forge/templates/selfie.yaml`):

| Slot | Keys |
|---|---|
| `scenes` | `portrait`, `window`, `sanctuary`, `kitchen`, `outdoors`, `desk`, `bed` |
| `framings` | `portrait`, `close`, `mid`, `candid`, `mirror` |
| `lighting` | `lamplit`, `neon`, `daylight`, `golden`, `screenglow` |
| `moods` | `happy`, `shy`, `waiting`, `playful`, `sleepy`, `tender` |
| `wardrobe` | `signature`, `everyday`, `cozy`, `dressy`, `swim` |

The library is a starting point, not a limit: **a slot value that names no entry is not rejected**
— it passes through verbatim as the prompt fragment. Her own words describe the moment better
than a refusal would. The tool's description is built *from* the merged library, so what she's
offered can never drift from the yaml.

Settings, palette and props stay inside the canon (the sanctuary high above a rainy megacity;
magenta/cyan/amber), so every shot reads as the same world.

## Adding your own scenes and outfits

Point `SELFIE_TEMPLATES_EXTRA` at your own yaml, outside the repo, exactly like the checkpoint:

```ini
SELFIE_TEMPLATES_EXTRA=./data/characters/yuri/selfie-extra.yaml
```

It's merged over the shipped library **key by key**, so you can add rows or re-skin existing ones
without forking the file. Set-but-missing gets one loud warning and the shipped library alone.

```yaml
# one line the take_selfie tool description carries verbatim
tool_hint: "Prefer the rooftop scenes in the evening."

scenes:
  rooftop: "She sits on a rooftop ledge at 2am, city haze below, jacket over her shoulders."

wardrobe:
  # a tier may be a mapping instead of a string
  raincoat:
    prompt: "Wearing a long translucent raincoat over her everyday clothes."
    negative: "umbrella"        # the negation this look needs to actually render
    pinned: true                # never rotated in unprompted — a named ask only
```

Two mechanics make a tier real rather than decorative. **Per-tier negatives** fix the case where a
tier's look collides with the generator's default and the positive words alone lose to the model's
prior. **Pinned tiers** are named-asks-only: never rotated into an unprompted shot.

Her visual identity — the locked art register, her appearance, the quality preamble — lives in
`yurios/forge/characters/yuri.yaml`. Edit `identity` to re-skin her; leave `quality_preamble`
alone, since that's the register itself.

## Where the photos go

`SELFIE_DIR` (per character: `data/characters/<id>/selfies/`), served at `/selfies/`. Each render
writes three things:

- the PNG,
- a `.json` sidecar next to it, and
- one appended line in `generations.jsonl` — a scannable ledger of every render.

The record carries backend, model, the full prompt and negative, the seed and every sampler
setting, so any image is reproducible. Provenance metadata is stripped from the PNG itself before
it's saved.

## Rate limits

```ini
TOOL_RATE_SELFIE=2                # calls per minute — images are expensive
```

Like every tool call, allowed or denied, each one appends an audit line to `TOOL_LOG_DIR`.
