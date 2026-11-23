#!/usr/bin/env python3
"""Discover the Minecraft versions a data pack's CI should target.

Reads Mojang's version_manifest_v2.json, selects versions by *release time*
(never by comparing ID strings), then fetches each selected version's own JSON
to collect the Java runtime and server.jar download details.

Selection rules (defaults match Stage 1 of the roadmap):
  * every official release from --release-from onward
  * every snapshot / pre-release / release candidate released strictly after
    the release time of --snapshot-after

Outputs (written to $GITHUB_OUTPUT when set, otherwise printed):
  versions  JSON array of exact version IDs, oldest first
  matrix    JSON object {"include": [{id, kind, java, server_url, sha1}, ...]}
  count     number of selected versions
"""

from __future__ import annotations

import argparse
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypedDict
from urllib.parse import urlsplit

from armour_common import (
    ArmourError,
    append_summary,
    fetch_json_object,
    run,
    set_output,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"

# GitHub Actions rejects matrices with more than 256 jobs.
MAX_MATRIX_JOBS = 256
FETCH_WORKERS = 8

# Release IDs: 1.21.11, 26.1, 26.1.2
RELEASE_RE = re.compile(r"^\d+\.\d+(\.\d+)?$")
# New-style development builds (26.1 onward): 26.1-snapshot-1, 26.1-pre-1, 26.1-rc-1
NEW_SNAPSHOT_RE = re.compile(r"^\d+\.\d+(\.\d+)?-(snapshot|pre|rc)-\d+$")
# Legacy 1.21.x development builds: 25w45a, 1.21.9-pre1, 1.21.9-rc1
LEGACY_SNAPSHOT_RE = re.compile(r"^(\d\dw\d\d[a-z]|\d+\.\d+(\.\d+)?-(pre|rc)\d+)$")


class VersionTarget(TypedDict):
    """One entry of the CI matrix."""

    id: str
    kind: Literal["release", "snapshot"]
    java: int
    server_url: str
    sha1: str


def release_time(entry: dict[str, Any]) -> datetime:
    """Parse a manifest entry's `releaseTime` (an ISO 8601 timestamp)."""
    try:
        return datetime.fromisoformat(entry["releaseTime"])
    except (KeyError, TypeError, ValueError) as error:
        msg = f"Manifest entry {entry.get('id')!r} has no valid releaseTime"
        raise ArmourError(msg) from error


def select_versions(
    manifest: dict[str, Any],
    release_from: str,
    snapshot_after: str,
    *,
    include_legacy_snapshots: bool,
    exclude: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Return manifest entries to test, sorted oldest to newest by releaseTime."""
    entries = {entry["id"]: entry for entry in manifest["versions"]}

    for name, version_id in (
        ("--release-from", release_from),
        ("--snapshot-after", snapshot_after),
    ):
        if version_id not in entries:
            msg = f"{name} version '{version_id}' is not in the manifest"
            raise ArmourError(msg)

    release_floor = release_time(entries[release_from])
    snapshot_floor = release_time(entries[snapshot_after])

    # Legacy (pre-26.1) snapshots are only wanted when backdating.
    snapshot_patterns = [NEW_SNAPSHOT_RE]
    if include_legacy_snapshots:
        snapshot_patterns.append(LEGACY_SNAPSHOT_RE)

    def wanted(entry: dict[str, Any]) -> bool:
        version_id, kind = entry["id"], entry["type"]
        if version_id in exclude:
            return False
        if kind == "release":
            return bool(
                RELEASE_RE.match(version_id) and release_time(entry) >= release_floor
            )
        if kind == "snapshot":
            return release_time(entry) > snapshot_floor and any(
                pattern.match(version_id) for pattern in snapshot_patterns
            )
        return False

    selected = [entry for entry in manifest["versions"] if wanted(entry)]
    selected.sort(key=lambda entry: (release_time(entry), entry["id"]))
    return selected


def describe(entry: dict[str, Any]) -> VersionTarget:
    """Fetch the per-version JSON and extract what later stages need."""
    detail = fetch_json_object(entry["url"])
    server = detail.get("downloads", {}).get("server")
    java = detail.get("javaVersion", {}).get("majorVersion")
    if server is None:
        msg = f"{entry['id']} has no server download"
        raise ArmourError(msg)
    if java is None:
        msg = f"{entry['id']} has no javaVersion"
        raise ArmourError(msg)
    return {
        "id": entry["id"],
        "kind": "release" if entry["type"] == "release" else "snapshot",
        "java": int(java),
        "server_url": server["url"],
        "sha1": server["sha1"],
    }


def load_manifest(location: str) -> dict[str, Any]:
    """Read the manifest from a URL, or from a local file when one exists there."""
    if urlsplit(location).scheme in {"http", "https"}:
        return fetch_json_object(location)
    path = Path(location)
    if not path.is_file():
        msg = f"--manifest {location!r} is neither an https URL nor an existing file"
        raise ArmourError(msg)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        msg = f"{path} is not valid JSON: {error}"
        raise ArmourError(msg) from error
    if not isinstance(manifest, dict) or "versions" not in manifest:
        msg = f"{path} is not a Mojang version manifest"
        raise ArmourError(msg)
    return manifest


def summary_markdown(targets: Iterable[VersionTarget]) -> str:
    """Render the job summary table for the selected versions."""
    targets = list(targets)
    rows = "".join(
        f"| `{item['id']}` | {item['kind']} | {item['java']} |\n" for item in targets
    )
    return (
        f"### Target Minecraft versions ({len(targets)})\n\n"
        "| Version | Kind | Java |\n|---|---|---|\n" + rows
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "--release-from", default="1.21.9", help="first release to include"
    )
    parser.add_argument(
        "--snapshot-after",
        default="26.3",
        help="include snapshots released after this version's release time",
    )
    parser.add_argument(
        "--legacy-snapshots",
        action="store_true",
        help="also include pre-26.1 snapshot names",
    )
    parser.add_argument(
        "--exclude",
        nargs="*",
        default=[],
        metavar="ID",
        help="version ids to skip, such as April Fools snapshots",
    )
    parser.add_argument(
        "--manifest", default=MANIFEST_URL, help="manifest URL or local file path"
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point."""
    args = parse_args(argv)

    selected = select_versions(
        load_manifest(args.manifest),
        args.release_from,
        args.snapshot_after,
        include_legacy_snapshots=args.legacy_snapshots,
        exclude=frozenset(args.exclude),
    )
    if not selected:
        msg = "No versions selected; refusing to emit an empty matrix"
        raise ArmourError(msg)
    if len(selected) > MAX_MATRIX_JOBS:
        msg = f"{len(selected)} versions exceeds the {MAX_MATRIX_JOBS}-job matrix limit"
        raise ArmourError(msg)

    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        include = list(pool.map(describe, selected))

    compact = (",", ":")
    set_output(
        "versions", json.dumps([item["id"] for item in include], separators=compact)
    )
    set_output("matrix", json.dumps({"include": include}, separators=compact))
    set_output("count", str(len(include)))
    append_summary(summary_markdown(include))
    return 0


if __name__ == "__main__":
    raise SystemExit(run(main))
