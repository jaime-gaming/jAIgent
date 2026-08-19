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
your apps already know how to call.

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

**Start**
- [Why jaigent](#why-jaigent)
- [Install](#install)
- [Get an API key](#get-an-api-key)
- [Usage](#usage)

**What it does**
- [Reviewing changes](#reviewing-changes-before-they-happen)
- [Streaming and cost](#streaming-and-cost)
- [Chat and sessions](#chat-and-sessions)
- [Undo anything](#undo-anything)
- [Models](#models) — auto, free, failover
- [Spend cap, compact, memory](#spend-cap-compact-memory)

**How it links**
- [MCP: ChatGPT and Claude](#mcp-chatgpt-and-claude)
- [Your own API](#your-own-api)

**Extend it**
- [Skills](#skills)
- [Plugins](#plugins)
- [Custom commands](#custom-commands)
- [Schedules](#schedules)
- [Settings](#settings)

**Reference**
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

---

## Install

### Standalone binary (no Python needed)

**macOS and Linux**

```bash
curl -fsSL https://raw.githubusercontent.com/jaime-gaming/jaigent/main/packaging/install.sh | sh
```

**Windows** (PowerShell)

```powershell
irm https://raw.githubusercontent.com/jaime-gaming/jaigent/main/packaging/install.ps1 | iex
```

Both scripts verify the published SHA-256 checksum before installing. Or download
the archive from [Releases](https://github.com/jaime-gaming/jaigent/releases) —
Windows x64, macOS (Intel and Apple Silicon), Linux (x64 and arm64).

### From PyPI

```bash
pip install jaigent
```

### From source

Requires Python 3.10–3.13.

```bash
git clone https://github.com/jaime-gaming/jaigent.git
cd jaigent
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -e .
```

Then pick a provider, store a key, and make a test call:

```bash
jaigent init
jaigent doctor      # keys, storage, providers
jaigent --logo      # the terracotta wordmark
```

`jgt` is a shorter alias for the same command.

The logo adapts to the terminal: six-row block letters when there is room, a
three-row wordmark in narrow windows, a single line below ~28 columns. Colour
drops under `--no-color` or when piped.

---

## Get an API key

jaigent has no key of its own — you supply one. `jaigent providers` prints the
same table.

| Provider | Where to get a key | Environment variable |
| --- | --- | --- |
| OpenAI (default) | <https://platform.openai.com/api-keys> | `OPENAI_API_KEY` |
| Anthropic | <https://console.anthropic.com/settings/keys> | `ANTHROPIC_API_KEY` |
| Gemini | <https://aistudio.google.com/apikey> | `GEMINI_API_KEY` |
| OpenRouter | <https://openrouter.ai/keys> | `OPENROUTER_API_KEY` |
| Groq | <https://console.groq.com/keys> | `GROQ_API_KEY` |
| Together | <https://api.together.xyz/settings/api-keys> | `TOGETHER_API_KEY` |
| DeepSeek | <https://platform.deepseek.com/api_keys> | `DEEPSEEK_API_KEY` |
| Mistral | <https://console.mistral.ai/api-keys> | `MISTRAL_API_KEY` |
| xAI (Grok) | <https://console.x.ai> | `XAI_API_KEY` |
| Ollama | none — runs locally | — |

> Don't want to pay? Use Ollama, or OpenRouter / Groq free models with `--model free`.

```bash
export OPENAI_API_KEY='sk-...'
# or
cp .env.example .env && $EDITOR .env
jaigent config          # key is printed as <set>, never in full
```

`.env` is git-ignored. Real environment variables always win. Web search uses
DuckDuckGo by default and needs **no** second key.

---

## Usage

```bash
jaigent "what changed in the latest Node.js LTS? write it to node-lts.md"
jaigent "read all .py files here and list every TODO comment"
jaigent chat
```

`jaigent <prompt>` is shorthand for `jaigent run <prompt>`.

```bash
jaigent "audit this repo" --workspace ~/projects/api
jaigent "research X" --verbose
jaigent "explain this code" -m gpt-4o
jaigent "run the tests and fix what fails" --allow-shell
```

| Command | What it does |
| --- | --- |
| `jaigent init` | Pick a provider, store a key, test it. |
| `jaigent run <prompt>` | One task, then exit. |
| `jaigent chat` | Interactive session. |
| `jaigent undo` / `rewind` / `checkpoints` | Revert file changes. |
| `jaigent mcp` | Tool server for ChatGPT and Claude Desktop. |
| `jaigent serve` / `keys` | OpenAI-compatible API and `jgt-` credentials. |
| `jaigent providers` / `models` / `route` | Backends, catalogue, auto/free preview. |
| `jaigent settings` / `config` / `doctor` | Persist, inspect, diagnose. |
| `jaigent skills` / `plugins` / `commands` | Procedures, local tools, slash templates. |
| `jaigent schedule` | Run a prompt on a timer. |
| `jaigent sessions` | Saved conversations. |
| `jaigent update` | Upgrade; `--check` only reports. Also syncs a source checkout. |
| `jaigent tools` | What the model can call. |
| `jgt` | Short alias. |
| `jaigent` / `--logo` | Splash, or the wordmark alone. |

---

## Reviewing changes before they happen

Interactive runs show a diff and wait before every file change or command:

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

`always` stops asking for that one tool. Declining is reported back to the
model, so it adapts instead of retrying blindly.

| Flag | Behaviour |
| --- | --- |
| *(default, interactive)* | Ask before each file change or command. |
| `-y`, `--yes` | Apply everything without asking. |
| `--ask` | Force the prompts on, even when piped. |
| `--dry-run` | Refuse every mutation; the agent may only read and search. |

When output is **not** a terminal the default flips to `--yes`, so scripts and
CI never hang.

---

## Streaming and cost

Answers stream as raw markdown, then redraw in place once complete. Piped
output is never redrawn, so `jaigent "..." > answer.md` gets the source.
`--no-stream` waits for the full reply.

```
2 tool calls · 2,895 tokens (2,460 in / 435 out) · ~$0.0006
```

Prices for common models are built in. Override with `JAIGENT_PRICES`. Hide
the line with `--no-cost`. Cap the run with `jaigent settings set budget 0.50`.

---

## Chat and sessions

```bash
jaigent chat                      # saved on exit
jaigent chat --resume             # most recent
jaigent chat --resume 20260818-093000
jaigent chat --no-save
jaigent sessions
jaigent sessions --delete <id>    # or --delete all
```

Sessions live in `~/.jaigent/sessions`.

| Command | Effect |
| --- | --- |
| `/help` | List these commands. |
| `/reset` | Clear the conversation. |
| `/tools` | Show available tools. |
| `/model <name>` | Switch model mid-session. |
| `/provider <name>` | Switch provider (and its own key). |
| `/workspace <path>` | Point the file tools somewhere else. |
| `/cost` | Tokens and spend so far. |
| `/save` | Write to disk now. |
| `/undo` | Drop the last exchange. |
| `/revert` / `/diff` / `/checkpoints` / `/rewind <id>` | Undo **files**, not chat. |
| `/status` | Provider, model, workspace, session. |
| `/approve <mode>` | `ask`, `auto` or `dry-run`. |
| `/commands` | Custom slash commands. |
| `/doctor` | Check keys, storage and providers. |
| `/compact` | Collapse older turns into a short summary. |
| `/memory` | Show project memory (off until `settings set memory true`). |
| `/exit` | Quit. |

---

## Undo anything

Every write is snapshotted *before* the approval prompt, so a change you
approved and then regretted is just as reversible as one you never saw.

```console
$ jaigent "tidy up the imports across the project"
  → edit_file(path='src/app.py')
  → edit_file(path='src/utils.py')

$ jaigent undo
  revert  src/utils.py
✓ reverted 1 file(s) to just now (edit_file src/utils.py)
```

Each `undo` consumes the checkpoint it restored. `rewind <id>` jumps to any
point and leaves history alone. In chat: `/revert`, `/diff`, `/checkpoints`,
`/rewind <id>`.

The store is content-addressed, keeps the last 100 checkpoints, skips files
over 5 MB, and lives in `.jaigent/checkpoints`. Turn it off with
`--no-checkpoints`, `JAIGENT_CHECKPOINTS=0`, or
`jaigent settings set checkpoints false`.

> **Not a substitute for version control.** Checkpoints cover files the agent
> touched through its own tools. They do not track `run_command` side effects.
> Commit before a big run.

---

## Models

### Auto

`--model auto` sizes the model to the job. A greeting does not cost a refactor.

```bash
jaigent -m auto "hi"                                   # → a cheap model
jaigent -m auto "refactor this package and add tests"  # → a capable one
jaigent settings set model auto
jaigent route "why does this deadlock under load?"     # preview, spend nothing
```

The router scores length, code blocks, multi-step phrasing and difficulty
keywords. It is a heuristic in [`router.py`](src/jaigent/router.py), not a
second LLM call.

### Free

`--model free` walks providers you can actually use and picks a no-cost model.
Ollama first, then Groq, Gemini and OpenRouter `:free` ids.

```bash
jaigent -m free "summarise README.md"
jaigent models --free
jaigent route --free "refactor this"
jaigent settings set model free
```

### Failover

A 503 or a rate limit retries with backoff, then falls through to the next
provider that has a key. 400 / 401 fail immediately.

```console
$ jaigent "summarise the changelog"
  ! openai failed (HTTP 529 overloaded) — falling back to anthropic
```

`jaigent doctor` shows the chain. Tune with `--retries N` or
`JAIGENT_FAILOVER=0`. A local Ollama counts as a fallback with no key.

---

## Spend cap, compact, memory

```bash
jaigent settings set budget 0.50       # hard USD stop for one run
jaigent settings set auto_compact true # collapse older turns when history grows
jaigent settings set memory true       # remember / recall + .jaigent/memory.md
```

`budget` is enforced in the agent loop, not just suggested. The built-in
`spend-cap` skill is the soft side: spend less *before* the run is killed.

`/compact` in chat (or `auto_compact`) shrinks older turns without another
model call. The built-in `compact` skill tells the model what to keep.

Memory stays **off** until you turn it on. Nothing is written or sent until
then. Notes live in `.jaigent/memory.md` inside the workspace.

---

## MCP: ChatGPT and Claude

Serve jaigent's tools over stdio to ChatGPT, Claude Desktop, or any
[MCP](https://spec.modelcontextprotocol.io) client. The client supplies the
model — no API key needed. This is a tool server, not a second chatbot.

Read-only by default. `--allow-write` or `JAIGENT_MCP_WRITE=1` adds write
tools. `run_command` is never exposed. Workspace files are resources (secrets
skipped); skills and commands are prompts. Protocol versions through
2025-11-25.

```bash
jaigent mcp --print-config claude     # paste into claude_desktop_config.json
jaigent mcp --print-config chatgpt    # command: jaigent   args: mcp --client chatgpt
```

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

The update-check notice is suppressed because stdout is the protocol stream.

---

## Your own API

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

Responses carry a `jaigent` block (`tool_calls`, `tools_used`, `estimated_usd`).
Keys are stored hashed. `serve` binds `127.0.0.1` by default — keep it there
unless you have real auth and TLS in front. A `jgt-` key grants full agent
access, billed to your provider account.

---

## Skills

Markdown procedures. Only descriptions go in the system prompt; the body is
fetched with `load_skill` when the model needs it.

```bash
jaigent skills new changelog -d "Write a release changelog from git history"
jaigent skills list
jaigent skills show changelog
```

Two skills ship built in: **`spend-cap`** and **`compact`**. You cannot
`jaigent skills remove` a built-in skill.

Project skills live in `./.jaigent/skills` (commit them). `--user` puts them
in `~/.jaigent/skills`. Skills are prompt text — loading one never executes
code.

---

## Plugins

Local Python that registers extra tools. Only files you put in
`.jaigent/plugins` or `~/.jaigent/plugins` are loaded — never anything from
the network. `register()` receives redacted settings (no live API keys).

```bash
jaigent plugins new wordcount
jaigent plugins list
```

A broken plugin is skipped. Turn them off with `JAIGENT_PLUGINS=0`.

---

## Custom commands

A markdown template becomes `/review` in chat and `jaigent /review` on the
shell. Placeholders: `$ARGUMENTS`, `$1` / `$2`, `$WORKSPACE`.

```bash
jaigent commands new review -d "Review the working tree" \
  --template 'Run git diff, then review $ARGUMENTS for correctness first, style second.'
```

Prompt text only — running one can only send a message.

---

## Schedules

```bash
jaigent schedule add "check my repos for failing CI and write status.md" --every 2h
jaigent schedule add "summarise today's commits" --every "daily at 18:00"
jaigent schedule list
jaigent schedule run              # anything due — safe for cron
jaigent schedule run --watch
```

Intervals: `30m`, `every 2h`, `hourly`, `daily`, `daily at 09:00`, `weekly`.
Scheduled runs force `auto` approval. `schedule run` only executes what is due:

```cron
*/15 * * * * cd ~/project && jaigent schedule run >> ~/.jaigent/cron.log 2>&1
```

---

## Settings

```bash
jaigent settings set model gpt-4o
jaigent settings set budget 0.50
jaigent settings set memory true
jaigent settings set max_steps 20 --project
jaigent settings list
jaigent settings path
```

Precedence, each layer winning over the one below:

1. CLI flags
2. Environment and `.env`
3. `./.jaigent/settings.json`
4. `~/.jaigent/settings.json`
5. Built-in defaults

API keys are refused by `settings set`. Values are validated before write — a
bad `provider` is not stored, so it cannot break every later command.

---

## Tools

The model chooses which of these to call, and in what order.

| Tool | Purpose |
| --- | --- |
| `web_search` | Search the web; titles, URLs and snippets. |
| `fetch_page` | Download a page and strip it to readable text. |
| `list_files` | List the workspace, optionally by glob. |
| `read_file` | Read a UTF-8 file with line numbers; paginated. |
| `write_file` | Create or overwrite a file. |
| `edit_file` | Replace an exact snippet. |
| `search_files` | Grep by substring or regex. |
| `delete_file` | Delete a file or empty directory. |
| `load_skill` | Fetch a skill body (when skills exist). |
| `remember` / `recall` | Project memory (only if `memory` is on). |
| `run_command` ⚠ | Shell. **Opt-in**, see [Safety model](#safety-model). |

File tools refuse `.env`, private keys, `*.pem` / `*.key` and anything under
`.git`. `.env.example` stays readable.

---

## Configuration

CLI flags override environment variables.

| Variable | Default | Meaning |
| --- | --- | --- |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / … | — | Provider key. See [Get an API key](#get-an-api-key). |
| `JAIGENT_API_KEY` | — | Generic key; wins over the provider-specific ones. |
| `JAIGENT_PROVIDER` | `openai` | openai, anthropic, gemini, openrouter, groq, deepseek, mistral, xai, together, ollama. |
| `JAIGENT_MODEL` | provider default | Model id, or `auto` / `free`. |
| `JAIGENT_BASE_URL` | provider default | API root for compatible gateways. |
| `JAIGENT_WORKSPACE` | current directory | Directory the file tools are locked to. |
| `JAIGENT_MAX_STEPS` | `12` | Tool-call budget per turn. |
| `JAIGENT_TEMPERATURE` | `0.2` | Sampling temperature. |
| `JAIGENT_MAX_TOKENS` | `2048` | Tokens per assistant turn. |
| `JAIGENT_TIMEOUT` | `60` | HTTP timeout in seconds. |
| `JAIGENT_SEARCH_BACKEND` | `duckduckgo` | `duckduckgo` (no key) or `tavily`. |
| `TAVILY_API_KEY` | — | Required only for Tavily. |
| `JAIGENT_ALLOW_SHELL` | `0` | `1` enables `run_command`. |
| `JAIGENT_VERBOSE` | `0` | Trace tool calls. |
| `JAIGENT_STREAM` | `1` | Stream the answer. |
| `JAIGENT_SHOW_COST` | `1` | Print the token and cost line. |
| `JAIGENT_APPROVAL` | tty-dependent | `ask`, `auto` or `dry-run`. |
| `JAIGENT_SESSION_DIR` | `~/.jaigent/sessions` | Saved conversations. |
| `JAIGENT_PRICES` | — | JSON file overriding the price table. |
| `JAIGENT_SKILLS` | `1` | Load skills and offer `load_skill`. |
| `JAIGENT_PLUGINS` | `1` | Load local plugins. |
| `JAIGENT_CHECKPOINTS` | `1` | Snapshot files before changing them. |
| `JAIGENT_FAILOVER` | `1` | Retry, then fall back. |
| `JAIGENT_RETRIES` | `3` | Attempts per provider. `1` disables retrying. |
| `JAIGENT_BUDGET` | `0` | Hard USD cap for one run. `0` disables it. |
| `JAIGENT_MEMORY` | `0` | `1` persists notes in `.jaigent/memory.md`. |
| `JAIGENT_AUTO_COMPACT` | `0` | `1` collapses older chat turns. |
| `JAIGENT_NO_UPDATE_CHECK` | — | `1` never checks for releases. |
| `JAIGENT_HOME` | `~/.jaigent` | Settings, skills, schedules. |
| `JAIGENT_SCHEDULE_FILE` | `$JAIGENT_HOME/schedules.json` | Scheduled task store. |
| `JAIGENT_KEYS_FILE` | `$JAIGENT_HOME/keys.json` | Gateway keys. |
| `JAIGENT_MCP_WRITE` | `0` | `1` exposes write tools from `jaigent mcp`. |

---

## Python API

```python
from jaigent import Agent, Settings

agent = Agent(Settings.from_env())
result = agent.run("Summarise every markdown file in this folder into overview.md")
print(result.output)
print(result.cost.summary())
```

```python
from pathlib import Path
from jaigent import Agent, Approver, Mode, Settings

agent = Agent(
    Settings(
        provider="anthropic",
        model="claude-3-5-sonnet-latest",
        api_key="sk-ant-...",
        workspace=Path("~/projects/report").expanduser(),
        budget=0.50,
        memory=True,
    ),
    instructions="Always cite sources.",
    on_tool_start=lambda name, args: print(f"→ {name}"),
    on_text=lambda chunk: print(chunk, end="", flush=True),
    approver=Approver(Mode.DRY_RUN),
)
```

```python
from jaigent import Session

session = Session.new(model="gpt-4o-mini")
agent.run("first question")
session.touch(agent.history)
session.save()
```

---

## Adding your own tool

A name, a description the model reads, a JSON Schema, and a function.
Prefer a [plugin](#plugins) if the tool should load automatically.

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

Write the description as instructions to a colleague. Raise `jaigent.ToolError`
for failures the model should recover from.

---

## Providers and models

Pick one with `--provider` or `jaigent settings set provider groq`.

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

```bash
jaigent models
jaigent models --only openrouter
jaigent models --free
jaigent models claude
```

The catalogue is a convenience — any model id works with `--model`. It must
support **tool / function calling**, or the agent can only chat.

Any other OpenAI-compatible endpoint:

```bash
export JAIGENT_BASE_URL=http://localhost:1234/v1
export JAIGENT_API_KEY=whatever
export JAIGENT_MODEL=your-model
```

Try the loop without spending: `examples/mock_llm_server.py` is a fake
OpenAI-compatible server that replays a scripted plan.

```bash
python examples/mock_llm_server.py &
JAIGENT_BASE_URL=http://localhost:8000/v1 JAIGENT_API_KEY=x jaigent "demo" --verbose
```

---

## Staying up to date

jaigent tells you once when a newer release exists, after the command you ran
has finished. The check is at most daily, three-second timeout, every failure
ignored. Suppressed when piped. Opt out with `JAIGENT_NO_UPDATE_CHECK=1`.

```console
$ jaigent update
  installed  0.5.2 (standalone binary)
  latest     0.6.0  ← new
```

| Installed via | `jaigent update` runs |
| --- | --- |
| standalone binary | the platform installer |
| `pip` | `pip install --upgrade jaigent` |
| `pipx` | `pipx upgrade jaigent` |
| source checkout | `git pull --ff-only` then `pip install -e .` |

`--check` reports without installing. A matching version tag with a different
SHA than GitHub `main` is reported as unsynced.

---

## Safety model

Defaults are deliberately conservative.

**Filesystem.** Every path is resolved and checked against the workspace.
Traversal, absolute paths and escaping symlinks are rejected. Reads are capped
at 1 MB.

**Shell.** Absent unless `--allow-shell` / `JAIGENT_ALLOW_SHELL=1`. Time-limited,
blocklisted (`rm -rf /`, `sudo`, `format c:`, …). A model that can run a shell
can work around a string filter — treat the flag as “I trust this model with
this directory”.

**Undo.** Every file change is snapshotted first. See [Undo anything](#undo-anything).

**Network.** `fetch_page` refuses loopback, link-local, private ranges and cloud
metadata (`169.254.169.254`). Hostnames are resolved and every redirect is
re-checked. Fetched pages are still untrusted input — don't combine
`--allow-shell` with sites you don't trust.

**Secrets.** Keys come from the environment or a git-ignored `.env`, never
printed in full. File tools refuse `.env`, private keys and similar files even
inside the workspace.

Run in a dedicated directory, keep it under version control, start with
`--verbose`.

---

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check . && ruff format .
mypy
bandit -r src/jaigent -ll
pip-audit
```

The suite is offline. Layout:

```
src/jaigent/
├── agent.py        # the tool-calling loop
├── branding.py     # terracotta wordmark
├── cli.py
├── config.py
├── memory.py       # optional project notes
├── mcp.py          # ChatGPT / Claude tool server
├── plugins.py      # local tool plugins
├── data/skills/    # built-in spend-cap and compact
├── llm/            # provider adapters
└── tools/          # sandbox, files, web, shell
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md). CI lives in
`.github/ci.yml` / `.github/release.yml`; the automation token cannot push
workflows — run `./scripts/activate-ci.sh` from a machine with that permission.

---

## License

[Apache License 2.0](LICENSE.md) © jaime-gaming

Apache-2.0 was chosen over MIT for its explicit patent grant. Attribution
required, no warranty. Summary:
<https://choosealicense.com/licenses/apache-2.0/>.
