# Third-Party Notices

- Three.js r173, MIT License: <https://github.com/mrdoob/three.js>
- `@pixiv/three-vrm` 3.5.4, MIT License: <https://github.com/pixiv/three-vrm>
- `@pixiv/three-vrm-animation` 3.5.4, MIT License: <https://github.com/pixiv/three-vrm>
- Kokoro-82M, Apache-2.0 model and software: <https://huggingface.co/hexgrad/Kokoro-82M>
- `AvatarSample_B`, VRoid Project sample model: <https://hub.vroid.com/en/characters/7932226027492445209/models/3535390362223210114>
- `idle.vrma` (the idle animation clip), from pixiv Inc.'s `ChatVRM` reference implementation
  (`public/idle_loop.vrma`, byte-identical, sha256 `ace95ba6dcc0bdf2ed1081c002332b4184441117c8d543b6f642b3d2c5cf99be`),
  MIT License, Copyright (c) 2023 pixiv Inc.: <https://github.com/pixiv/ChatVRM>

Review a model's own license before loading or redistributing it.

## User-supplied image checkpoints (`SELFIE_BACKEND=diffusers`)

No image-generation weights are shipped with this repository. With the local
`diffusers` forge backend, the user downloads an SDXL checkpoint themselves
(e.g. an Illustrious-lineage model such as the [Pie Models](https://civitai.com/models/1593793/pie-models)
family from Civitai; Illustrious itself is by [OnomaAI](https://civitai.com/ecosystems/illustrious)).
Each checkpoint is governed by its own license as published on its download
page — review it before use, and never redistribute the weights from this repo.

The same knob also loads a user-supplied **Krea 2** checkpoint (detected from
the file, rendered by the `krea2` backend). Two further parties are involved,
and neither is redistributed here:

- `comfy-kitchen`, Apache-2.0, Copyright (c) Comfy Org: <https://pypi.org/project/comfy-kitchen>
  — the INT4 "ConvRot W4A4" tensor layout and its GEMM, installed with the
  `forge-krea2` extra.
- The text encoder and VAE are fetched at first render from
  [`krea/Krea-2-Raw`](https://huggingface.co/krea/Krea-2-Raw), a **gated** repo
  under Krea's own licence (accept it on that page, then `huggingface-cli
  login`). Krea 2 weights — including any community quantization of them —
  carry that licence and its distribution restrictions. Review it before use.
