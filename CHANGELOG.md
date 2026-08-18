# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.5.3] - 2026-08-18

The first CI run this project has ever had went red. Everything here is a fix
for something it found — all of it on Windows, none of it visible locally.

### Fixed

- **Paths shown to the model used the native separator.** On Windows
  `list_files` reported `src\app.py` while every path the model writes uses
  `/`, leaving it to guess which convention applied. The same strings are keys
  in the checkpoint index, so a change of separator would orphan an entry.
  Everything relative is now rendered with forward slashes.
- **The dangerous-command blocklist did nothing on Windows.** Every rule was
  written for a POSIX shell — `rm -rf /`, `mkfs`, `sudo` — none of which mean
  anything to `cmd.exe`, which is what `shell=True` actually runs there.
  Added rules for formatting a drive, recursive deletes of a drive root,
  `diskpart`, deleting shadow copies, deleting `HKLM` keys, taking ownership of
  a drive and wiping free space. They are anchored to a command position, so
  `echo format c: is dangerous` is not refused — a blocklist that blocks
  ordinary work teaches people to switch it off.
- **The model was never told which shell it was writing for.** The `run_command`
  description now names it, so a model on Windows knows that `;`, `>&2` and
  `ls` will not do what it expects.
- **Shell scripts could be checked out with CRLF line endings.** Git on Windows
  converts by default, which makes `#!/usr/bin/env sh\r` an invalid interpreter
  on Linux and trips shellcheck's SC1017 on every line. A `.gitattributes` now
  pins `*.sh` to LF, and `*.ps1` to CRLF.
- **`install.sh` failed its own lint job.** SC2016 fired on the `$PATH` in the
  profile line it prints — deliberately literal, now marked as such.

### Internal

- shellcheck runs in the test suite via `shellcheck-py`, so an installer
  mistake is caught before a push rather than by CI afterwards.
- Test failures become GitHub annotations, so they show up on the pull request
  diff instead of only inside a job log.
- Three tests were quietly passing for the wrong reason on Windows: one set
  only `HOME` when `Path.expanduser` reads `USERPROFILE` there, one assumed
  POSIX shell syntax, and one shelled out to whatever `bash` was on PATH — which
  on a Windows runner is the WSL stub, with no distribution installed.

## [0.5.2] - 2026-08-18

### Added

- **A release workflow that refuses to ship something broken.** Pushing a `v*` tag
  builds a standalone executable on five runners — Linux x64 and arm64, macOS Intel and
  Apple Silicon, and Windows — and attaches them to the release along with the wheel,
  the sdist and `checksums.txt`. It stops before publishing if the tag disagrees with
  the version in the source (checked *before* the builds run, so a mistyped tag costs
  seconds rather than twenty minutes), if a binary will not start, if an archive does
  not survive being re-extracted and run, or if any asset is missing or empty. The
  Linux images are pinned to the oldest supported release so the binaries run on older
  distributions, and the macOS images are pinned so `-latest` moving to ARM cannot
  silently drop the Intel build.
- **`Agent.on_tool_start`**, a hook that fires just before a tool runs, with its name
  and arguments.

### Changed

- **Markdown is rendered once streaming finishes.** Streaming has to print each chunk
  the moment it arrives, which is far too early to know where a code fence, list or
  table ends, so what you watched was raw markup. The streamed text is now erased and
  redrawn as rendered markdown in place. It is left alone when output is piped, when
  colour is off, and when the answer is taller than the window — that has already
  scrolled, and rewinding would erase the wrong lines.
- **The status line now names the tool while it runs**, not after it has finished.
- **The status line fits any terminal.** It used to overflow narrow windows, wrap, and
  leave a stale row behind on every frame. The trailing metadata is now dropped a piece
  at a time until what is left fits, and the verb is kept longest.
- Durations reach into hours (`2h 5m`) and token counts into millions (`1.2M`).

### Fixed

