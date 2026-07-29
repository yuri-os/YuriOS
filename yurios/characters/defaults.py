"""The portrait a fresh Yuri starts with.

0.1 had nowhere to keep a face: the legacy roots are vault/corpus/traces/tool-logs/
selfies, none of which carries an image. So every character the migration assembles
— which is also what a first-time install becomes, since `install.sh` seeds the 0.1
Vault and the first run migrates it — arrives with `portrait.png` missing: the
dashboard tile renders a placeholder and an exported card gets the blank slate
`_export_card` falls back to.

This ships her canon portrait (the D-011 register the forge renders to, → ch. 26)
and hands it to that character once, at migration. Only ever *once*, and only when
the file is absent: a portrait the user replaced, or one the forge rendered, is
hers and is never overwritten.

The image is Yuri's face, so it is only installed for Yuri. Someone who pointed
`SOUL_SRC` at another character's SOUL gets no default rather than the wrong one.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

from .models import CharacterPaths
from .registry import atomic_write_bytes

DEFAULT_PORTRAIT_RESOURCE = "assets/default-portrait.png"

# The name in the seeded soul.yaml this portrait belongs to (matched case-folded).
DEFAULT_PORTRAIT_NAME = "yuri"


def default_portrait_bytes() -> bytes | None:
    """Her shipped portrait, or ``None`` if this install has no packaged copy."""
    try:
        return (
            resources.files(__package__)
            .joinpath(DEFAULT_PORTRAIT_RESOURCE)
            .read_bytes()
        )
    except (FileNotFoundError, OSError, ModuleNotFoundError):
        return None


def install_default_portrait(paths: CharacterPaths, display_name: str) -> bool:
    """Write the default portrait for *display_name*, if it is hers and absent.

    Returns whether a portrait was written.  Never raises: a character without a
    face is a cosmetic loss, and nothing about a migration should fail over one.
    """
    if display_name.strip().casefold() != DEFAULT_PORTRAIT_NAME:
        return False
    portrait = Path(paths.portrait)
    try:
        if portrait.exists():
            return False
        png = default_portrait_bytes()
        if png is None:
            return False
        portrait.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(portrait, png)
    except OSError:
        return False
    return True
