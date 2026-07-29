"""Self-contained character registry, card parser, and storage core."""

from .card import (
    CardLimits,
    CardParseError,
    PNGCardParser,
    ParsedCard,
    card_fields,
    parse_png_card,
)
from .importer import (
    CharacterImportError,
    CharacterImporter,
    import_character_card,
)
from .connections import ConnectionProfile, ConnectionProfiles
from .defaults import (
    DEFAULT_PORTRAIT_NAME,
    default_portrait_bytes,
    install_default_portrait,
)
from .models import (
    BodyBinding,
    CharacterPaths,
    CharacterRecord,
    ConnectionBinding,
    DisplayMetadata,
    LifecycleFlags,
    LoopSwitches,
    ModelBinding,
    VoiceBinding,
    new_character_id,
)
from .registry import (
    REGISTRY_SCHEMA_VERSION,
    CharacterRegistry,
    atomic_write_bytes,
    atomic_write_json,
)

__all__ = [
    "BodyBinding",
    "CardLimits",
    "CardParseError",
    "CharacterImportError",
    "CharacterImporter",
    "CharacterPaths",
    "CharacterRecord",
    "CharacterRegistry",
    "ConnectionBinding",
    "ConnectionProfile",
    "ConnectionProfiles",
    "DEFAULT_PORTRAIT_NAME",
    "DisplayMetadata",
    "LifecycleFlags",
    "LoopSwitches",
    "ModelBinding",
    "PNGCardParser",
    "ParsedCard",
    "REGISTRY_SCHEMA_VERSION",
    "VoiceBinding",
    "atomic_write_bytes",
    "atomic_write_json",
    "card_fields",
    "default_portrait_bytes",
    "import_character_card",
    "install_default_portrait",
    "new_character_id",
    "parse_png_card",
]
