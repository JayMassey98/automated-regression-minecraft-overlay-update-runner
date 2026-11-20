"""Tests for scripts/run_server_tests.py."""

from __future__ import annotations

import io
import json
import os
import stat
import sys
import unittest
from contextlib import AbstractContextManager, redirect_stdout
from typing import TYPE_CHECKING
from unittest import mock

from support import TESTS, TempDirTestCase, make_tests_pack, write

import run_server_tests
from armour_common import ArmourError

if TYPE_CHECKING:
    from pathlib import Path

FAKE_SERVER = TESTS / "fake_server.py"


def fast_pauses() -> AbstractContextManager[object]:
    """Shorten the log-catch-up pauses so tests stay quick."""
    return mock.patch.multiple(
        run_server_tests, DIAGNOSTIC_PAUSE_SECONDS=0.05, FLUSH_PAUSE_SECONDS=0.05
    )


class StageTests(TempDirTestCase):
    def test_stage_tests_pack_needs_the_tests_namespace_folder(self) -> None:
        pack = make_tests_pack(self.tmp, "demo_test")
        with self.assertRaisesRegex(ArmourError, "data.other.function"):
            run_server_tests.stage_tests_pack(pack, self.tmp / "out", "other")

    def test_stage_tests_pack_writes_the_run_tests_tag(self) -> None:
        pack = make_tests_pack(self.tmp, "demo_test")
        out = self.tmp / "out"
        run_server_tests.stage_tests_pack(pack, out, "demo_test")
        self.assertTrue((out / "data/armour/tags/function/run_tests.json").is_file())

    def test_stage_harness_pack_copies_the_shared_pack_and_widens_it(self) -> None:
        out = self.tmp / "out"
        run_server_tests.stage_harness_pack(out)
        function = out / "data/armour/function"
        self.assertTrue((function / "harness/start.mcfunction").is_file())
        self.assertTrue((function / "assert/block.mcfunction").is_file())
        mcmeta = json.loads((out / "pack.mcmeta").read_text(encoding="utf-8"))
        self.assertEqual(mcmeta["pack"]["max_format"], [9999, 0])

    def test_stage_harness_pack_leaves_the_original_alone(self) -> None:
        before = (run_server_tests.HARNESS_PACK_DIR / "pack.mcmeta").read_text()
        run_server_tests.stage_harness_pack(self.tmp / "out")
        after = (run_server_tests.HARNESS_PACK_DIR / "pack.mcmeta").read_text()
        self.assertEqual(before, after)

    def test_stage_harness_pack_reports_a_missing_pack(self) -> None:
        with (
            mock.patch.object(run_server_tests, "HARNESS_PACK_DIR", self.tmp / "gone"),
            self.assertRaisesRegex(ArmourError, "submodule update --init"),
        ):
            run_server_tests.stage_harness_pack(self.tmp / "out")

    def test_stage_tests_pack_widens_format_and_regenerates(self) -> None:
        pack = make_tests_pack(self.tmp, "demo_test")
        out = self.tmp / "out"
        self.assertEqual(run_server_tests.stage_tests_pack(pack, out, "demo_test"), 2)
        mcmeta = json.loads((out / "pack.mcmeta").read_text(encoding="utf-8"))
        self.assertEqual(mcmeta["pack"]["max_format"], [9999, 0])
        self.assertTrue(
            (out / "data/demo_test/function/harness/run_tests.mcfunction").is_file()
        )

    def test_server_properties_enable_all_three_packs(self) -> None:
        text = run_server_tests.SERVER_PROPERTIES.format(
            pack_name="demo", tests_namespace="demo_test"
        )
        self.assertIn(
            "initial-enabled-packs=vanilla,file/armour,file/demo,file/demo_test\n",
            text,
        )
        self.assertIn(f"server-port={run_server_tests.SERVER_PORT}\n", text)

    def test_write_server_files(self) -> None:
        run_server_tests.write_server_files(self.tmp, "demo", "demo_test")
        self.assertEqual((self.tmp / "eula.txt").read_text(), "eula=true\n")
        self.assertIn("file/demo_test", (self.tmp / "server.properties").read_text())

    def test_widen_max_format_needs_a_pack_section(self) -> None:
        write(self.tmp / "pack.mcmeta", "{}")
        with self.assertRaisesRegex(ArmourError, "pack"):
            run_server_tests.widen_max_format(self.tmp / "pack.mcmeta")


