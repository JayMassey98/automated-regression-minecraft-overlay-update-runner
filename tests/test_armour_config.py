"""Tests for scripts/armour_config.py."""

from __future__ import annotations

from support import TempDirTestCase, write

import armour_config
from armour_common import ArmourError


class LoadTests(TempDirTestCase):
    def test_defaults_tests_namespace(self) -> None:
        write(self.tmp / "armour.json", '{"pack_name": "my_pack"}')
        config = armour_config.load(self.tmp)
        self.assertEqual(config.pack_name, "my_pack")
        self.assertEqual(config.tests_namespace, "my_pack_test")

    def test_optional_pack_path(self) -> None:
        write(self.tmp / "armour.json", '{"pack_name": "p", "pack_path": "packs/p"}')
        self.assertEqual(armour_config.load(self.tmp).pack_path, "packs/p")

    def test_empty_pack_path_is_rejected(self) -> None:
        write(self.tmp / "armour.json", '{"pack_name": "p", "pack_path": ""}')
        with self.assertRaisesRegex(ArmourError, "pack_path"):
            armour_config.load(self.tmp)

    def test_invalid_utf8_is_a_clean_error(self) -> None:
        (self.tmp / "armour.json").write_bytes(b"\\xff")
        with self.assertRaisesRegex(ArmourError, "UTF-8"):
            armour_config.load(self.tmp)

    def test_invalid_pack_path_type(self) -> None:
        write(self.tmp / "armour.json", '{"pack_name": "p", "pack_path": 7}')
        with self.assertRaisesRegex(ArmourError, "pack_path"):
            armour_config.load(self.tmp)

    def test_explicit_namespace(self) -> None:
        write(
            self.tmp / "armour.json",
            '{"pack_name": "p", "tests_namespace": "p_tests"}',
        )
        self.assertEqual(armour_config.load(self.tmp).tests_namespace, "p_tests")

    def test_empty_namespace_falls_back_to_default(self) -> None:
        write(self.tmp / "armour.json", '{"pack_name": "p", "tests_namespace": ""}')
        self.assertEqual(armour_config.load(self.tmp).tests_namespace, "p_test")

    def test_start_command_runs_the_shared_harness(self) -> None:
        self.assertEqual(armour_config.START_COMMAND, "function armour:harness/start")

    def test_names_must_not_collide(self) -> None:
        bad = {
            "pack named like the harness": '{"pack_name": "armour"}',
            "tests named like the harness": (
                '{"pack_name": "p", "tests_namespace": "armour"}'
            ),
            "tests named like the pack": ('{"pack_name": "p", "tests_namespace": "p"}'),
        }
        for label, text in bad.items():
            with self.subTest(label):
                write(self.tmp / "armour.json", text)
                with self.assertRaisesRegex(ArmourError, "must all be different"):
                    armour_config.load(self.tmp)

    def test_missing_file(self) -> None:
        with self.assertRaisesRegex(ArmourError, "armour.json is missing"):
            armour_config.load(self.tmp)

    def test_invalid_contents(self) -> None:
        bad = {
            "not json": "{",
            "not an object": "[]",
            "no pack name": "{}",
            "empty pack name": '{"pack_name": ""}',
            "pack name not a string": '{"pack_name": 7}',
            "unsafe pack name": '{"pack_name": "a,b"}',
            "uppercase pack name": '{"pack_name": "Pack"}',
            "unsafe namespace": '{"pack_name": "p", "tests_namespace": "x/y"}',
        }
        for label, text in bad.items():
            with self.subTest(label):
                write(self.tmp / "armour.json", text)
                with self.assertRaises(ArmourError):
                    armour_config.load(self.tmp)

    def test_config_is_immutable(self) -> None:
        write(self.tmp / "armour.json", '{"pack_name": "p"}')
        config = armour_config.load(self.tmp)
        with self.assertRaises(AttributeError):
            config.pack_name = "other"  # type: ignore[misc]
