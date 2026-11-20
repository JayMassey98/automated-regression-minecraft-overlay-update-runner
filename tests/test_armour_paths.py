"""Tests for repository data-pack discovery."""

from __future__ import annotations

import json
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

    def test_invalid_configured_pack_is_reported(self) -> None:
        write(self.tmp / "armour.json", json.dumps({"pack_path": "missing"}))
        with self.assertRaisesRegex(ArmourError, "Configured data pack"):
            armour_paths.resolve_pack_root(self.tmp)
