# Runner OS label updates

The `Update runner OS labels` workflow checks GitHub's official
`actions/runner-images` README monthly and opens or refreshes a pull request
against `automated_testing` when the stable versioned labels change.

## Required repository secret

Add a fine-grained personal access token as the repository Actions secret
`RUNNER_UPDATE_TOKEN`.

Limit the token to this repository and grant:
- Contents: Read and write
- Pull requests: Read and write

The token is used to push the automation branch and open/update its pull
request. Using a separate token (rather than the workflow's `GITHUB_TOKEN`)
allows the resulting pull request to trigger the repository's normal
`pull_request` checks.

## Running it

The workflow can be started manually from the Actions tab using
`workflow_dispatch`. Select the branch containing the workflow.

GitHub scheduled workflows run only from the repository's default branch.
Therefore, the monthly schedule will become active only after this workflow
has been merged into the default branch. The pull request it creates targets
`automated_testing`.

The updater only edits the OS matrix under `unit_tests` in
`.github/workflows/self-test.yml`; it does not change other jobs or runner
labels elsewhere in the repository.
