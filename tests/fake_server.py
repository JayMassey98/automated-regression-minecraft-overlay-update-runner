"""A stand-in for a Minecraft server, so the tests can run without one.

Usage: fake_server.py MODE
  passes       starts, and reports a passing run when told to start the tests
  fails        starts, and reports a failing test
  load_error   like `passes`, but also logs a load error for the "demo" pack
  startup_error logs a load error for the "demo" pack while starting up, and then
               behaves like `passes` (what a broken data pack looks like with no tests)
  silent       starts, but never reports a result
  never_starts never prints its "Done" line
Every mode exits when it receives `stop`.
"""

from __future__ import annotations

import sys


def log(text: str, level: str = "INFO") -> None:
    """Print one line the way the real server's log looks."""
    print(f"[12:00:00] [Server thread/{level}]: {text}", flush=True)


def main(mode: str) -> None:
    """Behave like a server in the given mode until `stop` arrives."""
    if mode == "startup_error":
        log("Couldn't parse demo:thing", "ERROR")
        print("    Unknown command near 'foo'", flush=True)
    if mode != "never_starts":
        log('Done (0.1s)! For help, type "help"')
    for raw in sys.stdin:
        command = raw.strip()
        if command == "stop":
            log("Stopping the server")
            return
        if command == "datapack list":
            log("There are 3 data packs enabled: [vanilla], [file/demo]")
        elif command.startswith("function ") and mode != "silent":
            if mode == "load_error":
                log("Couldn't parse demo:thing", "ERROR")
                print("    Unknown command near 'foo'", flush=True)
            if mode == "fails":
                log("[Server] ARMOUR_FAIL generic/b - expected 1, got 2")
                log("[Server] ARMOUR_COUNTS 1 failed of 2")
                log("[Server] ARMOUR_RESULT FAILED")
            else:
                log("[Server] ARMOUR_COUNTS 2 passed of 2")
                log("[Server] ARMOUR_RESULT PASSED")


if __name__ == "__main__":
    main(sys.argv[1])
