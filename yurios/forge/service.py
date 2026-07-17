"""ImageForge — the service YuriOS calls to get images.

It owns three things the backend never sees: *who she is* (the Character /
locked register), *what to render* (the selfie template library), and *what
leaves the building* (provenance). It turns a high-level ask — ``selfie()``,
``portrait()``, ``scenery()``, ``edit()`` — into a backend request, applies
provenance, and saves the result.

The backend is held behind one attribute and swappable at any time with
``set_backend(...)``. That is the whole point: the companion's image capability
is provider-agnostic, so you can move from a hosted API to your own GPU (or to an
uncensored local model for the intimate register, → ch. 11) without the rest of
the runtime noticing.
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
        scene: Optional[str] = None,
        framing: Optional[str] = None,
        lighting: Optional[str] = None,
        mood: Optional[str] = None,
        wardrobe: str = "everyday",
        seed: Optional[int] = None,
        save: bool = True,
        **over: Any,
    ) -> ImageResult:
        """A selfie 'of her': compose a varied scene from the template library
        (→ ch. 26, the anti-collapse fix), then render on-register."""
        scene_prompt, chosen = self.book.compose(
            scene=scene, framing=framing, lighting=lighting, mood=mood,
            wardrobe=wardrobe, seed=seed)
        label = "selfie-" + "-".join(chosen.get(k, "") for k in ("scene", "wardrobe")).strip("-")
        result = self.generate(scene_prompt, include_character=True, label=label or "selfie",
                               seed=seed, save=save, **over)
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
