"""Repository and data-pack path discovery shared by ARMOUR entry points."""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath

from armour_common import ArmourError

CONFIG_FILE = "armour.json"
EXCLUDED_DIRS = frozenset(
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


def _is_pack(path: Path) -> bool:
    """A pack has valid metadata and a data directory."""
    if not (path / "pack.mcmeta").is_file() or not (path / "data").is_dir():
        return False
    try:
        metadata = json.loads((path / "pack.mcmeta").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(metadata, dict) and isinstance(metadata.get("pack"), dict)


def _configured_path(repo: Path) -> str | None:
    config = repo / CONFIG_FILE
    if not config.is_file():
        return None
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ArmourError(f"{config} is not valid JSON: {error}") from error
    if not isinstance(data, dict):
        raise ArmourError(f"{config} must contain a JSON object")
    value = data.get("pack_path")
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ArmourError(f'{config}: "pack_path" must be a relative path string')
    return value


def resolve_pack_root(repo: Path, pack_path: str | None = None) -> Path:
    """Resolve one pack inside repo, independent of the caller's working directory.

    An explicit/configured path is authoritative. Otherwise exactly one valid
    pack is discovered recursively; ambiguous and missing cases fail clearly.
    """
    repo = repo.resolve()
    selected = pack_path if pack_path is not None else _configured_path(repo)
    if selected is not None:
        relative = PurePosixPath(selected)
        if relative.is_absolute() or ".." in relative.parts or not selected.strip():
            raise ArmourError(
                f"pack_path must be a relative path inside {repo}: {selected!r}"
            )
        target = (repo / Path(*relative.parts)).resolve()
        if not target.is_relative_to(repo):
            raise ArmourError(
                f"pack_path must stay inside the repository: {selected!r}"
            )
        if not _is_pack(target):
            raise ArmourError(
                f"Configured data pack {selected!r} is invalid: expected pack.mcmeta "
                "with a pack object and a data/ directory."
            )
        return target

    candidates: list[Path] = []
    for current, dirs, _files in os.walk(repo):
        current_path = Path(current)
        dirs[:] = sorted(name for name in dirs if name not in EXCLUDED_DIRS)
        if _is_pack(current_path):
            candidates.append(current_path)
            dirs[:] = []
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