- **A segfault on every error path.** The background update-check thread was joined
  only on the success path; every error branch returned straight out of `main()`,
  leaving a daemon thread mid-TLS-handshake when the interpreter tore down. It crashed
  roughly a third of error-path runs. The join now happens in a `finally` block, so it
  covers the configuration, `JaigentError`, interrupt and unexpected-exception paths.
- **`settings set` could write a value that broke every later command.** Values were
  type-checked but never validated, so `settings set provider notreal` was accepted and
  written to a file read at every startup, after which `run`, `models` and `route` all
  failed. Values are now checked against the known providers, approval modes and search
  backends; empty strings are refused, counts must be positive, and temperature must be
  in range. A rejected value is not written at all.
- **The Windows build would have failed outright.** The PyInstaller spec pointed at
  `packaging/icon.ico`, which was not committed, and PyInstaller aborts rather than
  skipping a missing icon. The icon is now committed — generated from shapes by
  `packaging/make_icon.py`, in the same terracotta as the terminal logo — and the spec
  degrades to no icon rather than failing the build.

### Internal

- `tests/test_terminal_render.py` drives the output through a real terminal emulator
  and asserts on the resulting screen instead of on the escape sequences emitted. The
  first cut of the markdown rewind passed every string-level assertion and still left
  debris on screen.
- `tests/test_packaging.py` executes the PyInstaller spec with stubs, so spec bugs
  surface without a build — the Windows executable is only ever produced on a Windows
  runner.
- `tests/test_workflows.py` parses both workflows, checks the job graph, shell-checks
  every `run:` block, and asserts that the assets the publish step requires are the
  ones the build matrix actually produces.
- `pyproject.toml` and `jaigent.__version__` are now asserted to agree, and the
  changelog to mention the current version.

## [0.5.1] - 2026-08-18

### Added

- **Update checking.** jaigent notices when a newer release exists and tells you once,
  after the command you ran has finished. The check runs at most once a day, in a
  background daemon thread with a 3-second timeout, and every failure is swallowed —
  being offline or rate-limited never slows a command down or breaks it. Opt out with
  `JAIGENT_NO_UPDATE_CHECK=1`, `NO_UPDATE_NOTIFIER=1`, or by running in CI, which is
  detected automatically. The notice is suppressed when output is piped.
- **`jaigent update`.** Installs the newest release. It detects how this copy was
  installed — standalone binary, pip, pipx or a source checkout — and uses the right
  method for each: pip upgrades with pip, a binary re-runs the platform installer, and
  a source checkout is told to `git pull` rather than being touched. `--check` reports
  without installing; `-y` skips the confirmation prompt.
- `jaigent doctor` now reports how jaigent was installed and whether it is current.

### Fixed

- **Frozen binaries crashed on startup.** `rich` builds the name of its unicode width
  table at runtime, so no static analysis could find it and the frozen binary died with
  `ModuleNotFoundError: rich._unicode_data.unicode17-0-0` the first time it measured a
  wide character — which the logo does immediately. All 22 tables are now bundled, and
  the release smoke test renders the logo so this cannot regress unnoticed.
- **Shared options before the subcommand were misparsed.** `jaigent --workspace /tmp
  tools` read `/tmp` as the command name and failed with a confusing "invalid choice"
  error. Leading options are now moved after the subcommand, which is what most people
  type. `--help`, `--version` and `--logo` keep their top-level behaviour.
- **An invalid `--workspace` was accepted silently**, surfacing later as a confusing
  sandbox error. A missing directory, or a path that is a file, is now rejected up
  front with an explanation.
- **An ambiguous checkpoint id silently picked one.** `jaigent rewind 1` would match
  several checkpoints and restore an arbitrary one. Since restoring is destructive, it
  now lists the candidates and asks for more characters.
- **`jaigent route ""` reported a routing decision for an empty prompt.** It now exits
  2 with a usage hint.

## [0.5.0] - 2026-08-18

### Added

