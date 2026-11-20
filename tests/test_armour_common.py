"""Tests for scripts/armour_common.py."""

from __future__ import annotations

import email.message
import hashlib
import io
import json
import os
import time
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import TestCase, mock

from support import TempDirTestCase

import armour_common
from armour_common import ArmourError

JAR = b"pretend this is a server jar"
JAR_SHA1 = hashlib.sha1(JAR, usedforsecurity=False).hexdigest()


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://example.test/x", code, "error", email.message.Message(), None
    )


class EscapeTests(TestCase):
    def test_escape_data(self) -> None:
        self.assertEqual(armour_common.escape_data("50%\r\nnext"), "50%25%0D%0Anext")

    def test_escape_property_also_escapes_colons_and_commas(self) -> None:
        self.assertEqual(armour_common.escape_property("a:b,c"), "a%3Ab%2Cc")

    def test_annotate(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            armour_common.annotate("error", "Title: x", "line1\nline2")
        self.assertEqual(out.getvalue(), "::error title=Title%3A x::line1%0Aline2\n")


class GitHubFileTests(TempDirTestCase):
    def test_append_summary_adds_a_blank_line_between_entries(self) -> None:
        summary = self.tmp / "summary.md"
        with mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}):
            armour_common.append_summary("one\n")
            armour_common.append_summary("two")
        self.assertEqual(summary.read_text(encoding="utf-8"), "one\n\ntwo\n\n")

    def test_append_summary_is_silent_outside_actions(self) -> None:
        with mock.patch.dict(os.environ):
            os.environ.pop("GITHUB_STEP_SUMMARY", None)
            armour_common.append_summary("ignored")  # must not raise

    def test_set_output_writes_to_github_output(self) -> None:
        output = self.tmp / "output"
        with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}):
            armour_common.set_output("count", "3")
        self.assertEqual(output.read_text(encoding="utf-8"), "count=3\n")

    def test_set_output_prints_outside_actions(self) -> None:
        out = io.StringIO()
        with mock.patch.dict(os.environ), redirect_stdout(out):
            os.environ.pop("GITHUB_OUTPUT", None)
            armour_common.set_output("count", "3")
        self.assertEqual(out.getvalue(), "count=3\n")

    def test_set_output_rejects_multiple_lines(self) -> None:
        with self.assertRaises(ValueError):
            armour_common.set_output("x", "a\nb")


class RunTests(TestCase):
    def test_returns_the_exit_code(self) -> None:
        self.assertEqual(armour_common.run(lambda: 0), 0)
        self.assertEqual(armour_common.run(lambda: 3), 3)

    def test_reports_armour_errors_on_stderr(self) -> None:
        def fail() -> int:
            raise ArmourError("boom")

        err = io.StringIO()
        with mock.patch.dict(os.environ), redirect_stderr(err):
            os.environ.pop("GITHUB_ACTIONS", None)
            self.assertEqual(armour_common.run(fail), 1)
        self.assertEqual(err.getvalue(), "error: boom\n")

    def test_reports_armour_errors_as_annotations_in_actions(self) -> None:
        def fail() -> int:
            raise ArmourError("boom")

        out = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}),
            redirect_stdout(out),
        ):
            self.assertEqual(armour_common.run(fail), 1)
        self.assertEqual(out.getvalue(), "::error title=ARMOUR::boom\n")


def fake_open(payload: bytes) -> mock.Mock:
    """Make `_open_url` return a fresh response with `payload` each time."""
    return mock.Mock(side_effect=lambda *_, **__: io.BytesIO(payload))


