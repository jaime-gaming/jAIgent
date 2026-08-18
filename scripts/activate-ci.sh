#!/usr/bin/env bash
#
# Put the GitHub Actions workflows where GitHub reads them, and keep the paths
# inside them honest.
#
# The automation account that writes these files cannot touch anything under
# .github/workflows/: GitHub refuses any push that creates or edits a file there
# unless the token carries the `workflows` scope, and it does not. Running this
# from your own machine, with your own credentials, does have that scope.
#
# It is safe to run repeatedly; it only commits when something actually changed.

set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

changed=0

# --------------------------------------------------------------------
# 1. Move the workflows into the directory GitHub actually reads.
# --------------------------------------------------------------------
for name in ci release; do
  if [ -f ".github/$name.yml" ]; then
    mkdir -p .github/workflows
    git mv ".github/$name.yml" ".github/workflows/$name.yml"
    echo "  moved .github/$name.yml -> .github/workflows/$name.yml"
    changed=1
  fi
done

# --------------------------------------------------------------------
# 2. Repair the workflow files.
#
# Each edit is a targeted, idempotent string replacement, so running this
# again once it has been applied is a no-op.
# --------------------------------------------------------------------
ci=".github/workflows/ci.yml"

# 2a. ci.yml cross-checks the asset names against the release workflow by
#     reading it from disk. That path is wrong the moment step 1 has run, and
#     the job fails with FileNotFoundError rather than anything that explains
#     itself.
if [ -f "$ci" ] && grep -q '"\.github/release\.yml"' "$ci"; then
  # Only the quoted literal, so prose and comments are left alone.
  perl -pi -e 's{"\.github/release\.yml"}{"\.github/workflows/release\.yml"}g' "$ci"
  git add "$ci"
  echo "  fixed the release.yml path inside $ci"
  changed=1
fi

# 2b. The CLI smoke test says `doctor || true`, which is shell syntax. GitHub
#     defaults Windows runners to PowerShell, where `true` is not a command,
#     and the runner prepends $ErrorActionPreference = 'stop' to every pwsh
#     step: doctor exits 1 without an API key, pwsh then tried to run `true`,
#     and the step aborted however well jaigent itself behaved. bash exists on
#     all three runner images, so pin the step to it.
if [ -f "$ci" ] && grep -q 'name: Smoke test the CLI' "$ci" &&
  ! grep -A8 'name: Smoke test the CLI' "$ci" | grep -q 'shell: bash'; then
  perl -0777 -pi -e \
    's{(- name: Smoke test the CLI\n)(\s+)(run: \|)}{$1$2# bash on purpose: the step says `|| true`, which is shell syntax.\n$2# The default Windows shell is pwsh, where `true` is not a command,\n$2# and the runner prepends \$ErrorActionPreference = '"'"'stop'"'"' to every\n$2# pwsh step — so `doctor || true` (doctor exits 1 without an API key)\n$2# would abort the whole step trying to run it. bash ships on all\n$2# three runner images.\n$2shell: bash\n$2$3}' "$ci"
  git add "$ci"
  echo "  pinned the CLI smoke test to bash in $ci (PowerShell has no \`true\`)"
  changed=1
fi

