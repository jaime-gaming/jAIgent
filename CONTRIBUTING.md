# Contributing to jaigent

Thanks for considering a contribution. jaigent aims to stay small, readable and safe by default — contributions that keep it that way are very welcome.

## Getting set up

```bash
git clone https://github.com/jaime-gaming/jaigent.git
cd jaigent
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

The test suite runs offline and needs no API key.

## Before opening a pull request

```bash
pytest          # all green
ruff check .    # clean
ruff format .   # applied
mypy            # clean
```

Then:

- Add tests for anything you changed. Coverage sits around 89%; please don't lower it.
- Update the README if you changed something a user can see (a flag, a tool, a setting).
- Add a line to `CHANGELOG.md` under "Unreleased".
- Use [Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`.

## What makes a good contribution

**Good fits:** a new tool that is genuinely useful and safely scoped, a new LLM provider, better error messages, docs fixes, tests for uncovered paths, bug fixes with a regression test.

**Discuss in an issue first:** anything that adds a runtime dependency, an async rewrite, a plugin system, a web UI, or a change to the sandbox rules. These are architectural decisions, not patches.

**Will not be merged:** telemetry or analytics of any kind, a bundled API key or a proxy between users and their provider, and anything that weakens the workspace sandbox for convenience.

## Conventions

The full conventions — code style, the error hierarchy, security rules, how to add a tool or a provider — live in [AGENTS.md](AGENTS.md). It is written for AI coding agents but applies equally to humans, and it is the authoritative reference.

Three rules worth repeating here:

1. Every filesystem path goes through `resolve_in_workspace()`. No exceptions.
2. New dangerous capabilities are opt-in, gated behind a `Settings` flag and marked `dangerous=True`.
3. Tool error messages are read by an LLM that will retry — say what was wrong *and* what to do instead.

## Reporting bugs

Open an issue with: what you ran, what you expected, what happened, and the output of `jaigent config` (the key is masked automatically). Include your Python version and provider.

For anything with security impact — a sandbox escape in particular — please report it privately via GitHub's security advisories rather than a public issue.

## License

By contributing you agree that your contributions are licensed under the [Apache License 2.0](LICENSE).
