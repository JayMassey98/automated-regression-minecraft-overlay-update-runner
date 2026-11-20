# ARMOUR

Automated Regression Minecraft Overlay Update Runner (ARMOUR) is a CI tooling
suite used to aid in testing Minecraft data packs.

Data packs add ARMOUR as a Git submodule so their GitHub workflows can run its
scripts locally.

It operates completely headlessly without requiring a Minecraft client, running
server tests across target versions in parallel to speed up verification.

## Getting Started

1. **Add ARMOUR as a submodule** (`armour` is a common shorthand):

   ```bash
   REPO="https://github.com/JayMassey98"
   git submodule add -b <branch> \
     "\$REPO/automated-regression-minecraft-overlay-update-runner.git" \
     .armour
   ```

2. **Add `armour.json` to the repo root** (`tests_namespace` defaults to
   `<pack_name>_test`; `pack_path` is optional):

   ```json
   {
     "pack_name": "elemental_tools",
     "tests_namespace": "elemental_tools_test",
   "pack_path": "packs/elemental_tools"
   }
   ```

3. **Write your tests** in `tests/data/<tests_namespace>/function/`, one
   `.mcfunction` file per test, with a `tests/pack.mcmeta` (see
   [Writing Tests](#writing-tests)). Then generate the test list:

   ```bash
   python3 .armour/scripts/generate_test_runner.py tests
   ```

4. **Call the scripts in your workflow** after checking out with submodules:

   ```yaml
   - uses: actions/checkout@v7
     with:
       submodules: true
   - run: >-
       python3 .armour/scripts/get_minecraft_versions.py
       --release-from 1.21.9 ...
   ```

   Which versions to target, the tests folder, and other options are command
   line arguments, so every repo decides for itself. `--repo` always identifies
   the consuming repository root. ARMOUR discovers a single data pack anywhere
   below that root; set `pack_path` in `armour.json` or pass `--pack-path` when
   more than one pack exists. Paths in `armour.json` and relative paths for the
   build and server-test scripts are interpreted from the repository root, not
   the current working directory. The version-discovery script's local
   `--manifest` path is relative to the current working directory; URLs are
   unaffected. The server runner's default work directory is a hidden sibling
   of the repository; any explicit `--work-dir` must also be outside the
   repository and staged inputs.
   See
   `elemental-tools/.github/workflows/ci.yml` for a complete example.

## Updating ARMOUR

```bash
git submodule update --remote .armour
git add .armour && git commit -m "Update ARMOUR"
```

Each repo is pinned to an exact ARMOUR commit, so a change here never breaks a
pack until that pack chooses to update.

## Repository Structure

- **`.github/workflows/`**: CI self-test workflow for linting and unit tests.
- **`harness/`**: The shared in-game test harness, as a data pack with the
  namespace `armour`. `run_server_tests.py` installs it next to the pack and
  its tests.
- **`scripts/`**:
  - **`spyglass/`**: Headless Spyglass lint runner (`lint.mjs`) and its pinned
    npm dependencies.
  - **`armour_common.py`**: Shared helpers: error handling, HTTPS downloads
    with retries, and GitHub Actions annotations and summaries.
  - **`armour_config.py`**: Reads the consuming repo's `armour.json`.
  - **`build_version_pack.py`**: Flattens a pack and the `pack.mcmeta`
    overlays that apply to one Minecraft version (Spyglass ignores overlays).
  - **`generate_test_runner.py`**: Generates `harness/run_tests.mcfunction`
    from the test files, so adding a test is just adding a file.
  - **`get_minecraft_versions.py`**: Reads Mojang's version manifest and emits
    the version matrix (releases and snapshots to target).
  - **`run_server_tests.py`**: Stages the pack and tests into a world, runs
    them on a real headless server, and reads the results from the log.
- **`tests/`**: Offline `unittest` suite and fake server helpers for ARMOUR's
  Python scripts.

## Writing Tests

Tests are plain data pack functions, so they need no mods and run on every
Minecraft version. Each test is one file in the tests pack, positioned at the
test block (`~ ~ ~`) in a stone arena that the harness builds and resets:

```mcfunction
setblock ~ ~ ~ minecraft:oak_log[axis=x]
function my_pack_test:harness/use_tool {tool:"windspanner"}
function armour:assert/block {block:"minecraft:oak_log[axis=y]"}
```

ARMOUR's harness provides these functions:

- **`armour:assert/block {block:"..."}`**: fails the test unless the block (and
  any states listed) matches.
- **`armour:harness/fail {message:"..."}`**: fails the test with a message
  (no double quotes or `@` in messages).
- **`armour:harness/todo`**: marks the test as not written yet. It is
  reported as skipped, never as passed.
- **`armour:harness/spawn_holder`**: summons the armor stand (tag
  `armour.holder`) that stands in for the player.

Anything that is specific to one pack (such as `use_tool` above, which calls
that pack's own code) belongs in that pack's tests, in its own namespace.
Files in the tests pack's `harness/` and `assert/` folders are treated as
helpers rather than tests.

### How a run works

`generate_test_runner.py` writes `harness/run_tests.mcfunction` (the list of
every test) and a function tag, `data/armour/tags/function/run_tests.json`,
that points the shared harness at it. Commit both, and run the generator again
whenever a test file is added or removed (CI makes its own fresh copy, so
a stale list is never run).

`armour:harness/start` then runs every test, and prints these lines to the
server log, which `run_server_tests.py` reads:

- **`ARMOUR_FAIL <test> - <message>`**: one for each failure.
- **`ARMOUR_SKIP <test>`**: one for each test marked with `harness/todo`.
- **`ARMOUR_COUNTS passed=.. failed=.. skipped=..`**: the totals.
- **`ARMOUR_RESULT PASSED`** or **`ARMOUR_RESULT FAILED`**: the overall result.

### Running tests in your own world

1. Put these three folders in the world's `datapacks` folder: the pack
   itself, its tests pack, and ARMOUR's `harness` folder renamed to `armour`.
2. Run `/reload`, then `/function armour:harness/start`.

The harness builds its own platform at `0 150 0`, so any world works.

## Data Pack Location

The data pack may live in any subdirectory of the consuming repository. ARMOUR
searches recursively for a directory containing a valid `pack.mcmeta` and
`data/`. If exactly one pack is found, it is selected automatically. If
multiple packs are found, select one with `"pack_path": "path/to/pack"` in
`armour.json`, or pass `--pack-path path/to/pack` to the build or server-test
script. The selected path is relative to the repository root.

Automatic discovery excludes known tooling and generated directories only at
the repository root; similarly named folders deeper in the project remain
searchable. An explicit or configured path is checked to remain inside the
repository. Build outputs must not overlap the source pack or protected repository
directories; cache directories must not overlap the source pack. Destructive
build and server-work destinations reject symlinked path components. Builds are
prepared in a temporary sibling directory and replace the previous output only
after validation and copying succeed. Source-pack symlinks are rejected so
staging cannot follow paths outside the pack. Neither workflow modifies the
source pack or its overlays.

## Running Unit Tests

```bash
python3 -m unittest discover -s tests -v
```

The Spyglass runner has its own tests (run `npm ci` in `scripts/spyglass`
first):

```bash
npm test --prefix scripts/spyglass
```
