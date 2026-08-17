# Security Policy

## Supported versions

jaigent is pre-1.0. Only the latest release receives security fixes.

| Version | Supported |
| --- | --- |
| 0.1.x | ✅ |

## Reporting a vulnerability

Please report security issues **privately** through
[GitHub security advisories](https://github.com/jaime-gaming/jaigent/security/advisories/new)
rather than opening a public issue.

Include what you found, how to reproduce it, and what an attacker could achieve.
You can expect an initial response within a few days.

Particularly interested in:

- **Sandbox escapes** — any way to make a file tool read or write outside the workspace.
- **Shell blocklist bypasses** that go beyond the documented limits of `run_command`.
- **Key leakage** — any path where an API key reaches stdout, a log, or disk.

## Threat model

Knowing what jaigent does and does not defend against will save you time.

**Defended:**

- File tools cannot leave the workspace. Relative traversal, absolute paths and
  symlinks pointing outside are rejected before any I/O happens.
- Shell execution is absent from the toolset unless explicitly enabled.
- API keys are read from the environment or a git-ignored `.env`, never written to
  disk by jaigent, and masked in all output including `jaigent config`.
- A failing tool cannot crash a run or leak a stack trace to the user; errors are
  returned to the model as text.

**Not defended — by design:**

- **Prompt injection from fetched content.** `fetch_page` returns untrusted text from
  the open web. A malicious page can try to instruct the model. Content is stripped and
  truncated, never executed, but do not combine `--allow-shell` with untrusted browsing.
- **A model you enabled the shell for.** `--allow-shell` grants command execution in the
  workspace. The blocklist prevents accidents, not a determined adversary.
- **What the model chooses to send.** File contents the agent reads are sent to your LLM
  provider. Don't point the workspace at a directory containing secrets.
- **Your provider's handling of your data.** That is between you and them; jaigent adds
  no intermediary.

## Good practice

- Run in a dedicated directory, not `$HOME` or `/`.
- Keep the workspace under version control so you can review and revert changes.
- Start with `--verbose` to see what the agent actually does.
- Leave `--allow-shell` off unless you need it and trust the task.
- Use a scoped API key with a spending limit.
