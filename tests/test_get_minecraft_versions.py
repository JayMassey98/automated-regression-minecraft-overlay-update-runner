"""Tests for scripts/get_minecraft_versions.py."""

from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stdout
from typing import Any, ClassVar
from unittest import TestCase, mock

from support import TempDirTestCase, write

import get_minecraft_versions as gmv
from armour_common import ArmourError


def entry(version_id: str, kind: str, released: str) -> dict[str, Any]:
    return {
        "id": version_id,
        "type": kind,
        "releaseTime": released,
        "url": f"https://meta.test/{version_id}.json",
    }


# Listed newest first, as Mojang does. "1.21.10" sorts before "1.21.9" as a string,
# so selecting by ID rather than by time would get these wrong.
MANIFEST: dict[str, Any] = {
    "versions": [
        entry("26.4-snapshot-2", "snapshot", "2026-09-02T10:00:00+00:00"),
        entry("26.4-snapshot-1", "snapshot", "2026-09-01T10:00:00+00:00"),
        entry("26.3", "release", "2026-08-01T10:00:00+00:00"),
        entry("26.3-rc-1", "snapshot", "2026-07-30T10:00:00+00:00"),
        entry("26.1", "release", "2026-03-01T10:00:00+00:00"),
        entry("1.21.11", "release", "2025-12-01T10:00:00+00:00"),
        entry("25w45a", "snapshot", "2025-11-01T10:00:00+00:00"),
        entry("1.21.10", "release", "2025-10-07T10:00:00+00:00"),
        entry("1.21.9", "release", "2025-09-30T10:00:00+00:00"),
        entry("1.21.8", "release", "2025-07-17T10:00:00+00:00"),
        entry("1.21.9-rc1", "snapshot", "2025-09-20T10:00:00+00:00"),
        entry("26.1-fools", "old_alpha", "2026-04-01T10:00:00+00:00"),
        entry("not-a-release", "release", "2026-05-01T10:00:00+00:00"),
    ]
}


def ids(selected: list[dict[str, Any]]) -> list[str]:
    return [item["id"] for item in selected]


class SelectVersionsTests(TestCase):
    def select(self, **overrides: object) -> list[str]:
        options: dict[str, Any] = {
            "release_from": "1.21.9",
            "snapshot_after": "26.3",
            "include_legacy_snapshots": False,
        }
        options.update(overrides)
        return ids(gmv.select_versions(MANIFEST, **options))

    def test_releases_from_the_floor_and_snapshots_after_the_snapshot_floor(
        self,
    ) -> None:
        self.assertEqual(
            self.select(),
            [
                "1.21.9",
                "1.21.10",
                "1.21.11",
                "26.1",
                "26.3",
                "26.4-snapshot-1",
                "26.4-snapshot-2",
            ],
        )

    def test_sorted_by_release_time_not_by_id(self) -> None:
        selected = self.select()
        self.assertLess(selected.index("1.21.9"), selected.index("1.21.10"))

    def test_legacy_snapshots_only_when_asked(self) -> None:
        legacy = self.select(snapshot_after="1.21.8", include_legacy_snapshots=True)
        self.assertIn("25w45a", legacy)
        self.assertIn("1.21.9-rc1", legacy)
        self.assertNotIn("25w45a", self.select(snapshot_after="1.21.8"))

    def test_exclude(self) -> None:
        self.assertNotIn("26.1", self.select(exclude=frozenset({"26.1"})))

    def test_ignores_other_types_and_odd_ids(self) -> None:
        selected = self.select(release_from="1.21.8")
        self.assertNotIn("26.1-fools", selected)
        self.assertNotIn("not-a-release", selected)

    def test_unknown_boundary_versions_are_errors(self) -> None:
        with self.assertRaisesRegex(ArmourError, "--release-from"):
            self.select(release_from="9.9")
        with self.assertRaisesRegex(ArmourError, "--snapshot-after"):
            self.select(snapshot_after="9.9")

    def test_release_time_must_be_valid(self) -> None:
        for bad in ({"id": "x"}, {"id": "x", "releaseTime": "yesterday"}):
            with self.subTest(bad), self.assertRaises(ArmourError):
                gmv.release_time(bad)


