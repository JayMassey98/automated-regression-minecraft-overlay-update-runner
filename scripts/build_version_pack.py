#!/usr/bin/env python3
"""Flatten the data pack the way Minecraft would load it for one game version.

Tools such as Spyglass do not understand pack.mcmeta overlays, so for each
version we build a plain pack: the root `data/` first, then every overlay whose
format range contains that version's data pack format, applied in the order
they are listed in pack.mcmeta (later overlays overwrite earlier files).

The data pack format comes from the `version.json` file inside Mojang's server
jar (Mojang's launcher JSON does not carry it). The jar is downloaded, checked
against its SHA-1 and cached, so a later stage can reuse the same file.

Usage:
  build_version_pack.py --server-url URL --sha1 SHA1 --cache-dir .mc-cache \
      --out build/pack --source-map build/source-map.json [--repo .]

For tests, `--pack-version '<json>'` replaces the download. `pack_version`
looks like:
  {"data_major": 94, "data_minor": 1, ...}   (26.1 onward)
  {"resource": 64, "data": 81}               (older)
  81                                         (oldest)

Outputs the flattened pack plus a JSON map {flattened path: repo path}.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from armour_common import (
    ArmourError,
    append_summary,
    download_server_jar,
    run,
)
from armour_paths import paths_overlap, reject_symlink_components, resolve_pack_root

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

# A bare integer upper bound such as `"max_format": 88` covers every minor
# version of 88.
MINOR_INFINITY = 10**9

# A data pack format as (major, minor).
Format = tuple[int, int]

_PAIR_LENGTH = 2
_MAX_LISTED_JAR_ENTRIES = 15


@dataclass
class BuildResult:
    """What `build_flattened_pack` produced."""

    fmt: Format
    applied: list[str] = field(default_factory=list)
    source_map: dict[str, str] = field(default_factory=dict)


def _load_json_object(text: str | bytes, where: str) -> dict[str, Any]:
    """Parse JSON that must be an object, reporting problems as `ArmourError`."""
    try:
        data = json.loads(text)
    except UnicodeDecodeError as error:
        msg = f"{where} is not readable UTF-8: {error}"
        raise ArmourError(msg) from error
    except json.JSONDecodeError as error:
        msg = f"{where} is not valid JSON: {error}"
        raise ArmourError(msg) from error
    if not isinstance(data, dict):
        msg = f"{where} must be a JSON object"
        raise ArmourError(msg)
    return data


def read_version_json(jar_path: Path) -> dict[str, Any]:
    """Return the version.json inside a server jar.

    Also looks inside a bundler jar's nested jar.
    """
    with zipfile.ZipFile(jar_path) as jar:
        names = jar.namelist()
        if "version.json" in names:
            return _load_json_object(jar.read("version.json"), "version.json")
        for name in names:
            if name.startswith("META-INF/versions/") and name.endswith(".jar"):
                with zipfile.ZipFile(io.BytesIO(jar.read(name))) as inner:
                    if "version.json" in inner.namelist():
                        return _load_json_object(
                            inner.read("version.json"), f"{name}!version.json"
                        )
    shown = ", ".join(names[:_MAX_LISTED_JAR_ENTRIES])
    msg = f"No version.json inside {jar_path.name}. First entries: {shown}"
    raise ArmourError(msg)


def _format_number(value: object) -> int:
    """Validate one Minecraft format component as a non-negative JSON integer."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ArmourError(f"Invalid data pack format component: {value!r}")
    return value


def data_format(pack_version: object) -> Format:
    """Normalise Mojang's `pack_version` into a (major, minor) data pack format."""
    if isinstance(pack_version, int) and not isinstance(pack_version, bool):
        return (_format_number(pack_version), 0)
    if isinstance(pack_version, dict):
        if "data_major" in pack_version:
            return (
                _format_number(pack_version["data_major"]),
                _format_number(pack_version.get("data_minor", 0)),
            )
        if "data" in pack_version:
            return (_format_number(pack_version["data"]), 0)
    msg = f"Cannot work out the data pack format from {pack_version!r}"
    raise ArmourError(msg)


def as_format(value: object, *, upper: bool) -> Format:
    """Read a `min_format`/`max_format` value; a bare integer spans every minor."""
    if isinstance(value, int) and not isinstance(value, bool):
        return (_format_number(value), MINOR_INFINITY if upper else 0)
    if isinstance(value, list) and len(value) == _PAIR_LENGTH:
        return (_format_number(value[0]), _format_number(value[1]))
    msg = f"Unrecognised pack format value: {value!r}"
    raise ArmourError(msg)


