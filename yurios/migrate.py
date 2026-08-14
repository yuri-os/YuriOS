"""One-time migration from YuriOS 0.1's shared roots to the 0.2 layout.

The legacy directories are copied, never moved.  A character is assembled under a
same-filesystem staging directory and made visible with one rename; ``layout.json``
is written last and is the durable indication that migration completed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from yurios.characters import (
    BodyBinding,
    CharacterPaths,
    CharacterRecord,
    CharacterRegistry,
    ConnectionBinding,
    DisplayMetadata,
    LifecycleFlags,
    LoopSwitches,
    ModelBinding,
    VoiceBinding,
    atomic_write_bytes,
    atomic_write_json,
    install_default_portrait,
)


LEGACY_LAYOUT_VERSION = "0.1"
CURRENT_LAYOUT_VERSION = "0.2"
LAYOUT_MARKER_NAME = "layout.json"
MIGRATION_MANIFEST_NAME = ".migration.json"
MIGRATION_NAME = "0.1-to-0.2"


class MigrationError(RuntimeError):
    """The migration cannot proceed without risking or losing data."""


@dataclass(frozen=True, slots=True)
class MigrationResult:
    status: str
    data_dir: Path
    character_id: str | None
    character_root: Path | None
    display_name: str | None

    @property
    def changed(self) -> bool:
        return self.status == "migrated"

    @property
    def migration_required(self) -> bool:
        return self.status == "needed"


@dataclass(frozen=True, slots=True)
class _Source:
    config_name: str
    destination_name: str
    path: Path
    required: bool = False


def _value(config: object, name: str, default: Any = None) -> Any:
    return getattr(config, name, default)


def _default_data_dir(config: object) -> Path:
    configured = _value(config, "data_dir")
    if configured is not None:
        return Path(configured)
    return Path(os.environ.get("DATA_DIR", "./data"))


def _canonical(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _sources(config: object) -> tuple[_Source, ...]:
    definitions = (
        ("vault_dir", "vault", True),
        ("corpus_dir", "corpus", False),
        ("trace_dir", "traces", False),
        ("tool_log_dir", "tool-logs", False),
        ("selfie_dir", "selfies", False),
    )
    result: list[_Source] = []
    for config_name, destination, required in definitions:
        value = _value(config, config_name)
        if value is None:
            if required:
                raise MigrationError(f"legacy config has no {config_name}")
            # A reduced Config can omit an optional root.  It migrates as empty.
            path = Path.cwd() / f".__missing_{config_name}__"
        else:
            raw_path = Path(value).expanduser()
            if raw_path.is_symlink():
                raise MigrationError(
                    f"legacy {config_name} is a symbolic link: {raw_path}"
                )
            path = raw_path.resolve()
        result.append(_Source(config_name, destination, path, required))
    return tuple(result)


def _validate_tree(root: Path, label: str) -> None:
    """Reject links and special files instead of following data outside a root."""
    try:
        stack = [root]
        while stack:
            directory = stack.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.is_symlink():
                        raise MigrationError(
                            f"legacy {label} contains a symbolic link: {entry.path}"
                        )
                    mode = entry.stat(follow_symlinks=False).st_mode
                    if stat.S_ISDIR(mode):
                        stack.append(Path(entry.path))
                    elif not stat.S_ISREG(mode):
                        raise MigrationError(
                            f"legacy {label} contains a special file: {entry.path}"
                        )
    except MigrationError:
        raise
    except OSError as exc:
        raise MigrationError(f"cannot inspect legacy {label}: {exc}") from exc


def _read_soul_name(vault: Path, config: object) -> str:
    soul_path = vault / "soul" / "soul.yaml"
    try:
        raw = soul_path.read_bytes()
        document = yaml.safe_load(raw)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise MigrationError(f"invalid legacy soul manifest {soul_path}: {exc}") from exc
    if not isinstance(document, Mapping):
        raise MigrationError(f"invalid legacy soul manifest {soul_path}: expected a mapping")

    existing_format = document.get("vault_format")
    if existing_format is not None and str(existing_format) not in {
        LEGACY_LAYOUT_VERSION,
        CURRENT_LAYOUT_VERSION,
    }:
        raise MigrationError(f"unsupported legacy vault_format: {existing_format!r}")

    name = document.get("name")
    if not isinstance(name, str) or not name.strip():
        name = _value(config, "companion_name", "")
    if not isinstance(name, str) or not name.strip():
        raise MigrationError("soul.yaml has no name and companion_name is empty")
    return name.strip()


def _any_source_exists(sources: tuple[_Source, ...]) -> bool:
    """Is there any 0.1 data here at all?

    Every boot comes through the migration (world/__main__.py), so the common
    case is a machine that never ran 0.1 and has nothing to bring forward. That
    is a fresh install, not a fault — and it must not be told its vault_dir is
    missing and refused a boot. A vault_dir that is absent while *other* legacy
    roots are present is still a broken pointer, and _validate_sources still
    refuses it.
    """
    for source in sources:
        try:
            if source.path.exists():
                return True
        except OSError as exc:
            raise MigrationError(f"cannot inspect legacy {source.config_name}: {exc}") from exc
    return False


def _validate_sources(
    sources: tuple[_Source, ...], data_dir: Path, characters_dir: Path
) -> str:
    existing: list[_Source] = []
    for source in sources:
        try:
            exists = source.path.exists()
            is_dir = source.path.is_dir()
            is_symlink = source.path.is_symlink()
        except OSError as exc:
            raise MigrationError(f"cannot inspect legacy {source.config_name}: {exc}") from exc
        if not exists:
            if source.required:
                raise MigrationError(
                    f"legacy {source.config_name} does not exist: {source.path}"
                )
            continue
        if not is_dir:
            raise MigrationError(
                f"legacy {source.config_name} is not a directory: {source.path}"
            )
        if is_symlink:
            raise MigrationError(
                f"legacy {source.config_name} is a symbolic link: {source.path}"
            )
        if source.path == data_dir or _is_within(characters_dir, source.path):
            raise MigrationError(
                f"DATA_DIR would place staging inside legacy {source.config_name}: "
                f"{source.path}"
            )
        _validate_tree(source.path, source.config_name)
        existing.append(source)

    for index, left in enumerate(existing):
        for right in existing[index + 1 :]:
            if _is_within(left.path, right.path) or _is_within(right.path, left.path):
                raise MigrationError(
                    f"legacy roots overlap: {left.config_name}={left.path} and "
                    f"{right.config_name}={right.path}"
                )
    return str(next(source.path for source in sources if source.required))


def _read_layout_marker(path: Path) -> Mapping[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MigrationError(f"invalid data layout marker {path}: {exc}") from exc
    if not isinstance(value, Mapping):
        raise MigrationError(f"invalid data layout marker {path}: expected an object")
    version = value.get("layout_version")
    if version != CURRENT_LAYOUT_VERSION:
        raise MigrationError(f"unsupported data layout version: {version!r}")
    return value


def _current_result(
    marker: Mapping[str, Any], registry: CharacterRegistry, data_dir: Path
) -> MigrationResult:
    migration = marker.get("migration")
    if not isinstance(migration, Mapping):
        return MigrationResult("current", data_dir, None, None, None)
    character_id = migration.get("character_id")
    if not isinstance(character_id, str):
        raise MigrationError("layout migration manifest has no character_id")
    record = registry.get(character_id)
    if record is None or not record.paths.root.is_dir():
        raise MigrationError(
            f"layout marker names missing character {character_id!r}; refusing to overwrite"
        )
    return MigrationResult(
        "already-migrated",
        data_dir,
        character_id,
        record.paths.root,
        record.display.name,
    )


def _source_manifest(sources: tuple[_Source, ...]) -> dict[str, Any]:
    return {
        source.config_name: {
            "path": str(source.path),
            "present": source.path.is_dir(),
        }
        for source in sources
    }


def _existing_migration(
    registry: CharacterRegistry, sources: tuple[_Source, ...]
) -> CharacterRecord | None:
    expected = _source_manifest(sources)
    for record in registry:
        manifest_path = record.paths.root / MIGRATION_MANIFEST_NAME
        if not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if (
            isinstance(manifest, Mapping)
            and manifest.get("migration") == MIGRATION_NAME
            and manifest.get("sources") == expected
            and manifest.get("character_id") == record.id
        ):
            return record
    return None


def _backfill_default_portrait(
    registry: CharacterRegistry, character_id: str | None
) -> None:
    """Hand her face to an install that migrated before the portrait shipped.

    Every start-up runs the migration, so this is where an already-migrated 0.2
    data directory picks up the packaged portrait: one ``exists()`` on the fast
    path, and a no-op the moment there is any portrait to keep.
    """
    if character_id is None:
        return
    record = registry.get(character_id)
    if record is not None:
        install_default_portrait(record.paths, record.display.name)


def _next_character_id(registry: CharacterRegistry, characters_dir: Path) -> str:
    for number in range(1, 10_001):
        candidate = "yuri" if number == 1 else f"yuri-{number}"
        root = characters_dir / candidate
        if registry.get(candidate) is None and not os.path.lexists(root):
            return candidate
    raise MigrationError("cannot allocate a collision-free character id based on 'yuri'")


def _connection_binding(config: object) -> ConnectionBinding:
    return ConnectionBinding(profile="legacy-default")


def _record(
    config: object, character_id: str, display_name: str, root: Path
) -> CharacterRecord:
    chat = str(_value(config, "chat_model", "") or "")
    utility = str(_value(config, "utility_model", "") or "")
    # A blank binding inherits the house .env. Do not turn an unconfigured
    # legacy install into a per-character NONE override.
    if chat.upper() == "NONE":
        chat = ""
    if utility.upper() == "NONE":
        utility = ""
    model_options = {
        name: _value(config, name)
        for name in ("context_length", "chat_thinking", "utility_thinking")
        if _value(config, name) is not None
    }
    return CharacterRecord(
        id=character_id,
        display=DisplayMetadata(name=display_name),
        paths=CharacterPaths.under(root),
        lifecycle=LifecycleFlags(
            enabled=True, autostart=True, review_required=False
        ),
        loops=LoopSwitches(
            mind=bool(_value(config, "mind_enabled", True)),
            utility=True,
            dream=True,
        ),
        connection=_connection_binding(config),
        models=ModelBinding(
            chat=chat,
            utility=utility,
            options=model_options,
        ),
        voice=VoiceBinding(
            tts_backend=str(_value(config, "tts_backend", "") or ""),
            voice_id=str(_value(config, "tts_register", "") or ""),
            stt_backend=str(_value(config, "stt_backend", "") or ""),
        ),
        body=BodyBinding(
            backend=str(_value(config, "desktop_body", "") or ""),
            model=str(_value(config, "avatar_model", "") or ""),
        ),
    )


def _repair_legacy_model_bindings(
    registry: CharacterRegistry, marker: Mapping[str, object], marker_path: Path
) -> None:
    """Convert pre-fix migrated NONE bindings into inherited house settings once."""
    migration = marker.get("migration")
    if not isinstance(migration, Mapping) or migration.get("name") != MIGRATION_NAME:
        return
    if marker.get("model_binding_inheritance_repaired"):
        return
    character_id = migration.get("character_id")
    record = registry.get(str(character_id)) if character_id else None
    if record is not None:
        changed = False
        for field in ("chat", "utility"):
            if getattr(record.models, field).upper() == "NONE":
                setattr(record.models, field, "")
                changed = True
        if changed:
            registry.upsert(record)
    updated_marker = dict(marker)
    updated_marker["model_binding_inheritance_repaired"] = True
    atomic_write_json(marker_path, updated_marker)


def _copy_sources(sources: tuple[_Source, ...], staged: CharacterPaths) -> None:
    for source in sources:
        destination = staged.root / source.destination_name
        if source.path.is_dir():
            try:
                shutil.copytree(
                    source.path,
                    destination,
                    copy_function=shutil.copy2,
                    symlinks=True,
                )
            except OSError as exc:
                raise MigrationError(
                    f"cannot copy legacy {source.config_name}: {exc}"
                ) from exc
        else:
            destination.mkdir(parents=True)


def _add_vault_format(soul_path: Path) -> None:
    try:
        original = soul_path.read_bytes()
        document = yaml.safe_load(original)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise MigrationError(f"cannot update copied soul.yaml: {exc}") from exc
    if not isinstance(document, Mapping):
        raise MigrationError("cannot update copied soul.yaml: expected a mapping")
    if str(document.get("vault_format", "")) == CURRENT_LAYOUT_VERSION:
        return

    addition = f'vault_format: "{CURRENT_LAYOUT_VERSION}"\n'.encode("ascii")
    lines = original.splitlines(keepends=True)
    if document.get("vault_format") is not None:
        key = re.compile(rb"^vault_format\s*:")
        matches = [index for index, line in enumerate(lines) if key.match(line)]
        if len(matches) != 1:
            raise MigrationError("cannot safely update legacy vault_format in soul.yaml")
        line = lines[matches[0]]
        ending = b"\r\n" if line.endswith(b"\r\n") else b"\n"
        lines[matches[0]] = addition.rstrip(b"\n") + ending
        mode = soul_path.stat().st_mode
        atomic_write_bytes(soul_path, b"".join(lines))
        os.chmod(soul_path, stat.S_IMODE(mode))
        return

    insert_at = len(lines)
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip() == b"...":
            insert_at = index
            break
        if lines[index].strip():
            break
    prefix = b"".join(lines[:insert_at])
    suffix = b"".join(lines[insert_at:])
    if prefix and not prefix.endswith((b"\n", b"\r")):
        prefix += b"\n"
    mode = soul_path.stat().st_mode
    atomic_write_bytes(soul_path, prefix + addition + suffix)
    os.chmod(soul_path, stat.S_IMODE(mode))


def _git(staged_vault: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    git = shutil.which("git")
    if git is None:
        raise MigrationError("legacy Vault is a git repository but git is unavailable")
    command = [
        git,
        f"--git-dir={staged_vault / '.git'}",
        f"--work-tree={staged_vault}",
        "-c",
        "core.hooksPath=/dev/null",
        *arguments,
    ]
    return subprocess.run(command, capture_output=True, text=True)


def _commit_vault_migration(staged_vault: Path) -> None:
    git_dir = staged_vault / ".git"
    if not git_dir.exists():
        return
    if not git_dir.is_dir():
        raise MigrationError("legacy Vault .git is not a self-contained repository")

    check = _git(staged_vault, "rev-parse", "--git-dir")
    if check.returncode != 0:
        raise MigrationError(f"copied Vault repository is invalid: {check.stderr.strip()}")
    added = _git(staged_vault, "add", "--", "soul/soul.yaml")
    if added.returncode != 0:
        raise MigrationError(f"cannot stage Vault migration: {added.stderr.strip()}")
    committed = _git(
        staged_vault,
        "-c",
        "user.name=yurios-migrate",
        "-c",
        "user.email=migrate@localhost",
        "commit",
        "-q",
        "--only",
        "-m",
        "migrate: vault format 0.2",
        "--",
        "soul/soul.yaml",
    )
    if committed.returncode != 0:
        raise MigrationError(f"cannot commit Vault migration: {committed.stderr.strip()}")


def _layout_payload(
    record: CharacterRecord,
    sources: tuple[_Source, ...],
    completed_at: str,
) -> dict[str, Any]:
    return {
        "layout_version": CURRENT_LAYOUT_VERSION,
        "migration": {
            "name": MIGRATION_NAME,
            "from_version": LEGACY_LAYOUT_VERSION,
            "character_id": record.id,
            "completed_at": completed_at,
            "sources": _source_manifest(sources),
        },
    }


def migrate_legacy_data(
    config: object,
    data_dir: str | Path | None = None,
    registry: CharacterRegistry | None = None,
    *,
    dry_run: bool = False,
    check: bool = False,
) -> MigrationResult:
    """Migrate one 0.1 Config into the default 0.2 character registry.

    ``check`` and ``dry_run`` both perform all source and collision validation but
    make no filesystem changes.  Repeated calls after success return
    ``already-migrated`` and do not inspect or rewrite the backup roots.  A machine
    with no 0.1 roots at all returns ``no-legacy-data`` and writes nothing.
    """
    if data_dir is None and registry is not None:
        target = registry.data_root
    else:
        target = _canonical(
            data_dir if data_dir is not None else _default_data_dir(config)
        )
    try:
        target_registry = registry if registry is not None else CharacterRegistry(target)
    except (OSError, ValueError) as exc:
        raise MigrationError(f"cannot open target character registry: {exc}") from exc
    if target_registry.data_root != target:
        raise MigrationError("target registry is rooted at a different DATA_DIR")

    marker_path = target / LAYOUT_MARKER_NAME
    marker = _read_layout_marker(marker_path)
    if marker is not None:
        result = _current_result(marker, target_registry, target)
        if not (check or dry_run):
            _backfill_default_portrait(target_registry, result.character_id)
            _repair_legacy_model_bindings(target_registry, marker, marker_path)
        return result

    sources = _sources(config)
    characters_dir = target / "characters"
    if characters_dir.is_symlink():
        raise MigrationError(f"character directory is a symbolic link: {characters_dir}")
    if characters_dir.exists() and not characters_dir.is_dir():
        raise MigrationError(f"character directory is not a directory: {characters_dir}")
    if not _any_source_exists(sources):
        # No marker is written: the check is one stat per root, and leaving the
        # marker unwritten keeps the migration available to a 0.1 vault that is
        # restored, mounted, or pointed at later.
        return MigrationResult("no-legacy-data", target, None, None, None)
    _validate_sources(sources, target, characters_dir)
    vault = next(source.path for source in sources if source.required)
    display_name = _read_soul_name(vault, config)

    recovered = _existing_migration(target_registry, sources)
    if recovered is not None:
        if check or dry_run:
            return MigrationResult(
                "needed", target, recovered.id, recovered.paths.root, recovered.display.name
            )
        completed_at = datetime.now(timezone.utc).isoformat()
        install_default_portrait(recovered.paths, recovered.display.name)
        atomic_write_json(
            marker_path, _layout_payload(recovered, sources, completed_at)
        )
        return MigrationResult(
            "already-migrated",
            target,
            recovered.id,
            recovered.paths.root,
            recovered.display.name,
        )

    character_id = _next_character_id(target_registry, characters_dir)
    final_root = characters_dir / character_id
    record = _record(config, character_id, display_name, final_root)
    if check or dry_run:
        return MigrationResult("needed", target, character_id, final_root, display_name)

    characters_existed = characters_dir.exists()
    try:
        characters_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise MigrationError(f"cannot create character staging directory: {exc}") from exc
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{character_id}.migrate-", dir=characters_dir)
    )
    moved = False
    try:
        staged = CharacterPaths.under(temporary)
        _copy_sources(sources, staged)
        # 0.1 had no portrait to carry over, so she arrives faceless unless the
        # packaged one is laid down here — inside the staging directory, so it
        # becomes visible with the same single rename as everything else.
        install_default_portrait(staged, display_name)
        _add_vault_format(staged.vault / "soul" / "soul.yaml")
        _commit_vault_migration(staged.vault)

        completed_at = datetime.now(timezone.utc).isoformat()
        character_manifest = {
            "migration": MIGRATION_NAME,
            "from_version": LEGACY_LAYOUT_VERSION,
            "to_version": CURRENT_LAYOUT_VERSION,
            "character_id": character_id,
            "completed_at": completed_at,
            "sources": _source_manifest(sources),
        }
        atomic_write_json(temporary / MIGRATION_MANIFEST_NAME, character_manifest)

        if os.path.lexists(final_root) or target_registry.get(character_id) is not None:
            raise MigrationError(f"character destination collided during migration: {final_root}")
        os.replace(temporary, final_root)
        moved = True
        try:
            target_registry.add(record)
        except Exception:
            shutil.rmtree(final_root, ignore_errors=True)
            moved = False
            raise
        atomic_write_json(marker_path, _layout_payload(record, sources, completed_at))
        return MigrationResult("migrated", target, character_id, final_root, display_name)
    except MigrationError:
        raise
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise MigrationError(f"migration failed safely: {exc}") from exc
    finally:
        if not moved:
            shutil.rmtree(temporary, ignore_errors=True)
        if not characters_existed:
            try:
                characters_dir.rmdir()
            except OSError:
                pass


def check_migration(
    config: object,
    data_dir: str | Path | None = None,
    registry: CharacterRegistry | None = None,
) -> MigrationResult:
    return migrate_legacy_data(config, data_dir, registry, check=True)


# A short spelling for callers and for compatibility with command terminology.
migrate = migrate_legacy_data


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Migrate YuriOS 0.1 legacy roots into the 0.2 character layout."
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="0.2 data root (default: DATA_DIR, then ./data)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="validate and report only")
    mode.add_argument(
        "--dry-run", action="store_true", help="show the migration without writing"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    # Config reads .env, so keep this import and construction behind CLI execution.
    from yurios.world.config import Config

    try:
        result = migrate_legacy_data(
            Config(), args.data_dir, dry_run=args.dry_run, check=args.check
        )
    except MigrationError as exc:
        print(f"migration refused: {exc}", file=sys.stderr)
        return 2

    location = f" at {result.character_root}" if result.character_root else ""
    print(f"{result.status}: YuriOS data layout {CURRENT_LAYOUT_VERSION}{location}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