class FetchJsonTests(TestCase):
    def test_returns_the_object(self) -> None:
        payload = json.dumps({"a": 1}).encode()
        with mock.patch.object(armour_common, "_open_url", fake_open(payload)):
            self.assertEqual(
                armour_common.fetch_json_object("https://x.test"), {"a": 1}
            )

    def test_rejects_non_https_urls_without_retrying(self) -> None:
        for url in ("http://x.test/a", "file:///etc/passwd"):
            with self.subTest(url), self.assertRaisesRegex(ArmourError, "non-HTTPS"):
                armour_common.fetch_json_object(url)

    def test_retries_then_gives_up_on_bad_json(self) -> None:
        opener = fake_open(b"[1, 2]")
        with (
            mock.patch.object(armour_common, "_open_url", opener),
            mock.patch.object(time, "sleep") as sleep,
            self.assertRaisesRegex(ArmourError, "Failed to fetch"),
        ):
            armour_common.fetch_json_object("https://x.test")
        self.assertEqual(opener.call_count, armour_common.DOWNLOAD_ATTEMPTS)
        # Waits 1, 2, 4 seconds, but not after the final attempt.
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1, 2, 4])

    def test_does_not_retry_a_404(self) -> None:
        opener = mock.Mock(side_effect=http_error(404))
        with (
            mock.patch.object(armour_common, "_open_url", opener),
            self.assertRaises(ArmourError),
        ):
            armour_common.fetch_json_object("https://x.test")
        self.assertEqual(opener.call_count, 1)

    def test_retries_a_server_error(self) -> None:
        responses = [http_error(503), io.BytesIO(b'{"ok": true}')]
        with (
            mock.patch.object(armour_common, "_open_url", side_effect=responses),
            mock.patch.object(time, "sleep"),
        ):
            self.assertEqual(
                armour_common.fetch_json_object("https://x.test"), {"ok": True}
            )


class DownloadTests(TempDirTestCase):
    def test_rejects_malformed_sha1_before_creating_cache(self) -> None:
        for value in (
            "../" + "a" * 38,
            "a" * 39,
            "g" * 40,
            "",
        ):
            cache = self.tmp / "cache"
            with self.subTest(value=value), self.assertRaisesRegex(
                ArmourError, "40 hexadecimal"
            ):
                armour_common.download_server_jar("https://x.test", value, cache)
            self.assertFalse(cache.exists())

    def test_sha1_of(self) -> None:
        path = self.tmp / "f"
        path.write_bytes(JAR)
        self.assertEqual(armour_common.sha1_of(path), JAR_SHA1)

    def test_downloads_and_verifies(self) -> None:
        with mock.patch.object(armour_common, "_open_url", fake_open(JAR)):
            jar = armour_common.download_server_jar(
                "https://x.test/server.jar", JAR_SHA1.upper(), self.tmp / "cache"
            )
        self.assertEqual(jar, self.tmp / "cache" / f"{JAR_SHA1}.jar")
        self.assertEqual(jar.read_bytes(), JAR)
        self.assertEqual(list((self.tmp / "cache").glob("*.part")), [])

    def test_reuses_a_cached_jar(self) -> None:
        cache = self.tmp / "cache"
        cache.mkdir()
        (cache / f"{JAR_SHA1}.jar").write_bytes(JAR)
        opener = mock.Mock()
        with mock.patch.object(armour_common, "_open_url", opener):
            armour_common.download_server_jar("https://x.test/s.jar", JAR_SHA1, cache)
        opener.assert_not_called()

    def test_replaces_a_corrupt_cached_jar(self) -> None:
        cache = self.tmp / "cache"
        cache.mkdir()
        (cache / f"{JAR_SHA1}.jar").write_bytes(b"corrupt")
        with mock.patch.object(armour_common, "_open_url", fake_open(JAR)):
            jar = armour_common.download_server_jar(
                "https://x.test/s.jar", JAR_SHA1, cache
            )
        self.assertEqual(jar.read_bytes(), JAR)

    def test_checksum_mismatch_fails_and_leaves_nothing_behind(self) -> None:
        cache = self.tmp / "cache"
        with (
            mock.patch.object(armour_common, "_open_url", fake_open(b"wrong")),
            mock.patch.object(time, "sleep"),
            self.assertRaisesRegex(ArmourError, "SHA-1 mismatch"),
        ):
            armour_common.download_server_jar("https://x.test/s.jar", JAR_SHA1, cache)
        self.assertEqual(list(Path(cache).iterdir()), [])
