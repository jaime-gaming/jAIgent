# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/jaime-gaming/jaigent/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/jaime-gaming/jaigent/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/jaime-gaming/jaigent/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/jaime-gaming/jaigent/releases/tag/v0.1.0
