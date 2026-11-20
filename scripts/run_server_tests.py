#!/usr/bin/env python3
"""Run a data pack's tests on a real, headless Minecraft server.

Steps:
  1. Download the version's server.jar (checked against its SHA-1, cached).
  2. Stage the data pack (with its real pack.mcmeta overlays), the tests pack
     and ARMOUR's shared harness pack into a fresh world. Only `max_format` is
     widened, so snapshots whose pack format is newer than the declared one
     still load.
  3. Start the server, run `/function armour:harness/start`, and read the
     ARMOUR_* result lines the tests print to the server log.
  4. Stop the server and report. Fails when a test fails, when any pack file
     logs a load error (broken recipe, tag, function...), or when no result
     arrives.

Exit codes: 0 = all tests passed, 1 = failures or problems.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import queue
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import armour_config
from armour_common import (
    ArmourError,
    annotate,
    append_summary,
    download_server_jar,
    run,
)
from armour_paths import paths_overlap, reject_symlink_components, resolve_pack_root
from build_version_pack import (
    load_mcmeta,
    overlay_entries,
    overlay_range,
    validate_overlay_sources,
)
from generate_test_runner import generate

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from typing import TextIO

# ARMOUR's shared harness data pack (the `harness/` folder next to `scripts/`).
HARNESS_PACK_DIR = Path(__file__).resolve().parent.parent / "harness"

WIDE_MAX_FORMAT = [9999, 0]

JAVA_HEAP = "-Xmx2G"
SERVER_PORT = 25599
SERVER_STOP_TIMEOUT_SECONDS = 90
READER_JOIN_TIMEOUT_SECONDS = 10
# Pauses that let the log catch up: after `datapack list`, and after the result.
DIAGNOSTIC_PAUSE_SECONDS = 3
FLUSH_PAUSE_SECONDS = 2
SERVER_PROPERTIES = f"""\
online-mode=false
enforce-secure-profile=false
spawn-protection=0
spawn-monsters=false
spawn-animals=false
spawn-npcs=false
allow-nether=false
generate-structures=false
view-distance=4
simulation-distance=4
max-players=1
max-tick-time=-1
pause-when-empty-seconds=-1
sync-chunk-writes=false
enable-rcon=false
enable-query=false
server-port={SERVER_PORT}
initial-enabled-packs=vanilla,file/{armour_config.HARNESS_NAMESPACE},file/{{pack_name}},file/{{tests_namespace}}
"""

# A line like "[12:00:00] [Server thread/ERROR]: ..." (we then check it mentions
# one of the packs).
LOG_LEVEL_RE = re.compile(r"/(ERROR|WARN|FATAL)\]")
# The start of a new log record, such as "[12:00:00] [Server thread/INFO]: ...".
LOG_RECORD_RE = re.compile(r"^\[\d\d:\d\d:\d\d\]")
# How many lines after a load error can hold its reason (the command that did
# not parse).
LOAD_ERROR_DETAIL_LINES = 8
MAX_LOAD_WARNINGS_SHOWN = 20
LOG_TAIL_LINES = 60


# =============================================================================
# Staging
# =============================================================================


def widen_max_format(mcmeta_path: Path) -> None:
    """Let a snapshot whose pack format is newer than max_format still load."""
    data = json.loads(mcmeta_path.read_text(encoding="utf-8"))
    if not isinstance(data.get("pack"), dict):
        msg = f'{mcmeta_path} has no "pack" section'
        raise ArmourError(msg)
    data["pack"]["max_format"] = WIDE_MAX_FORMAT
    mcmeta_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _reject_symlinks(root: Path) -> None:
    """Reject source symlinks so staging cannot read outside the source tree."""
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ArmourError(f"Symbolic links are not supported in data packs: {path}")


def stage_data_pack(repo: Path, destination: Path, *, widen: bool) -> None:
    """Copy the shipped pack: pack.mcmeta, data/, and every overlay folder.

    Released versions are tested with pack.mcmeta exactly as shipped. Only
    snapshots (whose pack format can be newer than max_format) get a widened
    max_format.
    """
    entries = overlay_entries(load_mcmeta(repo))
    for entry in entries:
        overlay_range(entry)
    validate_overlay_sources(repo, entries)
    _reject_symlinks(repo)
    destination.mkdir(parents=True)
    shutil.copyfile(repo / "pack.mcmeta", destination / "pack.mcmeta")
    shutil.copytree(repo / "data", destination / "data")
    for entry in entries:
        directory = entry["directory"]
        overlay_root = (repo / directory).resolve()
        if not overlay_root.is_relative_to(repo.resolve()):
            raise ArmourError(f"Overlay directory escapes the pack: {directory!r}")
        if not overlay_root.is_dir():
            msg = (
                f"pack.mcmeta lists overlay '{directory}' "
                f"but {overlay_root} does not exist"
            )
            raise ArmourError(msg)
        shutil.copytree(overlay_root, destination / directory)
    if widen:
        widen_max_format(destination / "pack.mcmeta")


def stage_harness_pack(destination: Path) -> None:
    """Copy ARMOUR's shared harness pack, with `max_format` widened."""
    if not (HARNESS_PACK_DIR / "pack.mcmeta").is_file():
        msg = (
            f"ARMOUR's harness pack is missing from {HARNESS_PACK_DIR}. "
            "If ARMOUR is a submodule, run `git submodule update --init`."
        )
        raise ArmourError(msg)
    shutil.copytree(HARNESS_PACK_DIR, destination)
    widen_max_format(destination / "pack.mcmeta")


