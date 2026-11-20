"""Tests for scripts/build_version_pack.py."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import zipfile
from contextlib import redirect_stdout
from typing import TYPE_CHECKING, Any
from unittest import TestCase, mock

from support import SCRIPTS, TempDirTestCase, write

import build_version_pack
from armour_common import ArmourError

if TYPE_CHECKING:
    from pathlib import Path

OVERLAY_MCMETA = {
    "pack": {"min_format": 88, "max_format": 121},
    "overlays": {
        "entries": [
            {"directory": "old", "min_format": 88, "max_format": 93},
            {"directory": "new", "min_format": 94},
        ]
    },
}


def make_repo(root: Path, mcmeta: dict[str, Any] | None = None) -> Path:
    write(root / "pack.mcmeta", json.dumps(mcmeta or OVERLAY_MCMETA))
    write(root / "data/p/function/root.mcfunction")
    write(root / "old/data/p/function/old.mcfunction")
    write(root / "new/data/p/function/new.mcfunction")
    return root


def make_jar(path: Path, version_json: dict[str, Any], *, nested: bool) -> Path:
    payload = json.dumps(version_json).encode()
    with zipfile.ZipFile(path, "w") as jar:
        if nested:
            inner = io.BytesIO()
            with zipfile.ZipFile(inner, "w") as inner_jar:
                inner_jar.writestr("version.json", payload)
            jar.writestr("META-INF/versions/1.21/server-1.21.jar", inner.getvalue())
        else:
            jar.writestr("version.json", payload)
    return path


class FormatTests(TestCase):
    def test_data_format(self) -> None:
        cases: list[tuple[object, tuple[int, int]]] = [
            (81, (81, 0)),
            ({"resource": 64, "data": 81}, (81, 0)),
            ({"data_major": 94, "data_minor": 1}, (94, 1)),
            ({"data_major": 94}, (94, 0)),
        ]
        for value, expected in cases:
            with self.subTest(value):
                self.assertEqual(build_version_pack.data_format(value), expected)

    def test_data_format_rejects_nonsense(self) -> None:
        values: list[object] = [None, True, "81", {}, [81]]
        for value in values:
            with self.subTest(value), self.assertRaises(ArmourError):
                build_version_pack.data_format(value)

    def test_as_format(self) -> None:
        self.assertEqual(build_version_pack.as_format(88, upper=False), (88, 0))
        self.assertEqual(
            build_version_pack.as_format(88, upper=True),
            (88, build_version_pack.MINOR_INFINITY),
        )
        self.assertEqual(build_version_pack.as_format([94, 1], upper=True), (94, 1))
        values: list[object] = ["88", True, [1], [1, 2, 3]]
        for value in values:
            with self.subTest(value), self.assertRaises(ArmourError):
                build_version_pack.as_format(value, upper=False)

    def test_overlay_range_for_each_style(self) -> None:
        infinity = build_version_pack.MINOR_INFINITY
        cases: list[tuple[dict[str, Any], tuple[tuple[int, int], tuple[int, int]]]] = [
            ({}, ((0, 0), (infinity, infinity))),
            ({"min_format": 88}, ((88, 0), (infinity, infinity))),
            ({"max_format": 93}, ((0, 0), (93, infinity))),
            ({"formats": 81}, ((81, 0), (81, infinity))),
            ({"formats": [80, 82]}, ((80, 0), (82, infinity))),
            (
                {"formats": {"min_inclusive": 80, "max_inclusive": 82}},
                ((80, 0), (82, infinity)),
            ),
        ]
        for entry, expected in cases:
            with self.subTest(entry):
                self.assertEqual(build_version_pack.overlay_range(entry), expected)

    def test_overlay_range_rejects_unknown_formats_value(self) -> None:
        with self.assertRaises(ArmourError):
            build_version_pack.overlay_range({"formats": "81"})


class ReadVersionJsonTests(TempDirTestCase):
    def test_reads_a_plain_jar(self) -> None:
        jar = make_jar(self.tmp / "a.jar", {"pack_version": 81}, nested=False)
        self.assertEqual(
            build_version_pack.read_version_json(jar), {"pack_version": 81}
        )

    def test_reads_a_bundler_jar(self) -> None:
        jar = make_jar(self.tmp / "b.jar", {"pack_version": 94}, nested=True)
        self.assertEqual(
            build_version_pack.read_version_json(jar), {"pack_version": 94}
        )

    def test_missing_version_json(self) -> None:
        jar = self.tmp / "c.jar"
        with zipfile.ZipFile(jar, "w") as archive:
            archive.writestr("other.txt", "x")
        with self.assertRaisesRegex(ArmourError, "No version.json"):
            build_version_pack.read_version_json(jar)


class OverlayEntriesTests(TestCase):
    def test_returns_entries(self) -> None:
        self.assertEqual(len(build_version_pack.overlay_entries(OVERLAY_MCMETA)), 2)

    def test_no_overlays(self) -> None:
        self.assertEqual(build_version_pack.overlay_entries({"pack": {}}), [])

    def test_rejects_bad_directories(self) -> None:
        for directory in (None, "", "../escape", "/abs", "a/../../b"):
            mcmeta = {"overlays": {"entries": [{"directory": directory}]}}
            with self.subTest(directory), self.assertRaises(ArmourError):
                build_version_pack.overlay_entries(mcmeta)


class BuildTests(TempDirTestCase):
    def build(self, repo: Path, fmt: tuple[int, int]) -> list[str]:
        out = self.tmp / "out"
        build_version_pack.build_flattened_pack(repo, out, fmt)
        return sorted(
            p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()
        )

    def test_overlays_applied_by_format(self) -> None:
        repo = make_repo(self.tmp / "repo")
        self.assertEqual(
            self.build(repo, (90, 0)),
            [
                "data/p/function/old.mcfunction",
                "data/p/function/root.mcfunction",
                "pack.mcmeta",
            ],
        )
        self.assertEqual(
            self.build(repo, (94, 1)),
            [
                "data/p/function/new.mcfunction",
                "data/p/function/root.mcfunction",
                "pack.mcmeta",
            ],
        )

    def test_later_overlays_overwrite_earlier_files(self) -> None:
        repo = make_repo(
            self.tmp / "repo",
            {"overlays": {"entries": [{"directory": "old"}, {"directory": "new"}]}},
        )
        write(repo / "data/p/function/shared.mcfunction", "root")
        write(repo / "old/data/p/function/shared.mcfunction", "old")
        write(repo / "new/data/p/function/shared.mcfunction", "new")
        self.build(repo, (90, 0))
        shared = self.tmp / "out/data/p/function/shared.mcfunction"
        self.assertEqual(shared.read_text(encoding="utf-8"), "new")

    def test_result_records_sources_and_counts(self) -> None:
        repo = make_repo(self.tmp / "repo")
        result = build_version_pack.build_flattened_pack(
            repo, self.tmp / "out", (90, 0)
        )
        self.assertEqual(result.applied, ["data/ (1 files)", "old/ (1 files)"])
        self.assertEqual(
            result.source_map["data/p/function/old.mcfunction"],
            "old/data/p/function/old.mcfunction",
        )

    def test_spyglass_json_travels_with_the_pack(self) -> None:
        repo = make_repo(self.tmp / "repo")
        write(repo / "spyglass.json", "{}")
        self.assertIn("spyglass.json", self.build(repo, (90, 0)))

    def test_stale_output_is_removed(self) -> None:
        repo = make_repo(self.tmp / "repo")
        write(self.tmp / "out/stale.txt")
        self.assertNotIn("stale.txt", self.build(repo, (90, 0)))

    def test_missing_overlay_folder_is_an_error(self) -> None:
        repo = make_repo(self.tmp / "repo")
        (repo / "old/data/p/function/old.mcfunction").unlink()
        (repo / "old/data/p/function").rmdir()
        (repo / "old/data/p").rmdir()
        (repo / "old/data").rmdir()
        with self.assertRaisesRegex(ArmourError, "does not exist"):
            self.build(repo, (90, 0))

    def test_missing_pack_mcmeta_is_an_error(self) -> None:
        with self.assertRaisesRegex(ArmourError, "pack.mcmeta"):
            self.build(self.tmp, (90, 0))

    def test_invalid_pack_mcmeta_is_an_error(self) -> None:
        write(self.tmp / "pack.mcmeta", "{")
        with self.assertRaisesRegex(ArmourError, "not valid JSON"):
            self.build(self.tmp, (90, 0))


class MainTests(TempDirTestCase):
    def run_main(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = build_version_pack.main(list(args))
        return code, out.getvalue()

    def test_pack_version_writes_pack_source_map_and_summary(self) -> None:
        repo = make_repo(self.tmp / "repo")
        summary = self.tmp / "summary.md"
        with mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}):
            code, printed = self.run_main(
                "--repo",
                str(repo),
                "--pack-version",
                '{"data_major": 94, "data_minor": 1}',
                "--out",
                str(self.tmp / "out"),
                "--source-map",
                str(self.tmp / "maps/source-map.json"),
            )
        self.assertEqual(code, 0)
        self.assertIn("Data pack format 94.1", printed)
        source_map = json.loads((self.tmp / "maps/source-map.json").read_text())
        self.assertEqual(
            source_map["data/p/function/new.mcfunction"],
            "new/data/p/function/new.mcfunction",
        )
        self.assertIn("94.1", summary.read_text(encoding="utf-8"))

    def test_nested_pack_path_uses_repo_root_for_outputs_and_source_map(self) -> None:
        repo = self.tmp / "repo"
        pack = make_repo(repo / "packs" / "deep pack")
        code, printed = self.run_main(
            "--repo",
            str(repo),
            "--pack-path",
            "packs/deep pack",
            "--pack-version",
            '{"data_major": 94, "data_minor": 1}',
            "--out",
            "build/flattened",
            "--source-map",
            "build/source-map.json",
        )
        self.assertEqual(code, 0, printed)
        self.assertTrue((repo / "build/flattened/pack.mcmeta").is_file())
        source_map = json.loads((repo / "build/source-map.json").read_text())
        self.assertEqual(
            source_map["data/p/function/new.mcfunction"],
            "packs/deep pack/new/data/p/function/new.mcfunction",
        )
        self.assertTrue((pack / "pack.mcmeta").is_file())

    def test_reads_the_format_from_a_downloaded_jar(self) -> None:
        repo = make_repo(self.tmp / "repo")
        jar = make_jar(self.tmp / "s.jar", {"pack_version": 90}, nested=False)
        with mock.patch.object(
            build_version_pack, "download_server_jar", return_value=jar
        ) as download:
            code, printed = self.run_main(
                "--repo",
                str(repo),
                "--server-url",
                "https://x.test/s.jar",
                "--sha1",
                "abc",
                "--cache-dir",
                str(self.tmp / "cache"),
                "--out",
                str(self.tmp / "out"),
            )
        self.assertEqual(code, 0)
        download.assert_called_once()
        self.assertIn("Data pack format 90.0", printed)

    def test_needs_a_jar_or_a_pack_version(self) -> None:
        with self.assertRaisesRegex(ArmourError, "--pack-version"):
            self.run_main("--out", str(self.tmp / "out"))

    def test_rejects_invalid_pack_version_json(self) -> None:
        with self.assertRaisesRegex(ArmourError, "not valid JSON"):
            self.run_main("--pack-version", "{", "--out", str(self.tmp / "out"))


class CommandLineTests(TempDirTestCase):
    """Run the script the way a workflow does."""

    def run_script(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {k: v for k, v in os.environ.items() if k != "GITHUB_ACTIONS"}
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "build_version_pack.py"), *args],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

    def test_success(self) -> None:
        repo = make_repo(self.tmp / "repo")
        done = self.run_script(
            "--repo",
            str(repo),
            "--pack-version",
            '{"data_major": 90, "data_minor": 0}',
            "--out",
            str(self.tmp / "out"),
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue((self.tmp / "out/data/p/function/old.mcfunction").is_file())

    def test_failure_is_a_clean_message_not_a_traceback(self) -> None:
        done = self.run_script(
            "--repo",
            str(self.tmp),
            "--pack-version",
            "90",
            "--out",
            str(self.tmp / "out"),
        )
        self.assertEqual(done.returncode, 1)
        self.assertIn("error:", done.stderr)
        self.assertNotIn("Traceback", done.stderr)