- **Checkpoints and undo.** Every file-modifying tool call snapshots the files it is
  about to change, *before* the approval prompt, so even an approved change is
  reversible. New commands `jaigent undo`, `jaigent checkpoints` (`--clear`) and
  `jaigent rewind <id>`, plus `/revert`, `/diff`, `/checkpoints` and `/rewind` in chat.
  The store is content-addressed, capped at 100 checkpoints, prunes unreferenced
  objects and skips files over 5 MB. Disable with `--no-checkpoints` or
  `JAIGENT_CHECKPOINTS=0`.
- **Provider failover.** Transient failures (408, 429, 500, 502, 503, 504, 529,
  timeouts, connection errors) retry with exponential backoff and jitter, then chain
  to the next provider that has a usable key. Client errors such as 400 and 401 fail
  immediately instead of wasting retries. Configure with `--retries` or
  `JAIGENT_RETRIES`; disable with `JAIGENT_FAILOVER=0`.
- **Standalone binaries.** Releases now ship a self-contained executable for Windows
  x64, macOS (Intel and Apple Silicon) and Linux (x64 and arm64), with no Python
  required. One-line installers for every platform verify the published SHA-256
  checksum before installing.
- **`jaigent doctor`.** Diagnoses environment, provider, storage and features, and
  exits non-zero when something is wrong.
- **New chat commands** `/status`, `/approve <mode>`, `/commands` and `/doctor`.
- **`jgt`** installed as a short alias for `jaigent`.
- **Security auditing in CI.** `bandit` and `pip-audit` run on every push.

### Changed

- **Every released version is now supported.** `SECURITY.md` no longer marks 0.1.x,
  0.2.x or 0.3.x end-of-life; security fixes are backported to all of them.
- CI now covers Python 3.10 through 3.13 on Linux, and both the oldest and newest
  supported versions on macOS and Windows.
- The shell blocklist is regex-based and normalises whitespace and case, so
  `RM  -RF  /` is caught. It now also covers `sudo`, piping a download into a shell,
  force pushes, reads of `~/.ssh` and `/etc/shadow`, and `chown`/`chmod` on `/`.

### Security

- **`fetch_page` no longer reaches private networks.** It previously followed any
  http(s) URL the model produced, including `http://169.254.169.254/…`, which returns
  cloud credentials on most VMs — reachable by a prompt injection from a fetched page.
  Loopback, link-local, private, reserved and metadata addresses are now rejected,
  hostnames are resolved and every resulting address checked, and redirects are
  followed manually so each hop is validated.
- **Credential files are no longer world-readable.** The `.env` written by
  `jaigent init` was created with mode 644, exposing the API key to every user on the
  machine. Both it and the gateway key store now use owner-only permissions, applied
  before any content is written.

### Fixed

- Checkpoint ids could collide when two tool calls landed in the same millisecond,
  which made `undo` rewind the wrong step.
- A malformed entry in the checkpoint index raised `AttributeError` and made the
  whole undo history unreadable; bad entries are now skipped.
- `CheckpointStore.list` shadowed the `list` builtin inside the class body, breaking
  type annotations; it is now `CheckpointStore.history`.
- `jaigent undo` and `/revert` always restored the newest checkpoint without consuming
  it, so running either twice re-applied the same revert instead of stepping back a
  second change. They now discard the checkpoint they restored.

## [0.4.0] - 2026-08-18

### Added

- **Animated status line.** A live spinner with a rotating verb ("Pondering…",
  "Reticulating…"), elapsed time, running token count and the tool currently
  executing. Thirty phrases, tool-specific verbs, and a clean teardown the moment
  streamed text starts arriving.
- **Auto model selection.** `--model auto` scores the prompt for length, code blocks,
  multi-step phrasing and difficulty keywords, buckets it into simple/standard/complex,
  and picks the cheapest capable model. `jaigent route <prompt>` explains the decision
  without spending anything.
