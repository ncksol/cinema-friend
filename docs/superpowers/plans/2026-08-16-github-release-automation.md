# GitHub Release Automation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish an idempotent, validated GitHub release for each release-eligible merge commit pushed to `main`.

**Architecture:** One GitHub Actions workflow filters eligible pushes and serializes runs. A read-only validation job tests the exact pushed commit; only its dependent release job receives `contents: write` and uses the authenticated `gh` CLI to verify or create the CalVer tag and release.

**Tech Stack:** GitHub Actions YAML, Bash, Python 3.12, pytest, Ruff, mypy, GitHub CLI and REST API

## Global Constraints

- Create only `.github/workflows/release.yml` for implementation; do not change application code.
- Trigger only for pushes to `main` that touch `src/**`, `scripts/**`, `install.sh`, or `pyproject.toml`.
- Do not release docs-only, tests-only, or workflow-only changes.
- Validate the exact push SHA on Python 3.12 before release creation.
- Run `python -m pytest -q`, `python -m ruff check src tests scripts`, and `python -m mypy src scripts`.
- Keep workflow-level permissions at `contents: read`; only the dependent release job gets `contents: write` plus read-only `actions: read` for stable run metadata.
- Use only `actions/checkout`, `actions/setup-python`, and the runner's authenticated `gh` CLI.
- Serialize with concurrency group `release-main` and `cancel-in-progress: false`.
- Publish a normal release with generated notes and no uploaded assets.
- Format tags exactly as `v<UTC YYYY.MM.DD>.<github.run_number>`.
- Treat matching reruns as success and partial or conflicting GitHub state as explicit failure.
- Do not add `Co-authored-by` trailers to commits.

## File Structure

- Create `.github/workflows/release.yml`: trigger filtering, validation, permissions,
  CalVer derivation, GitHub-state verification, and release creation.
- Retain `docs/superpowers/specs/2026-08-16-github-release-automation-design.md`:
  approved behavior and review criteria.
- Create `docs/superpowers/plans/2026-08-16-github-release-automation.md`: this execution
  plan.

---

### Task 1: Add the Validated Release Workflow

**Files:**
- Create: `.github/workflows/release.yml`

**Interfaces:**
- Consumes: GitHub event values `github.sha`, `github.run_number`, and
  `github.repository`
- Consumes: GitHub REST endpoints for refs, annotated tags, commits, and releases
- Produces: one normal GitHub release tagged
  `v<UTC YYYY.MM.DD>.<github.run_number>` at the exact pushed commit

- [ ] **Step 1: Create the workflow with trigger, concurrency, validation, and permission boundaries**

Create `.github/workflows/release.yml` with:

```yaml
name: Release

on:
  push:
    branches:
      - main
    paths:
      - "src/**"
      - "scripts/**"
      - "install.sh"
      - "pyproject.toml"

concurrency:
  group: release-main
  cancel-in-progress: false

permissions:
  contents: read

jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - name: Check out release commit
        uses: actions/checkout@v4
        with:
          ref: ${{ github.sha }}

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip

      - name: Install development dependencies
        run: python -m pip install ".[dev]"

      - name: Run tests
        run: python -m pytest -q

      - name: Run Ruff
        run: python -m ruff check src tests scripts

      - name: Run mypy
        run: python -m mypy src scripts

  release:
    needs: validate
    runs-on: ubuntu-latest
    permissions:
      actions: read
      contents: write
    env:
      GH_TOKEN: ${{ github.token }}
    steps:
      - name: Check out release commit
        uses: actions/checkout@v4
        with:
          ref: ${{ github.sha }}

      - name: Create release
        shell: bash
        run: |
          set -euo pipefail

          repository_api="repos/${GITHUB_REPOSITORY}"
          run_created_at="$(
            gh api "${repository_api}/actions/runs/${GITHUB_RUN_ID}" \
              --jq '.created_at'
          )"
          tag="v$(date -u -d "${run_created_at}" +%Y.%m.%d).${GITHUB_RUN_NUMBER}"
          ref_json="${RUNNER_TEMP}/release-ref.json"
          release_json="${RUNNER_TEMP}/release.json"

          api_get() {
            local endpoint="$1"
            local output_file="$2"
            local response_file
            local status

            response_file="$(mktemp "${RUNNER_TEMP}/gh-api.XXXXXX")"
            if gh api --include "${endpoint}" >"${response_file}" 2>&1; then
              awk 'body { print } /^\r?$/ { body = 1 }' \
                "${response_file}" >"${output_file}"
              rm -f "${response_file}"
              return 0
            else
              status=$?
            fi

            if head -n 1 "${response_file}" | grep -Eq '^HTTP/[0-9.]+ 404 '; then
              rm -f "${response_file}"
              return 44
            fi

            cat "${response_file}" >&2
            rm -f "${response_file}"
            return "${status}"
          }

          resolve_object_to_commit() {
            local object_type="$1"
            local object_sha="$2"
            local tag_object_json
            local depth=0

            while [[ "${object_type}" == "tag" ]]; do
              if (( depth >= 10 )); then
                echo "annotated tag chain exceeds 10 objects for ${tag}" >&2
                return 1
              fi
              tag_object_json="$(
                gh api "${repository_api}/git/tags/${object_sha}"
              )"
              object_type="$(jq -r '.object.type' <<<"${tag_object_json}")"
              object_sha="$(jq -r '.object.sha' <<<"${tag_object_json}")"
              depth=$((depth + 1))
            done

            if [[ "${object_type}" != "commit" ]]; then
              echo "tag ${tag} resolves to ${object_type}, not a commit" >&2
              return 1
            fi

            printf '%s\n' "${object_sha}"
          }

          if api_get "${repository_api}/git/ref/tags/${tag}" "${ref_json}"; then
            ref_status=0
          else
            ref_status=$?
          fi
          if api_get "${repository_api}/releases/tags/${tag}" "${release_json}"; then
            release_status=0
          else
            release_status=$?
          fi

          if [[ ${ref_status} -eq 44 && ${release_status} -eq 44 ]]; then
            gh release create "${tag}" \
              --repo "${GITHUB_REPOSITORY}" \
              --target "${GITHUB_SHA}" \
              --generate-notes \
              --title "${tag}"
            exit 0
          fi

          if [[ ${ref_status} -ne 0 && ${ref_status} -ne 44 ]]; then
            echo "failed to inspect tag ${tag}" >&2
            exit "${ref_status}"
          fi
          if [[ ${release_status} -ne 0 && ${release_status} -ne 44 ]]; then
            echo "failed to inspect release ${tag}" >&2
            exit "${release_status}"
          fi
          if [[ ${ref_status} -eq 44 || ${release_status} -eq 44 ]]; then
            echo "tag and release state is incomplete for ${tag}" >&2
            exit 1
          fi

          ref_type="$(jq -r '.object.type' "${ref_json}")"
          ref_sha="$(jq -r '.object.sha' "${ref_json}")"
          tag_commit="$(
            resolve_object_to_commit "${ref_type}" "${ref_sha}"
          )"
          release_target="$(jq -r '.target_commitish' "${release_json}")"
          release_target_encoded="$(
            jq -rn --arg value "${release_target}" '$value | @uri'
          )"
          release_commit="$(
            gh api "${repository_api}/commits/${release_target_encoded}" --jq '.sha'
          )"

          if [[ "${tag_commit}" != "${GITHUB_SHA}" ]]; then
            echo "tag ${tag} targets ${tag_commit}, expected ${GITHUB_SHA}" >&2
            exit 1
          fi
          if [[ "${release_commit}" != "${GITHUB_SHA}" ]]; then
            echo \
              "release ${tag} targets ${release_commit}, expected ${GITHUB_SHA}" \
              >&2
            exit 1
          fi

          echo "release ${tag} already exists for ${GITHUB_SHA}"
```