class DescribeTests(TestCase):
    DETAIL: ClassVar[dict[str, Any]] = {
        "downloads": {"server": {"url": "https://dl.test/s.jar", "sha1": "abc"}},
        "javaVersion": {"majorVersion": 21},
    }

    def describe(
        self, detail: dict[str, Any], kind: str = "release"
    ) -> gmv.VersionTarget:
        with mock.patch.object(gmv, "fetch_json_object", return_value=detail):
            return gmv.describe(entry("1.21.11", kind, "2025-12-01T10:00:00+00:00"))

    def test_extracts_what_later_stages_need(self) -> None:
        self.assertEqual(
            self.describe(self.DETAIL),
            {
                "id": "1.21.11",
                "kind": "release",
                "java": 21,
                "server_url": "https://dl.test/s.jar",
                "sha1": "abc",
            },
        )

    def test_anything_that_is_not_a_release_is_a_snapshot(self) -> None:
        self.assertEqual(self.describe(self.DETAIL, "snapshot")["kind"], "snapshot")

    def test_missing_server_or_java_is_an_error(self) -> None:
        for missing in ("downloads", "javaVersion"):
            detail = {k: v for k, v in self.DETAIL.items() if k != missing}
            with self.subTest(missing), self.assertRaises(ArmourError):
                self.describe(detail)


class LoadManifestTests(TempDirTestCase):
    def test_reads_a_local_file(self) -> None:
        write(self.tmp / "m.json", json.dumps(MANIFEST))
        self.assertEqual(gmv.load_manifest(str(self.tmp / "m.json")), MANIFEST)

    def test_fetches_an_https_url(self) -> None:
        with mock.patch.object(gmv, "fetch_json_object", return_value=MANIFEST) as f:
            self.assertEqual(gmv.load_manifest(gmv.MANIFEST_URL), MANIFEST)
        f.assert_called_once_with(gmv.MANIFEST_URL)

    def test_bad_locations_and_files(self) -> None:
        write(self.tmp / "bad.json", "{")
        write(self.tmp / "list.json", "[]")
        for location in (
            str(self.tmp / "nope.json"),
            str(self.tmp / "bad.json"),
            str(self.tmp / "list.json"),
        ):
            with self.subTest(location), self.assertRaises(ArmourError):
                gmv.load_manifest(location)


class SummaryTests(TestCase):
    def test_summary_markdown(self) -> None:
        text = gmv.summary_markdown(
            [
                {
                    "id": "1.21.11",
                    "kind": "release",
                    "java": 21,
                    "server_url": "u",
                    "sha1": "s",
                },
            ]
        )
        self.assertIn("### Target Minecraft versions (1)", text)
        self.assertIn("| `1.21.11` | release | 21 |", text)


class MainTests(TempDirTestCase):
    def run_main(self, *args: str) -> dict[str, str]:
        """Run main with a local manifest; return the step outputs it wrote."""
        write(self.tmp / "m.json", json.dumps(MANIFEST))
        output = self.tmp / "output"
        detail = {
            "downloads": {"server": {"url": "https://dl.test/s.jar", "sha1": "abc"}},
            "javaVersion": {"majorVersion": 25},
        }
        env = {
            "GITHUB_OUTPUT": str(output),
            "GITHUB_STEP_SUMMARY": str(self.tmp / "s.md"),
        }
        with (
            mock.patch.dict(os.environ, env),
            mock.patch.object(gmv, "fetch_json_object", return_value=detail),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                gmv.main(["--manifest", str(self.tmp / "m.json"), *args]), 0
            )
        return dict(
            line.split("=", 1)
            for line in output.read_text(encoding="utf-8").splitlines()
        )

    def test_writes_versions_matrix_and_count(self) -> None:
        outputs = self.run_main("--release-from", "26.1", "--snapshot-after", "26.3")
        self.assertEqual(
            json.loads(outputs["versions"]),
            ["26.1", "26.3", "26.4-snapshot-1", "26.4-snapshot-2"],
        )
        matrix = json.loads(outputs["matrix"])["include"]
        self.assertEqual(len(matrix), 4)
        self.assertEqual(matrix[0]["java"], 25)
        self.assertEqual(outputs["count"], "4")
        self.assertNotIn(" ", outputs["versions"])  # compact JSON
        self.assertIn("Target Minecraft versions (4)", (self.tmp / "s.md").read_text())

    def test_exclude_option(self) -> None:
        outputs = self.run_main("--exclude", "26.1", "26.4-snapshot-2")
        self.assertNotIn("26.1", json.loads(outputs["versions"]))
        self.assertNotIn("26.4-snapshot-2", json.loads(outputs["versions"]))

    def test_refuses_an_empty_matrix(self) -> None:
        with self.assertRaisesRegex(ArmourError, "empty matrix"):
            self.run_main(
                "--release-from",
                "26.3",
                "--exclude",
                "26.3",
                "26.4-snapshot-1",
                "26.4-snapshot-2",
            )

    def test_refuses_a_matrix_over_github_limit(self) -> None:
        with (
            mock.patch.object(gmv, "MAX_MATRIX_JOBS", 2),
            self.assertRaisesRegex(ArmourError, "matrix limit"),
        ):
            self.run_main()
