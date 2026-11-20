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
START = "function armour:harness/start"


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

    def test_server_properties_enable_the_given_packs(self) -> None:
        text = run_server_tests.SERVER_PROPERTIES.format(
            enabled_packs="file/armour,file/demo,file/demo_test"
        )
        self.assertIn(
            "initial-enabled-packs=vanilla,file/armour,file/demo,file/demo_test\n",
            text,
        )
        self.assertIn(f"server-port={run_server_tests.SERVER_PORT}\n", text)

    def test_write_server_files_enables_the_packs_in_order(self) -> None:
        run_server_tests.write_server_files(self.tmp, ["armour", "demo", "demo_test"])
        self.assertEqual((self.tmp / "eula.txt").read_text(), "eula=true\n")
        self.assertIn(
            "initial-enabled-packs=vanilla,file/armour,file/demo,file/demo_test\n",
            (self.tmp / "server.properties").read_text(),
        )

    def test_write_server_files_with_only_the_data_pack(self) -> None:
        run_server_tests.write_server_files(self.tmp, ["demo"])
        self.assertIn(
            "initial-enabled-packs=vanilla,file/demo\n",
            (self.tmp / "server.properties").read_text(),
        )

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
    def run_mode(
        self, mode: str, *, start_command: str | None = START, **timeouts: float
    ) -> tuple[list[str], str | None]:
        command = [sys.executable, str(FAKE_SERVER), mode]
        with fast_pauses():
            return run_server_tests.run_tests_on_server(
                command,
                self.tmp,
                start_command,
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

    def test_load_only_run_does_not_start_any_tests(self) -> None:
        lines, problem = self.run_mode("passes", start_command=None)
        self.assertIsNone(problem)
        self.assertTrue(any("Done (" in line for line in lines))
        # The fake server only reports a result when it is told to start tests.
        self.assertFalse(any("ARMOUR_RESULT" in line for line in lines))

    def test_load_only_run_does_not_wait_for_a_result(self) -> None:
        _, problem = self.run_mode("silent", start_command=None, test=0.3)
        self.assertIsNone(problem)

    def test_load_only_server_that_never_starts(self) -> None:
        _, problem = self.run_mode("never_starts", start_command=None, startup=0.3)
        self.assertEqual(problem, "The server did not finish starting")

    def test_load_only_run_sees_errors_logged_while_starting(self) -> None:
        lines, problem = self.run_mode("startup_error", start_command=None)
        self.assertIsNone(problem)
        found = run_server_tests.analyse(lines, ("demo",))
        self.assertEqual(len(found.load_errors), 1)
        self.assertIn("Unknown command near 'foo'", found.load_errors[0])


class ReportTests(unittest.TestCase):
    def report(
        self,
        found: run_server_tests.Analysis,
        problem: str | None = None,
        *,
        load_only: bool = False,
    ) -> tuple[bool, str]:
        out = io.StringIO()
        with mock.patch.dict(os.environ), redirect_stdout(out):
            os.environ.pop("GITHUB_STEP_SUMMARY", None)
            ok = run_server_tests.report(
                "1.21.11", found, problem, ["last line"], load_only=load_only
            )
        return ok, out.getvalue()

    def test_load_only_passes_without_a_test_result(self) -> None:
        ok, printed = self.report(run_server_tests.Analysis(), load_only=True)
        self.assertTrue(ok)
        self.assertIn("Minecraft 1.21.11: load only; 0 failures", printed)

    def test_load_only_still_fails_on_a_load_error(self) -> None:
        found = run_server_tests.Analysis(load_errors=["e"])
        self.assertFalse(self.report(found, load_only=True)[0])

    def test_load_only_still_fails_when_the_server_did_not_start(self) -> None:
        found = run_server_tests.Analysis()
        problem = "The server did not finish starting"
        ok, printed = self.report(found, problem, load_only=True)
        self.assertFalse(ok)
        self.assertIn("::error title=Server test run (1.21.11)::The server", printed)

    def test_without_load_only_a_missing_result_fails(self) -> None:
        self.assertFalse(self.report(run_server_tests.Analysis())[0])

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
    def run_main(self, mode: str, *, load_only: bool = False) -> tuple[int, str]:
        repo = self.tmp / "repo"
        write(repo / "armour.json", '{"pack_name": "demo"}')
        write(repo / "pack.mcmeta", json.dumps({"pack": {"max_format": 94}}))
        write(repo / "data/demo/function/a.mcfunction")
        if not load_only:
            make_tests_pack(repo, "demo_test")
        java = self.tmp / "fake-java"
        java.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_SERVER}" "{mode}"\n'
        )
        java.chmod(java.stat().st_mode | stat.S_IEXEC)
        jar = self.tmp / "server.jar"
        jar.write_bytes(b"")
        argv = [
            "--version", "1.21.11", "--server-url", "https://x.test/s.jar",
            "--sha1", "0", "--server-jar", str(jar), "--java", str(java),
            "--repo", str(repo), "--work-dir", str(self.tmp / "run"),
            "--startup-timeout", "10", "--test-timeout", "10",
        ]  # fmt: skip
        argv += ["--load-only"] if load_only else ["--tests-dir", str(repo / "tests")]
        out = io.StringIO()
        with fast_pauses(), redirect_stdout(out):
            code = run_server_tests.main(argv)
        return code, out.getvalue()

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

    def test_load_only_stages_just_the_data_pack(self) -> None:
        code, printed = self.run_main("passes", load_only=True)
        self.assertEqual(code, 0, printed)
        self.assertIn("Staged the data pack for Minecraft 1.21.11 (load only)", printed)
        self.assertIn("Minecraft 1.21.11: load only;", printed)
        datapacks = self.tmp / "run/world/datapacks"
        self.assertEqual([p.name for p in datapacks.iterdir()], ["demo"])
        self.assertIn(
            "initial-enabled-packs=vanilla,file/demo\n",
            (self.tmp / "run/server.properties").read_text(),
        )

    def test_load_only_error_while_starting_gives_exit_code_1(self) -> None:
        code, printed = self.run_main("startup_error", load_only=True)
        self.assertEqual(code, 1)
        self.assertIn("Pack failed to load", printed)
        self.assertIn("Unknown command near 'foo'", printed)

    def test_failing_test_gives_exit_code_1(self) -> None:
        code, printed = self.run_main("fails")
        self.assertEqual(code, 1)
        self.assertIn("::error title=Test failed (1.21.11)::generic/b", printed)

    def test_load_error_gives_exit_code_1(self) -> None:
        code, printed = self.run_main("load_error")
        self.assertEqual(code, 1)
        self.assertIn("Pack failed to load", printed)
        self.assertIn("Unknown command near 'foo'", printed)
