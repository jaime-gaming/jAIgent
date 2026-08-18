#!/usr/bin/env bash
#
# Move the workflow files into .github/workflows/ and push them.
#
# They cannot be committed there by the automation account that wrote them:
# GitHub refuses any push that creates or edits a file under .github/workflows/
# unless the token carries the `workflows` scope, and it does not. Running this
# from your own machine, with your own credentials, does have that scope.
#
# Once this has run, pushing a v* tag builds the binaries for all five platform
# targets and attaches them to the release automatically.

set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

if [ ! -f .github/ci.yml ] && [ ! -f .github/release.yml ]; then
  echo "Nothing to do: the workflows are already in .github/workflows/."
  exit 0
fi

mkdir -p .github/workflows

for name in ci release; do
  if [ -f ".github/$name.yml" ]; then
    git mv ".github/$name.yml" ".github/workflows/$name.yml"
    echo "  moved .github/$name.yml -> .github/workflows/$name.yml"
  fi
done

git commit -m "ci: activate GitHub Actions workflows

Moves the workflow files into the directory GitHub actually reads. They
were committed one level up because the automation token lacks the
'workflows' scope."

echo
echo "Committed. Now push:"
echo "    git push"
echo
echo "Then cut a release, which builds and attaches the binaries:"
version=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)
echo "    git tag v${version}"
echo "    git push --tags"
