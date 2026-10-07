# Security Policy

## Supported versions

**Supported versions:** 0.5.x (current beta). Older minors (0.1–0.4) are no longer supported and will not receive security patches. Upgrade with `pip install --upgrade jaigent`.

| Version | Released | Supported | Notes |
| --- | --- | --- | --- |
| 0.5.x | 2026-09-11 | ✅ | Current beta candidate: 0.5.7 prepared; latest published: 0.5.6. Auth, binaries and remote MCP. |
| 0.4.x | 2026-08-18 | ❌ | End of support. Use 0.5.x or later for security patches. |
| 0.3.x | 2026-08-18 | ❌ | End of support. Use 0.5.x or later for security patches. |
| 0.2.x | 2026-08-18 | ❌ | End of support. Use 0.5.x or later for security patches. |
| 0.1.x | 2026-08-18 | ❌ | End of support. Use 0.5.x or later for security patches. |

Fixes land on `main` first and are released as patch releases for 0.5.x. Older versions (0.1–0.4) are not backported.

### Supported Python versions

jaigent supports **every Python that upstream still supports**: 3.10, 3.11, 3.12 and
3.13. CI runs the full suite against all four on Linux, and against 3.10 and 3.13 on
macOS and Windows. The standalone binaries bundle their own interpreter, so they work with no
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
- Provider API keys are read from the environment, a git-ignored `.env`, or
  `~/.jaigent/secrets.env` (owner-only, written by `jaigent auth` / `init`).
  They are masked in all output including `jaigent config`.
  `jaigent settings set` refuses to store a secret at all.
- Gateway keys (`jgt-…`) are stored only as SHA-256 hashes, in a file created with
  owner-only permissions. The plain text is shown once, at creation. Comparison is
  constant-time, so timing cannot reveal a valid prefix.
- `jaigent serve` binds `127.0.0.1` by default and refuses to start with no keys
  unless you pass `--no-auth` explicitly. `--no-auth` on an interface other than
  loopback is refused outright: requests run with approvals forced to `auto`, so
  an unauthenticated gateway on a reachable address is remote control of the
  workspace. Bind loopback, or create a key.
- Skills and custom commands are prompt text, never code; loading one cannot execute
  anything. Their descriptions/templates/bodies can still be sent to the connected
  model, so do not store secrets in them. Skill and command discovery ignores
  symlinked files and directories, preventing prompt links from reading outside
  their configured directories. Project-memory reads and writes also reject
  symlink paths and credential targets.
- A failing tool cannot crash a run or leak a stack trace to the user; errors are
  returned to the model as text.
- Every file the agent modifies is snapshotted first, so an unwanted change can be
  reverted with `jaigent undo`. The snapshot is taken before the approval prompt, so
  a change you approved and then regretted is recoverable too. Restoring cannot
  write outside the workspace: a `../..` or absolute path in a hand-edited
  checkpoint index is skipped rather than followed.
- Commands are screened against a blocklist covering recursive deletes of `/` or `~`,
  raw disk writes, filesystem formats, fork bombs, `sudo`, piping a download into a
  shell, force pushes, reads of `~/.ssh` and `/etc/shadow`, and machine shutdown.
  Screening runs on a whitespace-normalised, lower-cased form of the command, so
  `RM  -RF  /` is treated the same as `rm -rf /`.
- Provider failures cannot silently leak your prompt to an unintended provider: the
  fallback chain only includes providers for which *you* have configured a key.
- Dependencies are audited by `pip-audit` and the source by `bandit` on every CI run,
  across all supported Python versions. The default runtime dependency list is two packages
  (`httpx`, `rich`); the inbound ChatGPT MCP endpoint is optional and adds `joserfc`
  only when installed with `jaigent[chatgpt]`.
- The inbound ChatGPT MCP listener binds to loopback; every `tools/call`
  requires a valid OAuth JWT. Tool discovery and initialization are public so
  ChatGPT can scan the app. The bridge checks signature, issuer, audience,
  expiry and scope against the configured issuer's discovery data and JWKS, and
  verifies the existing keyed gateway's safety capabilities. It refuses
  gateways with shell access. Non-read-only tools are exposed only when the
  owner explicitly enables bridge writes **and** the gateway is writable; the
  bridge rechecks that policy before each mutating call. Built-in direct file
  tools use the workspace sandbox and checkpoint store when checkpoints are
  enabled. Local plugins are executable trusted Python, not sandboxed; review
  them or disable plugins before exposing the bridge. `serve --read-only` also
  removes every tool not explicitly declared `read_only=True` and every tool
  marked `dangerous=True`. That declaration is not a sandbox.
- ChatGPT-created sessions use the existing JSON session store and carry an
  integration origin. The remote list excludes unrelated CLI sessions, IDs are
  validated before loading, and same-session requests are serialized. The local
  CLI can still list/resume these sessions; this is a single-owner boundary,
  not per-user isolation.
- The gateway's authenticated `/v1/models` response reports only whether its agent
  is read-only and whether shell is enabled. This lets the bridge check the actual
  gateway safety configuration without introducing another capability endpoint.
