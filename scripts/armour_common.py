"""Helpers shared by ARMOUR's scripts.

* `ArmourError` and `run`: one way for every script to report a failure.
* `fetch_json_object` and `download_server_jar`: HTTPS downloads with retries.
* GitHub Actions helpers: annotations, the job summary and step outputs.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable

# Sent with every download so Mojang's servers can tell this traffic apart.
USER_AGENT = "armour-ci"

DOWNLOAD_ATTEMPTS = 4
JSON_TIMEOUT_SECONDS = 30
JAR_TIMEOUT_SECONDS = 60
_HASH_CHUNK_SIZE = 1 << 20

_T = TypeVar("_T")


class ArmourError(Exception):
    """A problem the user can fix (bad input, failed download, broken pack)."""


class ChecksumError(ArmourError):
    """A downloaded file did not match its expected SHA-1."""


def run(main: Callable[[], int]) -> int:
    """Run a script's `main`, turning an `ArmourError` into a clean failure."""
    try:
        return main()
    except ArmourError as error:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            annotate("error", "ARMOUR", str(error))
        else:
            print(f"error: {error}", file=sys.stderr)
        return 1


# =============================================================================
# GitHub Actions
# =============================================================================


def escape_data(text: str) -> str:
    """Escape a workflow command message (the part after `::`)."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def escape_property(text: str) -> str:
    """Escape a workflow command property such as a title."""
    return escape_data(text).replace(":", "%3A").replace(",", "%2C")


def annotate(level: Literal["error", "warning"], title: str, message: str) -> None:
    """Print a workflow command that GitHub shows as an annotation."""
    print(f"::{level} title={escape_property(title)}::{escape_data(message)}")


def append_summary(markdown: str) -> None:
    """Add Markdown to the job summary; does nothing outside GitHub Actions."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with Path(path).open("a", encoding="utf-8") as handle:
            handle.write(markdown.rstrip("\n") + "\n\n")


def set_output(name: str, value: str) -> None:
    """Set a step output, or print it when run outside GitHub Actions."""
    if "\n" in value:
        msg = f"Output {name!r} must be a single line"
        raise ValueError(msg)
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with Path(path).open("a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")
    else:
        print(f"{name}={value}")


# =============================================================================
# Downloads
# =============================================================================


def _open_url(url: str, timeout: float) -> Any:  # noqa: ANN401 - urllib's response type
    """Open an HTTPS URL (nothing else: no `file:` or plain `http:`)."""
    if urllib.parse.urlsplit(url).scheme != "https":
        msg = f"Refusing to download from a non-HTTPS URL: {url}"
        raise ArmourError(msg)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})  # noqa: S310
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310


def _is_retryable(error: Exception) -> bool:
    """Retry network trouble and server errors, but not a 404 or similar."""
    if isinstance(error, urllib.error.HTTPError):
        return error.code >= 500 or error.code == 429  # noqa: PLR2004
    return True


def _with_retries(action: Callable[[], _T], description: str) -> _T:
    """Call `action`, retrying with exponential back-off (1 s, 2 s, 4 s)."""
    last_error: Exception | None = None
    for attempt in range(DOWNLOAD_ATTEMPTS):
        try:
            return action()
        except (OSError, ValueError, ChecksumError) as error:
            if not _is_retryable(error):
                msg = f"Failed to {description}: {error}"
                raise ArmourError(msg) from error
            last_error = error
            if attempt + 1 < DOWNLOAD_ATTEMPTS:
                time.sleep(2**attempt)
    msg = f"Failed to {description}: {last_error}"
    raise ArmourError(msg) from last_error


def fetch_json_object(url: str) -> dict[str, Any]:
    """Download a JSON document whose top level is an object."""

    def attempt() -> dict[str, Any]:
        with _open_url(url, JSON_TIMEOUT_SECONDS) as response:
            data = json.load(response)
        if not isinstance(data, dict):
            msg = "expected a JSON object"
            raise ValueError(msg)  # noqa: TRY004 - retried like any bad response
        return data

    return _with_retries(attempt, f"fetch {url}")


def sha1_of(path: Path) -> str:
    """Return the hex SHA-1 of a file (Mojang publishes SHA-1 checksums)."""
    with path.open("rb") as handle:
        digest = hashlib.file_digest(
            handle,
            lambda: hashlib.sha1(usedforsecurity=False),
            _bufsize=_HASH_CHUNK_SIZE,
        )
    return digest.hexdigest()


def download_server_jar(url: str, sha1: str, cache_dir: Path) -> Path:
    """Download a server jar to `cache_dir/<sha1>.jar`, verifying its SHA-1.

    A jar that is already cached and still matches is reused.
    """
    expected = sha1.lower()
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / f"{expected}.jar"
    if target.exists() and sha1_of(target) == expected:
        print(f"Using cached server jar {target.name}")
        return target

    partial = target.with_suffix(".part")

    def attempt() -> Path:
        try:
            with (
                _open_url(url, JAR_TIMEOUT_SECONDS) as response,
                partial.open("wb") as handle,
            ):
                shutil.copyfileobj(response, handle)
            if sha1_of(partial) != expected:
                msg = f"SHA-1 mismatch for {url}"
                raise ChecksumError(msg)
            partial.replace(target)
        finally:
            partial.unlink(missing_ok=True)
        return target

    jar = _with_retries(attempt, f"download {url}")
    print(f"Downloaded server jar {jar.name}")
    return jar
