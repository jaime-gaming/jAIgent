<div align="center">

```
     ██╗  █████╗  ██╗  ██████╗  ███████╗ ███╗   ██╗ ████████╗
     ██║ ██╔══██╗ ██║ ██╔════╝  ██╔════╝ ████╗  ██║ ╚══██╔══╝
     ██║ ███████║ ██║ ██║  ███╗ █████╗   ██╔██╗ ██║    ██║
██   ██║ ██╔══██║ ██║ ██║   ██║ ██╔══╝   ██║╚██╗██║    ██║
╚█████╔╝ ██║  ██║ ██║ ╚██████╔╝ ███████╗ ██║ ╚████║    ██║
 ╚════╝  ╚═╝  ╚═╝ ╚═╝  ╚═════╝  ╚══════╝ ╚═╝  ╚═══╝    ╚═╝
        searches the web · writes your files
```

</div>

# jaigent

The agent that **looks it up**, **writes it down**, and **lets you undo**.

Claude Code, Cursor and ChatGPT are great at the file you already have open.
jaigent is the one you keep *next to them*: it searches the live web, writes
into a sandboxed folder, and snapshots every change so `jaigent undo` puts the
disk back. Bring your own key. No account, no telemetry, no hosted backend.

It also **plugs into the others**. `jaigent mcp` hands the same tools to
ChatGPT and Claude Desktop. `jaigent serve` is an OpenAI-compatible endpoint
your apps already know how to call. You do not have to leave your editor agent
to get a researcher that can touch files.

```console
$ jaigent "find the current stable Python version and save a note about it to python.md"

  → web_search(query='current stable Python version')
  → fetch_page(url='https://www.python.org/downloads/')
  → write_file(path='python.md', content='# Python …')

Saved python.md with the current stable release and its date.
Source: https://www.python.org/downloads/
```

---

## Contents

