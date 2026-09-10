# Changelog

All notable changes to jAIgent will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Docs

- New guides in `docs/`: **architecture.md** (how the agent loop, providers,
  failover, tools, approval and undo fit together — the walkthrough to read
  before changing `src/`), **terminal-ui.md** (every element on screen, every
  key it answers to, and the degradation contract for `--no-color`, pipes and
  legacy Windows consoles), and **web-ui-proposal.md** (the local web page
  linked to the CLI — a proposal, nothing built).
- The CI and release guide moved from `.github/README.md` to
  `docs/ci-and-releases.md`, and `.github/README.md` is gone — it was never
  rendered anywhere and was only findable by browsing.
- The README gained a Documentation section indexing the guides;
  CONTRIBUTING.md points new contributors at the architecture walkthrough;
  AGENTS.md documents the docs/ conventions and drops two stale references
  (`.github/release.yml` moved to `.github/workflows/` in 0.5.1, and
  `HELP_TEXT` in `cli.py` is now `CHAT_COMMANDS`).

### Added

- **Runaway tool results are capped.** The registry now enforces a 40,000
  character ceiling on every tool result — the safety net behind the caps
  built-in tools already apply — and tells the model to narrow the request
  (smaller range, tighter pattern) when it hits the cap.
- **Repeated tool calls get a note.** The same tool with the same arguments
  twice in one run returns the same result; the model now finds a note on
  the second result telling it to change something instead of looping to
  the step budget. Observers and step records keep the raw output.
- **`ask_user` is now an arrow-key picker.** With options on a terminal, the
  question renders as a panel of radio options: `↑`/`↓` (or `j`/`k`) move,
  Enter confirms, digits jump-pick, and `Esc` switches to typing a free-form
  answer. The selected marker pulses so the prompt reads as waiting, and once
  answered the panel collapses into a single `✓ question → answer` summary
  line so the transcript stays compact. Piped sessions, `serve`, schedules and
  MCP still get the "nobody can answer" behaviour, and anything the picker
  cannot do (no raw mode, legacy consoles) falls back to the numbered prompt.
- **Every tool call now leaves a quiet trace line.** A turn shows
  `→ Reading files · README.md ✓` per call instead of silence followed by an
  answer; failures are marked with `✗`. `--verbose` still prints the full
  argument dumps.
- An `on_approval` callback on `Agent` (and `Approver.will_prompt`), so UIs
  can pause their animations before an approval prompt appears.

### Changed

- The live status line is now anchored like a status bar: the action sits on
  the left, elapsed time and token count on the right edge, with the tool
  target named next to the verb. Narrow terminals still shed the metadata a
  piece at a time rather than wrapping.
- The spinner, approval prompts and `ask_user` questions no longer fight over
  the screen: the status animation pauses while a question is up, and
  `ask_user` renders on the same console as everything else (it used to build
  a private one, which garbled output during turns).
- `/help` renders as two aligned columns, and `/key [provider] [key]` shows
  its arguments — rich was swallowing the `[...]` as markup.
- Answers are set apart by a blank line from the prompt that caused them, and
  each turn ends with breathing room before the next prompt.

### Fixed

- **Streamed narration no longer runs into the answer.** When a model
  streams text, calls a tool, and then streams the final answer, the two
  texts printed as one run-on line ("…notes.md for The notes say…"). Each
  tool boundary now starts a new paragraph, and the paragraph break counts
  toward the in-place redraw's row math.
- **A notice between chunks no longer garbles the redraw.** A failover
  announcement landing mid-stream used to make the end-of-turn markdown
  redraw erase the wrong rows; when anything the stream does not own has
  been printed, the raw text is left as the output.
- A streamed answer ending in a newline left its raw markdown on screen above
  the rendered redraw — the cursor walk-back under-counted the trailing
  newline's row.
- Glyphs chosen while a rich `Live` is running (the tool trace, spinner
  neighbours) degraded to ASCII on Unicode terminals: rich's `FileProxy`
  hides the stream's encoding. `supports_unicode` now unwraps it.

## [0.5.4] - 2026-09-09

### Fixed

- **`/resume` actually switches backend now.** It used to rename the settings
  while the old provider kept answering — `/status` said anthropic while
  OpenAI billed the turns. The provider is rebuilt through the same path as
  `/provider`; without a key for that backend the chat stays put with a
  warning, and the conversation still resumes.
