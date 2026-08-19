# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.5.2] - 2026-08-19

The work after 0.5.1: it links into ChatGPT and Claude, picks free models,
loads local plugins, caps spend, optionally remembers, and wears the
terracotta wordmark again. OmniRoute is gone. Identity is **all your agents
in one place**.

### Removed

- **OmniRoute.** Provider, catalogue, env vars and current docs. Use Ollama
  locally or OpenRouter for a one-key gateway. Historical 0.3.0 still names it.

### Added

**Linking**

- **`jaigent mcp`.** Serves jaigent's tools over stdio to ChatGPT, Claude
  Desktop and any MCP client. Read-only by default; `--allow-write` /
  `JAIGENT_MCP_WRITE=1` opts into write tools. `run_command` is never
  exposed. The client supplies the model, so no API key is needed.
- **MCP resources and prompts.** Workspace files as resources (secrets
  skipped), skills and commands as prompts, protocol versions through
  2025-11-25, tool titles and server instructions. `jaigent mcp --print-config
  claude|chatgpt` prints a ready-to-paste snippet.

**Models and update**

- **`jaigent providers`.** Every backend and the URL to mint a key.
  `jaigent init` shows the same URLs. Chat has `/provider`.
- **`--model free`.** Ollama first, then Groq, Gemini and OpenRouter `:free`.
  `jaigent models --free` lists them; `jaigent route --free "…"` previews.
- Together and Ollama routing tables, Together catalogue entries, OpenRouter
  `:free` models.
- **`jaigent update` reports source sync.** A matching version tag is not
  enough: the checkout is compared to GitHub `main`. Source installs
  `git pull --ff-only` then `pip install -e .`.

**Extend**

- **Plugins.** A Python file in `.jaigent/plugins` with
  `register(registry, settings)`. `jaigent plugins list|new|remove`. Local
  files only; a broken plugin is skipped; `register` gets redacted settings.
- **Built-in skills `spend-cap` and `compact`.** Hard USD stop:
  `jaigent settings set budget 0.50`. `/compact` collapses older chat turns.
  `auto_compact` does it automatically.
- **Optional project memory.** Off until `jaigent settings set memory true`.
  Tools `remember` / `recall`; notes in `.jaigent/memory.md`.

### Changed

- **Terracotta wordmark is back.** Six-row block letters on the README and in
  the terminal, `❯` prompt, unicode glyphs with ASCII fallbacks on cp1252.
  Positioning is the research-and-write loop you keep *next to* Claude Code /
  Cursor / ChatGPT, not a replacement.
- **`COMMANDS` tuple** includes `mcp` and `providers`, so they are not
  rewritten to `run …`.

### Fixed

**Leaks**

- **File tools could send secrets to the model.** `.env`, `id_rsa`, `*.pem` /
  `*.key` and `.git` are refused. MCP already skipped them; both paths share
  one helper. `.env.example` stays readable.
- **Plugins received a live `Settings.api_key`.** `register` now gets
  redacted settings.
- **Session files were world-readable.** They go through `write_private`.
- **`/provider` reused the previous key** when the new provider had none.
  `key_for_provider` no longer prefers `JAIGENT_API_KEY` for every backend.

**Undo**

- **Undo history listed older first when timestamps collided.** Windows
  ``time.time()`` often stays put for several captures; ``history()`` then
  kept load order. Insertion order is now the tie-break, and each capture
  is stamped strictly after the previous one.
- **``jaigent chat --resume`` could open the older session** when two were
  saved in the same clock tick. Listing now tie-breaks on session id.

**Routing**

- **Failover reused the primary key and URL.** Each hop now gets that
  provider's own model, base URL and env-var key.
- **`--model auto` dropped failover.** `set_model` and `/model` keep the
  wrapper.
- **Custom commands could shadow `/provider`.** `RESERVED` lists every
  built-in slash command.

**Providers and MCP**

- **MCP tool calls skipped the registry.** Calls go through
  `ToolRegistry.call`; `ping`, `resources/list` and `prompts/list` are
  answered; stdout is forced to UTF-8.
- **Anthropic and Gemini rejected parallel tool results.** Consecutive
  results are coalesced.
- **Reasoning models rejected `max_tokens`.** `o1`/`o3`/`o4`/`gpt-5` send
  `max_completion_tokens`.
- **OpenRouter unidentified traffic.** Requests send `HTTP-Referer` and
  `X-Title`.
- **`OpenAIProvider._stream`** retried without `stream_options` when a
  compatible gateway rejected `include_usage` (Ollama, older vLLM).
- **`1.0` was treated as older than `1.0.0`.** Version compare pads missing
  parts.
- **`jaigent update` lied when GitHub was unreachable.** It now says it
  could not reach GitHub. Requests send a `User-Agent`.
- **The update confirmation hid `pip install -e .`.** Source upgrades always
  reinstall after `git pull --ff-only`; the prompt shows both steps.
- **Up-to-date pip/binary installs said "source are in sync".** They now
  say "You're up to date."
- **Release workflows** (apply with `./scripts/activate-ci.sh` — the
  automation token cannot push `.github/workflows/`): CLI smoke test pinned
  to bash, Windows binary smoke test exits 0, Intel runner is
  `macos-15-intel`.

