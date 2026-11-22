"""Tests for scripts/build_release_pack.py."""

from __future__ import annotations

import json
import zipfile
from typing import TYPE_CHECKING, Any

from support import TempDirTestCase, write

import build_release_pack
import build_version_pack
from armour_common import ArmourError

if TYPE_CHECKING:
    from pathlib import Path

MCMETA = {
    "pack": {"min_format": 88, "max_format": 121},
    "overlays": {
        "entries": [
            {"directory": "release/26.2", "min_format": 88, "max_format": 107},
            {"directory": "snapshot/26.3/1_to_2", "min_format": 108},
        ]
    },
}


def make_repo(root: Path, mcmeta: dict[str, Any] | None = None) -> Path:
    write(root / "pack.mcmeta", json.dumps(mcmeta or MCMETA))
    write(root / "armour.json", json.dumps({"pack_name": "my_pack"}))
    write(root / "data/p/function/root.mcfunction", "root")
    write(root / "release/26.2/data/p/function/r.mcfunction", "r")
    write(root / "snapshot/26.3/1_to_2/data/p/function/s.mcfunction", "s")
    write(root / "pack.png", "png")
    write(root / "LICENSE", "mit")
    write(root / "tests/data/x.mcfunction", "not shipped")
    write(root / ".github/workflows/ci.yml", "not shipped")
    return root


class BuildReleasePackTests(TempDirTestCase):
    def build(self, repo: Path) -> tuple[list[str], dict[str, Any]]:
        target = self.tmp / "dist" / "p.zip"
        names = build_release_pack.build_zip(repo, target)
        with zipfile.ZipFile(target) as archive:
            return names, json.loads(archive.read("pack.mcmeta"))

    def test_overlays_are_flattened_in_zip_and_pack_mcmeta(self) -> None:
        names, shipped = self.build(make_repo(self.tmp / "repo"))
        self.assertEqual(
            [e["directory"] for e in shipped["overlays"]["entries"]],
            ["release_26.2", "snapshot_26.3_1_to_2"],
        )
        self.assertIn("release_26.2/data/p/function/r.mcfunction", names)
        self.assertIn("snapshot_26.3_1_to_2/data/p/function/s.mcfunction", names)
        self.assertFalse([n for n in names if n.startswith(("release/", "snapshot/"))])

    def test_only_pack_files_are_shipped(self) -> None:
        names, _ = self.build(make_repo(self.tmp / "repo"))
        self.assertEqual(names[0], "pack.mcmeta")
        self.assertIn("pack.png", names)
        self.assertIn("LICENSE", names)
        self.assertIn("data/p/function/root.mcfunction", names)
        self.assertFalse([n for n in names if n.startswith(("tests", ".github"))])

    def test_zip_is_reproducible(self) -> None:
        repo = make_repo(self.tmp / "repo")
        build_release_pack.build_zip(repo, self.tmp / "a.zip")
        build_release_pack.build_zip(repo, self.tmp / "b.zip")
        self.assertEqual((self.tmp / "a.zip").read_bytes(), (self.tmp / "b.zip").read_bytes())

    def test_missing_overlay_folder_fails(self) -> None:
        repo = make_repo(self.tmp / "repo")
        (repo / "release/26.2/data/p/function/r.mcfunction").unlink()
        (repo / "release/26.2/data/p/function").rmdir()
        (repo / "release/26.2/data/p").rmdir()
        (repo / "release/26.2/data").rmdir()
        with self.assertRaises(ArmourError):
            self.build(repo)

    def test_invalid_flattened_name_fails(self) -> None:
        mcmeta = {
            "pack": MCMETA["pack"],
            "overlays": {"entries": [{"directory": "Bad Name/x", "min_format": 1}]},
        }
        repo = make_repo(self.tmp / "repo", mcmeta)
        write(repo / "Bad Name/x/data/p/a.json")
        with self.assertRaises(ArmourError):
            self.build(repo)

    def test_colliding_flattened_names_fail(self) -> None:
        mcmeta = {
            "pack": MCMETA["pack"],
            "overlays": {
                "entries": [
                    {"directory": "a/b", "min_format": 1},
                    {"directory": "a_b", "min_format": 1},
                ]
            },
        }
        repo = make_repo(self.tmp / "repo", mcmeta)
        write(repo / "a/b/data/p/a.json")
        write(repo / "a_b/data/p/a.json")
        with self.assertRaises(ArmourError):
            self.build(repo)

    def test_flatten_helper_does_not_change_its_input(self) -> None:
        flat, pairs = build_version_pack.flatten_overlay_directories(MCMETA)
        self.assertEqual(pairs[1], ("snapshot/26.3/1_to_2", "snapshot_26.3_1_to_2"))
        self.assertEqual(MCMETA["overlays"]["entries"][1]["directory"], "snapshot/26.3/1_to_2")
        self.assertEqual(flat["overlays"]["entries"][1]["directory"], "snapshot_26.3_1_to_2")
