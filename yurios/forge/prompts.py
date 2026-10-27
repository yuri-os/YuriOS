"""The camera's wording — prefix, postfix and negatives, per model (SPEC §7.6).

`Character` (character.py) is *who she is*; this is *how a picture is asked
for*, which depends on the model doing the asking. An SDXL base answers to booru
quality tags, an Anima checkpoint to its own `score_*` tags and a tag-shaped
negative, a hosted model to plain prose — and two checkpoints of the same
family can still want different words. So the four "how" fields
(`FIELDS`) can be set at three targets:

    every model  <  one architecture  <  one model (checkpoint file / hosted id)

and in four layers, each one later winning over everything before it:

    shipped register  <  house file  <  her appearance.yaml  <  her own file

A layer is one profile: the top-level fields are its "every model" cell,
`architectures:` and `models:` its narrower ones. Within a layer the narrowest
cell that sets a field wins; across layers the later layer wins outright. So her
page always beats the house, and a house setting for one model beats the
house's general one. An empty field is never a setting — it means "inherit".

Resolution happens per render (`PromptSources.resolve`), not once at boot, so
an edit in the studio reaches the very next shot without a restart, and the
camera it resolves for is the backend actually holding the card.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import yaml

log = logging.getLogger("forge.prompts")

#: The fields a profile may set, in prompt order, with what the studio calls them.
FIELDS: tuple[tuple[str, str, str], ...] = (
    ("quality_preamble", "Prefix",
     "Leads every prompt, before who she is — quality and style tags."),
    ("quality_postfix", "Postfix",
     "Closes every prompt, after the scene."),
    ("base_negative", "Negative",
     "Every render's negative prompt."),
    ("character_negative", "Negative, when she's in the shot",
     "Added to the negative whenever a person is in frame — the anatomy guard."),
)
FIELD_KEYS = tuple(key for key, _label, _hint in FIELDS)

#: The architecture keys a profile's `architectures:` section is keyed by —
#: the local ones are `sniff.py`'s answers, the rest are backend names.
ARCHITECTURES: tuple[tuple[str, str], ...] = (
    ("sdxl", "SDXL"),
    ("krea2", "Krea 2"),
    ("anima", "Anima"),
    ("openrouter", "Hosted (OpenRouter)"),
    ("mock", "Mock"),
)

#: The house's own file, under DATA_DIR, and each character's, beside her
#: appearance.yaml. Both are written by the studio and hand-editable.
HOUSE_FILE = "selfie-prompts.yaml"
CHARACTER_FILE = "selfie-prompts.yaml"


@dataclass
class Profile:
    """One layer: its every-model cell and its narrower ones."""
    default: dict[str, str] = field(default_factory=dict)
    architectures: dict[str, dict[str, str]] = field(default_factory=dict)
    models: dict[str, dict[str, str]] = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not (self.default or self.architectures or self.models)

    def to_dict(self) -> dict[str, Any]:
        return {"default": dict(self.default),
                "architectures": {k: dict(v) for k, v in self.architectures.items()},
                "models": {k: dict(v) for k, v in self.models.items()}}


def _cell(raw: Any, where: str, *, strict: bool) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"{where} must be a mapping of fields")
    cell: dict[str, str] = {}
    for key, value in raw.items():
        if key not in FIELD_KEYS:
            if strict:
                raise ValueError(f"{where}: unknown field {key!r} — expected one of "
                                 f"{', '.join(FIELD_KEYS)}")
            continue
        if value is None:
            continue
        if not isinstance(value, str):
            raise ValueError(f"{where}.{key} must be text")
        text = " ".join(value.split())        # one line, however it was folded
        if text:                               # empty is "inherit", never a value
            cell[key] = text
    return cell


def _section(raw: Any, name: str, *, strict: bool) -> dict[str, dict[str, str]]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"{name} must be a mapping")
    out: dict[str, dict[str, str]] = {}
    for key, value in raw.items():
        target = str(key).strip()
        if not target:
            raise ValueError(f"{name} has an entry with no name")
        cell = _cell(value, f"{name}.{target}", strict=strict)
        if cell:
            out[target] = cell
    return out


def parse(data: Any, *, strict: bool = True) -> Profile:
    """A profile from a parsed YAML/JSON mapping. `strict` refuses keys this
    module doesn't know (a prompts file); off, they're ignored (an appearance
    file, whose `name` and `identity` are someone else's business).

    Accepts both the file shape (fields at the top level) and the API shape
    (`default:` holding them), so the studio and a hand-written file agree."""
    if data is None:
        return Profile()
    if not isinstance(data, Mapping):
        raise ValueError("a prompt profile must be a mapping")
    allowed = {*FIELD_KEYS, "default", "architectures", "models"}
    if strict:
        unknown = sorted(str(k) for k in data if k not in allowed)
        if unknown:
            raise ValueError(f"unknown key(s) {', '.join(unknown)} — a prompt profile "
                             f"holds {', '.join(FIELD_KEYS)}, architectures and models")
    default = _cell({k: v for k, v in data.items() if k in FIELD_KEYS},
                    "default", strict=strict)
    default.update(_cell(data.get("default"), "default", strict=strict))
    return Profile(default=default,
                   architectures=_section(data.get("architectures"), "architectures",
                                          strict=strict),
                   models=_section(data.get("models"), "models", strict=strict))


def load(path: str | Path | None, *, strict: bool = True) -> Profile:
    """The profile in a file; an absent file is an empty profile."""
    if not path:
        return Profile()
    p = Path(path)
    if not p.is_file():
        return Profile()
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ValueError(f"{p.name} is not valid YAML: {e}") from e
    return parse(data, strict=strict)


_HEADER = """\
# How her camera asks for a picture — prefix, postfix and negatives, by model
# (SPEC §7.6; forge/prompts.py). Written by the studio's Selfies section and
# yours to edit. Top-level fields apply to every model; `architectures:` (sdxl,
# krea2, anima, openrouter, mock) and `models:` (a checkpoint's file name, or a
# hosted model id) narrow them. The narrowest one that sets a field wins; an
# empty field inherits.
"""


def render_yaml(profile: Profile) -> str:
    body: dict[str, Any] = dict(profile.default)
    if profile.architectures:
        body["architectures"] = profile.architectures
    if profile.models:
        body["models"] = profile.models
    text = yaml.safe_dump(body, sort_keys=False, allow_unicode=True, width=88) if body else ""
    return _HEADER + ("\n" + text if text else "")


def write(path: str | Path, profile: Profile) -> None:
    """Write a profile atomically, or remove the file when it says nothing."""
    p = Path(path)
    if profile.is_empty():
        p.unlink(missing_ok=True)
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(render_yaml(profile), encoding="utf-8")
    tmp.replace(p)


# ---- resolution --------------------------------------------------------------

def resolve(layers: Sequence[tuple[str, Profile]], arch: str,
            model: str) -> dict[str, dict[str, str]]:
    """Each field's value for this camera, and where it came from.

    `layers` is (name, profile), earliest first. Returns
    `{field: {"value": text, "source": "<layer> · <target>"}}` for every field,
    with an empty value and source for a field nobody set."""
    out = {key: {"value": "", "source": ""} for key in FIELD_KEYS}
    for name, profile in layers:
        cells: list[tuple[str, Optional[dict[str, str]]]] = [
            ("every model", profile.default),
            (f"architecture {arch}", profile.architectures.get(arch)),
            (f"model {model}", profile.models.get(model) if model else None)]
        for target, cell in cells:             # broadest first: narrower wins
            for key in FIELD_KEYS:
                if cell and cell.get(key):
                    out[key] = {"value": cell[key], "source": f"{name} · {target}"}
    return out


def camera_of(backend: Any) -> tuple[str, str]:
    """(architecture, model) of the backend holding the card — the keys the
    `architectures:` and `models:` sections are matched against. A local
    checkpoint is named by its file name, so moving the drive keeps its
    settings; a hosted model by its id."""
    name = str(getattr(backend, "name", "") or "")
    arch = "sdxl" if name == "diffusers" else name
    model_path = str(getattr(backend, "model_path", "") or "")
    model = Path(model_path).name if model_path else str(getattr(backend, "model", "") or "")
    return arch, model


@dataclass
class PromptSources:
    """Where one camera's wording comes from, read afresh on every render."""
    register: Optional[Path] = None           # the shipped register
    house: Optional[Path] = None              # DATA_DIR/selfie-prompts.yaml
    appearance: Optional[Path] = None         # her appearance.yaml (top level)
    character: Optional[Path] = None          # her own selfie-prompts.yaml

    def layers(self) -> list[tuple[str, Profile]]:
        """The readable layers, earliest first. A file that won't parse is
        skipped with a warning rather than failing the shot — a typo in a
        hand-edited file should cost its wording, not her camera.

        An appearance field that merely restates the shipped register is not
        an override: the shipped yuri.yaml carries the register's prefix and
        negative word for word, and counting them as hers would put them above
        every house and per-model setting."""
        out: list[tuple[str, Profile]] = []
        for name, path, strict in (("shipped", self.register, False),
                                   ("house", self.house, True),
                                   ("her appearance", self.appearance, False),
                                   ("her", self.character, True)):
            try:
                profile = load(path, strict=strict)
            except (ValueError, OSError) as e:
                log.warning("selfies: ignoring the %s prompt wording in %s: %s",
                            name, path, e)
                continue
            if name == "her appearance" and out:
                shipped = out[0][1].default
                profile.default = {k: v for k, v in profile.default.items()
                                   if shipped.get(k) != v}
            out.append((name, profile))
        return out

    def resolve(self, backend: Any) -> dict[str, dict[str, str]]:
        arch, model = camera_of(backend)
        return resolve(self.layers(), arch, model)
