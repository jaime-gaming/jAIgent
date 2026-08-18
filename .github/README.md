# Activating the workflows

Both GitHub Actions workflows for this project live in this directory rather than in
`.github/workflows/`, because the automation account that created them does not hold
the `workflows` permission. GitHub only runs workflows from `.github/workflows/`, so
they are inert until you move them.

To enable both, move them into place and push:

```bash
mkdir -p .github/workflows
git mv .github/ci.yml .github/workflows/ci.yml
git mv .github/release.yml .github/workflows/release.yml
git commit -m "ci: activate GitHub Actions workflows"
git push
```

## [`ci.yml`](ci.yml) — on every push and pull request

| Job | What it does |
| --- | --- |
| `test` | Runs the suite on Python 3.10, 3.11, 3.12 and 3.13 on Linux, plus 3.10 and 3.13 on macOS and Windows. Then smoke-tests the CLI. |
| `lint` | `ruff check`, `ruff format --check`, `mypy`. |
| `security` | `pip-audit` against the declared dependencies and `bandit` over the source. |
| `build` | Builds the wheel and sdist and validates them with `twine check`. |

It needs no secrets — the test suite is fully offline.

## [`release.yml`](release.yml) — on a `v*` tag

Builds the standalone binaries with PyInstaller on five runners:

| Target | Runner | Asset |
| --- | --- | --- |
| Linux x64 | `ubuntu-latest` | `jaigent-linux-x64.tar.gz` |
| Linux arm64 | `ubuntu-24.04-arm` | `jaigent-linux-arm64.tar.gz` |
| macOS Intel | `macos-13` | `jaigent-macos-x64.tar.gz` |
| macOS Apple Silicon | `macos-latest` | `jaigent-macos-arm64.tar.gz` |
| Windows x64 | `windows-latest` | `jaigent-windows-x64.zip` |

Each binary is executed before it is packaged, so a build that cannot start never
ships. The job also builds the wheel, collects every SHA-256 into `checksums.txt`,
and creates the GitHub release with all of it attached. The installer scripts in
[`packaging/`](../packaging) check those checksums, so publishing them is required,
not optional.

Cutting a release:

```bash
# versions in pyproject.toml and src/jaigent/__init__.py must already agree
git tag v0.5.0
git push origin v0.5.0
```

You can also run it by hand from the Actions tab, passing the tag as an input.

### Why the binaries cannot be built here

PyInstaller needs `libpython3.x.so`, which Debian ships in a separate package that
this environment cannot install without root. The spec and the launcher are verified
locally by other means — the launcher runs as a normal entry point and every hidden
import is checked to resolve — but the actual freeze happens in CI, on each real
target platform. Cross-compiling is not possible with PyInstaller in any case: each
platform's binary must be built on that platform.
