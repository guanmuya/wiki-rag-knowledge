# Automatic Windows Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run existing CI and automatically publish a verified Windows GitHub Release after a valid version tag is pushed.

**Architecture:** Extend the existing workflow with tag-only release jobs that depend on the same run's CI. Small Python helpers validate versions, release notes and update assets, and perform fail-closed publication; a Windows smoke test validates the installed desktop application before publication. A restricted token is only made available to the publishing job.

**Tech Stack:** GitHub Actions, Python 3.13, PyYAML 6.0.3, Node.js 22, pnpm 9, Playwright, PowerShell, GitHub CLI.

**Spec:** `docs/superpowers/specs/2026-11-16-ci-cd-auto-release-design.md`

## Global Constraints

- PR, `master` and manual triggers run CI; only a pushed strict `vMAJOR.MINOR.PATCH` tag can publish.
- Test and build the tag's exact commit; require it to be an ancestor of remote `master` and its version to exceed all published stable releases.
- Validate and publish the exact same nonempty installer, `latest.yml`, `.blockmap` and extracted public release notes; never overwrite an existing published release or an unrelated draft.
- Isolate test data and disable the updater during the installer smoke test; do not call real models or expose user data.
- Do not create a real production tag or release while developing or verifying this change.
- Use read-only permissions until the tag-only publishing job; queue release jobs without canceling in-progress publications.

## Review Focus

1. A malformed, prerelease, moved, or non-master tag must never publish; cover parsing, ancestry and remote-ref checks.
2. A missing/ambiguous notes section must fail without publishing internal release instructions; cover both LF and CRLF documents.
3. A mismatched filename, duplicate YAML key, missing blockmap, incorrect size or changed installer hash must fail.
4. Interrupted upload, stale or unrelated draft, an already published tag, and a newer competing release must not cause overwrite or final publication.
5. Installer launch failure or timeout must stop publication, close child processes and leave the update client disabled during the test.

## File Structure

- Create `backend/requirements-release.txt`: directly pin the YAML parser used by release tools.
- Create `.github/scripts/release_checks.py`: pure version, notes and asset checks plus command-line entry points.
- Create `.github/scripts/release_publish.py`: GitHub CLI adapter and guarded draft/upload/verify/publish transaction.
- Create `.github/scripts/tests/test_release_checks.py` and `.github/scripts/tests/test_release_publish.py`: behavior and failure-path tests.
- Create `frontend/scripts/smoke-release.mjs` and `frontend/tests/smoke-release.test.mjs`: installed app smoke test and testable timeout/path handling.
- Modify `.github/workflows/ci.yml`: preserve existing checks; add helper tests, tag validation, package/smoke, and serialized automatic publication.
- Modify `README.md` and `docs/UPDATE.md`: explain tag-driven publishing and how to inspect failures.

## Task 1: Release validation

**Interfaces:** `parse_tag(tag) -> tuple[int, int, int]`; `extract_notes(text) -> str`; `verify_assets(version, directory) -> dict`; `check_release_version(tag, frontend_version, backend_source, releases) -> None`.

- [ ] Write failing tests for valid `v1.0.23`, malformed/pre-release tags, version mismatches, stable versus draft/pre-release comparison, and matching the highest published version.
- [ ] Write failing tests for one public `markdown` block under section 1, missing or ambiguous blocks, and CRLF input.
- [ ] Write failing tests for a real temporary installer/manifest/blockmap set, then missing, duplicate-key, wrong version/path/size/hash and modified-file cases.
- [ ] Run `python -m unittest discover -s .github/scripts/tests -p 'test_release_checks.py' -v`; confirm failures are due to missing implementation.
- [ ] Implement the interfaces with safe YAML parsing, explicit pinned dependency, streamed SHA-512 and strict path/file validation.
- [ ] Rerun the tests and add them to the workflow's regular CI using the release requirements file.

## Task 2: Guarded automatic publication

**Interfaces:** `preflight(tag, sha, repo, git, github) -> ReleaseContext`; `publish(context, assets, github) -> None`. The GitHub adapter wraps only explicit CLI/API calls and receives the authentication environment from the runner.

- [ ] Write failing tests using a fake GitHub adapter for tag ancestry/ref movement, release-version ordering, existing published release, unrelated draft, interrupted upload, mismatched downloaded asset and successful final publication.
- [ ] Implement preflight using exact commit identity, existing stable releases and notes extracted from the tagged source; preserve tag names as validated arguments rather than shell code.
- [ ] Implement draft creation with an ownership marker containing tag, commit and run identity. Allow safe retries only for the same marked draft; never overwrite a published release or unrelated draft.
- [ ] Download and verify every uploaded asset before automatically changing the draft to a public latest release. Serialize publication and recheck version/tag immediately before publishing.
- [ ] Run `python -m unittest discover -s .github/scripts/tests -v`; verify every negative case leaves the public release unchanged.

## Task 3: Installed Windows smoke test

**Interfaces:** `runSmoke({ installer, installDir, userDataDir, launch, timeoutMs }) -> Promise<void>`; CLI invoked on Windows against the package generated in the same workflow.

- [ ] Write failing tests for path validation, install failure, app failure/timeout, and always cleaning up the launched process; run `node --test frontend/tests/smoke-release.test.mjs` and confirm missing implementation fails.
- [ ] Implement silent installation in a temporary runner directory, launch the installed app with `WRK_UPDATE_DISABLE=1` and isolated user data, use Playwright/Electron to verify window and loopback backend health, and close the entire process tree on all paths.
- [ ] Rerun Node tests and exercise the real installer smoke test on a Windows GitHub-hosted runner in an isolated or non-publishing validation run.

## Task 4: Integrate, verify, and document

- [ ] Retain existing CI check name and five checks; add helper tests without exposing write permissions to PRs.
- [ ] Add tag push trigger and tag-only preflight/package/smoke/publish jobs with explicit `needs`, same-commit checkout, temporary artifacts, read-only defaults and publication-only write permissions.
- [ ] Add a queued concurrency group for publishing and do not cancel in-progress tag runs; ensure PR/master CI cancellation remains appropriate.
- [ ] Validate workflow syntax and every trigger/dependency/permission path; run Python helper tests and Node smoke tests.
- [ ] Run the existing relevant project checks where the environment supports them and exercise the actual Windows workflow in a non-publishing context; report any inability to verify real Windows packaging or GitHub release behavior.
- [ ] Update README and update guide with version, notes, tag commands, automatic-publication consequences, monitoring and failure recovery. Clarify branch protection is a separate GitHub setting.
- [ ] Review all changes against the spec and run `git diff --check` and the relevant test suites before creating a reviewable change.