- **`chat --resume` respects explicit flags.** `--model`, `--provider`,
  `--workspace` and `--base-url` used to lose to the stored session values,
  and resuming silently reset a custom base URL to the provider default.
  Flags win now; provider and model travel together; switching to a keyless
  backend explains itself with the exact `--provider` override or
  `auth set` command that fixes it.
- **Ctrl-C during a custom command stays in chat.** The interrupt escaped to
  `main()` and quit without offering to save; it now stops the run like any
  other turn.
- **The gateway answers 400s instead of hanging up.** A `messages` value that
  was not a list of objects crashed the handler and dropped the connection
  with an empty reply. Malformed shapes — and non-object bodies — are plain
  400s now, bodies over 8 MB are 413s, and a non-string `model` falls back
  to the default.
- **Providers survive mangled responses.** A `null` or missing tool-call
  index in a stream, a non-object in a tool-call list, a non-dict Gemini
  candidate or usage block — all crashed the turn with `TypeError` instead
  of a provider error. Malformed pieces are skipped or coerced everywhere.
- **Tampered session files open instead of crashing.** Non-dict entries in
  `messages` killed resume and `sessions --show`; they are dropped on load,
  and a non-dict `usage` becomes empty.
- **File tools no longer walk the whole tree.** Recursive listing and search
  `sorted()` a full `rglob` — hundreds of thousands of stats under
  `node_modules` — before the result cap could stop them. The walk prunes
  ignored directories without entering them and stops at the cap; the MCP
  resource listing got the same fix.
- **MCP reads work under paths like `~/dist/project`.** The ignored-directory
  check ran over the *absolute* path, so any noise word in the workspace path
  refused every read. It checks workspace-relative parts now.
- **A routed provider switch never reuses the old key.** `apply_routing`
  kept the previous backend's key when the new one had none; the key is now
  cleared so the provider fails loudly instead of cross-billing.
- **`models --refresh` gathers in parallel.** Providers were queried one
  after another, so the command could wait out every timeout in sequence.
- **MCP stays silent when it should.** Unknown JSON-RPC notifications used to
  get an error response with a null id; notifications now never get a response
  at all, and batch requests return a single response array (or nothing, for
  an all-notification batch).
- **`jaigent update` works behind system certificate stores.** GitHub requests
  now use the operating system trust store, fixing the misleading "could not
  reach GitHub" result seen when `curl` worked but Python's bundled CA list did
  not. Corrupt update-cache timestamps are ignored safely.
- **CI smoke tests no longer hide crashes.** The health check is still allowed
  to report its expected missing-key status, but exits above 1 now fail CI.
  Release asset uploads also only fall back to an existing release after
  confirming that the release exists.

### Changed

- **The model can ask you a question.** The new `ask_user` tool interrupts the
  run with its own panel and numbered options, so a genuine choice never looks
  like more streamed text to skim past. Where nobody can answer (`serve`,
  schedules, pipes), the model is told that and proceeds with its best
  judgment; MCP never exposes the tool, and `serve`/schedules run it in
  non-interactive mode so it can never block on stdin.
