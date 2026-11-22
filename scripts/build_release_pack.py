#!/usr/bin/env python3
"""Build the data pack zip that players download.

The source repository can keep overlays in nested folders such as
`snapshot/26.3/1_to_2`, but Minecraft only accepts flat overlay names, so the
zip is built like the server tests stage the pack: every overlay folder is
renamed to `snapshot_26.3_1_to_2` and `pack.mcmeta` is rewritten to match.

The zip holds `pack.mcmeta`, `data/`, every overlay folder listed in
pack.mcmeta, and `pack.png` / `license*` if the pack has them. Everything else
(`.github/`, `.armour/`, `tests/`, `spyglass.json`, `armour.json`, git files,
build output) is left out. The zip is reproducible: the same files give the
same bytes.

Usage:
  build_release_pack.py --version v1.2.3 [--name The-Nether-Refined]
      [--repo .] [--pack-path PATH] [--out dist]

Writes <out>/<name>-<version>.zip and fails if the result is not a valid pack.
"""

from __future__ import annotations

import argparse
import json
import re
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

import armour_config
from armour_common import ArmourError, append_summary, run, set_output
from armour_paths import resolve_pack_root
from build_version_pack import (
    flatten_overlay_directories,
    load_mcmeta,
    overlay_entries,
    overlay_range,
    validate_overlay_sources,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

FIXED_TIME = (2020, 1, 1, 0, 0, 0)  # keeps the zip reproducible
EXTRA_FILES = re.compile(r"^(pack\.png|license.*)$", re.IGNORECASE)
# What Minecraft accepts as an overlay folder name.
VALID_OVERLAY_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


def files_under(folder: Path) -> list[Path]:
    """Every file below `folder`, in a stable order; symlinks are refused."""
    found = []
    for path in sorted(folder.rglob("*")):
        if path.is_symlink():
            raise ArmourError(f"Symbolic links are not supported in data packs: {path}")
        if path.is_file():
            found.append(path)
    return found


def build_zip(pack: Path, target: Path) -> list[str]:
    """Write the zip for the pack at `pack`; return the names inside it."""
    mcmeta = load_mcmeta(pack)
    entries = overlay_entries(mcmeta)
    for entry in entries:
        overlay_range(entry)
    validate_overlay_sources(pack, entries)
    if not (pack / "data").is_dir():
        raise ArmourError("the data/ folder is missing")
    flat_mcmeta, pairs = flatten_overlay_directories(mcmeta)
    for _, runtime in pairs:
        if not VALID_OVERLAY_NAME.match(runtime):
            raise ArmourError(
                f"Overlay folder name {runtime!r} is not valid in Minecraft "
                "(use letters, digits, '_', '.' and '-' after flattening)"
            )
        if runtime in {"data", "pack.mcmeta"}:
            raise ArmourError(f"Overlay folder name {runtime!r} is reserved")

    # (name inside the zip, bytes), in the order they are written.
    members: list[tuple[str, bytes]] = [
        ("pack.mcmeta", (json.dumps(flat_mcmeta, indent=2) + "\n").encode("utf-8"))
    ]
    for entry in sorted(pack.iterdir(), key=lambda p: p.name):
        if entry.is_file() and EXTRA_FILES.match(entry.name):
            members.append((entry.name, entry.read_bytes()))
    for folder, name in [(pack / "data", "data")] + [
        (pack / source, runtime) for source, runtime in pairs
    ]:
        for path in files_under(folder):
            members.append(
                (f"{name}/{path.relative_to(folder).as_posix()}", path.read_bytes())
            )

    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, content in members:
            info = zipfile.ZipInfo(name, FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, content)

    # Check the finished zip, not the plan.
    with zipfile.ZipFile(target) as archive:
        names = archive.namelist()
        if archive.testzip() is not None:
            raise ArmourError("the zip is corrupt")
        shipped = json.loads(archive.read("pack.mcmeta"))
    if len(set(names)) != len(names):
        raise ArmourError("the zip contains duplicate file names")
    for entry in overlay_entries(shipped):
        directory = entry["directory"]
        if not VALID_OVERLAY_NAME.match(directory):
            raise ArmourError(f"shipped overlay {directory!r} is not a flat name")
        if not any(n.startswith(f"{directory}/data/") for n in names):
            raise ArmourError(f"overlay '{directory}' has no files in the zip")
    return names


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--version", required=True, help="tag or label for the file name")
    parser.add_argument("--name", help="file name prefix (default: pack_name)")
    parser.add_argument("--repo", default=".", help="repository root")
    parser.add_argument("--pack-path", help="data pack directory relative to --repo")
    parser.add_argument("--out", default="dist", help="folder to write the zip to")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point."""
    args = parse_args(argv)
    repo = Path(args.repo).resolve()
    pack = resolve_pack_root(repo, args.pack_path)
    name = re.sub(
        r"[^A-Za-z0-9._-]+", "-", args.name or armour_config.load(repo).pack_name
    ).strip("-")
    version = re.sub(r"[^A-Za-z0-9._-]+", "-", args.version).strip("-")
    if not name or not version:
        raise ArmourError("--name and --version must contain letters or digits")
    out = Path(args.out)
    target = (out if out.is_absolute() else Path.cwd() / out).resolve() / f"{name}-{version}.zip"
    names = build_zip(pack, target)
    size_kib = target.stat().st_size / 1024
    print(f"Built {target.name}: {len(names)} files, {size_kib:.0f} KiB")
    set_output("zip_path", str(target))
    set_output("zip_name", target.name)
    append_summary(f"📦 `{target.name}`: {len(names)} files, {size_kib:.0f} KiB")
    return 0


if __name__ == "__main__":
    raise SystemExit(run(main))