- Release binaries are built by CI from a tagged commit, never from a developer's
  machine, and published with SHA-256 checksums. Both installer scripts verify the
  checksum and abort on a mismatch.
- `fetch_page` refuses to reach the local machine or a private network. Cloud
  metadata endpoints (`169.254.169.254` and friends), loopback, link-local, private
  and reserved ranges are all rejected. The hostname is resolved first and every
  address it maps to is checked, so a public-looking name pointed at `127.0.0.1`
  is caught too, and each redirect hop is re-checked so a public URL cannot bounce
  inward. This matters because the model may be acting on instructions from a page
  it just read.
- Files containing credentials — the `.env` written by `jaigent init` and the
  gateway key store — are created with owner-only permissions, set before the first
  byte is written rather than fixed up afterwards.
- The update check is read-only and contacts nothing but the GitHub releases API. It
  sends no identifying information, and `jaigent update` never installs anything
  without an explicit command (and, on a terminal, a confirmation).

**Not defended — by design:**

- **Prompt injection from fetched content.** `fetch_page` returns untrusted text from
  the open web. A malicious page can try to instruct the model. Content is stripped and
  truncated, never executed, but do not combine `--allow-shell` with untrusted browsing.
- **A model you enabled the shell for.** `--allow-shell` grants command execution in the
  workspace. The blocklist prevents accidents, not a determined adversary.
- **What the model chooses to send.** File contents read by the local agent are sent to
  the configured LLM provider. For ChatGPT's direct MCP tools, requested file/page
  contents are returned to ChatGPT as tool results. Provider API keys stay on the
  jAIgent host, but read access still shares data with the connected model service.
  Don't point the workspace at a directory containing secrets.
- **Your provider's handling of your data.** That is between you and them; jaigent adds
  no intermediary.
- **Anyone who can reach an exposed gateway.** A `jgt-` key grants full agent access —
  file tools, web access, and the shell if you enabled it — inside the server's
  workspace, billed to your provider account. Treat one like a production credential.
  Binding `jaigent serve` to `0.0.0.0` hands that access to your whole network,
  which is why it requires a key; `--no-auth` is only ever accepted on loopback.
- **Sharing one ChatGPT bridge among untrusted users.** OAuth authenticates and
  authorizes access to the bridge, but all authorized connections still use the same
  configured jAIgent gateway, workspace and provider account. The bridge is
  single-owner, not a multi-tenant service. Restrict the OAuth issuer/scope to trusted
  users; do not publish a shared instance as if users had isolated workspaces.
- **HTTPS reverse-proxy mistakes and traffic exhaustion.** `jaigent chatgpt` speaks
  plain HTTP on loopback; the operator is responsible for TLS termination and must
  not publish the backend port directly. Resource metadata, initialization and
  tool discovery are intentionally public; only tool calls require OAuth. The
  standard-library listener has no per-client rate limiter. Configure request-size,
  connection/read timeouts and rate limits at the reverse proxy, and keep its
  loopback listener inaccessible from the network. Only register the public HTTPS
  MCP URL in ChatGPT.
- **Scheduled tasks.** They run unattended with approval forced to `auto`, so they can
  write files without anyone confirming. Their changes are still checkpointed.
- **Side effects of shell commands.** Checkpoints cover files touched through the
  agent's own file tools. A `run_command` invocation could change anything on the
  machine, so nothing is snapshotted for it and `undo` cannot help. Commit before
  running a task with `--allow-shell`.
- **Exfiltration to a public host.** The SSRF guard stops the agent reaching *inward*.
  It cannot stop a model from sending workspace contents to an attacker-controlled
  public URL if a prompt injection convinces it to. Review what the agent fetches
  when the task involves untrusted pages.
- **File permissions on Windows.** POSIX mode bits do not exist there; NTFS
  inheritance governs access to `.env` and the key store instead.
- **A hostile GitHub release.** `jaigent update` runs the official installer script,
  which verifies the published checksum — but anyone who can publish a release to this
  repository can publish a binary. That is the same trust you extend to any package
  manager. Pin a version if that matters to you.
- **A compromised provider endpoint.** If you point `--base-url` at a host you do not
  control, it sees every prompt and can return anything, including tool calls the
  agent will then execute. Only use base URLs you trust.

## Good practice

- Run in a dedicated directory, not `$HOME` or `/`.
- Keep the workspace under version control so you can review and revert changes.
- Start with `--verbose` to see what the agent actually does.
- Leave `--allow-shell` off unless you need it and trust the task.
- Use a scoped API key with a spending limit.
- Run `jaigent doctor` after installing or upgrading; it checks key configuration,
  storage permissions and which providers are actually reachable.
- Verify the checksum if you download a release binary by hand. The installer scripts
  do this for you.
- Keep `jaigent` up to date. Every version is supported, but fixes land in the newest
  patch release first.
- Keep `jaigent serve` on loopback unless you have put real authentication and TLS in
  front of it. For an inbound ChatGPT connection, prefer `jaigent serve --read-only`,
  leave `--allow-shell` off, and publish only `jaigent chatgpt` through an HTTPS
  reverse proxy. Issue one gateway key per application so you can revoke them
  individually with `jaigent keys revoke`, and check `jaigent keys list` for calls you
  do not recognise.