- [Why jaigent](#why-jaigent)
- [Install](#install)
- [Get an API key](#get-an-api-key)
- [Usage](#usage)
- [Undo anything](#undo-anything)
- [Staying up to date](#staying-up-to-date)
- [Failover](#failover)
- [Auto model selection](#auto-model-selection)
- [Free models](#free-models)
- [Your own API](#your-own-api)
- [Skills](#skills)
- [Plugins](#plugins)
- [Custom commands](#custom-commands)
- [Schedules](#schedules)
- [Settings](#settings)
- [Tools](#tools)
- [Configuration](#configuration)
- [Python API](#python-api)
- [Adding your own tool](#adding-your-own-tool)
- [Providers and models](#providers-and-models)
- [Safety model](#safety-model)
- [Development](#development)
- [License](#license)

---

## Why jaigent

Most coding agents live inside one editor and one model. jaigent is a
**research-and-write loop you can run from anywhere**, then **wire into the
tools you already use**.

| You already have… | Keep it. Add jaigent when you need… |
| --- | --- |
| Claude Code / Cursor / Aider | Live web → a file on disk, with undo after you said yes |
| ChatGPT or Claude Desktop | The same tools, over MCP, without giving those apps a shell |
| An app on the OpenAI SDK | `jaigent serve` — one URL, hashed `jgt-` keys, tools included |
| A local Ollama / Groq free tier | `--model free` so a greeting does not cost a refactor |
| Several API keys | Failover: a 429 on OpenAI continues on Anthropic, then Ollama |

What is actually different:

- **Undo is the product.** Every write is snapshotted *before* the approval
  prompt. `jaigent undo`, `rewind <id>`, `/revert`. Other agents ask; jaigent
  also lets you change your mind afterwards.
- **Web + files in one loop.** `web_search` → `fetch_page` → `write_file`.
  It is not a chat wrapper and not a repo-only coder.
- **It links instead of replacing.** MCP for ChatGPT and Claude Desktop;
  `serve` for anything that speaks OpenAI; skills, plugins and slash commands
  as local files you can commit.
- **Any of ten providers, or none.** OpenAI, Anthropic, Gemini, DeepSeek, Grok,
  Groq, Mistral, OpenRouter, Together, Ollama. `--model auto` sizes the job;
  `--model free` picks a no-cost model you can actually reach.
- **A hard spend cap.** `jaigent settings set budget 0.50` stops the run.
  Built-in `spend-cap` and `compact` skills; `/compact` in chat; memory only
  if you turn it on.
- **Conservative by default.** Workspace sandbox, no shell unless you pass
  `--allow-shell`, secret files refused, no telemetry. One binary if you do
  not want Python.
- **Small enough to read.** The agent loop is one function. No framework.

## Install

### Standalone binary (no Python needed)

The release page ships a single self-contained executable per platform. It bundles
its own interpreter, so there is nothing to install and nothing to conflict with.

**macOS and Linux**

```bash
curl -fsSL https://raw.githubusercontent.com/jaime-gaming/jaigent/main/packaging/install.sh | sh
```

**Windows** (PowerShell)

```powershell
irm https://raw.githubusercontent.com/jaime-gaming/jaigent/main/packaging/install.ps1 | iex
```

Both scripts verify the published SHA-256 checksum before installing and refuse to
continue if it does not match. Or download the archive yourself from
[Releases](https://github.com/jaime-gaming/jaigent/releases) — binaries are built for
Windows x64, macOS (Intel and Apple Silicon) and Linux (x64 and arm64).

### From PyPI

```bash
pip install jaigent
```

### From source

Requires Python 3.10, 3.11, 3.12 or 3.13.

```bash
git clone https://github.com/jaime-gaming/jaigent.git
cd jaigent
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -e .
```

Then set it up — this picks a provider, stores your key in `.env` and makes a test call:

```bash
jaigent init
```

Or verify an existing setup:

```bash
jaigent --version
jaigent doctor      # check keys, storage, providers — tells you what is wrong
jaigent tools       # list what the agent can do
jaigent config      # show the resolved configuration
```

`jgt` is installed as a shorter alias for the same command.

## Get an API key

jaigent has no key of its own — you supply one.

| Provider | Where to get a key | Environment variable |
| --- | --- | --- |
| OpenAI (default) | <https://platform.openai.com/api-keys> | `OPENAI_API_KEY` |
| Anthropic | <https://console.anthropic.com/settings/keys> | `ANTHROPIC_API_KEY` |
| OpenRouter | <https://openrouter.ai/keys> | `OPENROUTER_API_KEY` |
| Groq | <https://console.groq.com/keys> | `GROQ_API_KEY` |
| Together | <https://api.together.xyz/settings/api-keys> | `TOGETHER_API_KEY` |
| xAI (Grok) | <https://console.x.ai> | `XAI_API_KEY` |
| Ollama | none — runs locally | — |

> Don't want to pay? Use Ollama locally, or OpenRouter's free models with `--model free`.

Set it for the current shell:

```bash
export OPENAI_API_KEY='sk-...'
```

…or keep it in a `.env` file next to your project, which jaigent loads automatically:

```bash
cp .env.example .env
$EDITOR .env
```

> `.env` is already in `.gitignore`. Never commit a key. Real environment variables always win over `.env`.

Confirm the key is picked up (it is printed masked, never in full):

```bash
jaigent config
```

Web search uses DuckDuckGo by default and needs **no** second key.

## Usage

### One-off tasks

```bash
jaigent "what changed in the latest Node.js LTS? write it to node-lts.md"
jaigent "read all .py files here and list every TODO comment"
jaigent "compare the pricing pages of Vercel and Netlify and make a table in pricing.md"
```

`jaigent <prompt>` is shorthand for `jaigent run <prompt>`.

### Interactive chat

```bash
jaigent chat
```

Conversation history is kept across turns. In-session commands: `/reset` clears history, `/tools` lists tools, `/exit` quits.

### Useful flags

```bash
jaigent "audit this repo" --workspace ~/projects/api   # point at another directory
jaigent "research X" --verbose                         # trace every tool call
jaigent "explain this code" -m gpt-4o                  # pick a model
jaigent "run the tests and fix what fails" --allow-shell
```

### Commands

| Command | What it does |
| --- | --- |
| `jaigent init` | Interactive setup: pick a provider, store a key, test it. |
| `jaigent run <prompt>` | Run one task and exit. |
| `jaigent chat` | Interactive session with memory. |
| `jaigent sessions` | List saved conversations. |
| `jaigent skills` | Create and manage reusable instruction packs. |
| `jaigent plugins` | Create and manage local tool plugins. |
| `jaigent providers` | List providers and where to get an API key. |
| `jaigent commands` | Create and manage custom slash commands. |
| `jaigent serve` | Expose the agent as an OpenAI-compatible API. |
| `jaigent keys` | Create and revoke keys for that API. |
| `jaigent route` | Show which model auto mode would pick, and why. |
| `jaigent undo` | Revert the agent's most recent file change. |
| `jaigent checkpoints` | Browse the undo history; `--clear` empties it. |
| `jaigent rewind <id>` | Restore a specific checkpoint. |
| `jaigent doctor` | Diagnose install, keys, storage and providers. |
| `jaigent update` | Install the newest release; `--check` only reports. |
| `jaigent schedule` | Run prompts on a timer. |
| `jaigent settings` | Read and write persistent settings. |
| `jaigent models` | Browse models known to support tool calling. |
| `jaigent mcp` | Start an MCP server over stdio for ChatGPT and Claude. |
| `jaigent tools` | List the tools available to the agent. |
| `jaigent config` | Show resolved settings; exits `1` if no API key is set. |
| `jgt` | Short alias for `jaigent`. |
| `jaigent` | No arguments: logo, examples and a pointer to `--help`. |
| `jaigent --logo` | Print the logo on its own. |

The logo adapts to your terminal: full block letters when there is room, a compact
three-row wordmark in narrow windows, and a single line below ~28 columns. Colour is
dropped automatically with `--no-color` or when you pipe the output to a file, so
`jaigent --logo --no-color > banner.txt` gives you the wordmark without colour.

## Reviewing changes before they happen

The agent writes to your disk, so by default an interactive run shows you a diff and
waits before every file change or command:

```console
╭───────────────────── write_file ─────────────────────╮
│ notes.md                                             │
│ --- a/notes.md                                       │
│ +++ b/notes.md                                       │
│ @@ -1,2 +1,3 @@                                      │
│ -# Old title                                         │
│ +# Python 3.13                                       │
│ +Released 2024-10-07.                                │
╰──────────────────────────────────────────────────────╯
Apply this change? [y]es / [n]o / [a]lways / [q]uit:
```

`always` stops asking for that one tool for the rest of the run. Declining is reported
back to the model, so it adapts instead of retrying blindly.

| Flag | Behaviour |
| --- | --- |
| *(default, interactive)* | Ask before each file change or command. |
| `-y`, `--yes` | Apply everything without asking. |
| `--ask` | Force the prompts on, even when piped. |
| `--dry-run` | Refuse every mutation; the agent may only read and search. |

When output is **not** a terminal the default flips to `--yes`, so scripts and CI never
hang waiting for an answer nobody can type.

## Streaming and cost

Answers stream token by token as the model produces them. While a reply is arriving you
see it as raw markdown — nothing else is possible, since a code fence or a table is only
recognisable once it ends — and the moment the answer is complete it is redrawn in
place, rendered. Add `--no-stream` to wait for the complete reply instead.

Piped output is never redrawn, so `jaigent "..." > answer.md` gets the markdown source.

After every turn jaigent prints what it used:

```
2 tool calls · 2,895 tokens (2,460 in / 435 out) · ~$0.0006
```

Prices for common OpenAI and Anthropic models are built in; unknown models show tokens
only. Override them with a JSON file if you need exact figures:

```bash
export JAIGENT_PRICES=~/prices.json   # {"my-model": {"input": 1.5, "output": 3.0}}
```

Hide the line with `--no-cost`.

## Sessions

Conversations are saved automatically to `~/.jaigent/sessions` and can be picked up later.

```bash
jaigent chat                      # a new session, saved on exit
jaigent chat --resume             # continue the most recent one
jaigent chat --resume 20260818-093000
jaigent chat --no-save            # don't persist this one

jaigent sessions                  # list them
jaigent sessions --delete <id>    # or --delete all
```

Inside `chat`:

| Command | Effect |
| --- | --- |
| `/help` | List these commands. |
| `/reset` | Clear the conversation. |
| `/tools` | Show available tools. |
| `/model <name>` | Switch model mid-session. |
| `/provider <name>` | Switch provider (and its own key) mid-session. |
| `/workspace <path>` | Point the file tools somewhere else. |
| `/cost` | Tokens and spend for the session so far. |
| `/save` | Write to disk now. |
| `/undo` | Drop the last exchange. |
| `/status` | Provider, model, workspace and session at a glance. |
| `/approve <mode>` | `ask`, `auto` or `dry-run`. |
| `/commands` | List custom slash commands. |
| `/doctor` | Check keys, storage and providers. |
| `/compact` | Collapse older turns into a short summary. |
| `/memory` | Show project memory (off until `settings set memory true`). |
| `/exit` | Quit. |

## Undo anything

Before the agent writes to a file, jaigent snapshots it. The snapshot is taken
*before* the approval prompt, so a change you approved and then regretted is just
as reversible as one you never saw.

```console
$ jaigent "tidy up the imports across the project"
  → edit_file(path='src/app.py')
  → edit_file(path='src/utils.py')

$ jaigent undo
  revert  src/utils.py
✓ reverted 1 file(s) to just now (edit_file src/utils.py)
```

Each `undo` consumes the checkpoint it restored, so running it repeatedly walks
back through the run one change at a time. `rewind` leaves history alone.

Browse further back and jump to any point:

```console
$ jaigent checkpoints
  ID        When       Tool        Files
  4f2a91c3  just now   edit_file   src/utils.py
  4f2a91b7  1m ago     edit_file   src/app.py
  4f2a90e2  4m ago     write_file  README.md

  3 checkpoints · 12.4 KB

$ jaigent rewind 4f2a90e2      # an unambiguous prefix is enough
```

In chat, the same thing without leaving the conversation:

| Command | Does |
| --- | --- |
| `/revert` | undo the last file change on disk |
| `/diff` | show what `/revert` would change |
| `/checkpoints` | list restorable points |
| `/rewind <id>` | go back to a specific one |

The store is content-addressed, so unchanged bytes are stored once. It keeps the
last 100 checkpoints, prunes objects nothing references, and skips files over 5 MB
(they are recorded as skipped rather than silently missed). Everything lives in
`.jaigent/checkpoints` inside the workspace — delete it and nothing else breaks.

Turn it off with `--no-checkpoints`, `JAIGENT_CHECKPOINTS=0`, or
`jaigent settings set checkpoints false`.

> **Not a substitute for version control.** Checkpoints cover files the agent
> touched through its own tools. They do not track `run_command` side effects,
> because a shell command could change anything. Commit before a big run.

## Staying up to date

jaigent tells you when a new release exists, once, after whatever you were doing has
finished:

```console
$ jaigent tools
  ...

jaigent 0.6.0 is available (you have 0.5.1). Run `jaigent update` to upgrade.
```

The check runs at most once a day in a background thread with a three-second timeout,
and every failure is ignored. Being offline, behind a proxy, or rate-limited by GitHub
never slows a command down or breaks it — and the notice is suppressed when output is
piped, so it cannot corrupt a script.

Upgrading picks the right method for how you installed:

```console
$ jaigent update
  installed  0.5.1 (standalone binary)
  location   /home/you/.local/bin/jaigent
  latest     0.6.0  ← new

  Install 0.6.0? This runs: sh -c curl -fsSL .../install.sh | sh
  [y/N]
```

| Installed via | `jaigent update` runs |
| --- | --- |
| standalone binary | the platform installer script, replacing the binary |
| `pip` | `pip install --upgrade jaigent` |
| `pipx` | `pipx upgrade jaigent` |
| source checkout | `git pull --ff-only` then `pip install -e .` |

`--check` reports without installing, and `-y` skips the prompt. To turn the passive
check off entirely, set `JAIGENT_NO_UPDATE_CHECK=1`. It is also skipped automatically
when `CI` is set.

## Failover

A provider being down should not end your run. When a request fails, jaigent
decides whether the failure is worth retrying:

- **Retryable** — 408, 429, 500, 502, 503, 504, 529, timeouts, connection errors,
  "overloaded". Retried with exponential backoff and jitter, so a fleet of clients
  does not stampede a recovering API.
- **Not retryable** — 400, 401, 403, 404. A malformed request fails identically on
  the second try, so jaigent fails immediately and tells you why.

After exhausting retries on one provider, it moves to the next one that has a
usable key, and keeps the answer coming:

```console
$ jaigent "summarise the changelog"
  ! openai failed (HTTP 529 overloaded) — falling back to anthropic
  …
```

`jaigent doctor` shows the chain that is actually available to you:

```console
Provider
  ✓ provider    openai
  ✓ api key     set
  ✓ model       gpt-4o-mini
  ✓ failover    3 provider(s) usable: openai, anthropic, ollama
```

Tune it with `--retries N`, `JAIGENT_RETRIES`, or turn it off with
`JAIGENT_FAILOVER=0`. A local Ollama counts as a fallback with no key
at all, which makes it a good last resort.

## Auto model selection

`--model auto` sizes the model to the job. A greeting does not need what a refactor
needs, and paying Opus rates to answer "hi" adds up.

```bash
jaigent -m auto "hi"                                   # → gpt-4.1-nano
jaigent -m auto "refactor this package and add tests"  # → gpt-4o
jaigent settings set model auto                        # make it the default
```

Ask what it would pick without spending anything:

```console
$ jaigent route "why does this deadlock under load?"

  prompt      why does this deadlock under load?
  difficulty  complex (score 5)
  signals     causal question, concurrency
  model       gpt-4o via openai
```

The router scores the prompt for length, code blocks, multi-step phrasing and
difficulty keywords, buckets it into **simple / standard / complex**, and picks the
cheapest model in your provider that clears the bar. It is a transparent heuristic in
[`router.py`](src/jaigent/router.py), not a second LLM call — paying a model to choose a
model would defeat the point.

Auto works for OpenAI, Anthropic, Gemini, DeepSeek, Grok, Groq, Mistral, OpenRouter,
Together and Ollama.

## Free models

`--model free` walks the providers you can actually use and picks a no-cost model
sized to the task. Ollama comes first, then Groq, Gemini
and OpenRouter's `:free` ids.

```bash
jaigent -m free "summarise README.md"
jaigent models --free                 # the catalogue
jaigent route --free "refactor this"  # preview, spend nothing
jaigent settings set model free       # make it the default
```

You still need a key for Groq, Gemini or OpenRouter. Ollama needs none.

## Your own API

`jaigent serve` turns the agent into an OpenAI-compatible endpoint. Your apps get one
URL and one key; behind it, jaigent picks the model, searches the web and uses its
tools before answering.

```bash
jaigent keys new my-app       # prints jgt-… once
jaigent serve                 # http://localhost:8787/v1
```

Any OpenAI client works unmodified:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8787/v1", api_key="jgt-...")

reply = client.chat.completions.create(
    model="auto",
    messages=[{"role": "user", "content": "research X and summarise it"}],
)
print(reply.choices[0].message.content)
```

Responses carry a `jaigent` block alongside the standard fields, so callers can see
what actually happened:

```json
"jaigent": {
  "tool_calls": 2,
  "tools_used": ["web_search", "write_file"],
  "estimated_usd": 0.00042
}
```

Keys are stored **hashed**; the plain text exists only when it is printed. Manage them
with `jaigent keys list` and `jaigent keys revoke <name>` — one key per app makes
revocation painless.

> **A `jgt-` key is a production credential.** It grants full agent access — files, the
> web, and the shell if you enabled it — billed to your provider account. `serve` binds
> `127.0.0.1` by default; keep it there unless you have put real auth and TLS in front.

## Skills

A skill is a saved procedure: markdown you write once and the agent reuses. Only the
one-line *descriptions* go into the system prompt — the body is fetched with the
`load_skill` tool when the model decides it is relevant, so a large library costs
almost nothing in context.

```bash
jaigent skills new changelog -d "Write a release changelog from git history"
jaigent skills list
jaigent skills show changelog
```

That creates `.jaigent/skills/changelog.md`:

```markdown
---
name: changelog
description: Write a release changelog from git history.
---

Read the git log since the last tag, group the commits by type, and write the
result to CHANGELOG.md following the Keep a Changelog format.
```

Now `jaigent "write the changelog for this release"` will pick it up on its own.

Two skills ship with jaigent: **`spend-cap`** (how to stay cheap before a hard
USD stop) and **`compact`** (what to keep when the chat gets long). You cannot
`jaigent skills remove` a built-in skill.

Skills in `./.jaigent/skills` belong to the project and can be committed so the whole
team shares them; `~/.jaigent/skills` (or `jaigent skills new --user`) holds personal
ones. A project skill shadows a user skill of the same name. Skills are plain prompt
text — loading one can never execute code.

### Spend cap, compact, memory

```bash
jaigent settings set budget 0.50      # hard stop once this run would cost $0.50
jaigent settings set auto_compact true
jaigent settings set memory true      # remember/recall tools + .jaigent/memory.md
```

`budget` is enforced in the agent loop, not just suggested. `/compact` in chat
collapses older turns without another model call. Memory stays off until you
turn it on — nothing is written or sent until then.

## Plugins

A plugin is a local Python file that registers extra tools. Unlike skills, plugins
**are** code — only files you put in `.jaigent/plugins` (project) or
`~/.jaigent/plugins` (personal) are loaded, never anything from the network.

```bash
jaigent plugins new wordcount
jaigent plugins list
```

That creates `.jaigent/plugins/wordcount.py` with a `register(registry, settings)`
hook. Edit it to add a real tool. A broken plugin is skipped so it cannot take
down a run. Turn them off with `JAIGENT_PLUGINS=0`.

## Custom commands

Save a prompt template as markdown and it becomes a slash command everywhere.

```bash
jaigent commands new review -d "Review the working tree" \
  --template 'Run git diff, then review $ARGUMENTS for correctness first, style second.'
```

```console
$ jaigent /review the auth module      # from the shell
› /review the auth module              # or in chat
```

The template understands `$ARGUMENTS` (everything after the command name), `$1`, `$2`
for individual words, and `$WORKSPACE`. Project commands live in `.jaigent/commands` and
can be committed; `--user` puts them in your home directory instead.

Like skills, commands are prompt text — running one can only send a message, never
execute code.

## Schedules

Run a prompt on a timer.

```bash
jaigent schedule add "check my repos for failing CI and write status.md" --every 2h
jaigent schedule add "summarise today's commits" --every "daily at 18:00"

jaigent schedule list
jaigent schedule run              # execute anything due — safe to put in cron
jaigent schedule run --watch      # or keep a worker in the foreground
jaigent schedule run --id task-1  # force one task now
jaigent schedule pause task-1
```

Intervals accept `30m`, `every 2h`, `hourly`, `daily`, `daily at 09:00` and `weekly`.
Each task remembers its own workspace and model, and records the result of its last run
(`jaigent schedule show task-1`).

Scheduled runs are non-interactive, so approval is forced to `auto` — there is nobody
to answer a prompt. Point them at workspaces you are happy to see change. For unattended
use, a cron line is enough, because `schedule run` only executes what is actually due:

```cron
*/15 * * * * cd ~/project && jaigent schedule run >> ~/.jaigent/cron.log 2>&1
```

## MCP: ChatGPT and Claude

jaigent can serve its tools to MCP clients — ChatGPT, Claude Desktop and anything
that speaks the [Model Context Protocol](https://spec.modelcontextprotocol.io) —
over stdio. The client supplies the model, so no API key or provider is needed:
this is purely a tool server.

Read-only tools are exposed by default. Add `--allow-write` (or `JAIGENT_MCP_WRITE=1`)
to also expose write tools. `run_command` is never exposed.

The server also advertises **resources** (workspace files, sandboxed; `.env` and
key files are skipped) and **prompts** (your skills and custom commands). It
negotiates protocol versions through 2025-11-25 and sends tool titles plus a
short instruction block that ChatGPT and Claude Desktop expect.

### Claude Desktop

```bash
jaigent mcp --print-config claude
```

Paste the JSON into `claude_desktop_config.json`, or write it by hand:

```json
{
  "mcpServers": {
    "jaigent": {
      "command": "jaigent",
      "args": ["mcp", "--client", "claude"]
    }
  }
}
```

### ChatGPT

```bash
jaigent mcp --print-config chatgpt
```

When connecting a custom MCP server in ChatGPT, use command `jaigent` with
arguments `mcp --client chatgpt`.

### What you get

The client sees the same tools jaigent's own agent uses — searching the web,
fetching pages, reading and writing files in the current directory — plus the
workspace as resources and your skills as prompts. The update-check notice is
suppressed because stdout is the protocol stream.

## Settings

Persist configuration instead of exporting variables every time.

```bash
jaigent settings set model gpt-4o
jaigent settings set approval ask
jaigent settings set max_steps 20 --project   # commit this one for the team
jaigent settings list
jaigent settings path
```

Five layers, each overriding the one below:

1. CLI flags
2. Environment variables and `.env`
3. Project settings — `./.jaigent/settings.json`
4. User settings — `~/.jaigent/settings.json`
5. Built-in defaults

API keys are refused by `settings set` on purpose: secrets belong in the environment or
a git-ignored `.env`, not in a file you might commit.

Values are checked before they are written. A settings file is read at every startup, so
a value jaigent cannot use would break every later command — including the ones you would
need to put it right:

```console
$ jaigent settings set provider notreal
configuration error: Unknown provider 'notreal'. Expected one of: openai, anthropic,
gemini, openrouter, groq, deepseek, mistral, xai, together, ollama
```

## Tools

The model chooses which of these to call, and in what order.

| Tool | Purpose |
| --- | --- |
| `web_search` | Search the web; returns titles, URLs and snippets. |
| `fetch_page` | Download a page or text document and strip it to readable text. |
| `list_files` | List the workspace, optionally filtered by glob. |
| `read_file` | Read a UTF-8 file with line numbers; paginated for large files. |
| `write_file` | Create or overwrite a file; parent directories are created. |
| `edit_file` | Replace an exact snippet inside an existing file. |
| `search_files` | Grep the workspace by substring or regex. |
| `delete_file` | Delete a file or an empty directory. |
| `run_command` ⚠ | Run a shell command. **Opt-in**, see [Safety model](#safety-model). |

## Configuration

Every setting has an environment variable; CLI flags override it.

| Variable | Default | Meaning |
| --- | --- | --- |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | — | Your provider key. |
| `JAIGENT_API_KEY` | — | Generic key; takes precedence over the two above. |
| `JAIGENT_PROVIDER` | `openai` | One of: openai, anthropic, gemini, openrouter, groq, deepseek, mistral, xai, together, ollama. |
| `JAIGENT_MODEL` | `gpt-4o-mini` | Model id. |
| `JAIGENT_BASE_URL` | provider default | API root — point this at any compatible gateway. |
| `JAIGENT_WORKSPACE` | current directory | Directory the file tools are locked to. |
| `JAIGENT_MAX_STEPS` | `12` | Tool-call budget per turn. |
| `JAIGENT_TEMPERATURE` | `0.2` | Sampling temperature. |
| `JAIGENT_MAX_TOKENS` | `2048` | Tokens per assistant turn. |
| `JAIGENT_TIMEOUT` | `60` | HTTP timeout in seconds. |
| `JAIGENT_SEARCH_BACKEND` | `duckduckgo` | `duckduckgo` (no key) or `tavily`. |
| `TAVILY_API_KEY` | — | Required only for the Tavily backend. |
| `JAIGENT_ALLOW_SHELL` | `0` | Set to `1` to enable `run_command`. |
| `JAIGENT_VERBOSE` | `0` | Trace tool calls. |
| `JAIGENT_STREAM` | `1` | Stream the answer as it is generated. |
| `JAIGENT_SHOW_COST` | `1` | Print the token and cost line after each run. |
| `JAIGENT_APPROVAL` | tty-dependent | `ask`, `auto` or `dry-run`. |
| `JAIGENT_SESSION_DIR` | `~/.jaigent/sessions` | Where conversations are saved. |
| `JAIGENT_PRICES` | — | JSON file overriding the built-in price table. |
| `JAIGENT_SKILLS` | `1` | Load skills and offer the `load_skill` tool. |
| `JAIGENT_PLUGINS` | `1` | Load local tool plugins from `.jaigent/plugins`. |
| `JAIGENT_CHECKPOINTS` | `1` | Snapshot files before changing them, enabling `undo`. |
| `JAIGENT_FAILOVER` | `1` | Retry transient failures and fall back to another provider. |
| `JAIGENT_RETRIES` | `3` | Attempts per provider before failing over. `1` disables retrying. |
| `JAIGENT_NO_UPDATE_CHECK` | — | Set to `1` to never check for new releases. |
| `JAIGENT_HOME` | `~/.jaigent` | Where settings, skills and schedules live. |
| `JAIGENT_SCHEDULE_FILE` | `$JAIGENT_HOME/schedules.json` | Scheduled task store. |
| `GEMINI_API_KEY` | — | Google Gemini key. |
| `DEEPSEEK_API_KEY` | — | DeepSeek key. |
| `XAI_API_KEY` | — | Grok (xAI) key. |
| `JAIGENT_KEYS_FILE` | `$JAIGENT_HOME/keys.json` | Where gateway keys are stored. |
| `JAIGENT_MCP_WRITE` | `0` | Set to `1` to expose write tools from `jaigent mcp`. |
| `JAIGENT_BUDGET` | `0` | Hard USD cap for one run. `0` disables it. |
| `JAIGENT_MEMORY` | `0` | Set to `1` to persist notes in `.jaigent/memory.md`. |
| `JAIGENT_AUTO_COMPACT` | `0` | Set to `1` to collapse older chat turns automatically. |

## Python API

```python
from jaigent import Agent, Settings

agent = Agent(Settings.from_env())

result = agent.run("Summarise every markdown file in this folder into overview.md")

print(result.output)  # the final answer
print(result.tool_calls)  # how many tools ran
for step in result.steps:
    print(step.tool, step.arguments, f"{step.duration:.2f}s")
```

Explicit configuration instead of the environment:

```python
from pathlib import Path
from jaigent import Agent, Settings

agent = Agent(
    Settings(
        provider="anthropic",
        model="claude-3-5-sonnet-latest",
        api_key="sk-ant-...",
        workspace=Path("~/projects/report").expanduser(),
        max_steps=20,
    )
)

print(agent.chat("Research the topic in brief.md and expand it into report.md"))
```

Steering behaviour, streaming, and observing tool calls:

```python
agent = Agent(
    Settings.from_env(),
    instructions="Always cite sources. Prefer primary documentation.",
    on_tool_start=lambda name, args: print(f"→ {name}"),  # before it runs
    on_tool_call=lambda name, args, out: print(f"[{name}] {args}"),  # after it runs
    on_text=lambda chunk: print(chunk, end="", flush=True),  # stream tokens
)

result = agent.run("Research X and save it to x.md")
print(result.cost.summary())  # "1,240 tokens (980 in / 260 out) · ~$0.0043"
```

Gate destructive tools from Python — useful when embedding the agent in something
that must never write without permission:

```python
from jaigent import Agent, Approver, Mode, Settings

agent = Agent(Settings.from_env(), approver=Approver(Mode.DRY_RUN))
```

Save and resume a conversation:

```python
from jaigent import Session

session = Session.new(model="gpt-4o-mini")
agent.run("first question")
session.touch(agent.history)
session.save()

# later
restored = Session.new()  # or jaigent.session.resolve("last")
agent.load_history(restored.messages)
```

## Adding your own tool

A tool is a name, a description the model reads, a JSON Schema, and a function.

```python
from jaigent import Agent, Settings, Tool, build_default_registry


def word_count(path: str) -> str:
    from pathlib import Path

    return f"{len(Path(path).read_text().split())} words"


settings = Settings.from_env()
registry = build_default_registry(settings)

registry.register(
    Tool(
        name="word_count",
        description="Count the words in a text file. Use when the user asks about length.",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "File to measure."}},
            "required": ["path"],
        },
        func=word_count,
    )
)

agent = Agent(settings, tools=registry)
```

The description is the only thing the model sees, so write it as instructions to a colleague: say *when* to use the tool, not just what it does. Raise `jaigent.ToolError` for failures the model should read and recover from — anything else is caught and returned as text too, so a broken tool never crashes a run.

## Providers and models

Ten providers are built in. Pick one with `--provider`, or store it:
`jaigent settings set provider groq`.

| Provider | Key | Default model |
| --- | --- | --- |
| `openai` | `OPENAI_API_KEY` | `gpt-4o-mini` |
| `anthropic` | `ANTHROPIC_API_KEY` | `claude-3-5-sonnet-latest` |
| `gemini` | `GEMINI_API_KEY` | `gemini-2.5-flash` |
| `deepseek` | `DEEPSEEK_API_KEY` | `deepseek-chat` |
| `xai` (Grok) | `XAI_API_KEY` | `grok-4` |
| `openrouter` | `OPENROUTER_API_KEY` | `anthropic/claude-sonnet-4` |
| `groq` | `GROQ_API_KEY` | `llama-3.3-70b-versatile` |
| `mistral` | `MISTRAL_API_KEY` | `mistral-small-latest` |
| `together` | `TOGETHER_API_KEY` | `meta-llama/Llama-3.3-70B-Instruct-Turbo` |
| `ollama` | none needed | `qwen2.5:14b` |

Browse what each one offers:

```bash
jaigent models                      # the whole catalogue, with prices
jaigent models --only openrouter
jaigent models --free               # no-cost models only
jaigent models claude               # search
```

The catalogue is a convenience, not a restriction — any model id works with `--model`.
Whatever you choose must support **tool / function calling**, or the agent can only chat.

### Gemini, DeepSeek and Grok

```bash
export GEMINI_API_KEY=...     && jaigent --provider gemini "summarise this repo"
export DEEPSEEK_API_KEY=...   && jaigent --provider deepseek "explain this function"
export XAI_API_KEY=...        && jaigent --provider xai "what changed in the news today?"
```

Gemini speaks its own `generateContent` protocol, so it has a dedicated adapter that
translates messages, tool schemas and streaming events. DeepSeek and Grok both ship
OpenAI-compatible endpoints, so they reuse that adapter with a different base URL — no
extra code, and streaming works the same way on all three.

### Anything else

Any other OpenAI-compatible endpoint works by overriding the URL:

```bash
export JAIGENT_BASE_URL=http://localhost:1234/v1   # LM Studio, vLLM, a proxy...
export JAIGENT_API_KEY=whatever
export JAIGENT_MODEL=your-model
```

Want to try the loop without spending anything? `examples/mock_llm_server.py` is a fake
OpenAI-compatible server, with streaming, that replays a scripted plan:

```bash
python examples/mock_llm_server.py &
JAIGENT_BASE_URL=http://localhost:8000/v1 JAIGENT_API_KEY=x jaigent "demo" --verbose
```

## Safety model

An agent that writes files and runs commands deserves care. jaigent's defaults are deliberately conservative.

**Filesystem.** Every path is resolved and checked against the workspace root before use. `../../etc/passwd`, `/etc/passwd`, `~/.ssh/id_rsa` and symlinks pointing outside are all rejected with `SandboxViolation`. Reads are capped at 1 MB and paginated.

**Shell.** `run_command` is absent from the toolset unless you pass `--allow-shell` (or set `JAIGENT_ALLOW_SHELL=1`). When enabled it runs inside the workspace, is time-limited, and screens each command against a blocklist covering recursive deletes of `/` or `~`, disk writes, filesystem formats, fork bombs, `sudo`, piping a download into a shell, force pushes, reads of `~/.ssh` and `/etc/shadow`, and machine shutdown. Matching is done on a whitespace-normalised, lower-cased form, so `RM  -RF  /` is caught too.

That blocklist stops accidents, not a determined adversary — a model that can run shell commands can work around any string filter given enough attempts. Treat the flag as "I trust this model with this directory".

**Undo.** Every file change is snapshotted before it happens, including changes you approve, so a mistake is one `jaigent undo` away. See [Undo anything](#undo-anything).

**Network.** The agent fetches URLs the model picks. Pages are stripped to text, truncated, and never executed. `fetch_page` refuses to reach the local machine or a private network — loopback, link-local, private ranges and cloud metadata endpoints such as `169.254.169.254` are all rejected, hostnames are resolved and every address checked, and each redirect is re-validated. Without that, a page could tell the model to fetch your cloud credentials and it would oblige.

Fetched content is still untrusted input that may attempt prompt injection. Don't combine `--allow-shell` with browsing sites you don't trust.

**Secrets.** Keys are read from the environment or `.env` (git-ignored), never written to disk by jaigent, and masked in all output including `jaigent config`. File tools refuse `.env`, private keys and similar credential files even when they sit inside the workspace, so a confused model cannot send them to the provider.

Sensible habits: run in a dedicated directory rather than `$HOME`, keep the workspace under version control so you can see and revert what changed, and start with `--verbose` to watch what the agent actually does.

## Development

```bash
pip install -e ".[dev]"

pytest                          # run the suite
pytest --cov --cov-report=term-missing
ruff check . && ruff format .   # lint and format
mypy                            # type-check

pip install bandit pip-audit
bandit -r src/jaigent -ll       # static security analysis
pip-audit                       # known CVEs in dependencies
```

The test suite is fully offline: HTTP is mocked and a scripted fake provider drives the agent loop, so no API key is needed to run it.

Layout:

```
src/jaigent/
├── agent.py        # the tool-calling loop
├── cli.py          # command line interface
├── config.py       # settings, env vars, .env loading
├── prompts.py      # system prompt
├── errors.py       # exception hierarchy
├── checkpoint.py   # snapshots behind undo and rewind
├── failover.py     # retry and provider chaining
├── mcp.py          # MCP server for ChatGPT and Claude
├── plugins.py      # local tool plugins
├── llm/            # provider adapters
└── tools/          # sandbox, files, web, shell

packaging/
├── jaigent.spec    # PyInstaller build for the standalone binary
├── launcher.py     # frozen entry point
├── install.sh      # macOS and Linux installer
└── install.ps1     # Windows installer
```

Build the standalone binary yourself:

```bash
pip install -e ".[build]"
pyinstaller packaging/jaigent.spec
./dist/jaigent --version
```

CI (`.github/ci.yml`) runs all of the above on Python 3.10–3.13 across Linux, macOS and Windows, plus `bandit` and `pip-audit`. `.github/release.yml` builds and publishes the binaries for all five platform targets. See [.github/README.md](.github/README.md) to activate them.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the contribution workflow and [AGENTS.md](AGENTS.md) for conventions to follow when an AI coding agent works on this repository.

## License

[Apache License 2.0](LICENSE.md) © jaime-gaming

Apache-2.0 was chosen over MIT for its explicit patent grant: contributors licence
any patents covering their contribution, so you can build on jaigent commercially
without that risk. Attribution required, no warranty given. The full text is in
[LICENSE.md](LICENSE.md); a summary is at
<https://choosealicense.com/licenses/apache-2.0/>.