- **Failover narrates itself.** Retries and provider switches are announced as
  they happen ("openai hit a rate limit — retrying…", "Continuing on
  anthropic…"), a run stopped early by the spend cap or the step budget gets a
  panel explaining what hit and what to do next, and run failures are
  translated into plain language with a next step instead of raw provider
  errors.
- **`/settings` and `/status` speak plainly.** Labels read as what they are
  ("Working folder", "File changes") and values as what they mean ("Ask me
  first", "Saved"), with the commands that change them underneath.
- **Closing the terminal keeps the chat.** SIGHUP/SIGTERM during `jaigent chat`
  save an unsaved conversation quietly instead of losing it; there is nobody
  left to ask, so keeping beats dropping. `--no-save` still disables it.
- **`jaigent update` was rewritten around channels and honesty.** A source
  checkout is compared against the channel branch (`main`, or `beta` with
  `--beta`/`JAIGENT_BETA=1`), ahead/behind is counted, and the update is a
  fetch plus fast-forward merge plus reinstalling the editable install — never
  a PyPI upgrade that could move a checkout backwards. A checkout that is only
  ahead reports "nothing to pull"; a feature branch is refused with
  instructions; a missing channel branch is named with a push hint instead of
  being reported as a connection failure; rate limits and absent releases are
  reported as what they are.
- **Friendlier chat opening.** The startup screen no longer presents a box of
  provider, model, workspace, approval and internal settings. Those details
  remain available when explicitly requested with `/settings` or `/status`.
- **Chat asks before it saves on exit.** Ctrl-D, Ctrl-C at the prompt and
  `/exit` now offer to save an unsaved conversation; turns are no longer
  silently persisted after every message. `/save` remains available for an
  immediate save.


### Release pipeline

- Publishing 0.5.3 to PyPI took three attempts to get right, and each failure
  was quieter than the one before it. The trusted-publishing exchange was
  refused (`invalid-publisher`) because the PyPI project did not match the
  `environment:pypi` claim the job presents; then a conditional
  `user: ${{ … || '' }}` input passed an empty *string*, which the upload
  action reads as "credentials supplied" and answers by turning Trusted
  Publishing off — so it uploaded nothing behind a green tick. The upload is
  now two steps selected by whether `PYPI_API_TOKEN` exists, and the job ends
  by asking pypi.org whether the version really landed.
- Installers: an unverifiable checksum is fatal, a failed install is reported,
  and the Windows installer moves a locked `jaigent.exe` aside instead of
  failing. `jaigent update` aims the installer at the binary you are running
  and no longer reports success it has not verified.

### Security

- **`jaigent serve --no-auth --host 0.0.0.0` is refused.** Gateway requests run
  with approvals forced to `auto`, so an unauthenticated gateway on a reachable
  interface was remote control of the workspace, billed to your provider
  account. `SECURITY.md` warned about the combination; `ServerConfig.validate`
  now rejects it (exit 78) and says how to fix it. Loopback is unaffected, and
  an authenticated server may still bind anywhere.
- **The installers no longer install an unverified binary.** `install.sh`
  installed anyway when neither `sha256sum` nor `shasum` existed, and
  `install.ps1` treated *any* failure to fetch `checksums.txt` — including an
  intercepted connection — as "no checksum published, skipping verification".
  Both are fatal now, as is a `checksums.txt` with no entry for the asset.
  A failed `mv`/`Copy-Item` is reported instead of leaving no binary behind an
  "Installed" message.

### Fixed

- **A binary update replaces the binary you are running.** Both installers
  default to their own directory (`~/.local/bin`, `%LOCALAPPDATA%`), so an
  update from anywhere else installed a second copy and left the shell running
  the old one. `jaigent update` now passes `JAIGENT_BIN_DIR`, and the Windows
  installer renames a locked `jaigent.exe` aside instead of failing.
- **A failed `pip install -e .` after a source pull is an error.** The tree was
  new and the import was old, and it printed "Updated successfully".
- **`pipx` is run through this interpreter** when it is installed there, because
  the `pipx` on `PATH` may belong to a different Python than the app it is
  upgrading; it falls back to `PATH` when it is not.

- **`jaigent update` no longer reports success it has not earned.** The
  upgrade command's exit code was the whole verdict, so "Already up to date"
  on a source checkout, a pip run with nothing newer on the index, and a
  stale binary first on `PATH` all printed *Updated successfully*. The
  installed CLI is now asked its version after the upgrade and compared with
  the one before; anything else exits 1 and names the cause, listing every
  other `jaigent` on `PATH` with its version.
- **A source checkout on a feature branch is refused** instead of quietly
  pulling that branch and calling it an update.
- **`pipx upgrade` has a fallback.** pipx refuses to upgrade an app that did
  not come from a registry; it is now reinstalled with
  `pipx install --force`, from PyPI and then from git.
- **The PyPI publish job states its own outcome.** v0.5.3 failed the
  trusted-publishing exchange (`invalid-publisher`: the PyPI project did not
  match the `environment:pypi` claim the job presents) and nothing said what
  to change. The job now prefers a `PYPI_API_TOKEN` secret when one is set,
  pins `pypa/gh-action-pypi-publish` to a commit, keeps `skip-existing: false`
  so a duplicate version fails loudly, and ends by asking
  `pypi.org/pypi/jaigent/<version>/json` whether the version actually landed.
  A failure writes the exact publisher settings to the job summary, and
  re-running the failed job republishes the tag without rebuilding anything.
  See `.github/README.md`.
- **The release workflow is a valid workflow file again.** `secrets` in a
  step's `if:` makes GitHub reject the *whole file*: every push failed with
  zero jobs started, so no tag could release. The PyPI job now publishes the
  token's presence as `HAS_PYPI_API_TOKEN` in its environment and branches on
  that instead. (`publish-token`/`publish-trusted` also became
  `publish_token`/`publish_trusted`, so the `steps.….outcome` reads cannot be
  misread as a subtraction.)

## [0.5.3] - 2026-09-08

The public 0.5.3 cut: orange jAI chrome, every saved chat, `jaigent auth`,
live model gathering, and a release pipeline that publishes binaries plus
the PyPI wheel.

### Added

- **`jaigent auth`.** Store a provider API key in `~/.jaigent/secrets.env`
  (owner-only) so it works from any directory. `jaigent auth set openai sk-…`,
  `list`, `unset`. `/key` in chat does the same. `jaigent init` writes this
  file as well as a project `.env`.
- **Orange jAI mark** as the app icon (`packaging/icon.ico` / README).
- **`/key` and `/settings`** in chat. `/key` stores a provider secret and
  never sends it to the model.
- **Clickable** provider consoles, settings paths, and markdown links.
- **Old sessions.** `jaigent sessions` lists every saved chat (not just the
  last 20). `jaigent sessions --show <id>` prints the transcript.
  `/sessions` and `/resume <id>` work in chat. Resume reprints recent turns.
- **Live model gathering.** `jaigent models --refresh` asks every provider
  you have a key for for its current `/models` list, caches it, and merges it
  with the catalogue.
- **Beta channel.** `jaigent settings set beta true` (or `jaigent update --beta`)
  **pushes** the current source checkout to `origin/beta` (then pip users
  install `git+…@beta`). `jaigent update --stable` or `settings set beta false`
  returns to `main`.
- **Working animation** is a braille orbit plus a travelling pulse bar.
  The line says **Thinking**, **Reading files**, **Editing files** or
  **Searching files** (with the path or query) as those tools run.

### Changed

- **CLI chrome** uses the jAI mark colours (`#FF8A00` / `#E85D04` on warm
  ivory), rounded tables, a thinner chat banner, a rule under the splash,
  an orange pulse on the wait line, and an accent bullet on the turn footer.
- **Chat answers render as markdown** after streaming. Empty Enter does not
  send; a trailing `\\` continues the line; paths like `/tmp/notes.md` are
  prompts, not slash commands.
- **Missing-key errors** name the console URL and `jaigent auth set` /
  `jaigent init`, not only the env var.
- **CI / Release** jobs have timeouts; smoke tests run `jaigent providers`.
  Packaging tests no longer need PyInstaller installed.
- Install docs lead with `pip install jaigent`.

### Fixed

- **`jaigent init` crashed in `C:\\WINDOWS\\System32`.** PowerShell often
  starts there; writing `.env` is refused and the key stays in the user
  secrets file instead.
- **Windows path tests** treated a mocked Linux `sys.platform` as NT because
  `is_windows()` also read `os.name`.
- **Markdown hyperlink tests** on Windows consoles that do not emit OSC-8.
- **API keys could not be pasted into the console.** `jaigent init` used a
  hidden password prompt that swallowed pastes on many terminals. Input is
  visible, and `--api-key` / `jaigent auth set` skip the prompt entirely.
- **Wheel layout.** The hatch config now maps `src/` so `pip install jaigent`
  imports `jaigent`, not `src.jaigent`.
- **Release workflow publishes to PyPI** via Trusted Publishing, so
  `pip install jaigent` can resolve from pypi.org once the publisher is
  connected.

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

- **`jaigent mcp`.** Serves jAIgent's tools over stdio to ChatGPT, Claude
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

[Unreleased]: https://github.com/jaime-gaming/jaigent/compare/v0.5.4...HEAD
[0.5.4]: https://github.com/jaime-gaming/jaigent/compare/v0.5.3...v0.5.4
[0.5.3]: https://github.com/jaime-gaming/jaigent/compare/v0.5.2...v0.5.3
[0.5.2]: https://github.com/jaime-gaming/jaigent/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/jaime-gaming/jaigent/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/jaime-gaming/jaigent/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/jaime-gaming/jaigent/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/jaime-gaming/jaigent/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/jaime-gaming/jaigent/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/jaime-gaming/jaigent/releases/tag/v0.1.0
ub.com/jaime-gaming/jaigent/releases/tag/v0.1.0