def overlay_range(entry: Mapping[str, Any]) -> tuple[Format, Format]:  # noqa: C901
    """Return the inclusive (low, high) format range an overlay entry applies to."""
    low: Format = (0, 0)
    high: Format = (MINOR_INFINITY, MINOR_INFINITY)
    if "min_format" in entry:
        low = as_format(entry["min_format"], upper=False)
    if "max_format" in entry:
        high = as_format(entry["max_format"], upper=True)
    if "formats" in entry and ("min_format" in entry or "max_format" in entry):
        raise ArmourError(
            "Overlay entry must use either legacy 'formats' or modern "
            "min_format/max_format bounds, not both"
        )
    if low > high:
        raise ArmourError(f"Overlay minimum format exceeds maximum: {entry!r}")
    if "formats" not in entry:
        return low, high
    legacy = entry["formats"]  # 1.20.2 - 1.21.8 style
    if legacy is None:
        raise ArmourError(f"Overlay formats must not be null: {entry!r}")
    if isinstance(legacy, int) and not isinstance(legacy, bool):
        value = _format_number(legacy)
        legacy_low, legacy_high = (value, 0), (value, MINOR_INFINITY)
    elif isinstance(legacy, list) and len(legacy) == _PAIR_LENGTH:
        legacy_low = (_format_number(legacy[0]), 0)
        legacy_high = (_format_number(legacy[1]), MINOR_INFINITY)
    elif isinstance(legacy, dict):
        try:
            legacy_low = (_format_number(legacy["min_inclusive"]), 0)
            legacy_high = (_format_number(legacy["max_inclusive"]), MINOR_INFINITY)
        except KeyError as error:
            msg = f"Unrecognised overlay formats value: {legacy!r}"
            raise ArmourError(msg) from error
    else:
        msg = f"Unrecognised overlay formats value: {legacy!r}"
        raise ArmourError(msg)
    if legacy_low > legacy_high:
        raise ArmourError(f"Overlay minimum format exceeds maximum: {entry!r}")
    return legacy_low, legacy_high


def load_mcmeta(repo: Path) -> dict[str, Any]:
    """Read the repository's pack.mcmeta."""
    path = repo / "pack.mcmeta"
    if not path.is_file():
        msg = f"{path} does not exist; is --repo the data pack's root?"
        raise ArmourError(msg)
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ArmourError(f"Cannot read {path} as UTF-8: {error}") from error
    return _load_json_object(content, str(path))


