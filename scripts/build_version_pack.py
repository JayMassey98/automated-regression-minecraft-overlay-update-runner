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
from armour_paths import resolve_pack_root

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
    """Convert one Minecraft format component without leaking conversion errors."""
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        raise ArmourError(f"Invalid data pack format component: {value!r}")
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ArmourError(f"Invalid data pack format component: {value!r}") from error
    if number < 0 or (isinstance(value, str) and str(number) != value.strip()):
        raise ArmourError(f"Invalid data pack format component: {value!r}")
    return number


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


def overlay_range(entry: Mapping[str, Any]) -> tuple[Format, Format]:
    """Return the inclusive (low, high) format range an overlay entry applies to."""
    low: Format = (0, 0)
    high: Format = (MINOR_INFINITY, MINOR_INFINITY)
    if "min_format" in entry:
        low = as_format(entry["min_format"], upper=False)
    if "max_format" in entry:
        high = as_format(entry["max_format"], upper=True)
    legacy = entry.get("formats")  # 1.20.2 - 1.21.8 style
    if legacy is None:
        if low > high:
            raise ArmourError(f"Overlay minimum format exceeds maximum: {entry!r}")
        return low, high
    if isinstance(legacy, int) and not isinstance(legacy, bool):
        value = _format_number(legacy)
        return (value, 0), (value, MINOR_INFINITY)
    if isinstance(legacy, list) and len(legacy) == _PAIR_LENGTH:
        return (
            (_format_number(legacy[0]), 0),
            (_format_number(legacy[1]), MINOR_INFINITY),
        )
    if isinstance(legacy, dict):
        try:
            return (
                (_format_number(legacy["min_inclusive"]), 0),
                (_format_number(legacy["max_inclusive"]), MINOR_INFINITY),
            )
        except KeyError as error:
            msg = f"Unrecognised overlay formats value: {legacy!r}"
            raise ArmourError(msg) from error
    msg = f"Unrecognised overlay formats value: {legacy!r}"
    raise ArmourError(msg)


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


def build_flattened_pack(
    repo: Path, out: Path, fmt: Format, *, source_root: Path | None = None
) -> BuildResult:
    """Build the plain pack for data pack format `fmt` in the (fresh) folder `out`."""
    mcmeta = load_mcmeta(repo)
    entries = overlay_entries(mcmeta)
    _reject_pack_symlinks(repo)
    source_root = (source_root or repo).resolve()
    repo = repo.resolve()
    out = out.resolve()
    if out == repo or out in repo.parents or repo in out.parents:
        raise ArmourError(f"Build output must not overlap the source pack: {out}")
    if out.exists() and not out.is_dir():
        raise ArmourError(f"Build output exists and is not a directory: {out}")

    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    result = BuildResult(fmt=fmt)

    shutil.copyfile(repo / "pack.mcmeta", out / "pack.mcmeta")
    # spyglass.json (lint rules) travels with the pack so Spyglass picks it up.
    if (repo / "spyglass.json").exists():
        shutil.copyfile(repo / "spyglass.json", out / "spyglass.json")

    root_files = copy_data_tree(repo / "data", out, source_root)
    result.source_map.update(root_files)
    result.applied.append(f"data/ ({len(root_files)} files)")

    for entry in entries:
        directory = entry["directory"]
        low, high = overlay_range(entry)
        if not low <= fmt <= high:
            continue
        overlay_root = (repo / directory).resolve()
        if not overlay_root.is_relative_to(repo):
            raise ArmourError(f"Overlay directory escapes the pack: {directory!r}")
        overlay_data = overlay_root / "data"
        if not overlay_data.is_dir():
            msg = (
                f"pack.mcmeta lists overlay '{directory}' "
                f"but {overlay_data} does not exist"
            )
            raise ArmourError(msg)
        overlay_files = copy_data_tree(overlay_data, out, source_root)
        result.source_map.update(overlay_files)
        result.applied.append(f"{directory}/ ({len(overlay_files)} files)")
    return result


def write_source_map(
    path: Path, result: BuildResult, repo: Path, pack_root: Path, out: Path
) -> None:
    """Write the source map without allowing it to replace source files."""
    source_map = path if path.is_absolute() else repo / path
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
    common_parent = Path(os.path.commonpath((out.parent, source_map.parent)))
    if common_parent == repo or (
        source_map.is_relative_to(repo) and not common_parent.is_relative_to(repo)
    ):
        raise ArmourError(
            "Source map and build output must share a dedicated output directory "
            f"below {repo}, not write into repository source files"
        )
    source_map.parent.mkdir(parents=True, exist_ok=True)
    source_map.write_text(json.dumps(result.source_map, indent=1), encoding="utf-8")


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
        "--cache-dir", default=".mc-cache", help="where server jars are cached"
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


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point."""
    args = parse_args(argv)
    repo = Path(args.repo).resolve()

    if args.pack_version is not None:
        try:
            pack_version = json.loads(args.pack_version)
        except json.JSONDecodeError as error:
            msg = f"--pack-version is not valid JSON: {error}"
            raise ArmourError(msg) from error
    elif args.server_url and args.sha1:
        repo = Path(args.repo).resolve()
        cache_dir = Path(args.cache_dir)
        if not cache_dir.is_absolute():
            cache_dir = repo / cache_dir
        jar = download_server_jar(args.server_url, args.sha1, cache_dir.resolve())
        pack_version = read_version_json(jar).get("pack_version")
    else:
        msg = "Give either --server-url and --sha1, or --pack-version"
        raise ArmourError(msg)

    pack_root = resolve_pack_root(repo, args.pack_path)
    out = Path(args.out)
    if not out.is_absolute():
        out = repo / out
    out = out.resolve()
    result = build_flattened_pack(
        pack_root, out, data_format(pack_version), source_root=repo
    )

    if args.source_map:
        write_source_map(Path(args.source_map), result, repo, pack_root, out)

    label = f"{result.fmt[0]}.{result.fmt[1]}"
    applied = " + ".join(result.applied)
    print(f"Data pack format {label}: applied {applied}")
    append_summary(f"Data pack format `{label}` → {applied}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run(main))
