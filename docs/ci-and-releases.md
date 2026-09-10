# CI and releases

Both GitHub Actions workflows live in [`.github/workflows/`](../.github/workflows/)
and are active: [`ci.yml`](../.github/workflows/ci.yml) runs on every push and
pull request, and [`release.yml`](../.github/workflows/release.yml) runs when a
`v*` tag is pushed.

They started life in `.github/` itself, because the automation account that
created them could not push to `.github/workflows/` at the time.
[`scripts/activate-ci.sh`](../scripts/activate-ci.sh) moved them into place and
repaired them; it is idempotent, so running it again finds nothing to do.

## [`ci.yml`](../.github/workflows/ci.yml) — on every push and pull request

| Job | What it does |
| --- | --- |
| `test` | Runs the suite on Python 3.10, 3.11, 3.12 and 3.13 on Linux, plus 3.10 and 3.13 on macOS and Windows. Then smoke-tests the CLI (`--version`, `doctor`, `route`, `models`, `providers`, `auth list`, `settings list`). Jobs have timeouts so a hung runner cannot sit until cancelled. |
| `lint` | `ruff check`, `ruff format --check`, `mypy`. |
| `security` | `pip-audit` against the declared dependencies and `bandit` over the source. |
| `build` | Builds the wheel and sdist and validates them with `twine check`. |

It needs no secrets — the test suite is fully offline.

## [`release.yml`](../.github/workflows/release.yml) — on a `v*` tag

Builds the standalone binaries with PyInstaller on five runners:

| Target | Runner | Asset |
| --- | --- | --- |
| Linux x64 | `ubuntu-22.04` | `jaigent-linux-x64.tar.gz` |
| Linux arm64 | `ubuntu-22.04-arm` | `jaigent-linux-arm64.tar.gz` |
| macOS Intel | `macos-15-intel` (not retired `macos-13`) | `jaigent-macos-x64.tar.gz` |
| macOS Apple Silicon | `macos-14` | `jaigent-macos-arm64.tar.gz` |
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

### Beta versions are pre-releases

A version is beta-tested on the `beta` branch first, and its release ships
flagged as a **Pre-release**: stable `jaigent update` users never see it,
while beta users are offered it and can install it directly. Cutting one:

```bash
git checkout beta
git merge --no-ff arena/01a08b1f-jaigent   # or whatever carries the version
git push origin beta
gh release create v0.5.5 --target beta --prerelease --title v0.5.5
git fetch --tags origin
```

Pushing the tag starts this workflow, which detects the tag is not on `main`
and re-applies the flag itself — a tag pushed to any branch off `main` always
ships as a pre-release, and PyPI is skipped, because our pre-releases reuse
the bare version number and uploading the beta would squat it so the final
could never publish. The Actions-tab input forces the flag either way for the
rare case the branch is wrong, and beta testers install binaries or pull the
branch: `pip install jaigent` keeps meaning the latest stable.

When the beta is proven, merge `beta` into `main`, move the tag onto the
merge commit, and re-run the release workflow by hand with `prerelease`
unticked — that rebuilds the final binaries and publishes to PyPI. Then
graduate the release itself with `gh release edit v0.5.5 --prerelease=false`:
re-runs only refresh assets and never flip a published release's flag, so the
same number needs that one explicit command to become a full release.

### Publishing to PyPI

`pip install jaigent` only works once the `pypi` job has uploaded the wheel,
and that job authenticates in one of two ways, tried in this order:

1. **`PYPI_API_TOKEN`** — a repository secret holding a pypi.org API token.
   Used whenever it is set, and needs no publisher configuration at all. This
   is the only way to publish the *first* release of a brand-new project: a
   trusted publisher can only be attached to a project that already exists.
2. **Trusted Publishing** — used when the secret is absent. The job declares
   `environment: pypi`, which puts `environment:pypi` into the OIDC token, so
   the PyPI project has to be configured with exactly:

   | PyPI field | Value |
   | --- | --- |
   | Owner | `jaime-gaming` |
   | Repository name | `jaigent` |
   | Workflow name | `release.yml` |
   | Environment name | `pypi` |

   Two of those are easy to get wrong. The *workflow name* is the file name,
   `release.yml` — not the `name: Release` the run is listed under. And the
   *environment name* is `pypi`, not blank: leaving it empty is what makes
   PyPI answer `invalid-publisher` ("valid token, but no corresponding
   publisher") even though owner, repository and workflow are all correct.

A failed run prints the exact claim set it presented, under "The claims
rendered below are for debugging purposes only". Compare that against the
project's publisher settings; whichever field differs is the bug.

The last step of the job asks `pypi.org/pypi/jaigent/<version>/json` whether
the version really landed. When it fails, the job summary names the fix. PyPI
publishing is non-blocking until the repository variable `PYPI_REQUIRED` is set
to `true`; this lets binary releases ship before the first PyPI project is
bootstrapped without pretending that `pip install jaigent` already works. Once
a token or trusted publisher is configured, set that variable so a failed
upload blocks the release.

### Republishing a tag

Fix the configuration, then **Actions → the failed run → Re-run failed
jobs**. Only the `pypi` job runs again, against the artifacts the original
run already uploaded — about a minute, instead of a five-platform build. The
binaries and the GitHub release are untouched, and `skip-existing: false`
means a version that is already on PyPI fails loudly rather than reporting a
successful no-op.

### Why the binaries cannot be built here

PyInstaller needs `libpython3.x.so`, which Debian ships in a separate package that
this environment cannot install without root. The spec and the launcher are verified
locally by other means — the launcher runs as a normal entry point and every hidden
import is checked to resolve — but the actual freeze happens in CI, on each real
target platform. Cross-compiling is not possible with PyInstaller in any case: each
platform's binary must be built on that platform.