def overlay_entries(mcmeta: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Validate and return the overlay entries listed in pack.mcmeta."""
    overlays = mcmeta.get("overlays", {})
    if not isinstance(overlays, dict):
        raise ArmourError('"overlays" in pack.mcmeta must be an object')
    entries = overlays.get("entries", [])
    if not isinstance(entries, list):
        raise ArmourError('"overlays.entries" in pack.mcmeta must be a list')
    validated: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ArmourError(f"Overlay entry must be an object: {entry!r}")
        directory = entry.get("directory")
        if not isinstance(directory, str) or not directory.strip():
            raise ArmourError(f"Overlay entry has no valid directory: {entry!r}")
        path = PurePosixPath(directory)
        if (
            path.is_absolute()
            or not path.parts
            or "\\" in directory
            or ".." in path.parts
            or ":" in path.parts[0]
        ):
            msg = f"Overlay directory must stay inside the pack: {directory!r}"
            raise ArmourError(msg)
        validated.append(entry)
    return validated


def _reject_pack_symlinks(repo: Path) -> None:
    """Reject symlinks anywhere in the source pack before reading or copying it."""
    for source in repo.rglob("*"):
        if source.is_symlink():
            msg = f"Symbolic links are not supported in data packs: {source}"
            raise ArmourError(msg)


def copy_data_tree(data_dir: Path, destination: Path, repo: Path) -> dict[str, str]:
    """Copy `data_dir` (a `data/` folder) under `destination`, keeping `data/`.

    Returns {path in the flattened pack: path in the repo} for each file copied.
    """
    copied: dict[str, str] = {}
    if not data_dir.is_dir():
        raise ArmourError(f"Data directory does not exist: {data_dir}")
    for path in sorted(data_dir.rglob("*")):
        if path.is_symlink():
            raise ArmourError(f"Symbolic links are not supported in data packs: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(data_dir.parent)  # keeps the leading `data/`
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        copied[relative.as_posix()] = path.relative_to(repo).as_posix()
    return copied


def validate_overlay_sources(repo: Path, entries: list[dict[str, Any]]) -> None:
    """Validate all overlay paths and metadata before modifying build output."""
    repo = repo.resolve()
    for entry in entries:
        directory = entry["directory"]
        overlay_root = (repo / directory).resolve()
        if not overlay_root.is_relative_to(repo):
            raise ArmourError(f"Overlay directory escapes the pack: {directory!r}")
        if not (overlay_root / "data").is_dir():
            raise ArmourError(
                f"pack.mcmeta lists overlay '{directory}' but "
                f"{overlay_root / 'data'} does not exist"
            )
        _reject_pack_symlinks(overlay_root)


def build_flattened_pack(  # noqa: C901, PLR0912, PLR0915
    repo: Path, out: Path, fmt: Format, *, source_root: Path | None = None
) -> BuildResult:
    """Build to a temporary sibling, preserving the last successful output."""
    mcmeta = load_mcmeta(repo)
    entries = overlay_entries(mcmeta)
    for entry in entries:
        overlay_range(entry)
    _reject_pack_symlinks(repo)
    validate_overlay_sources(repo, entries)
    source_root = (source_root or repo).resolve()
    repo = repo.resolve()
    out = out.absolute()
    reject_symlink_components(out, Path(out.anchor))
    out = out.resolve()
    if paths_overlap(out, repo):
        raise ArmourError(f"Build output must not overlap the source pack: {out}")
    build_root = source_root / "build"
    if out.is_relative_to(source_root) and (
        not out.is_relative_to(build_root) or out == build_root
    ):
        raise ArmourError(
            "Build output inside the repository must be a dedicated subdirectory "
            f"of {build_root}: {out}"
        )
    if out.exists() and not out.is_dir():
        raise ArmourError(f"Build output exists and is not a directory: {out}")

    temp = out.with_name(f".{out.name}.armour-tmp")
    backup = out.with_name(f".{out.name}.armour-backup")
    for candidate in (temp, backup):
        reject_symlink_components(candidate, Path(candidate.anchor))
        if candidate.exists():
            raise ArmourError(
                f"Temporary build path already exists; refusing cleanup: {candidate}"
            )

    out_existed = out.exists()
    temp.mkdir(parents=True)
    backup_created = False
    try:
        result = BuildResult(fmt=fmt)
        shutil.copyfile(repo / "pack.mcmeta", temp / "pack.mcmeta")
        if (repo / "spyglass.json").exists():
            shutil.copyfile(repo / "spyglass.json", temp / "spyglass.json")
        root_files = copy_data_tree(repo / "data", temp, source_root)
        result.source_map.update(root_files)
        result.applied.append(f"data/ ({len(root_files)} files)")
        for entry in entries:
            directory = entry["directory"]
            low, high = overlay_range(entry)
            if low <= fmt <= high:
                overlay_root = (repo / directory).resolve()
                overlay_files = copy_data_tree(overlay_root / "data", temp, source_root)
                result.source_map.update(overlay_files)
                result.applied.append(f"{directory}/ ({len(overlay_files)} files)")
        if out.exists():
            out.rename(backup)
            backup_created = True
        temp.rename(out)
    except BaseException:
        if temp.exists():
            shutil.rmtree(temp, ignore_errors=True)
        # Restore whenever a backup exists, including interruption between rename
        # and setting the bookkeeping flag.
        if backup.exists():
            if out.exists():
                shutil.rmtree(out, ignore_errors=True)
            backup.rename(out)
        elif not out_existed and out.exists():
            # A first build interrupted after install has no previous output.
            shutil.rmtree(out, ignore_errors=True)
        raise
    else:
        if backup_created:
            shutil.rmtree(backup, ignore_errors=True)
        return result


def validate_source_map_path(
    path: Path, repo: Path, pack_root: Path, out: Path
) -> Path:
    """Validate and return a source-map destination without writing anything."""
    source_map = path if path.is_absolute() else repo / path
    source_map = source_map.absolute()
    reject_symlink_components(source_map, Path(source_map.anchor))
    source_map = source_map.resolve()
    if (
        source_map == pack_root
        or pack_root in source_map.parents
        or source_map in pack_root.parents
    ):
        msg = f"Source map must not overwrite the source pack: {source_map}"
        raise ArmourError(msg)
    if source_map == out or out in source_map.parents or source_map in out.parents:
        raise ArmourError(f"Source map must not overlap build output: {source_map}")
    try:
        common_parent = Path(os.path.commonpath((out.parent, source_map.parent)))
    except ValueError:  # Different Windows drives have no common parent.
        common_parent = None
    if common_parent == repo or (
        source_map.is_relative_to(repo)
        and (common_parent is None or not common_parent.is_relative_to(repo))
    ):
        raise ArmourError(
            "Source map and build output must share a dedicated output directory "
            f"below {repo}, not write into repository source files"
        )

    if source_map.exists() and not source_map.is_file():
        raise ArmourError(f"Source map must be a file path: {source_map}")
    for parent in source_map.parents:
        if parent.exists() and not parent.is_dir():
            raise ArmourError(f"Source-map parent is not a directory: {parent}")
    temporary = source_map.with_name(f".{source_map.name}.armour-tmp")
    if temporary.exists() or temporary.is_symlink():
        raise ArmourError(f"Source-map temporary path already exists: {temporary}")
    return source_map


def write_source_map(
    path: Path, result: BuildResult, repo: Path, pack_root: Path, out: Path
) -> None:
    """Write the source map atomically without replacing source files."""
    source_map = validate_source_map_path(path, repo, pack_root, out)
    temporary = source_map.with_name(f".{source_map.name}.armour-tmp")
    if temporary.exists() or temporary.is_symlink():
        raise ArmourError(f"Source-map temporary path already exists: {temporary}")
    source_map.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_text(json.dumps(result.source_map, indent=1), encoding="utf-8")
    temporary.replace(source_map)



def recover_main_transaction(  # noqa: C901, PLR0912
    out: Path, source_map_path: Path | None
) -> None:
    """Recover an interrupted output/source-map commit using its journal."""
    journal = out.with_name(f".{out.name}.armour-main-transaction.json")
    if not journal.exists():
        return
    reject_symlink_components(journal, Path(journal.anchor))
    try:
        state = json.loads(journal.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ArmourError(
            f"Cannot read transaction journal {journal}: {error}"
        ) from error
    if not isinstance(state, dict) or not all(
        isinstance(state.get(key), bool) for key in ("out_existed", "map_existed")
    ):
        raise ArmourError(f"Invalid transaction journal: {journal}")
    out_backup = out.with_name(f".{out.name}.armour-main-backup")
    staged_out = out.with_name(f".{out.name}.armour-main-stage")
    map_backup = (
        source_map_path.with_name(f".{source_map_path.name}.armour-main-backup")
        if source_map_path
        else None
    )
    staged_map = (
        source_map_path.with_name(f".{source_map_path.name}.armour-main-stage")
        if source_map_path
        else None
    )
    for path in (out, out_backup, staged_out, source_map_path, map_backup, staged_map):
        if path is not None:
            reject_symlink_components(path, Path(path.anchor))
    if out_backup.exists():
        if out.exists():
            shutil.rmtree(out, ignore_errors=True)
        out_backup.rename(out)
    elif not state["out_existed"] and out.exists():
        shutil.rmtree(out, ignore_errors=True)
    if map_backup is not None and map_backup.exists():
        if source_map_path.exists():
            source_map_path.unlink(missing_ok=True)
        map_backup.rename(source_map_path)
    elif source_map_path is not None and not state["map_existed"]:
        source_map_path.unlink(missing_ok=True)
    if staged_out.exists():
        shutil.rmtree(staged_out, ignore_errors=True)
    if staged_map is not None:
        staged_map.unlink(missing_ok=True)
    journal.unlink()



def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "--repo", default=".", help="repository root (contains pack.mcmeta and data/)"
    )
    parser.add_argument("--pack-path", help="data pack directory relative to --repo")
    parser.add_argument("--server-url", help="Mojang server.jar URL for this version")
    parser.add_argument("--sha1", help="expected SHA-1 of the server jar")
    parser.add_argument(
        "--cache-dir", help="where server jars are cached (defaults to a sibling cache)"
    )
    parser.add_argument(
        "--pack-version", help="testing only: pack_version JSON instead of downloading"
    )
    parser.add_argument(
        "--out", required=True, help="directory to build the flattened pack in"
    )
    parser.add_argument(
        "--source-map", help="write {flattened path: repo path} JSON here"
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:  # noqa: C901, PLR0912, PLR0915
    """Command-line entry point."""
    args = parse_args(argv)
    download_version = args.pack_version is None
    if download_version and not (args.server_url and args.sha1):
        raise ArmourError("Give either --server-url and --sha1, or --pack-version")
    pack_version = None
    if not download_version:
        try:
            pack_version = json.loads(args.pack_version)
        except json.JSONDecodeError as error:
            raise ArmourError(f"--pack-version is not valid JSON: {error}") from error
    repo = Path(args.repo).resolve()

    pack_root = resolve_pack_root(repo, args.pack_path)
    cache_dir = (
        Path(args.cache_dir)
        if args.cache_dir is not None
        else repo.parent / f".{repo.name}-mc-cache"
    )
    if not cache_dir.is_absolute():
        cache_dir = repo / cache_dir
    if download_version:
        cache_dir = cache_dir.absolute()
        reject_symlink_components(cache_dir, Path(cache_dir.anchor))
        cache_dir = cache_dir.resolve()
        if paths_overlap(cache_dir, repo):
            raise ArmourError(
                "Cache directory must be outside the consuming repository "
                f"and source pack: {cache_dir}"
            )

    out = Path(args.out)
    if not out.is_absolute():
        out = repo / out
    out = out.absolute()
    reject_symlink_components(out, Path(out.anchor))
    out = out.resolve()
    source_map_path = None
    if args.source_map:
        source_map_path = Path(args.source_map)
        if not source_map_path.is_absolute():
            source_map_path = repo / source_map_path
        source_map_path = source_map_path.absolute()
        validate_source_map_path(source_map_path, repo, pack_root, out)

    if download_version:
        if paths_overlap(cache_dir, out):
            raise ArmourError("Cache directory and build output must not overlap")
        if source_map_path is not None and paths_overlap(cache_dir, source_map_path):
            raise ArmourError("Cache directory and source map must not overlap")
        jar = download_server_jar(args.server_url, args.sha1, cache_dir)
        pack_version = read_version_json(jar).get("pack_version")

    fmt = data_format(pack_version)
    staged_out = out.with_name(f".{out.name}.armour-main-stage")
    out_backup = out.with_name(f".{out.name}.armour-main-backup")
    staged_map = (
        source_map_path.with_name(f".{source_map_path.name}.armour-main-stage")
        if source_map_path is not None
        else None
    )
    map_backup = (
        source_map_path.with_name(f".{source_map_path.name}.armour-main-backup")
        if source_map_path is not None
        else None
    )
    recover_main_transaction(out, source_map_path)
    for candidate in (staged_out, out_backup, staged_map, map_backup):
        if candidate is not None:
            reject_symlink_components(candidate, Path(candidate.anchor))
            if candidate.exists():
                raise ArmourError(
                    "Transaction staging path already exists; refusing cleanup: "
                    f"{candidate}"
                )
    try:
        result = build_flattened_pack(pack_root, staged_out, fmt, source_root=repo)
        if staged_map is not None:
            staged_map.parent.mkdir(parents=True, exist_ok=True)
            staged_map.write_text(
                json.dumps(result.source_map, indent=1), encoding="utf-8"
            )
    except BaseException:
        if staged_out.exists():
            shutil.rmtree(staged_out, ignore_errors=True)
        if staged_map is not None:
            staged_map.unlink(missing_ok=True)
        raise

    out_existed_before_commit = out.exists()
    map_existed_before_commit = (
        source_map_path.exists() if source_map_path is not None else False
    )
    journal = out.with_name(f".{out.name}.armour-main-transaction.json")
    journal_tmp = journal.with_name(f".{journal.name}.tmp")
    journal_tmp.write_text(json.dumps({
        "out_existed": out_existed_before_commit,
        "map_existed": map_existed_before_commit,
    }), encoding="utf-8")
    journal_tmp.replace(journal)
    try:
        if out_existed_before_commit:
            out.rename(out_backup)
        if map_existed_before_commit:
            source_map_path.rename(map_backup)
        staged_out.rename(out)
        if source_map_path is not None:
            staged_map.rename(source_map_path)
    except BaseException:
        # Recover by inspecting filesystem state rather than relying on flags
        # which could be skipped by interruption between rename and assignment.
        if out_backup.exists():
            if out.exists():
                shutil.rmtree(out, ignore_errors=True)
            out_backup.rename(out)
        elif not out_existed_before_commit and out.exists():
            shutil.rmtree(out, ignore_errors=True)
        if map_backup is not None and map_backup.exists():
            if source_map_path.exists():
                source_map_path.unlink(missing_ok=True)
            map_backup.rename(source_map_path)
        elif source_map_path is not None and not map_existed_before_commit:
            source_map_path.unlink(missing_ok=True)
        raise
    else:
        if out_backup.exists():
            shutil.rmtree(out_backup)
        if map_backup is not None and map_backup.exists():
            map_backup.unlink(missing_ok=True)
        journal.unlink(missing_ok=True)
    finally:
        if staged_out.exists():
            shutil.rmtree(staged_out, ignore_errors=True)
        if staged_map is not None:
            staged_map.unlink(missing_ok=True)

    label = f"{result.fmt[0]}.{result.fmt[1]}"
    applied = " + ".join(result.applied)
    print(f"Data pack format {label}: applied {applied}")
    append_summary(f"Data pack format `{label}` → {applied}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run(main))