class StageDataPackTests(TempDirTestCase):
    def make_repo(self) -> Path:
        repo = self.tmp / "repo"
        write(
            repo / "pack.mcmeta",
            json.dumps(
                {
                    "pack": {"max_format": 94},
                    "overlays": {"entries": [{"directory": "ov", "min_format": 90}]},
                }
            ),
        )
        write(repo / "data/p/function/a.mcfunction")
        write(repo / "ov/data/p/function/b.mcfunction")
        return repo

    def test_releases_keep_pack_mcmeta_as_shipped(self) -> None:
        out = self.tmp / "out"
        run_server_tests.stage_data_pack(self.make_repo(), out, widen=False)
        self.assertTrue((out / "data/p/function/a.mcfunction").is_file())
        self.assertTrue((out / "ov/data/p/function/b.mcfunction").is_file())
        mcmeta = json.loads((out / "pack.mcmeta").read_text(encoding="utf-8"))
        self.assertEqual(mcmeta["pack"]["max_format"], 94)

    def test_snapshots_get_a_widened_max_format(self) -> None:
        out = self.tmp / "out"
        run_server_tests.stage_data_pack(self.make_repo(), out, widen=True)
        mcmeta = json.loads((out / "pack.mcmeta").read_text(encoding="utf-8"))
        self.assertEqual(mcmeta["pack"]["max_format"], [9999, 0])

    def test_overlay_directory_symlink_escape_is_rejected(self) -> None:
        repo = self.make_repo()
        outside = self.tmp / "outside"
        outside.mkdir()
        (repo / "ov").rename(repo / "ov-original")
        try:
            (repo / "ov").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("symlinks are unavailable")
        with self.assertRaisesRegex(ArmourError, "escapes the pack|Symbolic links"):
            run_server_tests.stage_data_pack(repo, self.tmp / "out", widen=False)

    def test_missing_overlay_folder(self) -> None:
        repo = self.make_repo()
        for path in sorted(repo.joinpath("ov").rglob("*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
        repo.joinpath("ov").rmdir()
        with self.assertRaisesRegex(ArmourError, "does not exist"):
            run_server_tests.stage_data_pack(repo, self.tmp / "out", widen=False)


class AnalyseTests(unittest.TestCase):
    def test_flags_only_our_packs(self) -> None:
        lines = [
            "[12:00:00] [Server thread/ERROR]: Couldn't parse demo:thing",
            "[12:00:00] [Server thread/ERROR]: Couldn't parse armour:harness/x",
            "[12:00:00] [Server thread/ERROR]: something about another pack",
            "[12:00:01] [Server thread/INFO]: [Server] ARMOUR_FAIL windspanner/a - bad",
            "[12:00:02] [Server thread/INFO]: [Server] ARMOUR_RESULT FAILED",
        ]
        found = run_server_tests.analyse(lines, ("armour", "demo", "demo_test"))
        self.assertEqual(len(found.load_errors), 2)
        self.assertEqual(found.fails, ["windspanner/a - bad"])
        self.assertEqual(found.result, "FAILED")

    def test_load_error_includes_its_detail_lines(self) -> None:
        lines = [
            "[12:00:00] [Server thread/ERROR]: Failed to load demo:f",
            "    Unknown command at position 4",
            "",
            "[12:00:01] [Server thread/INFO]: unrelated",
            "    not part of the error",
        ]
        found = run_server_tests.analyse(lines, ("demo",))
        self.assertEqual(
            found.load_errors,
            [lines[0] + "\n" + "Unknown command at position 4"],
        )

    def test_warnings_and_fatal(self) -> None:
        lines = [
            "[12:00:00] [Server thread/WARN]: demo is odd",
            "[12:00:01] [Server thread/FATAL]: demo broke",
            "[12:00:02] [Server thread/INFO]: demo is fine",
        ]
        found = run_server_tests.analyse(lines, ("demo",))
        self.assertEqual(found.load_warnings, [lines[0]])
        self.assertEqual(len(found.load_errors), 1)

    def test_harness_lines(self) -> None:
        lines = [
            "x ARMOUR_SKIP a",
            "x ARMOUR_SKIP b",
            "x ARMOUR_COUNTS 3 passed",
            "x ARMOUR_RESULT PASSED",
            "x ARMOUR_ERROR harness broke",
        ]
        found = run_server_tests.analyse(lines, ("demo",))
        self.assertEqual(found.counts, "3 passed")
        self.assertEqual(found.result, "PASSED")
        self.assertEqual(found.errors, ["harness broke"])


class RunTestsOnServerTests(TempDirTestCase):
    def run_mode(self, mode: str, **timeouts: float) -> tuple[list[str], str | None]:
        command = [sys.executable, str(FAKE_SERVER), mode]
        with fast_pauses():
            return run_server_tests.run_tests_on_server(
                command,
                self.tmp,
                "function armour:harness/start",
                startup_timeout=timeouts.get("startup", 10),
                test_timeout=timeouts.get("test", 10),
            )

    def test_passing_run(self) -> None:
        lines, problem = self.run_mode("passes")
        self.assertIsNone(problem)
        found = run_server_tests.analyse(lines, ("demo",))
        self.assertEqual(found.result, "PASSED")
        self.assertIn("Done (", (self.tmp / "server-output.log").read_text())

    def test_failing_run(self) -> None:
        lines, problem = self.run_mode("fails")
        self.assertIsNone(problem)
        found = run_server_tests.analyse(lines, ("demo",))
        self.assertEqual(found.fails, ["generic/b - expected 1, got 2"])

    def test_server_that_never_starts(self) -> None:
        _, problem = self.run_mode("never_starts", startup=0.3)
        self.assertEqual(problem, "The server did not finish starting")

    def test_tests_that_never_report(self) -> None:
        lines, problem = self.run_mode("silent", test=0.3)
        self.assertIn("did not report a result", problem or "")
        # The `datapack list` diagnostic reply is captured too.
        self.assertTrue(any("data packs enabled" in line for line in lines))


class ReportTests(unittest.TestCase):
    def report(
        self, found: run_server_tests.Analysis, problem: str | None = None
    ) -> tuple[bool, str]:
        out = io.StringIO()
        with mock.patch.dict(os.environ), redirect_stdout(out):
            os.environ.pop("GITHUB_STEP_SUMMARY", None)
            ok = run_server_tests.report("1.21.11", found, problem, ["last line"])
        return ok, out.getvalue()

    def test_passed(self) -> None:
        found = run_server_tests.Analysis(result="PASSED", counts="2 passed")
        ok, printed = self.report(found)
        self.assertTrue(ok)
        self.assertIn("Minecraft 1.21.11: 2 passed; result=PASSED", printed)

    def test_each_kind_of_problem_fails_the_run(self) -> None:
        cases = {
            "no result": run_server_tests.Analysis(),
            "failed result": run_server_tests.Analysis(result="FAILED"),
            "a failure": run_server_tests.Analysis(result="PASSED", fails=["x"]),
            "load error": run_server_tests.Analysis(result="PASSED", load_errors=["e"]),
            "harness error": run_server_tests.Analysis(result="PASSED", errors=["e"]),
        }
        for label, found in cases.items():
            with self.subTest(label):
                self.assertFalse(self.report(found)[0])

    def test_problem_prints_the_log_tail_and_an_annotation(self) -> None:
        found = run_server_tests.Analysis(result="PASSED")
        ok, printed = self.report(found, "The server did not finish starting")
        self.assertFalse(ok)
        self.assertIn("::error title=Server test run (1.21.11)::The server", printed)
        self.assertIn("last 60 server log lines", printed)
        self.assertIn("last line", printed)

    def test_annotations(self) -> None:
        found = run_server_tests.Analysis(
            fails=["f"],
            errors=["h"],
            load_errors=["l"],
            load_warnings=["w"],
        )
        _, printed = self.report(found)
        for expected in (
            "::warning title=Pack log warning (1.21.11)::w",
            "::error title=Pack failed to load (1.21.11)::l",
            "::error title=Test harness (1.21.11)::h",
            "::error title=Test failed (1.21.11)::f",
        ):
            self.assertIn(expected, printed)


@unittest.skipIf(sys.platform == "win32", "needs a POSIX shell script as fake java")
class MainTests(TempDirTestCase):
    def run_main(
        self, mode: str, *, nested: bool = False, default_work_dir: bool = False
    ) -> tuple[int, str]:
        repo = self.tmp / "repo"
        pack = repo / "packs" / "deep pack" if nested else repo
        config = {"pack_name": "demo"}
        if nested:
            config["pack_path"] = "packs/deep pack"
        write(repo / "armour.json", json.dumps(config))
        write(pack / "pack.mcmeta", json.dumps({"pack": {"max_format": 94}}))
        write(pack / "data/demo/function/a.mcfunction")
        make_tests_pack(repo, "demo_test")
        java = repo / "tools" / "java"
        java.parent.mkdir(parents=True)
        java.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_SERVER}" "{mode}"\n'
        )
        java.chmod(java.stat().st_mode | stat.S_IEXEC)
        jar = self.tmp / "server.jar"
        jar.write_bytes(b"")
        args = [
            "--version",
            "1.21.11",
            "--server-url",
            "https://x.test/s.jar",
            "--sha1",
            "0",
            "--server-jar",
            "../server.jar",
            "--java",
            "tools/java",
            "--repo",
            str(repo),
            "--tests-dir",
            "tests",
            "--startup-timeout",
            "10",
            "--test-timeout",
            "10",
        ]
        if not default_work_dir:
            args.extend(["--work-dir", str(self.tmp / "run")])
        out = io.StringIO()
        with fast_pauses(), redirect_stdout(out):
            code = run_server_tests.main(args)
        return code, out.getvalue()

    def test_work_directory_inside_repository_is_rejected_before_deletion(self) -> None:
        repo = self.tmp / "repo"
        write(repo / "armour.json", '{"pack_name": "demo"}')
        write(repo / "pack.mcmeta", '{"pack": {"max_format": 94}}')
        write(repo / "data/demo/function/a.mcfunction")
        sentinel = repo / "keep.txt"
        sentinel.write_text("safe")
        args = [
            "--version",
            "1.21.11",
            "--server-url",
            "https://x.test/s.jar",
            "--sha1",
            "0",
            "--repo",
            str(repo),
            "--work-dir",
            ".",
        ]
        with self.assertRaisesRegex(ArmourError, "must not overlap"):
            run_server_tests.main(args)
        self.assertEqual(sentinel.read_text(), "safe")

    def test_cache_directory_inside_nested_source_pack_is_rejected(self) -> None:
        repo = self.tmp / "repo"
        pack = repo / "packs" / "source"
        write(
            repo / "armour.json",
            json.dumps({"pack_name": "demo", "pack_path": "packs/source"}),
        )
        write(pack / "pack.mcmeta", '{"pack": {"max_format": 94}}')
        write(pack / "data/demo/function/a.mcfunction")
        args = [
            "--version",
            "1.21.11",
            "--server-url",
            "https://x.test/s.jar",
            "--sha1",
            "0",
            "--repo",
            str(repo),
            "--cache-dir",
            "packs/source/cache",
            "--work-dir",
            str(self.tmp / "work"),
        ]
        with self.assertRaisesRegex(ArmourError, "Cache directory must not overlap"):
            run_server_tests.main(args)
        self.assertTrue((pack / "pack.mcmeta").is_file())
        self.assertFalse((pack / "cache").exists())

    def test_symlinked_work_directory_is_rejected_without_deleting_target(self) -> None:
        repo = self.tmp / "repo"
        write(repo / "armour.json", '{"pack_name": "demo"}')
        write(repo / "pack.mcmeta", '{"pack": {"max_format": 94}}')
        write(repo / "data/demo/function/a.mcfunction")
        external = self.tmp / "external"
        write(external / "keep.txt", "safe")
        link = self.tmp / "work-link"
        try:
            link.symlink_to(external, target_is_directory=True)
        except OSError:
            self.skipTest("symlinks are unavailable")
        args = [
            "--version", "1.21.11", "--server-url", "https://x.test/s.jar",
            "--sha1", "0", "--repo", str(repo), "--work-dir", str(link),
        ]
        with self.assertRaisesRegex(ArmourError, "Symbolic links"):
            run_server_tests.main(args)
        self.assertEqual((external / "keep.txt").read_text(), "safe")

    def test_default_work_directory_is_a_sibling_of_repo(self) -> None:
        code, printed = self.run_main("passes", default_work_dir=True)
        self.assertEqual(code, 0, printed)
        work = self.tmp / ".repo-server-run"
        self.assertTrue((work / "world/datapacks/demo/pack.mcmeta").is_file())
        self.assertTrue((self.tmp / "repo/pack.mcmeta").is_file())

    def test_passing_pack(self) -> None:
        code, printed = self.run_main("passes")
        self.assertEqual(code, 0, printed)
        self.assertIn("Staged the data pack and 2 tests", printed)
        datapacks = self.tmp / "run/world/datapacks"
        self.assertTrue((datapacks / "demo/pack.mcmeta").is_file())
        self.assertTrue((datapacks / "demo_test/pack.mcmeta").is_file())
        self.assertTrue((datapacks / "armour/pack.mcmeta").is_file())
        self.assertIn(
            "file/armour,file/demo,file/demo_test",
            (self.tmp / "run/server.properties").read_text(),
        )

    def test_nested_pack_is_staged_without_modifying_the_source(self) -> None:
        code, printed = self.run_main("passes", nested=True)
        self.assertEqual(code, 0, printed)
        staged = self.tmp / "run/world/datapacks/demo"
        self.assertTrue((staged / "pack.mcmeta").is_file())
        self.assertTrue((staged / "data/demo/function/a.mcfunction").is_file())
        source = self.tmp / "repo/packs/deep pack"
        self.assertEqual(
            json.loads((source / "pack.mcmeta").read_text())["pack"]["max_format"],
            94,
        )

    def test_failing_test_gives_exit_code_1(self) -> None:
        code, printed = self.run_main("fails")
        self.assertEqual(code, 1)
        self.assertIn("::error title=Test failed (1.21.11)::generic/b", printed)

    def test_load_error_gives_exit_code_1(self) -> None:
        code, printed = self.run_main("load_error")
        self.assertEqual(code, 1)
        self.assertIn("Pack failed to load", printed)
        self.assertIn("Unknown command near 'foo'", printed)
