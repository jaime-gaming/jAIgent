<div align="center">

```
     ██╗  █████╗  ██╗  ██████╗  ███████╗ ███╗   ██╗ ████████╗
     ██║ ██╔══██╗ ██║ ██╔════╝  ██╔════╝ ████╗  ██║ ╚══██╔══╝
     ██║ ███████║ ██║ ██║  ███╗ █████╗   ██╔██╗ ██║    ██║
██   ██║ ██╔══██║ ██║ ██║   ██║ ██╔══╝   ██║╚██╗██║    ██║
╚█████╔╝ ██║  ██║ ██║ ╚██████╔╝ ███████╗ ██║ ╚████║    ██║
 ╚════╝  ╚═╝  ╚═╝ ╚═╝  ╚═════╝  ╚══════╝ ╚═╝  ╚═══╝    ╚═╝
        all your agents in one place
```

</div>

# jaigent

**All your agents in one place.**

The CLI that talks to every model you already pay for, hands the same tools
to ChatGPT and Claude Desktop, and exposes them as an OpenAI-compatible API
for the rest of your stack. It searches the web, writes your files, and
`jaigent undo` puts the disk back. Bring your own key. No account, no
telemetry, no hosted backend. Current version: **0.5.2**.

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
- [Features](#features)
- [How it all fits together](#how-it-all-fits-together)
- [Install](#install)
- [Get an API key](#get-an-api-key)
- [Quick start](#quick-start)
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
- [Staying up to date](#staying-up-to-date)
- [Releasing](#releasing)
- [FAQ](#faq)
- [Development](#development)
- [License](#license)

---

## Why jaigent

One binary. Ten providers. The same tools in the terminal, in ChatGPT, in
Claude Desktop, and in any app that speaks OpenAI.

| You already have… | jaigent is the one place that… |
| --- | --- |
| Claude Code / Cursor / Aider | Looks things up on the live web and writes a file you can undo |
| ChatGPT or Claude Desktop | Serves those tools over MCP, without giving those apps a shell |
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

## Features

Every feature below shipped in a numbered release. Nothing sits in
“unreleased.” See [CHANGELOG.md](CHANGELOG.md) for the full notes.

| Feature | What it does | Since |
| --- | --- | --- |
| Agent loop | Plan → tools → answer, with a step budget | 0.1.0 |
| Web search + fetch | DuckDuckGo (no key) or Tavily; pages stripped to text | 0.1.0 |
| Sandboxed files | Read / write / edit / search / delete inside one folder | 0.1.0 |
| Opt-in shell | `run_command` only with `--allow-shell` | 0.1.0 |
| Streaming + cost | Tokens as they arrive; USD line after each turn | 0.2.0 |
| Approvals | Diff, then y / n / always / quit | 0.2.0 |
| Sessions | Saved chats, `--resume`, `/save` | 0.2.0 |
| Terracotta wordmark | Six-row block letters, `❯`, ASCII fallbacks | 0.2.0 / 0.5.2 |
| Skills | Markdown procedures, loaded on demand | 0.3.0 |
| Settings | Five-layer config, no secrets in the file | 0.3.0 |
| Schedules | `30m`, `daily at 09:00`, cron-safe `schedule run` | 0.3.0 |
| Extra providers | Groq, OpenRouter, Ollama, Gemini, … | 0.3.0–0.4.0 |
| Auto routing | `--model auto` / `jaigent route` | 0.4.0 |
| `jaigent serve` | OpenAI-compatible API + hashed `jgt-` keys | 0.4.0 |
| Custom commands | `/review` from a markdown template | 0.4.0 |
| Undo / rewind | Snapshots *before* the approval prompt | 0.5.0 |
| Failover | Retry, then the next provider that has a key | 0.5.0 |
| Standalone binary | Windows, macOS Intel/ARM, Linux x64/arm64 | 0.5.0 |
| `jaigent update` | pip / pipx / binary / `git pull --ff-only` | 0.5.1 |
| `--model free` | Ollama, then Groq / Gemini / OpenRouter `:free` | 0.5.2 |
| Plugins | Local Python in `.jaigent/plugins` only | 0.5.2 |
| MCP | Tools + resources + prompts for ChatGPT / Claude | 0.5.2 |
| Spend cap | Hard USD stop: `settings set budget 0.50` | 0.5.2 |
| Compact | `/compact` and `auto_compact`, no extra LLM call | 0.5.2 |
| Memory | Off until `settings set memory true` | 0.5.2 |

---

## How it all fits together

```
  ChatGPT ──┐
  Claude  ──┼── jaigent mcp ──┐
  Cursor  ──┘                 │
                              ├── same tools ── workspace (sandboxed)
  your app ──── jaigent serve ┤         │
                              │         ├── web_search / fetch_page
  terminal ──── jaigent run ──┘         ├── read / write / undo
                jaigent chat            └── optional shell
```

One process, one workspace, one spend cap. Switch the *model* with
`--provider` / `--model auto` / `--model free`. Switch the *front door*
without rewriting tools: CLI, MCP, or the OpenAI-compatible API.

Typical setups:

| You want… | Run… |
| --- | --- |
| A one-shot research note | `jaigent "… write it to notes.md"` |
| A conversation you can resume | `jaigent chat` |
| ChatGPT / Claude Desktop to see this folder | `jaigent mcp` |
| An app to call the agent | `jaigent keys new app && jaigent serve` |
| A free local loop | Ollama + `jaigent -m free "…"` |
| A hard dollar stop | `jaigent settings set budget 0.50` |

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

Both scripts verify the published SHA-256 checksum before installing. Or
download the archive from
[Releases](https://github.com/jaime-gaming/jaigent/releases) — Windows x64,
macOS (Intel and Apple Silicon), Linux (x64 and arm64).

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

## Quick start

```bash
jaigent init                          # pick a provider, paste a key, test it
jaigent "summarise README.md into overview.md"
jaigent undo                          # put overview.md back if you hate it
jaigent chat                          # then keep going interactively
```

In chat, `/provider groq` switches backend mid-session (needs that key),
`/compact` shrinks a long thread, `/memory` shows project notes once you have
turned memory on.

Wire the same tools into another client without leaving this directory:

```bash
jaigent mcp --print-config claude     # paste into Claude Desktop
jaigent mcp --print-config chatgpt
jaigent serve                         # http://127.0.0.1:8787/v1
```

Confirm the install:

```bash
jaigent doctor
jaigent update --check
```

---

## Usage

```bash
jaigent "what changed in the latest Node.js LTS? write it to node-lts.md"
jaigent "read all .py files here and list every TODO comment"
jaigent "compare the pricing pages of Vercel and Netlify into pricing.md"
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

The agent writes to disk. Interactive runs show a unified diff and wait
before every `write_file`, `edit_file`, `delete_file` or `run_command`:

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

`always` stops asking for that one tool for the rest of the run. Declining
is sent back to the model, so it adapts instead of retrying blindly.

| Flag | Behaviour |
| --- | --- |
| *(default, interactive)* | Ask before each file change or command. |
| `-y`, `--yes` | Apply everything without asking. |
| `--ask` | Force the prompts on, even when piped. |
| `--dry-run` | Refuse every mutation; the agent may only read and search. |

When output is **not** a terminal the default flips to `--yes`, so scripts
and CI never hang. Persist a policy with `jaigent settings set approval ask`.

---

## Streaming and cost

Answers stream as raw markdown (a code fence is only visible once it ends),
then redraw in place as rendered markdown. Piped output is never redrawn, so
`jaigent "..." > answer.md` gets the source. `--no-stream` waits for the
full reply.

After every turn:

```
2 tool calls · 2,895 tokens (2,460 in / 435 out) · ~$0.0006
```

Prices for common OpenAI and Anthropic models are built in; unknown models
show tokens only. Override with a JSON file:

```bash
export JAIGENT_PRICES=~/prices.json   # {"my-model": {"input": 1.5, "output": 3.0}}
```

Hide the line with `--no-cost`. Cap the run with
`jaigent settings set budget 0.50` — that is a hard stop, not a hint.

---

## Chat and sessions

Conversations are saved to `~/.jaigent/sessions` (or
`%APPDATA%\jaigent\sessions` on Windows) and can be picked up later.

```bash
jaigent chat                      # a new session, saved on exit
jaigent chat --resume             # most recent
jaigent chat --resume 20260818-093000
jaigent chat --no-save
jaigent sessions
jaigent sessions --delete <id>    # or --delete all
```

`/undo` drops the last **exchange**. `/revert` undoes the last **file**
change. They are not the same command.

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
| `/revert` / `/diff` / `/checkpoints` / `/rewind <id>` | Undo **files**. |
| `/status` | Provider, model, workspace, session. |
| `/approve <mode>` | `ask`, `auto` or `dry-run`. |
| `/commands` | Custom slash commands. |
| `/doctor` | Check keys, storage and providers. |
| `/compact` | Collapse older turns into a short summary. |
| `/memory` | Show project memory (off until `settings set memory true`). |
| `/exit` | Quit. |

---

## Undo anything

Every mutating tool call is snapshotted *before* the approval prompt, so a
change you approved and then regretted is just as reversible as one you
never saw.

```console
$ jaigent "tidy up the imports across the project"
  → edit_file(path='src/app.py')
  → edit_file(path='src/utils.py')

$ jaigent undo
  revert  src/utils.py
✓ reverted 1 file(s) to just now (edit_file src/utils.py)
```

Each `undo` consumes the checkpoint it restored, so repeating it walks back
one change at a time. `jaigent rewind <id>` jumps to any point and leaves
history alone. In chat: `/revert`, `/diff`, `/checkpoints`, `/rewind <id>`.

The store is content-addressed (unchanged bytes are stored once), keeps the
last 100 checkpoints, prunes unreferenced objects, and skips files over 5 MB.
Everything lives in `.jaigent/checkpoints` inside the workspace.

Turn it off with `--no-checkpoints`, `JAIGENT_CHECKPOINTS=0`, or
`jaigent settings set checkpoints false`.

> **Not a substitute for version control.** Checkpoints cover files the agent
> touched through its own tools. They do not track `run_command` side effects.
> Commit before a big run.

---

## Models

### Auto

`--model auto` sizes the model to the job. A greeting does not cost a
refactor.

```bash
jaigent -m auto "hi"                                   # → a cheap model
jaigent -m auto "refactor this package and add tests"  # → a capable one
jaigent settings set model auto
jaigent route "why does this deadlock under load?"     # preview, spend nothing
```

The router scores length, code blocks, multi-step phrasing and difficulty
keywords, then buckets simple / standard / complex. It is a heuristic in
[`router.py`](src/jaigent/router.py), not a second LLM call.

### Free

`--model free` walks providers you can actually use and picks a no-cost
model. Ollama first, then Groq, Gemini and OpenRouter `:free` ids.

```bash
jaigent -m free "summarise README.md"
jaigent models --free
jaigent route --free "refactor this"
jaigent settings set model free
```

You still need a key for Groq, Gemini or OpenRouter. Ollama needs none.

### Failover

A 503 or a rate limit retries with backoff and jitter, then falls through to
the next provider that has a **key of its own**. 400 / 401 fail immediately
— retrying a bad request wastes time.

```console
$ jaigent "summarise the changelog"
  ! openai failed (HTTP 529 overloaded) — falling back to anthropic
```

`jaigent doctor` shows the chain. Tune with `--retries N` or
`JAIGENT_FAILOVER=0`. A local Ollama counts as a fallback with no key.
Switching with `/provider` never reuses the previous backend's key.

---

## Spend cap, compact, memory

Three separate controls. None of them is on until you say so, except the
built-in skills (those are just prompt text).

```bash
jaigent settings set budget 0.50       # hard USD stop for one run
jaigent settings set auto_compact true # collapse older turns when history grows
jaigent settings set memory true       # remember / recall + .jaigent/memory.md
```

**Spend cap.** `budget` is enforced in the agent loop after each model
reply. When the estimate reaches the cap, the run stops and the footer says
`spend cap reached`. The built-in `spend-cap` skill is the soft side: spend
less *before* the run is killed. `0` disables it.

**Compact.** `/compact` in chat collapses older turns into one summary
without another model call. `auto_compact` does the same when history
passes twenty messages. The built-in `compact` skill tells the model what
to keep.

**Memory.** Off until you turn it on. Then the model gets `remember` and
`recall`, and standing notes live in `.jaigent/memory.md` inside the
workspace. Nothing is written or sent while the setting is off. Do not store
secrets there.

---

## MCP: ChatGPT and Claude

Serve jaigent's tools over stdio to ChatGPT, Claude Desktop, or any
[MCP](https://spec.modelcontextprotocol.io) client. The client supplies the
model — no API key needed. This is a tool server, not a second chatbot.

Read-only by default (`web_search`, `fetch_page`, `list_files`, `read_file`,
`search_files`). `--allow-write` or `JAIGENT_MCP_WRITE=1` adds write tools.
`run_command` is never exposed. Workspace files are advertised as resources
(secrets skipped); skills and commands as prompts. Protocol versions through
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

Other MCP clients (VS Code, Cursor, Windsurf, Zed, …) take the same shape:
command `jaigent`, arguments `mcp`. The working directory of the client is
the workspace.

---

## Your own API

`jaigent serve` turns the agent into an OpenAI-compatible endpoint at
`/v1/chat/completions` and `/v1/models`. Your apps get one URL; behind it
jaigent picks the model, searches the web and uses its tools.

```bash
jaigent keys new my-app       # prints jgt-… once — copy it now
jaigent serve                 # http://127.0.0.1:8787/v1
```

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8787/v1", api_key="jgt-...")
reply = client.chat.completions.create(
    model="auto",
    messages=[{"role": "user", "content": "research X and summarise it"}],
)
print(reply.choices[0].message.content)
print(reply.jaigent if hasattr(reply, "jaigent") else "")
```

Responses carry a `jaigent` block (`tool_calls`, `tools_used`,
`estimated_usd`). Keys are stored hashed with owner-only permissions;
comparison is constant-time. `jaigent keys list` / `revoke <name>` manage
them. `serve` binds `127.0.0.1` by default.

> **A `jgt-` key is a production credential.** It grants full agent access —
> files, the web, and the shell if you enabled it — billed to your provider
> account. Keep it on loopback unless you have real auth and TLS in front.

---

## Skills

A skill is a saved procedure: markdown you write once and the agent reuses.
Only the one-line *descriptions* go into the system prompt. The body is
fetched with `load_skill` when the model decides it is relevant, so a large
library costs almost nothing in context.

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

Read the git log since the last tag, group the commits by type, and write
the result to CHANGELOG.md following Keep a Changelog.
```

Two skills ship built in: **`spend-cap`** (stay cheap before the hard stop)
and **`compact`** (what to keep when the chat is long). You cannot
`jaigent skills remove` a built-in skill.

Project skills live in `./.jaigent/skills` (commit them). `--user` puts them
in `~/.jaigent/skills`. A project skill shadows a user skill of the same
name. Skills are prompt text — loading one never executes code.

---

## Plugins

A plugin is local Python that registers extra tools. Unlike skills, plugins
**are** code — only files you put in `.jaigent/plugins` (project) or
`~/.jaigent/plugins` (personal) are loaded, never anything from the network.
`register()` receives redacted settings: `api_key` is always `None`.

```bash
jaigent plugins new wordcount
jaigent plugins list
jaigent plugins remove wordcount
```

The starter file:

```python
from jaigent.tools import Tool

def register(registry, settings) -> None:
    def word_count(path: str) -> str:
        from pathlib import Path
        target = Path(settings.workspace) / path
        return f"{len(target.read_text().split())} words"

    registry.register(
        Tool(
            name="word_count",
            description="Count words in a workspace file.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            func=word_count,
        )
    )
```

A broken plugin is skipped so it cannot take down a run. Turn them off with
`JAIGENT_PLUGINS=0`.

---

## Custom commands

A markdown prompt template becomes `/review` in chat and `jaigent /review`
on the shell.

```bash
jaigent commands new review -d "Review the working tree" \
  --template 'Run git diff, then review $ARGUMENTS for correctness first, style second.'
```

Placeholders: `$ARGUMENTS` (everything after the name), `$1` / `$2`
(individual words), `$WORKSPACE`. Project commands live in
`.jaigent/commands`; `--user` puts them in your home directory. Names that
would shadow a built-in (`/provider`, `/compact`, `/memory`, …) are refused.

Prompt text only — running one can only send a message.

---

## Schedules

Run a prompt on a timer. Approval is forced to `auto` because nobody is
there to answer a prompt.

```bash
jaigent schedule add "check my repos for failing CI and write status.md" --every 2h
jaigent schedule add "summarise today's commits" --every "daily at 18:00"
jaigent schedule list
jaigent schedule run              # anything due — safe for cron
jaigent schedule run --watch
jaigent schedule pause task-1
```

Intervals: `30m`, `every 2h`, `hourly`, `daily`, `daily at 09:00`, `weekly`.
Each task remembers its workspace and model, and records the last result
(`jaigent schedule show task-1`).

```cron
*/15 * * * * cd ~/project && jaigent schedule run >> ~/.jaigent/cron.log 2>&1
```

---

## Settings

Persist configuration instead of exporting variables every time.

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

API keys are refused by `settings set`. Values are validated before write —
`settings set provider notreal` is rejected, so it cannot break every later
command.

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
`.git`. `.env.example` stays readable. Every path goes through
`resolve_in_workspace()`.

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
for step in result.steps:
    print(step.tool, step.arguments, f"{step.duration:.2f}s")
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

Write the description as instructions to a colleague. Raise
`jaigent.ToolError` for failures the model should recover from.

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

## Safety model

Defaults are deliberately conservative.

**Filesystem.** Every path is resolved and checked against the workspace.
Traversal, absolute paths and escaping symlinks are rejected. Reads are
capped at 1 MB.

**Shell.** Absent unless `--allow-shell` / `JAIGENT_ALLOW_SHELL=1`.
Time-limited, blocklisted (`rm -rf /`, `sudo`, `format c:`, …). A model that
can run a shell can work around a string filter — treat the flag as “I trust
this model with this directory”.

**Undo.** Every file change is snapshotted first. See
[Undo anything](#undo-anything).

**Network.** `fetch_page` refuses loopback, link-local, private ranges and
cloud metadata (`169.254.169.254`). Hostnames are resolved and every
redirect is re-checked. Fetched pages are still untrusted input — don't
combine `--allow-shell` with sites you don't trust.

**Secrets.** Keys come from the environment or a git-ignored `.env`, never
printed in full. File tools refuse `.env`, private keys and similar files
even inside the workspace.

Run in a dedicated directory, keep it under version control, start with
`--verbose`.

---

## Staying up to date

jaigent tells you once when a newer release exists, after the command you
ran has finished. The check is at most daily, three-second timeout, every
failure ignored. Suppressed when piped. Opt out with
`JAIGENT_NO_UPDATE_CHECK=1`.

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

`--check` reports without installing. A matching version tag with a
different SHA than GitHub `main` is reported as unsynced. Offline, it says
it could not *reach* GitHub, not that there is no release.

---

## Releasing

A new version is one commit that agrees with itself, then a tag. The Release
workflow refuses to publish if those disagree.

1. Bump **both** `version` in `pyproject.toml` and `__version__` in
   `src/jaigent/__init__.py` to the same `X.Y.Z`.
2. Put every change under `## [X.Y.Z]` in `CHANGELOG.md` — do not leave
   user-facing work in `[Unreleased]`.
3. Add the version to the table in `SECURITY.md`.
4. Merge to `main`.
5. Tag and push:

```bash
git tag vX.Y.Z
git push origin vX.Y.Z
```

The tag **must** be `v` plus the source version (`v0.5.2` for `0.5.2`). A
mistyped tag fails in seconds, before the five binary builds start.

You can also run **Release** from the Actions tab and pass the tag as input.

| Job | Purpose |
| --- | --- |
| `verify` | Tag == `pyproject.toml` == `__version__` |
| `build` × 5 | PyInstaller on Linux x64/arm64, macOS Intel/ARM, Windows x64 |
| `wheel` | sdist + wheel, installed and run |
| `publish` | Attaches every archive, the wheel, and `checksums.txt` |

**Workflows.** CI and Release live in `.github/workflows/`. Three repairs are
required for a tag to produce binaries (Windows `doctor || true`, Windows
smoke-test exit code, `macos-15-intel` instead of retired `macos-13`). If
they drift, run `./scripts/activate-ci.sh` from an account with the
`workflows` permission and push.

---

## FAQ

**Do I need Python?** No. The standalone installer ships a self-contained
binary. Python is only for `pip install` or hacking on the source.

**Which model should I use?** `--model auto` sizes the job. `--model free`
picks a no-cost one you can actually reach. Name any tool-calling id with
`-m`.

**Will it replace Claude Code / Cursor?** No. It sits next to them. Point
those apps at `jaigent mcp` when you want this workspace's tools.

**Can it run shell commands?** Only with `--allow-shell` or
`JAIGENT_ALLOW_SHELL=1`. Off by default. Undo does not cover shell side
effects.

**Where do files live?** Per-user data is `~/.jaigent` (or
`%APPDATA%\jaigent` on Windows). Project data is `./.jaigent` — settings,
skills, plugins, commands, checkpoints, memory.

**How do I stop it spending?** `jaigent settings set budget 0.50`. The run
stops when the estimate reaches the cap. Also `--model free` / `auto`.

**Is there telemetry?** No. The only optional network call besides your
provider is a once-a-day GitHub release check. Disable with
`JAIGENT_NO_UPDATE_CHECK=1`.

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

See [CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md).

---

## License

[Apache License 2.0](LICENSE.md) © jaime-gaming

Apache-2.0 was chosen over MIT for its explicit patent grant. Attribution
required, no warranty. Summary:
<https://choosealicense.com/licenses/apache-2.0/>.
