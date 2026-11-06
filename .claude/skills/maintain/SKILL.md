---
name: maintain
description: Maintenance pass for the switchboard-ha Home Assistant integration — bump the mise toolchain (exact pins + mise.lock), the Home Assistant test harness and the pinned GitHub Actions, verify lint/tests/HACS metadata, reconcile the README with the code, commit. Use when the user runs /maintain or asks for a maintenance/housekeeping pass on this repo.
---

# switchboard-ha maintenance pass

Run the phases in order. Work from ground truth (registries, the code, the Switchboard repo's
`docs/HA.md` contract) — never from memory. Small Conventional Commits, one per concern. This repo
has standing push authorization from the Switchboard project (its CLAUDE.md "Plugin sync"); push
over SSH and watch the GitHub Actions run until green. **Never change behavior in a maintenance
pass** — the `/api` contract is `docs/HA.md` in Switchboard, and a behavior change is its own task.

**Release safety:** a push to `main` runs `scripts/release.sh`, which cuts a CalVer release when the
range holds a `feat`/`fix`/`perf`/`revert` or breaking commit. Maintenance commits are
`build:`/`ci:`/`chore:`/`docs:`/`test:` so they never cut a release on their own.

## Phase 1 — mise toolchain (`mise.toml` + `mise.lock`)

1. Every pin in `mise.toml` is an exact `X.Y.Z` (never `latest`, never a bare minor). Find the
   newest of each: `mise outdated --bump`, or the registry (`mise ls-remote <tool>`).
2. **Release-age quarantine: one day.** A new version must have been published at least 24 h ago
   (`minimum_release_age = "1d"` filters only what mise *resolves*, not exact pins — check by hand:
   PyPI `upload_time` for ruff/uv, crates.io `created_at` for git-cliff, `mise ls-remote --json
   python` `created_at`). Too young → take the previous version, retry next pass.
3. **python**: stays on the minor that Home Assistant's CURRENT release requires — check
   `https://pypi.org/pypi/homeassistant/json` → `info.requires_python` (2026-11: `>=3.14.2`) — and
   on the newest patch of it. A new HA minimum minor moves `python` here AND the venv follows (the
   `venv` task uses `mise which python`). `pyproject.toml` `target-version` follows `hacs.json`'s
   `homeassistant` minimum (the OLDEST HA supported), not this pin — raise both together only as a
   deliberate support-window decision (ask the user).
4. `mise lock` (anonymous GitHub API limit is 60/h; set `MISE_GITHUB_TOKEN` if it bites — the lock
   still records URLs + checksums, only the provenance check needs the API), then `mise install`.
   Commit `mise.toml` + `mise.lock` together. CI runs `MISE_LOCKED=1` (workflow `env`), so a pin
   without a matching lock entry fails every job. `lockfile_platforms` = linux-x64, linux-arm64,
   macos-arm64, windows-x64.
5. A ruff bump can bring new rules/format changes: run `mise run lint`; `mise run format` for format
   drift; fix lint findings in code, never by widening `ignore`.

## Phase 2 — Home Assistant test harness

`requirements-test.txt` pins `pytest-homeassistant-custom-component`, which pins the exact HA
version the suite runs against (PyPI `requires_dist` → `homeassistant==…`). Bump it to the newest
release that is at least a day old, `rm -rf .venv && mise run test`. The suite turns HA
`DeprecationWarning`s into errors (`pyproject.toml` `filterwarnings`) — a new failure there is an
API to migrate off, fix it in code (that one is a `fix:` and does cut a release — say so).

## Phase 3 — GitHub Actions

Every `uses:` in `.github/workflows/` is pinned to a full commit SHA with the version in a trailing
comment (`# v7.0.1`; `# main` / `# master` for the branch-only `hacs/action` and
`home-assistant/actions`). Find newer tags without the API: `git ls-remote --tags
https://github.com/<owner>/<repo>.git`; dereference annotated tags (`refs/tags/vX^{}`). Read the
changelog between the old and new major before bumping. `jdx/mise-action`'s `version:` input pins
the mise binary itself — bump it to a mise release at least a day old (crates.io `mise`).

## Phase 4 — Verify

- `mise run lint` and `mise run test` (fresh `.venv` after a harness or python bump): zero
  failures, zero warnings.
- `git-cliff --config cliff.toml --tag vTEST <last-tag>..HEAD` still renders the notes after a
  git-cliff bump.
- `hacs.json` / `custom_components/switchboard/manifest.json` untouched unless the support window
  moved; the CI `HACS` and `hassfest` jobs validate them.

## Phase 5 — Docs

README "Development" matches `mise.toml` and the tasks. Any toolchain statement in comments
(`mise.toml`, `requirements-test.txt`, `pyproject.toml`) matches the new pins.

## Phase 6 — Push and watch CI

`git push` over SSH. `gh` may be unauthenticated: watch
`https://api.github.com/repos/Kirbo/switchboard-ha/actions/runs?branch=main&per_page=3` with a slow
poll (60 s or more; the anonymous limit is 60 requests/h), and on failure read the job/step names
from the run's `jobs_url`. Fix the root cause and push again — never rerun until green.

## Report

Before → after per tool and dependency, commits, the CI run id + conclusion, anything deferred
(too-young versions, a needed support-window decision).
