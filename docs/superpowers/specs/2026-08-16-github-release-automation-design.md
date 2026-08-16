# GitHub Release Automation Design

## Purpose

Cinema Friend has no automated application-release path. A GitHub Actions workflow will
validate each release-eligible merge commit on `main`, then publish one GitHub release for
that exact commit.

The release is a distribution marker rather than a package build. It contains generated
release notes and GitHub's standard source archives only.

## Goals

- Publish releases automatically for runtime and installer changes merged to `main`.
- Validate the exact merge commit before granting release write access.
- Use deterministic CalVer tags tied to the workflow run.
- Make reruns safe without hiding tag or API inconsistencies.
- Keep permissions minimal and isolate `contents: write` to the release job.
- Use only official GitHub actions and the runner's authenticated `gh` CLI.

## Non-goals

- Publishing releases for documentation, test, or workflow-only changes.
- Building wheels, source distributions, binaries, or installer artifacts.
- Publishing to PyPI or another package registry.
- Deriving versions from `pyproject.toml`.
- Backfilling releases for existing commits.
- Refactoring application or installer code.

## Trigger and Concurrency

The workflow lives at `.github/workflows/release.yml` and runs on pushes to `main` only
when at least one changed path matches:

- `src/**`
- `scripts/**`
- `install.sh`
- `pyproject.toml`

This path filter deliberately excludes docs, tests, and workflow files. A commit touching
both an eligible path and an excluded path remains release-eligible.

The workflow uses the fixed concurrency group `release-main` with
`cancel-in-progress: false` and `queue: max`. Runs therefore serialize without replacing
an older pending run when another eligible push arrives. GitHub processes up to 100 pending
runs in first-in-first-out order based on when they enter the concurrency queue; that
platform limit is the only queue bound.

## Jobs and Permissions

The workflow has two jobs:

1. `validate` checks out the pushed commit, configures Python 3.12, installs `.[dev]`, and
   runs the repository's full pytest, Ruff, and mypy commands.
2. `release` depends on `validate`, receives `contents: write`, checks out the same commit,
   calculates the tag, verifies existing GitHub state, and creates the release when needed.

Workflow-level permissions are `contents: read`. The `release` job receives
`contents: write` plus read-only `actions: read` so it can obtain the workflow run's stable
creation timestamp. The validation job has no write permission, and the release job cannot
start unless validation succeeds.

Both checkout steps explicitly use the push event's commit SHA. Release creation therefore
cannot drift to a newer `main` commit while a serialized run waits.

## Validation

The validation job runs on the GitHub-hosted Ubuntu runner with Python 3.12:

```sh
python -m pip install '.[dev]'
python -m pytest -q
python -m ruff check src tests scripts
python -m mypy src scripts
```

These are the repository's documented full validation commands. Installation, tests,
linting, or type-checking failures remain visible workflow failures and prevent release
permissions from being exercised.

## Tag and Release Creation

The release job reads the workflow run's original `created_at` value through the GitHub API
and derives the tag in UTC using this exact format:

```text
v<YYYY.MM.DD>.<github.run_number>
```

For example, run 42 created on 16 August 2026 produces `v2026.08.16.42`. The run number
makes the tag unique even when multiple eligible commits land on the same UTC date. Both
the run ID and its creation timestamp remain stable across rerun attempts, so a rerun on a
later date still computes the same tag.

The release is a normal, non-draft, non-prerelease GitHub release. The authenticated
runner `gh` CLI creates it with generated notes and `--target` set to the exact pushed
commit SHA. No files are uploaded, leaving only GitHub's standard source archives.

## Idempotency and Failure Handling

Before creating anything, the release job queries both the Git ref and GitHub release for
the computed tag:

- If neither exists, it creates the release targeting the pushed commit.
- If the tag exists and resolves to the pushed commit, and the release also exists for that
  tag and targets the same commit, the job succeeds without creating a duplicate.
- If only the tag or only the release exists, the job fails because repository state is
  incomplete and requires operator inspection.
- If either existing object resolves to another commit, the job fails explicitly.
- If GitHub returns an authorization, API, or unexpected lookup error, the job fails.

Tag resolution follows annotated tags to their commit before comparison. Release target
validation resolves the release's `target_commitish` through the Git commits API, so a
branch name or SHA is compared by commit identity rather than text.

The create operation can race only with external actors because workflow runs are
serialized. If creation fails, the job performs no success-shaped fallback: the `gh` error
remains visible. A later rerun succeeds only when the resulting tag and release are both
present and resolve to the expected commit.

## Alternatives Considered

### Recommended: explicit `gh` CLI state checks

An inline shell step using `gh api` and `gh release create` makes commit identity,
idempotency, and failure behavior explicit while using GitHub-supported tooling already on
the runner.

### Marketplace release action

A dedicated action could reduce shell code, but it adds a third-party dependency and does
not provide the required explicit handling for partial or conflicting existing state.

### GitHub-generated tag without preflight checks

Calling `gh release create` directly is shorter, but rerun behavior and conflicts are left
to command failure text. It cannot distinguish a valid same-commit rerun from inconsistent
tag/release state with the required precision.

## Testing and Review

Local validation covers the repository's full pytest, Ruff, and mypy commands. The workflow
YAML is also parsed with the Python environment's existing YAML support when available;
otherwise it is inspected structurally without adding a dependency.

Review should verify:

- trigger paths and `main` scoping;
- exact-SHA checkout in both jobs;
- dependency and permission boundaries, including read-only run metadata access;
- UTC CalVer construction;
- correct handling of absent, matching, partial, and conflicting GitHub state;
- generated release notes with no uploaded assets;
- serialized, non-cancelling execution with the multi-run queue enabled.
