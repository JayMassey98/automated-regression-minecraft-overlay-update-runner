# ARMOUR

**A**utomated **R**egression **M**inecraft **O**verlay **U**pdate **R**unner.

Shared CI tooling for Minecraft data packs, which can pull ARMOUR in as a Git
submodule to allow its GitHub workflow to call its scripts locally.

ARMOUR is designed to run without needing any Minecraft clients installed, and
all Minecraft servers are run in parallel to further speed up testing.

## Getting Started

1. **Add ARMOUR as a submodule** (`armour` is a common shorthand):

   ```bash
   REPO="https://github.com/JayMassey98"
   git submodule add -b <branch> \
     "\$REPO/automated-regression-minecraft-overlay-update-runner.git" \
     .armour
   ```

2. **Add `armour.json` to the repo root** (`tests_namespace` defaults to
   `<pack_name>_test`):

   ```json
   {
     "pack_name": "elemental_tools",
     "tests_namespace": "elemental_tools_test"
   }
   ```

3. **Call the scripts in your workflow** after checking out with submodules:

   ```yaml
   - uses: actions/checkout@v7
     with:
       submodules: true
   - run: >-
       python3 .armour/scripts/get_minecraft_versions.py
       --release-from 1.21.9 ...
   ```

   Which versions to target, the tests folder, and other options are command
   line arguments, so every repo decides for itself. See
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

## Current Limitations

- **Test harness is not yet bundled in ARMOUR**: The in-game test runner and
  assertion functions (`harness/*.mcfunction` and `assert/`) still have to
  live inside each consuming pack's `tests/` folder under `<tests_namespace>`.
  `run_server_tests.py` parses the `ET_*` log lines emitted by that harness,
  so moving the shared harness files into ARMOUR is planned next.
- **Data pack must live at the repository root**: `build_version_pack.py` and
  `run_server_tests.py` expect `pack.mcmeta`, `data/`, and any version
  overlay folders to sit directly at the top level of the consuming repo,
  rather than inside a subdirectory.

## Running Unit Tests

```bash
python3 -m unittest discover -s tests -v
```

The Spyglass runner has its own tests (run `npm ci` in `scripts/spyglass`
first):

```bash
npm test --prefix scripts/spyglass
```
