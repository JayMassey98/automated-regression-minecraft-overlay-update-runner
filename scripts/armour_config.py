"""Per-repository settings for ARMOUR.

Each data pack that uses ARMOUR keeps an `armour.json` in its repository root:

    {
      "pack_name": "elemental_tools",
      "tests_namespace": "elemental_tools_test"
    }

pack_name           folder/namespace the shipped pack is staged under on the
                    test server
tests_namespace     namespace of the tests pack (the folder under tests/data/).
                    Defaults to "<pack_name>_test".

Both are limited to lower-case letters, digits and `_ . -`, which is what a
namespace allows and keeps them safe to use in paths and `server.properties`.

Everything else (which Minecraft versions to target, where the tests live) is
passed on the command line by the repo's own workflow, so repos can differ
without changing ARMOUR.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from armour_common import ArmourError

if TYPE_CHECKING:
    from pathlib import Path

CONFIG_FILE = "armour.json"

_NAME_RE = re.compile(r"[a-z0-9_.-]+")


@dataclass(frozen=True, slots=True)
class Config:
    """The settings read from a repository's `armour.json`."""

    pack_name: str
    tests_namespace: str

    @property
    def start_command(self) -> str:
        """The in-game command that starts the test harness."""
        return f"function {self.tests_namespace}:harness/start"


def _name(data: dict[str, object], key: str, path: Path) -> str | None:
    """Return a validated name from the config, or None when it is absent."""
    value = data.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not _NAME_RE.fullmatch(value):
        msg = (
            f'{path}: "{key}" must use only a-z, 0-9, "_", "." and "-" (got {value!r})'
        )
        raise ArmourError(msg)
    return value


def load(repo: Path) -> Config:
    """Read and validate `armour.json` from the root of `repo`."""
    path = repo / CONFIG_FILE
    if not path.is_file():
        msg = (
            f"{CONFIG_FILE} is missing from {repo}. ARMOUR needs it to know the "
            "pack's name; see armour_config.py for the format, "
            'e.g. {"pack_name": "my_pack"}.'
        )
        raise ArmourError(msg)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        msg = f"{path} is not valid JSON: {error}"
        raise ArmourError(msg) from error
    if not isinstance(data, dict):
        msg = f"{path} must contain a JSON object"
        raise ArmourError(msg)

    pack_name = _name(data, "pack_name", path)
    if pack_name is None:
        msg = f'{path} must contain a "pack_name" string'
        raise ArmourError(msg)
    tests_namespace = _name(data, "tests_namespace", path) or f"{pack_name}_test"
    return Config(pack_name=pack_name, tests_namespace=tests_namespace)