def stage_tests_pack(tests: Path, destination: Path, namespace: str) -> int:
    """Copy the tests pack, regenerate its runner and return how many tests it has."""
    function_dir = tests / "data" / namespace / "function"
    if not function_dir.is_dir():
        msg = (
            f"The tests folder has no {function_dir.relative_to(tests)} folder, "
            f"so there are no tests to run (looked in {tests})."
        )
        raise ArmourError(msg)
    _reject_symlinks(tests)
    shutil.copytree(tests, destination)
    count = generate(destination, namespace)  # never run a stale test list
    widen_max_format(destination / "pack.mcmeta")
    return count


def write_server_files(work: Path, pack_name: str, tests_namespace: str) -> None:
    """Accept the EULA and write server.properties for the test server."""
    (work / "eula.txt").write_text("eula=true\n", encoding="utf-8")
    (work / "server.properties").write_text(
        SERVER_PROPERTIES.format(pack_name=pack_name, tests_namespace=tests_namespace),
        encoding="utf-8",
    )


# =============================================================================
# Server Running
# =============================================================================


class Server:
    """A running server with a reader thread, so waiting never blocks forever."""

    def __init__(self, process: subprocess.Popen[str], log_file: TextIO) -> None:
        """Start reading `process`'s output into `log_file` and a line queue."""
        if process.stdin is None or process.stdout is None:
            msg = "the server process needs stdin and stdout pipes"
            raise ValueError(msg)
        self._process = process
        self._stdin = process.stdin
        self._stdout = process.stdout
        self._log_file = log_file
        self._stopped = False
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        for raw in self._stdout:
            line = raw.rstrip("\n")
            self._log_file.write(line + "\n")
            self._log_file.flush()
            self._lines.put(line)
        self._lines.put(None)  # the server closed its output

    def send(self, command: str) -> None:
        """Type a command into the server console (ignored if it already exited)."""
        with contextlib.suppress(OSError):
            self._stdin.write(command + "\n")
            self._stdin.flush()

    def wait_for(
        self, marker: str | None, timeout: float, collected: list[str]
    ) -> str | None:
        """Collect lines until one contains `marker`, and return that line.

        Returns None on timeout or when the server exits. With `marker=None`
        this simply collects output for `timeout` seconds.
        """
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                line = self._lines.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue
            if line is None:
                return None
            collected.append(line)
            if marker is not None and marker in line:
                return line

    def stop(self) -> None:
        """Stop the server (killing it if it ignores `stop`) and finish logging.

        Safe to call more than once.
        """
        if self._stopped:
            return
        self._stopped = True
        self.send("stop")
        try:
            self._process.wait(timeout=SERVER_STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait()
        # Closing flushes anything unsent, which fails once the server has gone.
        with contextlib.suppress(OSError):
            self._stdin.close()
        self._reader.join(timeout=READER_JOIN_TIMEOUT_SECONDS)

    def drain(self) -> list[str]:
        """Return the lines that arrived but have not been collected yet."""
        remaining: list[str] = []
        while True:
            try:
                line = self._lines.get_nowait()
            except queue.Empty:
                return remaining
            if line is not None:
                remaining.append(line)


@contextlib.contextmanager
def launch_server(
    command: Sequence[str], cwd: Path, log_path: Path
) -> Iterator[Server]:
    """Start the server, copy its output to `log_path`, and stop it on exit."""
    with (
        log_path.open("w", encoding="utf-8") as log_file,
        subprocess.Popen(  # noqa: S603 - the command comes from this script's own arguments
            command,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
        ) as process,
    ):
        server = Server(process, log_file)
        try:
            yield server
        finally:
            server.stop()


def run_tests_on_server(
    command: Sequence[str],
    work: Path,
    start_command: str,
    *,
    startup_timeout: float,
    test_timeout: float,
) -> tuple[list[str], str | None]:
    """Run the harness and return (server log lines, a problem description or None)."""
    lines: list[str] = []
    problem = None
    with launch_server(command, work, work / "server-output.log") as server:
        if server.wait_for("Done (", startup_timeout, lines) is None:
            problem = "The server did not finish starting"
        else:
            server.send(start_command)
            if server.wait_for("ARMOUR_RESULT", test_timeout, lines) is None:
                problem = "The tests did not report a result (see the server log below)"
                # Show which data packs the server has enabled, to help diagnose.
                server.send("datapack list")
                server.wait_for(None, DIAGNOSTIC_PAUSE_SECONDS, lines)
            # Give the log a moment to flush anything printed after the result.
            server.wait_for(None, FLUSH_PAUSE_SECONDS, lines)
        # Anything the server prints while stopping can still hold load errors.
        server.stop()
        lines.extend(server.drain())
    return lines, problem


# =============================================================================
# Log Analysis
# =============================================================================


@dataclass
class Analysis:
    """What the server log says about the test run."""

    fails: list[str] = field(default_factory=list)
    counts: str | None = None
    result: str | None = None
    errors: list[str] = field(default_factory=list)
    load_errors: list[str] = field(default_factory=list)
    load_warnings: list[str] = field(default_factory=list)


def _after(line: str, marker: str) -> str:
    return line.split(marker, 1)[1].strip()


def _record_harness_line(found: Analysis, line: str) -> bool:
    """File an `ARMOUR_*` line from the test harness; False if it is not one."""
    if "ARMOUR_FAIL " in line:
        found.fails.append(_after(line, "ARMOUR_FAIL "))
    elif "ARMOUR_SKIP " in line:
        pass  # only for people reading the log; the totals already count them
    elif "ARMOUR_COUNTS " in line:
        found.counts = _after(line, "ARMOUR_COUNTS ")
    elif "ARMOUR_RESULT " in line:
        found.result = _after(line, "ARMOUR_RESULT ")
    elif "ARMOUR_ERROR " in line:
        found.errors.append(_after(line, "ARMOUR_ERROR "))
    else:
        return False
    return True


def _record_pack_problem(found: Analysis, lines: Sequence[str], index: int) -> None:
    """File the log line at `index` if it is an error or warning."""
    line = lines[index]
    level = LOG_LEVEL_RE.search(line)
    if level is None:
        return
    if level.group(1) == "WARN":
        found.load_warnings.append(line)
        return
    # The reason (the exact command that did not parse) is on the lines after this one.
    detail = []
    for following in lines[index + 1 : index + 1 + LOAD_ERROR_DETAIL_LINES]:
        if LOG_RECORD_RE.match(following):
            break
        detail.append(following.strip())
    found.load_errors.append("\n".join([line, *filter(None, detail)]))


def analyse(lines: Sequence[str], pack_names: Sequence[str]) -> Analysis:
    """Sort the log's ARMOUR_* lines and any errors mentioning our packs."""
    found = Analysis()
    for index, line in enumerate(lines):
        if _record_harness_line(found, line):
            continue
        if any(name in line for name in pack_names):
            _record_pack_problem(found, lines, index)
    return found


def report(
    version: str, found: Analysis, problem: str | None, lines: list[str]
) -> bool:
    """Print annotations and the summary; return True when everything passed."""
    for warning in found.load_warnings[:MAX_LOAD_WARNINGS_SHOWN]:
        annotate("warning", f"Pack log warning ({version})", warning)
    for error in found.load_errors:
        annotate("error", f"Pack failed to load ({version})", error)
    for error in found.errors:
        annotate("error", f"Test harness ({version})", error)
    for failure in found.fails:
        annotate("error", f"Test failed ({version})", failure)

    ok = (
        problem is None
        and found.result == "PASSED"
        and not found.fails
        and not found.load_errors
        and not found.errors
    )
    totals = found.counts or "no totals reported"
    status = "🟢 passed" if ok else "🔴 failed"
    append_summary(
        f"{status} `{version}`: {totals}, {len(found.load_errors)} pack load errors"
    )

    if problem:
        annotate("error", f"Server test run ({version})", problem)
        print(f"---- last {LOG_TAIL_LINES} server log lines ----")
        print("\n".join(lines[-LOG_TAIL_LINES:]))
    print(
        f"Minecraft {version}: {totals}; result={found.result}; "
        f"{len(found.fails)} failures; {len(found.load_errors)} pack load errors"
    )
    return ok


# =============================================================================
# Command Line
# =============================================================================


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the command-line arguments."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "--version", required=True, help="Minecraft version id, for messages"
    )
    parser.add_argument(
        "--kind",
        choices=["release", "snapshot"],
        default="release",
        help="snapshots get a widened max_format; releases are tested as shipped",
    )
    parser.add_argument("--server-url", required=True)
    parser.add_argument("--sha1", required=True)
    parser.add_argument(
        "--cache-dir", help="server JAR cache directory (defaults to a sibling)"
    )
    parser.add_argument("--repo", default=".", help="consuming repository root")
    parser.add_argument(
        "--pack-path",
        help=(
            "data pack directory relative to --repo; overrides armour.json "
            "and auto-discovery"
        ),
    )
    parser.add_argument("--tests-dir", default="tests")
    parser.add_argument(
        "--work-dir",
        help="server work directory (defaults to a sibling of --repo)",
    )
    parser.add_argument("--java", default="java")
    parser.add_argument("--startup-timeout", type=float, default=300)
    parser.add_argument("--test-timeout", type=float, default=180)
    parser.add_argument(
        "--server-jar", help="testing only: use this jar instead of downloading"
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:  # noqa: C901, PLR0912, PLR0915
    """Command-line entry point."""
    args = parse_args(argv)

    repo = Path(args.repo).resolve()
    config = armour_config.load(repo)
    pack_path = args.pack_path if args.pack_path is not None else config.pack_path
    pack_root = resolve_pack_root(repo, pack_path)
    work_dir = (
        Path(args.work_dir)
        if args.work_dir is not None
        else Path("..") / f".{repo.name}-server-run"
    )
    work_candidate = work_dir if work_dir.is_absolute() else repo / work_dir
    work_candidate = work_candidate.absolute()
    reject_symlink_components(work_candidate, Path(work_candidate.anchor))
    work = work_candidate.resolve()
    tests_dir = Path(args.tests_dir)
    if not tests_dir.is_absolute():
        tests_dir = repo / tests_dir
    tests_dir = tests_dir.resolve()
    server_jar = Path(args.server_jar) if args.server_jar else None
    if server_jar is not None and not server_jar.is_absolute():
        server_jar = repo / server_jar
    if server_jar is not None:
        server_jar = server_jar.resolve()
    cache_dir = (
        Path(args.cache_dir)
        if args.cache_dir is not None
        else repo.parent / f".{repo.name}-mc-cache"
    )
    if not cache_dir.is_absolute():
        cache_dir = repo / cache_dir
    if server_jar is None:
        cache_dir = cache_dir.absolute()
        reject_symlink_components(cache_dir, Path(cache_dir.anchor))
        cache_dir = cache_dir.resolve()
        cache_sources = (
            repo,
            tests_dir,
            HARNESS_PACK_DIR.resolve(),
            *((server_jar,) if server_jar is not None else ()),
        )
        if any(paths_overlap(cache_dir, source) for source in cache_sources):
            raise ArmourError(
                "Cache directory must not overlap the repository or any source "
                f"input: {cache_dir}"
            )
    else:
        cache_dir = cache_dir.resolve()
    protected = (
        repo,
        pack_root,
        tests_dir,
        HARNESS_PACK_DIR.resolve(),
        cache_dir,
        *((server_jar,) if server_jar is not None else ()),
    )
    for source in protected:
        if paths_overlap(work, source):
            raise ArmourError(
                "Server work directory "
                f"{work} must not overlap source directory {source}"
            )
    if work.exists() and not work.is_dir():
        msg = f"Server work directory exists and is not a directory: {work}"
        raise ArmourError(msg)
    if not tests_dir.is_dir():
        raise ArmourError(f"Tests directory does not exist: {tests_dir}")
    _reject_symlinks(pack_root)
    _reject_symlinks(tests_dir)
    if not (pack_root / "data").is_dir():
        raise ArmourError(f"Data directory does not exist: {pack_root / 'data'}")
    entries = overlay_entries(load_mcmeta(pack_root))
    for entry in entries:
        overlay_range(entry)
    validate_overlay_sources(pack_root, entries)
    tests_function_dir = tests_dir / "data" / config.tests_namespace / "function"
    if not tests_function_dir.is_dir():
        raise ArmourError(
            f"Tests directory has no {tests_function_dir.relative_to(tests_dir)} folder"
        )
    if not (HARNESS_PACK_DIR / "pack.mcmeta").is_file():
        raise ArmourError(
            f"ARMOUR's harness pack is missing from {HARNESS_PACK_DIR}. "
            "If ARMOUR is a submodule, run git submodule update --init."
        )
    if not (tests_dir / "pack.mcmeta").is_file():
        raise ArmourError(
            f"Tests pack metadata is missing: {tests_dir / 'pack.mcmeta'}"
        )
    if server_jar is not None and not server_jar.is_file():
        raise ArmourError(f"Server jar does not exist: {server_jar}")
    if not isinstance(load_mcmeta(HARNESS_PACK_DIR).get("pack"), dict):
        raise ArmourError(
            f"Harness pack metadata has no pack section: {HARNESS_PACK_DIR}"
        )
    if not isinstance(load_mcmeta(tests_dir).get("pack"), dict):
        raise ArmourError(f"Tests pack metadata has no pack section: {tests_dir}")

    jar = (
        server_jar
        if server_jar is not None
        else download_server_jar(args.server_url, args.sha1, cache_dir)
    )
    if work.exists():
        shutil.rmtree(work)
    datapacks = work / "world" / "datapacks"
    datapacks.mkdir(parents=True)
    stage_data_pack(
        pack_root, datapacks / config.pack_name, widen=args.kind == "snapshot"
    )
    stage_harness_pack(datapacks / armour_config.HARNESS_NAMESPACE)
    test_count = stage_tests_pack(
        tests_dir,
        datapacks / config.tests_namespace,
        config.tests_namespace,
    )
    write_server_files(work, config.pack_name, config.tests_namespace)
    print(f"Staged the data pack and {test_count} tests for Minecraft {args.version}")

    java = args.java
    if ("/" in java or "\\" in java) and not Path(java).is_absolute():
        java = str((repo / java).resolve())
    lines, problem = run_tests_on_server(
        [java, JAVA_HEAP, "-jar", str(Path(jar).resolve()), "nogui"],
        work,
        armour_config.START_COMMAND,
        startup_timeout=args.startup_timeout,
        test_timeout=args.test_timeout,
    )
    found = analyse(
        lines,
        (armour_config.HARNESS_NAMESPACE, config.pack_name, config.tests_namespace),
    )
    return 0 if report(args.version, found, problem, lines) else 1


if __name__ == "__main__":
    raise SystemExit(run(main))