- [ ] **Step 2: Parse the workflow with existing local tooling**

First try the current Python environment:

```bash
.venv/bin/python -c \
  'import pathlib, yaml; yaml.safe_load(pathlib.Path(".github/workflows/release.yml").read_text())'
```

If `yaml` is unavailable, use the platform Ruby standard library without installing
anything:

```bash
ruby -e 'require "yaml"; YAML.parse_file(".github/workflows/release.yml")'
```

Expected: exit 0 with no output.

- [ ] **Step 3: Inspect the resulting workflow against the approved invariants**

Run:

```bash
git --no-pager diff --check
git --no-pager diff -- .github/workflows/release.yml
```

Expected: no whitespace errors. Confirm the diff contains only the approved path filters,
exact-SHA checkout in both jobs, read-only workflow permissions, the release-job write
override with read-only run metadata access, validation dependency, stable UTC CalVer tag,
generated notes, and explicit matching/partial/conflicting-state branches.

- [ ] **Step 4: Run the repository's full local validation**

Run:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check src tests scripts
.venv/bin/python -m mypy src scripts
```

Expected: all tests pass, Ruff reports no errors, and mypy reports no issues.

- [ ] **Step 5: Commit the workflow and plan**

```bash
git add .github/workflows/release.yml \
  docs/superpowers/plans/2026-08-16-github-release-automation.md
git commit -m "ci: automate GitHub releases"
```

---

### Task 2: Publish the Branch and Open the Pull Request

**Files:**
- Inspect: `.github/PULL_REQUEST_TEMPLATE.md`
- Inspect: `.github/PULL_REQUEST_TEMPLATE/**`

**Interfaces:**
- Consumes: the current branch and its commits
- Produces: an open pull request targeting `main`; does not merge it

- [ ] **Step 1: Confirm the final commit set and clean worktree**

Run:

```bash
git --no-pager status --short --branch
git --no-pager log --oneline main..HEAD
git --no-pager diff --stat main...HEAD
```

Expected: the worktree is clean and the branch contains the design, plan, and workflow
commits only.

- [ ] **Step 2: Inspect any pull request template**

Run:

```bash
find .github -maxdepth 2 -iname '*pull_request_template*' -type f -print
```

If a template exists, read it and retain its complete structure in the PR body. If none
exists, use a concise body describing the trigger, validation gate, permissions, CalVer,
and idempotency, followed by the exact local validation results.

- [ ] **Step 3: Push the branch**

```bash
git push -u origin ncksol-github-release-automation
```

Expected: the remote branch is created and tracks `origin/ncksol-github-release-automation`.

- [ ] **Step 4: Open but do not merge the pull request**

Use `gh pr create --base main --head ncksol-github-release-automation` with title:

```text
Automate validated GitHub releases
```

Expected: one open pull request URL. Do not invoke any merge command.