# 2c. The Windows binary smoke test allows `doctor` to exit 1 — it checks the
#     code itself — but two runner behaviours turned that into a build failure
#     first: the runner appends `exit $LASTEXITCODE` to every pwsh step, which
#     published doctor's 1 as the step's own exit code, and PowerShell 7.4+
#     can make a non-zero native exit a terminating error while
#     $ErrorActionPreference is Stop. Make the step the sole judge.
release=".github/workflows/release.yml"
if [ -f "$release" ] && ! grep -q 'PSNativeCommandUseErrorActionPreference' "$release"; then
  perl -0777 -pi -e \
    's{(- name: Smoke test the binary \(windows\)\n(?:[^\n]*\n)*?(\s+)\$ErrorActionPreference = "Stop"\n)}{$1$2# doctor reports a missing API key by exiting 1, and the checks\n$2# below allow exactly that. Two runner behaviours would otherwise\n$2# turn it into a build failure first: PowerShell 7.4+ can make a\n$2# non-zero native exit a terminating error while ErrorActionPreference\n$2# is Stop, and the runner appends `exit \$LASTEXITCODE` to every pwsh\n$2# step, which would publish doctor'"'"'s 1 as this step'"'"'s own exit code.\n$2\$PSNativeCommandUseErrorActionPreference = \$false\n}' "$release"
  perl -0777 -pi -e \
    's{( +)if \(\$LASTEXITCODE -gt 1\) \{ throw "doctor crashed \(\$LASTEXITCODE\)" \}\n}{$1if (\$LASTEXITCODE -gt 1) { throw "doctor crashed (\$LASTEXITCODE)" }\n$1# The step has decided for itself that everything passed; without\n$1# this, the runner'"'"'s appended `exit \$LASTEXITCODE` would resurface\n$1# doctor'"'"'s 1 and fail the build of a perfectly good binary.\n$1exit 0\n}' "$release"
  git add "$release"
  echo "  stopped doctor's exit 1 failing the Windows smoke test in $release"
  changed=1
fi

# 2d. macos-13 was retired in December 2025: a job asking for a retired image
#     is never picked up, so the release hung in "queued" until it was
#     cancelled. macos-15-intel is GitHub's designated successor for x86_64
#     macOS builds.
if [ -f "$release" ] && grep -q 'os: macos-13$' "$release"; then
  perl -0777 -pi -e \
    's{- os: macos-13\n}{- os: macos-15-intel\n}' "$release"
  perl -0777 -pi -e \
    's{# The macOS images are pinned rather than -latest because macos-13 is the\n  # last Intel runner, and "latest" silently moving to ARM would drop x64\.\n}{# The macOS images are pinned rather than -latest, because "latest" is ARM\n  # only and would silently drop the Intel build: macos-15-intel is the\n  # successor to macos-13, which was retired in December 2025, and is the\n  # last x86_64 image GitHub offers.\n}' "$release"
  git add "$release"
  echo "  moved the Intel build from macos-13 to macos-15-intel in $release"
  changed=1
fi

if [ "$changed" -eq 0 ]; then
  echo "Nothing to do: the workflows are in place and correct."
  exit 0
fi

git commit -m "ci: activate and repair the GitHub Actions workflows

Moves the workflow files into the directory GitHub actually reads, if they
are not there already, and applies the repairs the automation account could
not push itself: the CLI smoke test is pinned to bash so the Windows runner
stops tripping over \`doctor || true\` in PowerShell; the Windows binary smoke
test decides its own exit code instead of letting the runner resurface
doctor's by-design exit 1; and the Intel build moves from macos-13, retired
in December 2025, to macos-15-intel. GitHub refuses any push from the
automation account that touches .github/workflows/ without the 'workflows'
token scope."

echo
echo "Committed. Now push:"
echo "    git push"
echo
version=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)
if git rev-parse -q --verify "refs/tags/v${version}" >/dev/null 2>&1; then
  tag_sha=$(git rev-parse --short "v${version}")
  head_sha=$(git rev-parse --short HEAD)
  if [ "$tag_sha" = "$head_sha" ]; then
    echo "v${version} is already tagged at this commit; nothing to re-cut."
  else
    echo "The tag v${version} already exists (at $tag_sha, not this commit)."
    echo "To re-cut it as this release:"
    echo "    gh release delete v${version} --yes"
    echo "    git push origin :refs/tags/v${version}"
    echo "    git tag v${version}"
    echo "    git push origin v${version}"
  fi
else
  echo "Then cut a release, which builds and attaches the binaries:"
  echo "    git tag v${version}"
  echo "    git push origin v${version}"
fi
