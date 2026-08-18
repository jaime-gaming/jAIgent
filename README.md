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

A small, hackable AI agent that **searches the web** and **works with local files** — from your terminal or from Python.

Bring your own API key. jaigent ships with no credentials, no telemetry and no hosted backend: it talks directly from your machine to whichever LLM provider you point it at.

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
- [Skills](#skills)
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

- **Two capabilities that matter.** Web access (search + page fetching) and a real filesystem, so the agent can research something and then write the result down.
- **Sandboxed by default.** Every file operation is confined to one workspace directory. Path traversal, absolute paths and escaping symlinks are all rejected.
- **Shows you the diff first.** Interactive runs confirm every file change before it happens; `--dry-run` refuses them all.
- **No shell unless you ask.** Command execution is opt-in behind an explicit flag.
- **Streams as it thinks,** and tells you what the turn cost in tokens and dollars.
- **Remembers.** Conversations are saved and resumable with `--resume`.
- **Ten providers built in,** including [OmniRoute](https://github.com/diegosouzapw/OmniRoute) — a free local gateway to 1200+ models that needs no API key at all.
- **Skills.** Save a procedure once as markdown; the agent loads it on demand.
- **Schedules.** Run a prompt every 30 minutes, or daily at 09:00.
- **Small enough to read.** Under 1,000 lines of source. The agent loop is one function you can follow top to bottom.
- **Your key, your machine.** No account, no proxy, no data collection.

## Install

Requires Python 3.10 or newer.

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
jaigent tools       # list what the agent can do
jaigent config      # show the resolved configuration
```

## Get an API key

jaigent has no key of its own — you supply one.

| Provider | Where to get a key | Environment variable |
| --- | --- | --- |
| OpenAI (default) | <https://platform.openai.com/api-keys> | `OPENAI_API_KEY` |
| Anthropic | <https://console.anthropic.com/settings/keys> | `ANTHROPIC_API_KEY` |
| **OmniRoute** | **no key needed** — [run the gateway](#omniroute--no-api-key-at-all) | — |

> Don't want to pay for anything? Run [OmniRoute](#omniroute--no-api-key-at-all)
> locally and skip this section entirely.

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
| `jaigent schedule` | Run prompts on a timer. |
| `jaigent settings` | Read and write persistent settings. |
| `jaigent models` | Browse models known to support tool calling. |
| `jaigent tools` | List the tools available to the agent. |
| `jaigent config` | Show resolved settings; exits `1` if no API key is set. |
| `jaigent` | No arguments: logo, examples and a pointer to `--help`. |
| `jaigent --logo` | Print the logo on its own. |

The logo adapts to your terminal: full block letters when there is room, a compact
three-row wordmark in narrow windows, and a single line below ~28 columns. Colour is
dropped automatically with `--no-color` or when you pipe the output to a file, so
`jaigent --logo --no-color > banner.txt` gives you clean ASCII.

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

Answers stream token by token as the model produces them. Add `--no-stream` to wait for
the complete reply instead (markdown is rendered properly in that mode).

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
| `/workspace <path>` | Point the file tools somewhere else. |
| `/cost` | Tokens and spend for the session so far. |
| `/save` | Write to disk now. |
| `/undo` | Drop the last exchange. |
| `/exit` | Quit. |

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

Skills in `./.jaigent/skills` belong to the project and can be committed so the whole
team shares them; `~/.jaigent/skills` (or `jaigent skills new --user`) holds personal
ones. A project skill shadows a user skill of the same name. Skills are plain prompt
text — loading one can never execute code.

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
| `JAIGENT_PROVIDER` | `openai` | `openai` or `anthropic`. |
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
| `JAIGENT_HOME` | `~/.jaigent` | Where settings, skills and schedules live. |
| `JAIGENT_SCHEDULE_FILE` | `$JAIGENT_HOME/schedules.json` | Scheduled task store. |
| `OMNIROUTE_BASE_URL` | `http://localhost:20128/v1` | OmniRoute gateway location. |

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
    on_tool_call=lambda name, args, out: print(f"[{name}] {args}"),
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
| `omniroute` | **none needed** | `auto` |
| `openrouter` | `OPENROUTER_API_KEY` | `anthropic/claude-sonnet-4` |
| `groq` | `GROQ_API_KEY` | `llama-3.3-70b-versatile` |
| `deepseek` | `DEEPSEEK_API_KEY` | `deepseek-chat` |
| `mistral` | `MISTRAL_API_KEY` | `mistral-small-latest` |
| `xai` | `XAI_API_KEY` | `grok-2-latest` |
| `together` | `TOGETHER_API_KEY` | `meta-llama/Llama-3.3-70B-Instruct-Turbo` |
| `ollama` | none needed | `qwen2.5:14b` |

Browse what each one offers:

```bash
jaigent models                      # the whole catalogue, with prices
jaigent models --only omniroute
jaigent models claude               # search
```

The catalogue is a convenience, not a restriction — any model id works with `--model`.
Whatever you choose must support **tool / function calling**, or the agent can only chat.

### OmniRoute — no API key at all

[OmniRoute](https://github.com/diegosouzapw/OmniRoute) is a free, MIT-licensed gateway
you run yourself. It fronts 340 providers and 1200+ models behind one OpenAI-compatible
endpoint, with quota-aware fallback between them, and many of those models are free.

```bash
npx omniroute            # starts the gateway on http://localhost:20128
jaigent settings set provider omniroute
jaigent "what changed in Python 3.13?"
```

That is the whole setup. jaigent defaults `omniroute` to `http://localhost:20128/v1`,
uses the `auto` model so OmniRoute picks and falls back for you, and supplies a
placeholder token because a local gateway does not check one.

Address a specific model with OmniRoute's `provider/model` prefixes:

```bash
jaigent -m if/kimi-k2-thinking "explain this repo"   # free tier
jaigent -m cc/claude-sonnet-4-20250514 "review my diff"
jaigent -m glm/glm-4.7 "summarise these notes"
```

Point at a remote instance with `OMNIROUTE_BASE_URL` (or the generic `JAIGENT_BASE_URL`):

```bash
export OMNIROUTE_BASE_URL=https://omniroute.example.com/v1
export OMNIROUTE_API_KEY=sk-...      # only if the gateway enforces keys
```

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

**Shell.** `run_command` is absent from the toolset unless you pass `--allow-shell` (or set `JAIGENT_ALLOW_SHELL=1`). When enabled it runs inside the workspace, is time-limited, and refuses a blocklist of catastrophic commands. That blocklist stops accidents, not a determined adversary — treat the flag as "I trust this model with this directory".

**Network.** The agent fetches URLs the model picks. Pages are stripped to text, truncated, and never executed — but remember that fetched content is untrusted input which may attempt prompt injection. Don't combine `--allow-shell` with browsing sites you don't trust.

**Secrets.** Keys are read from the environment or `.env` (git-ignored), never written to disk by jaigent, and masked in all output including `jaigent config`.

Sensible habits: run in a dedicated directory rather than `$HOME`, keep the workspace under version control so you can see and revert what changed, and start with `--verbose` to watch what the agent actually does.

## Development

```bash
pip install -e ".[dev]"

pytest                          # run the suite
pytest --cov --cov-report=term-missing
ruff check . && ruff format .   # lint and format
mypy                            # type-check
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
├── llm/            # provider adapters (openai, anthropic)
└── tools/          # sandbox, files, web, shell
```

CI (`.github/ci.yml`) runs all of the above on Python 3.10–3.13 across Linux, macOS and Windows; see [.github/README.md](.github/README.md) to activate it.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the contribution workflow and [AGENTS.md](AGENTS.md) for conventions to follow when an AI coding agent works on this repository.

## License

[Apache License 2.0](LICENSE) © jaime-gaming
