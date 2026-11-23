#!/usr/bin/env python3
"""Update the unit-test OS matrix to GitHub's current stable runner labels."""

from __future__ import annotations

import re
import urllib.request
from pathlib import Path

README_URL = (
    "https://raw.githubusercontent.com/actions/runner-images/main/README.md"
)
WORKFLOW_PATH = Path(".github/workflows/self-test.yml")

# These aliases are maintained by GitHub to point at the newest GA image.
# Resolve the aliases to versioned labels so the test matrix stays pinned.
LATEST_ALIASES = {
    ("x64", "ubuntu-latest"): re.compile(r"ubuntu-\d+\.\d+"),
    ("arm64", "macos-latest"): re.compile(r"macos-\d+"),
    ("x64", "windows-latest"): re.compile(r"windows-\d+"),
}


def fetch_runner_readme() -> str:
    request = urllib.request.Request(
        README_URL,
        headers={"User-Agent": "os-runner-label-updater"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8")


def discover_latest_labels(readme: str) -> dict[str, str]:
    available_images = readme.split("## Available Images", 1)[1].split(
        "### Label scheme", 1
    )[0]
    found: dict[str, str] = {}

    for line in available_images.splitlines():
        if not line.startswith("|"):
            continue

        columns = [part.strip() for part in line.strip().strip("|").split("|")]
        if len(columns) < 3:
            continue

        image, architecture, label_column = columns[:3]
        labels = re.findall(r"`([^\`]+)`", label_column)

        for (expected_arch, alias), version_pattern in LATEST_ALIASES.items():
            if architecture.lower() != expected_arch or alias not in labels:
                continue

            versioned_labels = [
                label for label in labels if version_pattern.fullmatch(label)
            ]
            if len(versioned_labels) != 1:
                raise RuntimeError(
                    f"Expected one versioned label for {alias} in: {line}"
                )

            family = alias.split("-", 1)[0]
            found[family] = versioned_labels[0]

    expected_families = {"ubuntu", "macos", "windows"}
    if set(found) != expected_families:
        raise RuntimeError(
            "Could not discover all stable runner labels from GitHub's "
            f"runner-images README. Found: {found}"
        )

    return found


def update_matrix(content: str, labels: dict[str, str]) -> str:
    matrix_pattern = re.compile(
        r"(?m)(^      matrix:\n        os:\n)"
        r"(?:          - [^\n]+\n)+"
    )
    replacement = (
        "      matrix:\n"
        "        os:\n"
        f"          - {labels['ubuntu']}\n"
        f"          - {labels['windows']}\n"
        f"          - {labels['macos']}\n"
    )
    updated, count = matrix_pattern.subn(replacement, content, count=1)
    if count != 1:
        raise RuntimeError(
            "Could not find exactly one unit-test OS matrix in "
            f"{WORKFLOW_PATH}"
        )
    return updated


def main() -> None:
    labels = discover_latest_labels(fetch_runner_readme())
    original = WORKFLOW_PATH.read_text(encoding="utf-8")
    updated = update_matrix(original, labels)

    if updated == original:
        print("Runner labels are already current:", labels)
        return

    WORKFLOW_PATH.write_text(updated, encoding="utf-8")
    print("Updated runner matrix to:", labels)


if __name__ == "__main__":
    main()
