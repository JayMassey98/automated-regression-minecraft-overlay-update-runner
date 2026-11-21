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
import tempfile
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
_SHA1_LENGTH = 40

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


class _HTTPSRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Allow redirects only to HTTPS destinations."""

    def redirect_request(  # noqa: ANN202, PLR0913, PLR0917
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> Any:
        if urllib.parse.urlsplit(newurl).scheme.lower() != "https":
            raise ArmourError(f"Refusing redirect to a non-HTTPS URL: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_url(url: str, timeout: float) -> Any:  # noqa: ANN401 - urllib response type
    """Open HTTPS URLs and enforce HTTPS across redirects."""
    if urllib.parse.urlsplit(url).scheme.lower() != "https":
        raise ArmourError(f"Refusing to download from a non-HTTPS URL: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})  # noqa: S310
    opener = urllib.request.build_opener(_HTTPSRedirectHandler)
    return opener.open(request, timeout=timeout)

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
    if len(expected) != _SHA1_LENGTH or any(
        char not in "0123456789abcdef" for char in expected
    ):
        raise ArmourError(
            f"Server JAR SHA-1 must be exactly {_SHA1_LENGTH} hexadecimal characters"
        )
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / f"{expected}.jar"
    if target.is_symlink():
        raise ArmourError(f"Cached server JAR must not be a symbolic link: {target}")
    if target.is_file() and sha1_of(target) == expected:
        print(f"Using cached server jar {target.name}")
        return target

    def attempt() -> Path:
        partial: Path | None = None
        try:
            # A unique exclusive temporary file prevents a pre-created .part symlink
            # from redirecting writes, and keeps retries isolated from one another.
            fd, name = tempfile.mkstemp(
                prefix=f".{expected}.", suffix=".part", dir=cache_dir
            )
            partial = Path(name)
            with (
                os.fdopen(fd, "wb") as handle,
                _open_url(url, JAR_TIMEOUT_SECONDS) as response,
            ):
                shutil.copyfileobj(response, handle)
            if sha1_of(partial) != expected:
                msg = f"SHA-1 mismatch for {url}"
                raise ChecksumError(msg)
            partial.replace(target)
        finally:
            if partial is not None:
                partial.unlink(missing_ok=True)
        return target

    jar = _with_retries(attempt, f"download {url}")
    print(f"Downloaded server jar {jar.name}")
    return jar