### Internal

- `test_the_commands_tuple_covers_every_subparser` so a new subcommand cannot
  silently become `run`.
- Release workflow repairs stay in `scripts/activate-ci.sh` (and
  `docs/workflow-repair-v0.5.1.md`): the automation token cannot push
  `.github/workflows/`.

## [0.5.1] - 2026-08-18

Re-issued as one release. 0.5.2 and 0.5.3 were tagged while the release
pipeline could not publish binaries, so none of their changes were ever
downloadable — everything they contain is folded in here. This is the first
version that ships as a standalone binary, and the first with a release
pipeline that works: the Windows smoke tests no longer fail builds that are
fine, and the Intel build runs on `macos-15-intel` because `macos-13` was
retired and its jobs never left the queue.

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

- **`jaigent init` is harder to trip up.** An unrecognised provider answer is
  announced rather than silently replaced; a key pasted with wrapping quotes or
  a `Bearer ` prefix is cleaned instead of stored broken; an empty paste gets
  one more try instead of throwing away every answer; a model that is not in
  the catalogue is confirmed before it is written; and the "get a key" link now
  covers every provider that has one.

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

- **Read-only tools filled the undo history.** `paths_for_tool` decided what to
  snapshot by looking at the *argument* name, so `list_files(path=".")` and
  `read_file(path="x")` each wrote a checkpoint. A three-step task left eight
  entries, six of which revert nothing, and `undo` had to be pressed once per
  read before it reached a real change. `MUTATING_TOOLS` is now the single
  source of truth — the same set that decides what needs approval.
- **`undo` could be spent on a checkpoint that changes nothing.** Re-running a
  task writes identical content, so the newest checkpoint often reverts to the
  state the file is already in. `undo` printed "nothing to revert", consumed it
  anyway, and left the user pressing undo watching nothing happen. It now walks
  back to the most recent change that actually differs, and says how many it
  skipped.
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

- shellcheck runs in the test suite via `shellcheck-py`, so an installer
  mistake is caught before a push rather than by CI afterwards.
- Test failures become GitHub annotations, so they show up on the pull request
  diff instead of only inside a job log.
- `tests/test_end_to_end.py` assembles the real thing — a real `Agent`, the real
  tool registry writing to a real directory, a real `CheckpointStore`, and only
  the model faked. Every other test isolates one piece, and both checkpoint bugs
  above were invisible to all of them.
- `undo`, `checkpoints`, `rewind`, `init`, `doctor` and `update` had no test
  that went through `cli.main`. That is the gap that let `undo` ship broken
  twice — the store was well covered, the command was not. `cli.py` coverage
  goes from 80% to 88%, including that `jaigent init` writes its `.env`
  owner-only.
- Three tests were quietly passing for the wrong reason on Windows: one set
  only `HOME` when `Path.expanduser` reads `USERPROFILE` there, one assumed
  POSIX shell syntax, and one shelled out to whatever `bash` was on PATH — which
  on a Windows runner is the WSL stub, with no distribution installed.
- `scripts/activate-ci.sh` now repairs the workflows as well as moving them:
  ci.yml read the release workflow from its pre-move path, its CLI smoke test
  used `|| true` where GitHub gives Windows PowerShell, and the release
  workflow's Windows smoke test threw on the first non-zero exit because
  PowerShell 7.4 turns those into terminating errors. Each repair is idempotent.
  They cannot be committed from here: GitHub refuses any push from an
  automation account that touches `.github/workflows/`.

- **The Windows CI smoke test runs under bash.** The step says `doctor || true`;
  on Windows the default shell is pwsh, to which the runner prepends
  `$ErrorActionPreference = 'stop'`. `doctor` exits 1 without an API key, pwsh
  then tried to run `true` — not a command there — and the step aborted on both
  Windows runners however well jaigent behaved. bash is present on every image.
- **The Windows release smoke test decides its own exit code.** The runner
  appends `exit $LASTEXITCODE` to every pwsh step, and the last native command
  the step ran was `doctor`, which exits 1 on purpose — so the step reported
  failure and the build of a perfectly good `jaigent.exe` was discarded. The
  step now clears that path and also sets
  `$PSNativeCommandUseErrorActionPreference = $false`, so a non-zero exit can
  never become a terminating error before the step's own checks allow it.
- **The Intel build moved from `macos-13` to `macos-15-intel`.** `macos-13` was
  retired in December 2025; a job asking for a retired image is never picked
  up, so the release run hung in "queued" until cancelled. `macos-15-intel` is
  GitHub's designated successor for x86_64 macOS builds.
- Regression tests cover all three: the CLI smoke test must be pinned to bash,
  the Windows smoke test must end with its own `exit 0`, and no matrix runner
  may name a retired image.

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

[Unreleased]: https://github.com/jaime-gaming/jaigent/compare/v0.5.2...HEAD
[0.5.2]: https://github.com/jaime-gaming/jaigent/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/jaime-gaming/jaigent/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/jaime-gaming/jaigent/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/jaime-gaming/jaigent/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/jaime-gaming/jaigent/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/jaime-gaming/jaigent/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/jaime-gaming/jaigent/releases/tag/v0.1.0
