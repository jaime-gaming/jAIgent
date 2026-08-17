# Activating CI

The GitHub Actions workflow for this project lives at [`ci.yml`](ci.yml) in this
directory rather than in `.github/workflows/`, because the automation account that
created it does not hold the `workflows` permission.

To enable it, move the file into place and push:

```bash
mkdir -p .github/workflows
git mv .github/ci.yml .github/workflows/ci.yml
git commit -m "ci: activate GitHub Actions workflow"
git push
```

The workflow runs the test suite on Python 3.10–3.13 (Linux, plus macOS and Windows
on 3.12), checks lint and formatting with ruff, type-checks with mypy, and builds
the package. It needs no secrets — the test suite is fully offline.