- **Your own API.** `jaigent serve` exposes the agent as an OpenAI-compatible endpoint
  at `/v1/chat/completions` and `/v1/models`. Works unmodified with the official OpenAI
  SDK. Responses include a `jaigent` block reporting tools used and estimated cost.
- **Gateway keys.** `jaigent keys new|list|revoke` issues `jgt-` credentials, stored as
  SHA-256 hashes with owner-only file permissions and compared in constant time.
- **Google Gemini** as a first-class provider, with a dedicated adapter for its
  `generateContent` protocol: message translation, tool-schema cleaning, SSE streaming
  and usage normalisation.
- **Grok and DeepSeek** expanded — Grok 4, Grok 3 and Grok 3 mini, DeepSeek V3 and R1,
  all with prices and auto-routing entries.
- **Custom commands.** Markdown prompt templates in `.jaigent/commands` become slash
  commands in chat and on the shell, with `$ARGUMENTS`, `$1`/`$2` and `$WORKSPACE`
  placeholders. Managed with `jaigent commands list|show|new|remove`.
- **Windows support.** Per-user files now resolve to `%APPDATA%\jaigent` on Windows and
  honour `XDG_CONFIG_HOME` elsewhere, centralised in a new `paths` module. Unicode
  glyphs fall back to ASCII on consoles that cannot encode them.

### Changed

- Verbose mode prints tool calls and results as styled lines instead of raw stderr text.
- The spinner is suppressed automatically when output is piped or `--no-color` is set.
- xAI's default model is now `grok-4`.
- `SECURITY.md` documents supported versions and the gateway threat model.

### Fixed

- An injected provider is no longer replaced when auto routing changes the model, which
  had made the router untestable without a network.
- "write all the tests" and "security audit" were scored below their real difficulty.

## [0.3.0] - 2026-08-18

### Added

- **Skills** — reusable markdown instruction packs in `.jaigent/skills` (project) and
  `~/.jaigent/skills` (personal). Only their descriptions enter the system prompt; the
  body is fetched on demand through a new `load_skill` tool, so a large library costs
  almost no context. Managed with `jaigent skills list|show|new|remove`.
- **Schedules** — run a prompt on a timer with `jaigent schedule`. Intervals accept
  `30m`, `every 2h`, `hourly`, `daily`, `daily at 09:00` and `weekly`. `schedule run`
  executes only what is due (safe for cron), `--watch` keeps a worker alive, and each
  task records its last result. Scheduled runs force `auto` approval since nobody is
  there to answer a prompt.
- **Persistent settings** — `jaigent settings set|unset|list|path`, stored per project
  (`./.jaigent/settings.json`) or per user (`~/.jaigent/settings.json`). Five-layer
  precedence: CLI flags, environment, project file, user file, defaults. Secrets are
  refused by design.
- **Eight more providers**: OmniRoute, OpenRouter, Groq, DeepSeek, Mistral, xAI,
  Together and Ollama, all sharing the OpenAI-compatible adapter.
- **OmniRoute support** — defaults to the local gateway at `http://localhost:20128/v1`,
  uses the `auto` model, and needs no API key. Override with `OMNIROUTE_BASE_URL`.
- **`jaigent models`** — browse the curated catalogue of tool-calling models with
  prices, filtered by `--only <provider>` or a search term.

### Changed

- `Settings(provider=...)` now adopts that provider's own default model and base URL
  instead of OpenAI's.
- Local providers (OmniRoute, Ollama) no longer demand an API key.

### Fixed

- A scheduled task with `next_run` of exactly `0` was treated as unscheduled and
  silently rescheduled instead of running.
- Relative times such as "in 2h" no longer round down to "in 1h".

## [0.2.0] - 2026-08-18

### Added

- **Custom ASCII-art logo** with the `ai` in j-**ai**-gent picked out in the accent
  colour. Shown by `jaigent` with no arguments, as the `jaigent chat` header, and on
  demand via `jaigent --logo`. It picks one of three sizes to fit the terminal and
  drops colour when piped or under `--no-color`.
