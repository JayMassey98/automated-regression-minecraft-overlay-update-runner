"""Shared helpers for ARMOUR's tests.

Importing this module first puts `scripts/` on the import path, so each test
module can then import the scripts by their bare names.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
TESTS = ROOT / "tests"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

# Imported only after `scripts/` is on the path.
import run_server_tests  # noqa: E402


def write(path: Path, text: str = "") -> None:
    """Write a text file, creating its folders."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_tests_pack(root: Path, namespace: str) -> Path:
    """Create a minimal tests pack with two tests and the required harness files."""
    pack = root / "tests"
    write(
        pack / "pack.mcmeta",
        json.dumps(
            {"pack": {"description": "t", "min_format": 88, "max_format": [121, 0]}}
        ),
    )
    function = pack / "data" / namespace / "function"
    for name in run_server_tests.REQUIRED_TEST_FILES:
        write(function / name, "# harness\n")
    write(function / "windspanner" / "a.mcfunction")
    write(function / "generic" / "b.mcfunction")
    return pack


class TempDirTestCase(unittest.TestCase):
    """A test case whose `self.tmp` is a fresh temporary folder."""

    tmp: Path

    def setUp(self) -> None:
        """Create the temporary folder and isolate tests from GitHub CI env vars."""
        super().setUp()
        clean_env = {k: v for k, v in os.environ.items() if not k.startswith("GITHUB_")}
        self.enterContext(mock.patch.dict(os.environ, clean_env, clear=True))
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
