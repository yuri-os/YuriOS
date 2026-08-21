"""ImageForge — the service YuriOS calls to get images.

It owns three things the backend never sees: *who she is* (the Character /
locked register), *what to render* (the selfie template library), and *what
leaves the building* (provenance). It turns a high-level ask — ``selfie()``,
``portrait()``, ``scenery()``, ``edit()`` — into a backend request, applies
provenance, and saves the result.

The backend is held behind one attribute and swappable at any time with
``set_backend(...)``. That is the whole point: the companion's image capability
is provider-agnostic, so you can move from a hosted API to your own GPU without
the rest of the runtime noticing.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

import yaml

from . import provenance as _prov
from .backends import ImageBackend, make_backend
from .character import Character
from .templates import SelfieBook
from .types import EditRequest, GenRequest, ImageResult


class ImageForge:
    def __init__(
        self,
        character: Character,
        book: SelfieBook,
        backend: ImageBackend,
        *,
        out_dir: str | Path = "out",
        provenance_mode: str = "strip",
    ) -> None:
        self.character = character
        self.book = book
        self.backend = backend
        self.out_dir = Path(out_dir)
        self.provenance_mode = provenance_mode

    # ---- construction ----

    @classmethod
    def from_config(cls, path: str | Path = "config.yaml") -> "ImageForge":
        path = Path(path)
        cfg = yaml.safe_load(path.read_text()) or {}
        root = path.parent

        def rel(p: str) -> Path:
            p = Path(p)
            return p if p.is_absolute() else (root / p)

        character = Character.load(rel(cfg["character"]))
        book = SelfieBook.load(rel(cfg["templates"]))
        b = cfg.get("backend", {"name": "mock"})
        backend = make_backend(b["name"], **{k: v for k, v in b.items() if k != "name"})
        return cls(
            character, book, backend,
            out_dir=rel(cfg.get("out_dir", "out")),
            provenance_mode=cfg.get("provenance", "strip"),
        )

    # ---- live backend swap ----

    def set_backend(self, backend: str | ImageBackend, **opts: Any) -> ImageBackend:
        """Swap the generator at runtime. Pass a name (+opts) or a ready instance."""
        self.backend = backend if isinstance(backend, ImageBackend) else make_backend(backend, **opts)
        return self.backend

    def capabilities(self):
        return self.backend.capabilities()

    # ---- the high-level asks ----

    def generate(
        self,
        scene_prompt: str,
        *,
        include_character: bool = True,
        negative_extra: str = "",
        label: str = "image",
        seed: Optional[int] = None,
        save: bool = True,
        **over: Any,
    ) -> ImageResult:
        """Assemble register + identity + scene, render, stamp, save."""
        positive, negative = self.character.assemble(
            scene_prompt, include_character=include_character, negative_extra=negative_extra)
        req = GenRequest(
            prompt=positive, negative_prompt=negative,
            width=over.pop("width", self.character.width),
            height=over.pop("height", self.character.height),
            steps=over.pop("steps", None),         # per-call override; else models.yaml default
            cfg=over.pop("cfg", None),
            seed=seed,
            reference_images=list(self.character.reference_images) if include_character else [],
            lora=self.character.lora if include_character else None,
            extra=over,
        )
        result = self.backend.generate(req)
        return self._finish(result, label, save)

    def selfie(
        self,
        *,
        look: str = "",
        scene: Optional[str] = None,
        framing: Optional[str] = None,
        lighting: Optional[str] = None,
        mood: Optional[str] = None,
        # Optional like every other slot: None is "she did not ask", which
        # `compose()` reads as leave-it-alone. The everyday default belongs to
        # a shot with nothing to go on, and the caller decides which that is.
        wardrobe: Optional[str] = "everyday",
        avoid: str = "",
        situation: str = "",
        seed: Optional[int] = None,
        save: bool = True,
        **over: Any,
    ) -> ImageResult:
        """A selfie 'of her': her own description of the shot, refined by any
        library slots she named, filled out by the world as it is, rendered
        on-register.

        The library rotates a shot in only when there is genuinely nothing to go
        on — no words of hers, no named slot, no situation. That is the honest
        reading of an empty ask ("take a selfie", no further thought), and it is
        the *only* case that should surprise her: everywhere else, what comes
        back is what she described.
        """
        # The world fills a *gap*, never an argument. She has said where she is
        # the moment she writes a `look` or names a `scene`, and appending "rain
        # on the window at night" to her sunlit beach is worse than adding
        # nothing at all — so context arrives only when nobody has set the
        # picture's place. A mood-only or wardrobe-only ask is exactly the case
        # it exists for.
        placed = bool(look.strip() or scene)
        if placed:
            situation = ""
        asked = bool(placed or situation.strip()
                     or any(v is not None for v in (framing, lighting, mood)))
        scene_prompt, chosen, negative_extra = self.book.compose(
            look=look, scene=scene, framing=framing, lighting=lighting, mood=mood,
            wardrobe=wardrobe, situation=situation, seed=seed, rotate=not asked)
        if avoid.strip():
            # Her own "not like that" (→ ch. 11: still no enforcement posture —
            # this is her steering the picture, not the engine refusing one).
            negative_extra = " ".join(x for x in (negative_extra, avoid.strip()) if x)
            chosen["avoid"] = avoid.strip()
        label = "selfie-" + "-".join(chosen.get(k, "") for k in ("scene", "wardrobe")).strip("-")
        result = self.generate(scene_prompt, include_character=True, label=label or "selfie",
                               negative_extra=negative_extra, seed=seed, save=save, **over)
        result.meta["template"] = chosen
        return result

    def portrait(self, *, seed: Optional[int] = None, save: bool = True, **over: Any) -> ImageResult:
        """The canonical hero portrait — the source of truth other media match
        (→ ch. 26, one source of truth)."""
        return self.selfie(scene="portrait", framing="portrait", lighting="lamplit",
                           mood="waiting", wardrobe="signature", seed=seed, save=save, **over)

    def scenery(self, scene_prompt: str, *, label: str = "scenery", save: bool = True, **over: Any) -> ImageResult:
        """Worldbuilding atlas render — no figure in frame (→ ch. 26)."""
        return self.generate(scene_prompt, include_character=False, label=label, save=save, **over)

    def picture(self, subject: str, *, avoid: str = "", seed: Optional[int] = None,
                save: bool = True, **over: Any) -> ImageResult:
        """A picture of something that *isn't* her — what she's looking at, the
        street below, a thing she's describing, a drawing she made.

        It is `scenery()` with a companion's manners: her words are the whole
        prompt (there is no library and no rotation here — a menu of five
        framings could never anticipate what she might want to show you), her
        `avoid` steers the same way it does on a selfie, and the chosen record
        comes back shaped like a selfie's so the lab can announce either kind
        without knowing which it got. Her likeness stays out of frame, which is
        the entire difference: a photo of the rain should not have her in it
        just because she is the one who took it.
        """
        result = self.scenery(
            subject.strip(), label="picture", seed=seed, save=save,
            negative_extra=avoid.strip(), **over)
        chosen = {"look": subject.strip()}
        if avoid.strip():
            chosen["avoid"] = avoid.strip()
        result.meta["template"] = chosen
        return result

    def edit(self, image: str | Path, instruction: str, *, label: str = "edit",
             seed: Optional[int] = None, strength: float = 0.7,
             use_identity: bool = True, save: bool = True, **over: Any) -> ImageResult:
        """Reference-driven re-render: hold her identity, change the scene/outfit.

        Passes the character's reference images so an identity-aware backend
        (e.g. diffusers + IP-Adapter) keeps *her* across the edit (→ ch. 26).
        """
        req = EditRequest(
            image=Path(image), instruction=instruction, seed=seed, strength=strength,
            reference_images=list(self.character.reference_images) if use_identity else [],
            extra=over)
        result = self.backend.edit(req)
        return self._finish(result, label, save)

    # ---- shared tail ----

    def _finish(self, result: ImageResult, label: str, save: bool) -> ImageResult:
        result.data = _prov.apply(result.data, result.meta, self.provenance_mode)
        result.meta["provenance"] = self.provenance_mode
        result.meta["character"] = self.character.name
        if save:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            seed = result.meta.get("seed", "x")
            path = result.save(self.out_dir / f"{stamp}-{label}-{seed}.png")
            self._write_provenance(path, result.meta)
        return result

    def _write_provenance(self, image_path: Path, meta: dict) -> None:
        """Record exactly how an image was made so any render is reproducible:
        backend + model (local LoRA or remote API), full prompt + negative, seed,
        and all sampler settings. Written two ways — a per-image ``.json`` sidecar
        next to the PNG (travels with the file), and one appended line in
        ``out/generations.jsonl`` (a scannable ledger of every render)."""
        record = {"image": image_path.name, **meta}
        blob = json.dumps(record, indent=2, ensure_ascii=False, default=str)
        image_path.with_suffix(".json").write_text(blob + "\n", encoding="utf-8")
        with (self.out_dir / "generations.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
