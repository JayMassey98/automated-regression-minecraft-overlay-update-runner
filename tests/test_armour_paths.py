"""Tests for repository data-pack discovery."""

from __future__ import annotations

import json
import shutil
from typing import TYPE_CHECKING

from support import TempDirTestCase, write

import armour_paths
from armour_common import ArmourError

if TYPE_CHECKING:
    from pathlib import Path


def make_pack(path: Path) -> None:
    write(path / "pack.mcmeta", json.dumps({"pack": {"pack_format": 1}}))
    write(path / "data/example/function/test.mcfunction")


class ResolvePackTests(TempDirTestCase):
    def test_discovers_root_pack(self) -> None:
        make_pack(self.tmp)
        self.assertEqual(armour_paths.resolve_pack_root(self.tmp), self.tmp.resolve())

    def test_discovers_nested_pack(self) -> None:
        pack = self.tmp / "packs" / "deep pack"
        make_pack(pack)
        self.assertEqual(armour_paths.resolve_pack_root(self.tmp), pack.resolve())

    def test_configured_path_selects_one_of_multiple_packs(self) -> None:
        make_pack(self.tmp / "packs/a")
        make_pack(self.tmp / "packs/b")
        write(self.tmp / "armour.json", json.dumps({"pack_path": "packs/b"}))
        self.assertEqual(
            armour_paths.resolve_pack_root(self.tmp), (self.tmp / "packs/b").resolve()
        )

    def test_explicit_path_overrides_config(self) -> None:
        make_pack(self.tmp / "packs/a")
        make_pack(self.tmp / "packs/b")
        write(self.tmp / "armour.json", json.dumps({"pack_path": "packs/a"}))
        self.assertEqual(
            armour_paths.resolve_pack_root(self.tmp, "packs/b"),
            (self.tmp / "packs/b").resolve(),
        )

    def test_nested_pack_inside_pack_is_not_hidden(self) -> None:
        make_pack(self.tmp)
        make_pack(self.tmp / "nested")
        with self.assertRaisesRegex(ArmourError, "multiple data packs"):
            armour_paths.resolve_pack_root(self.tmp)

    def test_pack_under_directory_named_build_or_tests_is_discovered(self) -> None:
        folders = (
            "packs/build",
            "content/tests",
            "examples/dist",
            "resources/harness",
        )
        for folder in folders:
            with self.subTest(folder):
                root = self.tmp / folder
                make_pack(root)
                self.assertEqual(
                    armour_paths.resolve_pack_root(self.tmp), root.resolve()
                )
                shutil.rmtree(root)

    def test_ambiguous_packs_give_actionable_error(self) -> None:
        make_pack(self.tmp / "packs/a")
        make_pack(self.tmp / "packs/b")
        with self.assertRaisesRegex(ArmourError, "multiple data packs.*pack_path"):
            armour_paths.resolve_pack_root(self.tmp)

    def test_missing_pack_gives_actionable_error(self) -> None:
        with self.assertRaisesRegex(ArmourError, "No data pack found.*pack_path"):
            armour_paths.resolve_pack_root(self.tmp)

    def test_excludes_harness_tests_and_generated_directories(self) -> None:
        make_pack(self.tmp / "harness")
        make_pack(self.tmp / "tests")
        make_pack(self.tmp / "build")
        make_pack(self.tmp / "node_modules" / "dependency")
        with self.assertRaisesRegex(ArmourError, "No data pack found"):
            armour_paths.resolve_pack_root(self.tmp)

    def test_rejects_traversal_and_absolute_paths(self) -> None:
        make_pack(self.tmp / "pack")
        for value in ("../outside", str(self.tmp / "absolute")):
            with self.subTest(value), self.assertRaises(ArmourError):
                armour_paths.resolve_pack_root(self.tmp, value)

    def test_empty_configured_path_is_rejected(self) -> None:
        write(self.tmp / "armour.json", json.dumps({"pack_path": ""}))
        with self.assertRaisesRegex(ArmourError, "non-empty relative"):
            armour_paths.resolve_pack_root(self.tmp)

    def test_invalid_utf8_config_is_a_clean_error(self) -> None:
        (self.tmp / "armour.json").write_bytes(bytes([255]))
        with self.assertRaisesRegex(ArmourError, "UTF-8"):
            armour_paths.resolve_pack_root(self.tmp)

    def test_pack_path_symlink_cannot_escape_repository(self) -> None:
        outside = self.tmp.parent / (self.tmp.name + "-outside")
        make_pack(outside)
        try:
            (self.tmp / "external").symlink_to(outside, target_is_directory=True)
        except OSError:
            shutil.rmtree(outside)
            self.skipTest("symlinks are unavailable")
        with self.assertRaisesRegex(ArmourError, "stay inside the repository"):
            armour_paths.resolve_pack_root(self.tmp, "external")
        shutil.rmtree(outside)

    def test_invalid_configured_pack_is_reported(self) -> None:
        write(self.tmp / "armour.json", json.dumps({"pack_path": "missing"}))
        with self.assertRaisesRegex(ArmourError, "Configured data pack"):
            armour_paths.resolve_pack_root(self.tmp)
