"""Tests for repository data-pack discovery."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from unittest import mock

from support import TempDirTestCase, write

import armour_paths
from armour_common import ArmourError


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

    def test_ignores_nested_vendor_trees_but_allows_explicit_selection(self) -> None:
        make_pack(self.tmp / "vendor/dependency")
        make_pack(self.tmp / "packs/real")
        self.assertEqual(
            armour_paths.resolve_pack_root(self.tmp),
            (self.tmp / "packs/real").resolve(),
        )
        self.assertEqual(
            armour_paths.resolve_pack_root(self.tmp, "vendor/dependency"),
            (self.tmp / "vendor/dependency").resolve(),
        )

    def test_configured_pack_exclude_skips_selected_subtree(self) -> None:
        make_pack(self.tmp / "vendor/ignored")
        make_pack(self.tmp / "packs/selected")
        write(
            self.tmp / "armour.json",
            json.dumps({"pack_exclude": ["vendor/ignored"]}),
        )
        self.assertEqual(
            armour_paths.resolve_pack_root(self.tmp),
            (self.tmp / "packs/selected").resolve(),
        )

    def test_invalid_pack_exclude_paths_are_rejected(self) -> None:
        for value in ("", "../outside", "/absolute"):
            with self.subTest(value=value):
                write(self.tmp / "armour.json", json.dumps({"pack_exclude": [value]}))
                with self.assertRaisesRegex(ArmourError, "pack_exclude"):
                    armour_paths.resolve_pack_root(self.tmp)

    def test_excludes_harness_tests_and_generated_directories(self) -> None:
        make_pack(self.tmp / "harness")
        make_pack(self.tmp / "tests")
        make_pack(self.tmp / "build")
        make_pack(self.tmp / "node_modules" / "dependency")
        with self.assertRaisesRegex(ArmourError, "No data pack found"):
            armour_paths.resolve_pack_root(self.tmp)

    def test_macos_system_temp_alias_is_allowed(self) -> None:
        original_is_symlink = Path.is_symlink

        def pretend_macos_tmp_is_symlink(path: Path) -> bool:
            return path == Path(os.path.sep, "tmp") or original_is_symlink(path)

        with (
            mock.patch.object(armour_paths.sys, "platform", "darwin"),
            mock.patch.object(Path, "is_symlink", pretend_macos_tmp_is_symlink),
        ):
            armour_paths.reject_symlink_components(
                Path(os.path.sep, "tmp", "ordinary", "output"), Path("/")
            )

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

    def test_pack_path_rejects_internal_symlink_alias(self) -> None:
        actual = self.tmp / "packs/real"
        make_pack(actual)
        link = self.tmp / "packs/selected"
        try:
            link.symlink_to(actual, target_is_directory=True)
        except OSError:
            self.skipTest("symlinks are unavailable")
        with self.assertRaisesRegex(ArmourError, "Symbolic links"):
            armour_paths.resolve_pack_root(self.tmp, "packs/selected")

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

    def test_empty_or_inverted_pack_metadata_is_not_discovered(self) -> None:
        for pack_metadata in (
            {},
            {"min_format": 100, "max_format": 80},
            {"pack_format": [80, 1]},
            {"pack_format": 80.5},
        ):
            with self.subTest(pack_metadata=pack_metadata):
                write(
                    self.tmp / "pack.mcmeta",
                    json.dumps({"pack": pack_metadata}),
                )
                write(self.tmp / "data/example/function/test.mcfunction")
                with self.assertRaisesRegex(ArmourError, "No data pack found"):
                    armour_paths.resolve_pack_root(self.tmp)

    def test_invalid_configured_pack_is_reported(self) -> None:
        write(self.tmp / "armour.json", json.dumps({"pack_path": "missing"}))
        with self.assertRaisesRegex(ArmourError, "Configured data pack"):
            armour_paths.resolve_pack_root(self.tmp)
