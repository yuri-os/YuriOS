# Selfies — her camera

`take_selfie` and `show_picture` are the two of her [hands](tools.md) that share a camera, and
the ones that teach the lesson the others can't: **a slow tool must not sit inside the turn.** A render takes 10–30 seconds; dead air
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

With `SELFIE_BACKEND=off` neither tool is advertised to the model: no hand, rather than a dead one.

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

### Idle unload

```ini
SELFIE_UNLOAD_AFTER_S=3600        # seconds; 0 = drop after every render; negative = keep it
```

A local pipeline stays warm between shots on purpose — reloading costs ~25 s. When her brain is
hosted (OpenRouter GLM, a cloud chat model) there is nothing else on the card, so the "does her
brain still fit beside it?" test answers yes forever and the weights would sit in VRAM until
restart. After an hour of no renders they are dropped; the next selfie loads them again. The
timer is process-wide (one card, one pipeline) and a new shot cancels it, so a selfie fifty-nine
minutes later still hits a warm pipeline.

## What she can ask for

`look` is the important one: **the whole picture in her own words**, written the way you'd
describe a photo rather than as keywords. It leads the prompt and overrides everything else. Five
dropdowns could never express *"curled on the window seat with my sleeves over my hands, grinning
at you sideways"*, and a companion who can only pick from a menu takes the same photo forever.

The rest — `scene`, `framing`, `lighting`, `mood`, `wardrobe` — are optional shorthands that
refine it, and `avoid` is her own "not like that". **A slot she doesn't name stays unnamed.**
Nothing is rolled behind her back: a rotation she never asked for is how one request came back as
two different photos, and how every unprompted shot turned into a costume change.

What she leaves out is filled from *where she actually is* — the hour as light, the rain on the
glass (`render_visual_situation`). Only the gaps: the moment she writes a `look` or names a
`scene` she has placed the picture herself, and the world stops volunteering. Appending "it is
night, rain on the window" to her sunlit meadow is worse than adding nothing.

The library only rotates a shot in when there is genuinely nothing to go on — no words, no slot,
no situation. That's the honest reading of "take a selfie" with no further thought, and it's the
only case that should surprise her.

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

## A library of her own

An overlay can only ever *add* to the shipped library, and the shipped library is one
character's world — a sanctuary above a rainy megacity, and a tail in half its scenes. A
different character needs those rows **gone**, not buried, and a house running four of them has
only one `SELFIE_TEMPLATES_EXTRA` to share between them.

So a character may carry her own **`data/characters/<id>/selfie.yaml`**, beside her
`appearance.yaml`. When she does, it *replaces* the shipped book outright — same schema, same
loader, same `tool_hint`, and the `take_selfie` description is built from her rows so she is
never offered a scene her camera would not compose. No file means the shipped defaults,
unchanged, which is where every character starts. `SELFIE_TEMPLATES_EXTRA` still merges over
whichever base wins, so a house-wide register keeps working for characters who never forked.

Edit it in the **card studio** (`/studio/?character=<id>` → *Selfies*): the section shows the
shipped library until she has one of her own, and the first save forks it into hers. Each row is
a key, the fragment it composes, and optionally the `negative` its look needs and a `pinned` flag
for named-asks-only tiers. *Back to the shipped library* deletes her file. Saving restarts her
runtime — the forge and the tool description are both built once at start.

The studio owns the file: it regenerates it from its rows on every save, so hand-written comments
in it will not survive a save from the page. Notes she should actually read belong in `tool_hint`.
Her library stays on this machine — it is not part of an exported card.

## Whose face the camera renders

Her visual identity — her appearance, the quality preamble, the structural negatives — lives in
an **appearance yaml**. The house's own is `yurios/forge/characters/yuri.yaml`; every imported
character gets `data/characters/<id>/appearance.yaml`, derived from her card at import and
hand-editable afterwards. Each inherits the shared register from
`yurios/forge/characters/_register.yaml`, so a derived file is usually two fields long. Edit
`identity` to re-skin her; leave `quality_preamble` alone, since that's the register itself.

A character runtime points `SELFIE_CHARACTER` at her own file. If it's missing, the camera
renders a **neutral stand-in** and says so loudly rather than falling back to whoever the house
ships — a photo of the wrong person is worse than a photo of no one.

## show_picture — the camera pointed away from her

`take_selfie` can only ever answer *"here is a picture of me"*. `show_picture` is the other half:
the street below, a sketch she made, the thing she keeps meaning to show you.

```
"here, look — [[show_picture {"subject": "the rain running down the glass, the city smeared behind it"}]]"
```

It takes a required `subject` and an optional `avoid`, and that's all. **No library, no slots, no
rotation, and no situation fill-in** — her words are the whole prompt, because no menu could
anticipate what she might want to show you, and she has already written the scene.

Her likeness is left out of that frame (`include_character=False`): a photo of the rain doesn't
have her in it just because she took it. Everything else is shared with the selfie path — one
lab, one VRAM loan, one provenance ledger, the same start-don't-await rule, the same quiet
message when a render fails.

## Where the photos go

`SELFIE_DIR` (per character: `data/characters/<id>/selfies/`), served at `/selfies/`. Each render
writes three things:

- the PNG,
- a `.json` sidecar next to it, and
- one appended line in `generations.jsonl` — a scannable ledger of every render.

The record carries backend, model, the full prompt and negative, the seed and every sampler
setting, so any image is reproducible. Provenance metadata is stripped from the PNG itself before
it's saved.

## Asking from the terminal

`take_selfie` and `show_picture` are how *she* asks. You can ask the same camera without a turn:

```bash
yurios selfie yuri --scene window          # waits for the PNG on a TTY
yurios selfie yuri --look "in the rain" --no-wait
yurios picture yuri --subject "rain on the glass"
yurios gallery list yuri
yurios gallery fetch yuri 1763…-a1b2.png -o shot.png
```

Those hit `POST /api/selfie` and `POST /api/picture` on her runtime. They use the same lab, stamp
`_deliver: "vault"` so the shot lands on the shelf and not as a chat bubble, and do not spend a
hand.

## The gallery tab

The chat column's fourth tab is the shelf as a page: every picture she has taken, newest first,
with her own words for the shot and the settings that made it. It reads `generations.jsonl`
through `GET /api/gallery`, and it is deliberately lazy — nothing is fetched until you click the
tab, twelve pictures to a page, `< >` to walk back through them. A shot she takes while the tab is
open drops in on its own.

Each one takes **a score out of ten**. Click a number to rate it; click the number it already has
to take the rating back.

```
selfies/ratings.jsonl     {"image": "1763…-a1b2.png", "score": 8, "by": "user", "at": "…"}
```

Append-only and keyed by file name, like the 👍/👎 sidecar on her replies: a judgement that arrives
long after the render never edits the record of how that render was made. The last line for a name
wins, and `null` unrates. What it is *for*: her camera has a dozen knobs — backend, checkpoint,
sampler, the rows in her library — and no other way back from "that one came out badly" to the
settings that did it. The score sits beside the seed and the prompt, so the ledger can answer
which of them actually takes a good picture.

## Rate limits

```ini
TOOL_RATE_SELFIE=2                # calls per minute — images are expensive
TOOL_RATE_PICTURE=2               # show_picture gets its own budget, not a share
```

Separate buckets on purpose: they cost the same GPU, but they're different urges, and spending
her picture budget on the street below shouldn't stop her sending you her face a minute later.

Like every tool call, allowed or denied, each one appends an audit line to `TOOL_LOG_DIR`.
