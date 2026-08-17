## What and why

<!-- What does this change, and what problem does it solve? Link any related issue. -->

## Checklist

- [ ] `pytest` passes
- [ ] `ruff check .` is clean and `ruff format .` has been applied
- [ ] `mypy` is clean
- [ ] Tests added or updated for the change
- [ ] README updated if user-visible behaviour changed (flag, tool, or setting)
- [ ] `CHANGELOG.md` updated under "Unreleased"

## Security

- [ ] Any new filesystem path goes through `resolve_in_workspace()`
- [ ] Any new dangerous capability is opt-in and marked `dangerous=True`
- [ ] No secrets, keys or telemetry added
