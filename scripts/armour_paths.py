"""Repository and data-pack path discovery shared by ARMOUR entry points."""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath

from armour_common import ArmourError

CONFIG_FILE = "armour.json"
FORMAT_RANGE_LENGTH = 2
FORMAT_MINOR_INFINITY = 10**9
# Only exclude repository-level tooling/output directories. A directory named
# "build", "tests", etc. deeper in a consuming repository may contain the pack.
EXCLUDED_ROOT_DIRS = frozenset(
    {
        ".git",
        ".github",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "node_modules",
        "build",
        "dist",
        ".mc-cache",
        "server-run",
        "harness",
        "tests",
        ".armour",
    }
)


def _valid_pack_metadata(metadata: object) -> bool:  # noqa: C901, PLR0911
    """Return whether metadata has a supported, non-empty format declaration."""
    if not isinstance(metadata, dict):
        return False
    pack = metadata.get("pack")
    if not isinstance(pack, dict):
        return False
    keys = ("pack_format", "min_format", "max_format")
    if not any(key in pack for key in keys):
        return False
    for key in keys:
        if key not in pack:
            continue
        value = pack[key]
        if key == "pack_format" and (
            isinstance(value, bool) or not isinstance(value, int)
        ):
            return False
        if isinstance(value, bool) or not isinstance(value, (int, list)):
            return False
        if isinstance(value, int) and value < 0:
            return False
        if isinstance(value, list) and (
            len(value) != FORMAT_RANGE_LENGTH
            or any(
                isinstance(part, bool) or not isinstance(part, int) or part < 0
                for part in value
            )
        ):
            return False
    if "min_format" in pack and "max_format" in pack:
        low = pack["min_format"]
        high = pack["max_format"]
        low_pair = (low, 0) if isinstance(low, int) else tuple(low)
        high_pair = (
            (high, FORMAT_MINOR_INFINITY) if isinstance(high, int) else tuple(high)
        )
        if low_pair > high_pair:
            return False
    return True


def _is_pack(path: Path) -> bool:
    """A pack has valid metadata and a data directory."""
    if not (path / "pack.mcmeta").is_file() or not (path / "data").is_dir():
        return False
    try:
        metadata = json.loads((path / "pack.mcmeta").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return _valid_pack_metadata(metadata)


def _configured_path(repo: Path) -> str | None:
    config = repo / CONFIG_FILE
    if not config.is_file():
        return None
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as error:
        raise ArmourError(f"{config} is not readable UTF-8: {error}") from error
    except json.JSONDecodeError as error:
        raise ArmourError(f"{config} is not valid JSON: {error}") from error
    if not isinstance(data, dict):
        raise ArmourError(f"{config} must contain a JSON object")
    value = data.get("pack_path")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ArmourError(
            f'{config}: "pack_path" must be a non-empty relative path string'
        )
    return value


def validate_pack_path(repo: Path, selected: str) -> Path:
    """Resolve a non-empty pack path and ensure it remains inside the repository."""
    repo = repo.resolve()
    if not isinstance(selected, str) or not selected.strip():
        raise ArmourError("pack_path must be a non-empty relative path string")
    relative = PurePosixPath(selected)
    if relative.is_absolute() or "\\" in selected or ".." in relative.parts:
        raise ArmourError(
            f"pack_path must be a relative path inside {repo}: {selected!r}"
        )
    lexical = repo / Path(*relative.parts)
    current = repo
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ArmourError(f"Symbolic links are not allowed in pack paths; pack_path must stay inside the repository: {current}")
    target = lexical.resolve()
    if not target.is_relative_to(repo):
        raise ArmourError(f"pack_path must stay inside the repository: {selected!r}")
    return target


def resolve_pack_root(repo: Path, pack_path: str | None = None) -> Path:
    """Resolve one pack inside repo, independent of the caller's working directory.

    An explicit/configured path is authoritative. Otherwise exactly one valid
    pack is discovered recursively; ambiguous and missing cases fail clearly.
    """
    repo = repo.resolve()
    selected = pack_path if pack_path is not None else _configured_path(repo)
    if selected is not None:
        target = validate_pack_path(repo, selected)
        if not _is_pack(target):
            raise ArmourError(
                f"Configured data pack {selected!r} is invalid: expected pack.mcmeta "
                "with a pack object and a data/ directory."
            )
        return target

    candidates: list[Path] = []
    for current, dirs, _files in os.walk(repo):
        current_path = Path(current)
        relative = current_path.relative_to(repo)
        if not relative.parts:
            dirs[:] = sorted(name for name in dirs if name not in EXCLUDED_ROOT_DIRS)
        else:
            dirs[:] = sorted(dirs)
        if _is_pack(current_path):
            candidates.append(current_path)
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise ArmourError(
            f"No data pack found under {repo}. Expected a directory containing "
            "pack.mcmeta and data/. Set pack_path in armour.json or pass --pack-path."
        )
    locations = ", ".join(
        path.relative_to(repo).as_posix() or "." for path in candidates
    )
    raise ArmourError(
        f"Found multiple data packs under {repo}: {locations}. "
        "Set pack_path in armour.json or pass --pack-path to select one."
    )


def reject_symlink_components(path: Path, base: Path) -> None:
    """Reject symlink components below a known, resolved base directory."""
    base = base.resolve()
    candidate = path if path.is_absolute() else base / path
    try:
        relative = candidate.relative_to(base)
    except ValueError as error:
        raise ArmourError(f"Path must be inside {base}: {candidate}") from error
    current = base
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ArmourError(
                f"Symbolic links are not allowed in destination paths: {current}"
            )


def paths_overlap(first: Path, second: Path) -> bool:
    """Return whether either resolved path contains the other."""
    first, second = first.resolve(), second.resolve()
    return first == second or first in second.parents or second in first.parents
