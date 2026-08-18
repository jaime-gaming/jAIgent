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
# 2. Repair references to the old location.
#
# ci.yml cross-checks the asset names against the release workflow by reading
# it from disk. That path is wrong the moment step 1 has run, and the job fails
# with FileNotFoundError rather than anything that explains itself.
# --------------------------------------------------------------------
ci=".github/workflows/ci.yml"
if [ -f "$ci" ] && grep -q '"\.github/release\.yml"' "$ci"; then
  # Only the quoted literal, so prose and comments are left alone.
  perl -pi -e 's{"\.github/release\.yml"}{"\.github/workflows/release\.yml"}g' "$ci"
  git add "$ci"
  echo "  fixed the release.yml path inside $ci"
  changed=1
fi

if [ "$changed" -eq 0 ]; then
  echo "Nothing to do: the workflows are in place and their paths are correct."
  exit 0
fi

git commit -m "ci: activate GitHub Actions workflows

Moves the workflow files into the directory GitHub actually reads, and
points ci.yml's asset-name cross-check at the new location. They were
committed one level up because the automation token lacks the
'workflows' scope."

echo
echo "Committed. Now push:"
echo "    git push"
echo
version=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)
echo "Then cut a release, which builds and attaches the binaries:"
echo "    git tag v${version}"
echo "    git push origin v${version}"
