# Voice

Her voice loop is three seams — **ears** (STT), **voice** (TTS) and **turn-taking** (VAD) — behind
one real-time websocket. Every one of them has a fake, so she boots and runs with none of them
installed; a fake seam is simply silent, and says so on startup.

The default install gives you all three, CPU-only, no CUDA: faster-whisper ears, the kokoro voice,
silero turn-taking. Their model weights fetch themselves the first time she speaks, and after
that she is offline.

## Using it

Open her sanctuary, click **enter the sanctuary**, then click **start listening** (the mic button,
bottom-left) to grant the page your microphone. Then just talk. You can interrupt her mid-sentence
— barge-in kills whatever she's saying, scripted or replied, and a barged-in turn persists
nothing: no memory line, no commit.

### Muting her

The speaker button beside the mic silences her voice on this device — it's on every page
(sanctuary, Live2D, text room) and it's remembered per character, so a muted room opens muted.

It mutes the speakers, not her: she keeps talking, her mouth keeps moving, her captions and the
transcript keep filling. Your microphone is a separate button, and the sanctuary's rain is a
third — muting her voice doesn't touch either.

## Ears (STT)

```ini
STT_BACKEND=faster_whisper        # faster_whisper | fake
STT_MODEL=base.en                 # tiny.en | base.en | small.en | medium.en | large-v3
STT_COMPUTE=int8                  # CTranslate2 quantization
```

faster-whisper runs on CTranslate2 and pulls **no torch at all** — it's the cheapest real backend
in the build (564 MB installed). Smaller models trade accuracy for latency; `base.en` is the
shipped compromise. On WSL her ears stay on the CPU, where the GPU passthrough can't reliably load
them.

An utterance that the VAD didn't confirm as real speech is dropped, and a transcript that is only
punctuation (whisper's favourite hallucination on silence) is dropped too — that's what keeps a
mechanical keyboard from starting a turn.

## Voice (TTS)

```ini
TTS_BACKEND=kokoro                # kokoro | qwen3_tts | gpt_sovits | fake
```

| Backend | What it is | Cost |
|---|---|---|
| **`kokoro`** *(default)* | CPU, faster-than-real-time, needs no GPU — leaves the whole GPU for the LLM and the body | `.[tts]`, 1.3 GB |
| `qwen3_tts` | the *designed* persona voice — authored from words once, then cloned so every utterance shares one timbre | `.[tts-qwen]`, wants CUDA |
| `gpt_sovits` | a zero-shot cloner: a client for a GPT-SoVITS server you run yourself | `.[tts-sovits]`, +2 MB |
| `fake` | silence, honestly reported | free |

### kokoro

Needs `espeak-ng` as a **system** package — install it even though a pip wheel bundles a copy.
The bundled phoneme data loses espeak-ng's own path-resolution race, and its answer to data it
can't read is to `exit(1)` the process. Kokoro checks for a working one in a child process and
falls back to the fake rather than take the server down with it.

`TTS_REGISTER` picks the voice: `default`, `late_night`, `expressive` — or any kokoro voice id
directly.

### qwen3_tts

The voice is **designed** once (that render is the bundled `designed.wav`) and then **cloned**
from it, because designing per-turn drifts and every utterance would sound like a slightly
different person.

```ini
TTS_BACKEND=qwen3_tts
QWEN_MODE=clone                   # clone (default, stable) | design (authors each turn, drifts)
QWEN_MODEL=Qwen/Qwen3-TTS-12Hz-1.7B-Base
QWEN_REF_AUDIO=                   # "" = the bundled designed clip
QWEN_REF_TEXT=                    # its transcript
QWEN_INSTRUCT=A warm, gentle young woman in her twenties; …
QWEN_DEVICE=cuda:0
QWEN_DTYPE=bfloat16
QWEN_ATTN=sdpa                    # flash_attention_2 for lower VRAM, if installed
```

### gpt_sovits

An HTTP client for a server you run, so no model runs in-process. It's a zero-shot cloner, so
every call needs a reference clip **and its exact transcript** — the server holds no default. The
defaults reuse the bundled designed clip so it works standalone; swap `SOVITS_REF_AUDIO` for the
voice you want. That path is on the *server's* filesystem.

```ini
TTS_BACKEND=gpt_sovits
SOVITS_BASE_URL=http://127.0.0.1:9880
SOVITS_REF_AUDIO=                 # "" → the bundled designed.wav
SOVITS_PROMPT_TEXT=               # "" → its transcript
SOVITS_PROMPT_LANG=en
SOVITS_TEXT_LANG=en
```

## Turn-taking (VAD)

```ini
VAD_BACKEND=silero                # silero | fake
VAD_THRESHOLD=0.5                 # speech-probability gate
VAD_MIN_SILENCE_MS=250            # endpointing dead air
VAD_ONSET_FRAMES=3                # consecutive speech frames that confirm a new turn
VAD_BARGEIN_FRAMES=5              # …and that confirm an interruption (stricter, on purpose)
VAD_CONFIRM=true                  # server-side: require confirmed speech in an endpointed turn
FRAME_MS=32                       # audio frame size fed to VAD/STT
```

The debounce is what makes it livable. A mechanical-keyboard click is a 1–2 frame transient; real
speech sustains, so requiring a *run* of frames rejects "I typed and she stopped". Interrupting
her is held to a higher bar than starting a new turn, because a false barge-in cuts her off
mid-sentence.

If a quiet mic or an over-strict VAD is dropping real speech, lower `VAD_THRESHOLD` or turn
`VAD_CONFIRM` off — the transcript filter still catches the punctuation-only hallucinations.

## Latency

The bar is **≤ ~1.2 s from end-of-speech to first audio**. Five stages stack up to it (endpoint →
STT → prompt assembly → first token → first TTS chunk), so the loop is measured rather than
assumed: every turn writes a trace with per-stage marks *and* the one number that matters,
end-of-speech → first sample out of the speaker. Traces land in `TRACE_DIR`.

`MASK_LATENCY=true` (the default) covers the gap with a **filler**: the instant your turn ends,
before the model has produced a token, she plays a short content-free reaction ("mm—", a breath).
Real conversation is full of these and it reads as attentiveness, not lag. Two rules keep it
honest: the clips are pre-rendered once and cached (firing one is tens of milliseconds), and
filler is real audio, so the same barge-in path that kills a reply kills a filler.

`MAX_REPLY_TOKENS` (1600) is a roomy ceiling, not a target — it leaves room for a heartfelt turn.
A no-think reply stops when it's done and rarely nears it.

`CHAT_THINKING=false` is the other half of real-time: a reasoning model that thinks before it
speaks stalls the voice loop. The utility model keeps thinking on, because it runs off the hot
path.

## Lip-sync

Visemes are derived from the audio she's actually producing and ride the event bus alongside it,
so her mouth matches her voice on both bodies rather than approximating it from text.

## Text only

Skip the voice stack entirely:

```ini
STT_BACKEND=fake
TTS_BACKEND=fake
VAD_BACKEND=fake
```

That's what `./install.sh --thin` writes. She's silent and says so; the chat column, the tools,
the bodies and the whole mind work exactly as before. Rerun `./install.sh` to add the real
backends later — everything is additive.

## Checking what's actually loaded

```bash
python -m yurios.doctor            # what .env selects vs what's installed
curl localhost:8768/api/health     # {"voice": {"ready", "stt", "tts", "vad"}, …}
```

A missing dependency is never a hard failure: the seam falls back to its fake and logs the exact
command that fixes it.
