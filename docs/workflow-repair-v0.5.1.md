# Workflow repairs for v0.5.1

This file documents the three repairs the GitHub Actions workflows need for
v0.5.1 to publish successfully. The bot token that wrote this file does not
have the `workflows` permission to push the fixes themselves, so they have
been collected here for whoever does.

## Why

`main` ships with workflows that fail on every platform:

1. **ci.yml — "Smoke test the CLI" failed on both Windows runners.** The step
   says `doctor || true`; the default Windows shell is `pwsh`, and the runner
   *prepends* `$ErrorActionPreference = 'stop'` to every pwsh step (confirmed
   in `actions/runner` `ScriptHandlerHelpers.cs`). `doctor` exits 1 without an
   API key, pwsh then tried to run `true` — not a command on Windows — and
   the step aborted.

   **Fix:** the step is pinned to `shell: bash`.

2. **release.yml — "Smoke test the binary (windows)" failed with exit 1 on a
   good `jaigent.exe`.** The runner *appends*
   `if ((Test-Path -LiteralPath variable:\LASTEXITCODE)) { exit $LASTEXITCODE }`
   to every pwsh step; the step's last native command was `doctor`, which
   exits 1 by design, so the step published 1 as its own exit code and the
   binary was discarded.

   **Fix:** `$PSNativeCommandUseErrorActionPreference = $false` at the top
   and `exit 0` at the end. The `throw`s still fail on real problems.

3. **release.yml — the `macos-x64` job never left the queue.** `macos-13`
   was retired 2025-12-04; jobs on retired images queue forever.

   **Fix:** `macos-15-intel`, GitHub's designated x86_64 successor.

## How to apply

The script on `main` does this for you. From the repository root:

```bash
./scripts/activate-ci.sh
git push
```

Running it on a freshly-cloned `main` produces a single commit modifying
exactly the lines in `packaging/workflow-fixes-v0.5.1.patch`. The diff in
this commit is byte-identical to what the bot's local command generated
during this session — verified by running the script, comparing, and
running it a second time, which prints "Nothing to do." Each repair has a
regression test in `tests/test_workflows.py`; the four-test suite passed
locally the entire time the fixes sat in the working tree.

If pushing `activate-ci.sh`'s commit is also blocked, the patch itself can
be applied by hand:

```bash
cd /path/to/jaigent
git apply /path/to/packaging/workflow-fixes-v0.5.1.patch
git add .github/workflows
git commit -m "ci: activate and repair the GitHub Actions workflows

Moves the workflow files into the directory GitHub actually reads, if they
are not there already, and applies the repairs the automation account could
not push itself: the CLI smoke test is pinned to bash so the Windows runner
stops tripping over \`doctor || true\` in PowerShell; the Windows binary smoke
test decides its own exit code instead of letting the runner resurface
doctor's by-design exit 1; and the Intel build moves from macos-13, retired
in December 2025, to macos-15-intel."
git push
```

## After the workflow fixes are on main

```bash
# Delete the placeholder v0.5.1 that this session created (assets pending).
gh release delete v0.5.1 --yes
git push origin :refs/tags/v0.5.1

# Re-tag the merged tip, push it, and let the Release workflow build the rest.
git tag v0.5.1
git push origin v0.5.1
```

Verify the assets when the run completes:

```bash
gh release view v0.5.1 --json assets --jq '.assets[].name'
```

Expected:

- `jaigent-linux-x64.tar.gz`
- `jaigent-linux-arm64.tar.gz`
- `jaigent-macos-x64.tar.gz`
- `jaigent-macos-arm64.tar.gz`
- `jaigent-windows-x64.zip`
- `jaigent-0.5.1-py3-none-any.whl`
- `jaigent-0.5.1.tar.gz`
- `checksums.txt`

## What that makes available downstream

- `pip install --upgrade jaigent` (going to PyPI for users, or `pip install`
  from the wheel/sdist for environments where the existing v0.5.1 is already
  installed) does not need binaries.
- The macOS and Windows installer scripts at
  `packaging/install.sh` and `packaging/install.ps1` will resolve to fresh
  binaries at last, instead of returning 404.
- The `jaigent update` command, both for binary installs and for background
  check notices, will find a release with a real version behind it.

## Heads-up

The `ubuntu-22.04` / `ubuntu-22.04-arm` runners used for the glibc floor
start deprecation 2026-09-17 and are fully unsupported 2027-04-17
(actions/runner-images#14254). The pin in the workflow is deliberate —
read the matrix comment in `release.yml` before "fixing" it.

`macos-15-intel` is the last x86_64 macOS image (until ~2027-08).
