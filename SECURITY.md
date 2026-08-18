# Security Policy

## Supported versions

**Every released version is supported.** jaigent is small enough that backporting a
security fix costs little, and abandoning users on an older minor to save that effort
is not a trade worth making. Security patches are released for all of the below.

| Version | Released | Supported | Notes |
| --- | --- | --- | --- |
| 0.5.x | 2026-08-18 | ✅ | Current. Checkpoints, failover, standalone binaries. |
| 0.4.x | 2026-08-18 | ✅ | API gateway, auto model routing, Gemini. |
| 0.3.x | 2026-08-18 | ✅ | Skills, settings, schedules. |
| 0.2.x | 2026-08-18 | ✅ | Streaming, cost reporting, approvals. |
| 0.1.x | 2026-08-18 | ✅ | Initial release. |

Fixes land on `main` first and are backported to every affected minor as a patch
release. If you are pinned to an old version and a fix cannot be backported cleanly,
say so on the advisory and it will be handled individually.

### Supported Python versions

jaigent supports **every Python that upstream still supports**: 3.10, 3.11, 3.12 and
3.13. CI runs the full suite against all four on Linux, and against 3.12 on macOS and
Windows. The standalone binaries bundle their own interpreter, so they work with no
Python installed at all.

Upgrade with `pip install --upgrade jaigent`, or re-run the installer script, then
confirm with `jaigent --version`. `jaigent doctor` will tell you if anything is wrong.

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
- **Gateway authentication flaws** — any way to reach `jaigent serve` without a valid
  key, to recover a key from `keys.json`, or to make one caller see another's data.

## Threat model

Knowing what jaigent does and does not defend against will save you time.

**Defended:**

- File tools cannot leave the workspace. Relative traversal, absolute paths and
  symlinks pointing outside are rejected before any I/O happens.
- Shell execution is absent from the toolset unless explicitly enabled.
- Provider API keys are read from the environment or a git-ignored `.env`, never
  written to disk by jaigent, and masked in all output including `jaigent config`.
  `jaigent settings set` refuses to store a secret at all.
- Gateway keys (`jgt-…`) are stored only as SHA-256 hashes, in a file created with
  owner-only permissions. The plain text is shown once, at creation. Comparison is
  constant-time, so timing cannot reveal a valid prefix.
- `jaigent serve` binds `127.0.0.1` by default and refuses to start with no keys
  unless you pass `--no-auth` explicitly.
- Skills and custom commands are prompt text, never code. Loading one cannot execute
  anything; it can only add words to the conversation.
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
- **Anyone who can reach an exposed gateway.** A `jgt-` key grants full agent access —
  file tools, web access, and the shell if you enabled it — inside the server's
  workspace, billed to your provider account. Treat one like a production credential.
  Binding `jaigent serve` to `0.0.0.0`, or running it with `--no-auth` anywhere other
  than a trusted machine, hands that access to your whole network.
- **Scheduled tasks.** They run unattended with approval forced to `auto`, so they can
  write files without anyone confirming.

## Good practice

- Run in a dedicated directory, not `$HOME` or `/`.
- Keep the workspace under version control so you can review and revert changes.
- Start with `--verbose` to see what the agent actually does.
- Leave `--allow-shell` off unless you need it and trust the task.
- Use a scoped API key with a spending limit.
- Keep `jaigent serve` on loopback unless you have put real authentication and TLS in
  front of it. Issue one gateway key per application so you can revoke them
  individually with `jaigent keys revoke`, and check `jaigent keys list` for calls you
  do not recognise.
