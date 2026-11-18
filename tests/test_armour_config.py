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
        self.assertEqual(config.start_command, "function my_pack_test:harness/start")

    def test_explicit_namespace(self) -> None:
        write(
            self.tmp / "armour.json",
            '{"pack_name": "p", "tests_namespace": "p_tests"}',
        )
        self.assertEqual(armour_config.load(self.tmp).tests_namespace, "p_tests")

    def test_empty_namespace_falls_back_to_default(self) -> None:
        write(self.tmp / "armour.json", '{"pack_name": "p", "tests_namespace": ""}')
        self.assertEqual(armour_config.load(self.tmp).tests_namespace, "p_test")

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
