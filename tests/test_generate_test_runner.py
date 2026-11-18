"""Tests for scripts/generate_test_runner.py."""

from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO

from support import TempDirTestCase, make_tests_pack, write

import generate_test_runner
from armour_common import ArmourError


class GenerateTests(TempDirTestCase):
    def test_runner_skips_harness_and_assert_and_uses_namespace(self) -> None:
        pack = make_tests_pack(self.tmp, "demo_test")
        self.assertEqual(generate_test_runner.generate(pack, "demo_test"), 2)
        text = (
            pack / "data/demo_test/function/harness/run_tests.mcfunction"
        ).read_text(encoding="utf-8")
        self.assertIn("function demo_test:harness/begin_test", text)
        self.assertIn(
            "execute positioned 8 150 8 run function demo_test:generic/b", text
        )
        self.assertIn(
            "execute positioned 8 150 8 run function demo_test:windspanner/a", text
        )
        self.assertNotIn("run function demo_test:harness/start", text)
        self.assertNotIn("elemental_tools", text)

    def test_tests_are_listed_in_sorted_order(self) -> None:
        pack = make_tests_pack(self.tmp, "demo_test")
        self.assertEqual(
            generate_test_runner.find_tests(pack, "demo_test"),
            ["generic/b", "windspanner/a"],
        )

    def test_test_named_like_a_reserved_folder_is_still_a_test(self) -> None:
        function = self.tmp / "data/x/function"
        write(function / "assert.mcfunction")
        write(function / "harness.mcfunction")
        write(function / "assert/block.mcfunction")
        write(function / "harness/start.mcfunction")
        self.assertEqual(
            generate_test_runner.find_tests(self.tmp, "x"), ["assert", "harness"]
        )

    def test_render_counts_tests(self) -> None:
        text = generate_test_runner.render(["a", "b/c"], "ns")
        self.assertIn("Runs each of the 2 test functions", text)
        self.assertIn('set value "b/c"', text)

    def test_no_tests_is_an_error(self) -> None:
        (self.tmp / "data/x/function").mkdir(parents=True)
        with self.assertRaises(ArmourError):
            generate_test_runner.generate(self.tmp, "x")

    def test_main_reads_the_namespace_from_armour_json(self) -> None:
        write(self.tmp / "armour.json", '{"pack_name": "demo"}')
        make_tests_pack(self.tmp, "demo_test")
        out = StringIO()
        with redirect_stdout(out):
            code = generate_test_runner.main(
                [str(self.tmp / "tests"), "--repo", str(self.tmp)]
            )
        self.assertEqual(code, 0)
        self.assertIn("2 tests", out.getvalue())