- **Streaming responses.** Assistant text is printed as it is generated, for both the
  OpenAI and Anthropic backends, including reassembly of tool-call arguments that
  arrive as fragments. Disable with `--no-stream`.
- **Cost and token reporting** after every turn, with a built-in price table for
  common OpenAI and Anthropic models. Override it with `JAIGENT_PRICES`, or hide the
  line with `--no-cost`.
- **Approval before destructive actions.** Interactive runs show a coloured diff and
  ask before any `write_file`, `edit_file`, `delete_file` or `run_command`. Answer
  `always` to stop asking for that tool. `--yes` skips the prompts, `--dry-run`
  refuses every mutation, and non-interactive runs default to `--yes` so scripts do
  not hang.
- **Sessions.** Conversations are saved to `~/.jaigent/sessions` and resumed with
  `jaigent chat --resume`. New `jaigent sessions` command lists and deletes them.
- **Slash commands in chat**: `/help`, `/reset`, `/tools`, `/model`, `/workspace`,
  `/cost`, `/save`, `/undo`, `/exit`.
- **`jaigent init`** — interactive setup that chooses a provider, stores the key in
  a git-ignored `.env`, and makes a live test call to confirm it works.
- Public API additions: `Approver`, `Mode`, `Cost`, `Session`, `estimate`, plus
  `on_text` and `approver` arguments to `Agent` and `Agent.load_history()`.

### Changed

- The colour scheme is now warm terracotta on soft off-white, replacing cyan/magenta.
- The chat prompt marker is `❯`.
- `--no-color` is accepted before a subcommand as well as after it.

### Fixed

- `[y]es / [n]o` style prompts no longer have their brackets swallowed as rich markup.
- `jaigent init` honours `JAIGENT_BASE_URL` when making its test call.

## [0.1.0] - 2026-08-18

First release.

### Added

- **Agent loop** (`jaigent.Agent`) that plans, calls tools, feeds results back to the
  model and stops on a final answer or a configurable step budget. Returns a full
  trace of every tool call.
- **Web tools**: `web_search` (DuckDuckGo by default, no API key required; Tavily
  optional) and `fetch_page`, which strips HTML to readable text.
- **File tools**: `list_files`, `read_file` (paginated, line-numbered), `write_file`,
  `edit_file`, `search_files` (substring or regex) and `delete_file`.
- **Workspace sandbox**: every path is resolved and verified to be inside the
  configured workspace. Traversal, absolute paths and escaping symlinks are rejected.
- **Opt-in shell tool** `run_command`, disabled unless `--allow-shell` /
  `JAIGENT_ALLOW_SHELL=1` is set, with a timeout and a blocklist of destructive commands.
- **Providers**: OpenAI-compatible chat completions and Anthropic Messages, both with
  native tool calling. Any compatible gateway works via `JAIGENT_BASE_URL`.
- **CLI**: `run` (with a bare-prompt shorthand), `chat`, `tools` and `config`, rendered
  with rich; `--verbose` traces tool calls to stderr.
- **Configuration** through environment variables or a git-ignored `.env` file, with
  CLI flags taking precedence. API keys are masked in all output.
- **Python API** with custom tool registration, extra system instructions and a
  tool-call observer callback.
- Mock OpenAI-compatible server in `examples/` for trying the loop without an API key.
- Test suite of 154 offline tests at ~89% coverage, plus ruff and mypy in CI.

[Unreleased]: https://github.com/jaime-gaming/jaigent/compare/v0.5.3...HEAD
[0.5.3]: https://github.com/jaime-gaming/jaigent/compare/v0.5.2...v0.5.3
[0.5.2]: https://github.com/jaime-gaming/jaigent/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/jaime-gaming/jaigent/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/jaime-gaming/jaigent/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/jaime-gaming/jaigent/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/jaime-gaming/jaigent/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/jaime-gaming/jaigent/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/jaime-gaming/jaigent/releases/tag/v0.1.0
