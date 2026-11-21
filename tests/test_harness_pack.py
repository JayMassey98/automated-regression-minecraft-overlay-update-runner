"""Static checks of the shared harness pack in `harness/`.

The harness only runs inside Minecraft, so these catch the mistakes that would
otherwise only show up in a slow server run: a call to a function that does not
exist, or a reference to one specific pack.
"""

from __future__ import annotations

import json
import re
import unittest

from support import ROOT

HARNESS = ROOT / "harness"
FUNCTIONS = HARNESS / "data/armour/function"
# `function ns:path` and `function #ns:tag`, with or without a leading `$` macro.
CALL_RE = re.compile(r"\bfunction (#?)([a-z0-9_.-]+):([a-z0-9_./-]+)")
# The only thing the harness takes from a tests pack: its list of runners.
EXTERNAL_TAGS = {"armour:run_tests"}


def harness_functions() -> dict[str, str]:
    """Map each function id in the pack (like `armour:harness/fail`) to its text."""
    return {
        f"armour:{path.relative_to(FUNCTIONS).with_suffix('').as_posix()}": (
            path.read_text(encoding="utf-8")
        )
        for path in sorted(FUNCTIONS.rglob("*.mcfunction"))
    }


class HarnessPackTests(unittest.TestCase):
    def test_pack_mcmeta_is_valid(self) -> None:
        mcmeta = json.loads((HARNESS / "pack.mcmeta").read_text(encoding="utf-8"))
        self.assertIn("min_format", mcmeta["pack"])
        self.assertIn("max_format", mcmeta["pack"])

    def test_the_entry_points_exist(self) -> None:
        functions = harness_functions()
        for name in (
            "armour:harness/start",
            "armour:harness/fail",
            "armour:harness/todo",
            "armour:harness/begin_test",
            "armour:harness/end_test",
            "armour:assert/block",
        ):
            self.assertIn(name, functions)

    def test_every_function_call_resolves(self) -> None:
        functions = harness_functions()
        calls = 0
        for name, text in functions.items():
            for line in text.splitlines():
                for is_tag, namespace, path in CALL_RE.findall(line):
                    calls += 1
                    target = f"{namespace}:{path}"
                    if is_tag:
                        self.assertIn(target, EXTERNAL_TAGS, f"{name}: #{target}")
                    else:
                        self.assertIn(target, functions, f"{name} calls {target}")
        self.assertGreater(calls, 0)

    def test_terminal_paths_release_the_forced_arena_chunk(self) -> None:
        functions = harness_functions()
        self.assertIn("forceload add 0 0 15 15", functions["armour:harness/start"])
        self.assertIn(
            "function armour:harness/cleanup", functions["armour:harness/finish"]
        )
        self.assertIn(
            "function armour:harness/cleanup", functions["armour:harness/timeout"]
        )
        self.assertIn("forceload remove 0 0 15 15", functions["armour:harness/cleanup"])

    def test_nothing_refers_to_a_specific_pack(self) -> None:
        for name, text in harness_functions().items():
            self.assertNotIn("elemental", text, name)

    def test_every_function_documents_itself(self) -> None:
        for name, text in harness_functions().items():
            self.assertTrue(text.startswith("##"), f"{name} has no ## header")
