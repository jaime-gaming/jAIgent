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

# 2b. The CLI smoke test uses `|| true`, which is shell syntax. GitHub defaults
#     Windows runners to PowerShell, where `true` is not a command, so the step
#     fails on Windows however well jaigent itself behaves. bash exists on all
#     three runner images, so pin the step to it.
if [ -f "$ci" ] && grep -q '^      - name: Smoke test the CLI$' "$ci" &&
  ! perl -0777 -ne 'exit(/- name: Smoke test the CLI\n\s+shell: bash/ ? 0 : 1)' "$ci"; then
  perl -0777 -pi -e \
    's{(- name: Smoke test the CLI\n)(\s+)(run: \|)}{$1$2shell: bash\n$2$3}' "$ci"
  git add "$ci"
  echo "  pinned the CLI smoke test to bash in $ci (PowerShell has no \`true\`)"
  changed=1
fi

if [ "$changed" -eq 0 ]; then
  echo "Nothing to do: the workflows are in place and their paths are correct."
  exit 0
fi

git commit -m "ci: activate and repair the GitHub Actions workflows

Moves the workflow files into the directory GitHub actually reads, points
ci.yml's asset-name cross-check at the new location, and pins the CLI
smoke test to bash so it works on the Windows runner too. These cannot be
committed by the automation account: GitHub refuses any push touching
.github/workflows/ without the 'workflows' token scope."

echo
echo "Committed. Now push:"
echo "    git push"
echo
version=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)
echo "Then cut a release, which builds and attaches the binaries:"
echo "    git tag v${version}"
echo "    git push origin v${version}"
