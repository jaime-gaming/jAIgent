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
- [Tools](#tools)
- [Configuration](#configuration)
- [Python API](#python-api)
- [Adding your own tool](#adding-your-own-tool)
- [Using other providers](#using-other-providers)
- [Safety model](#safety-model)
- [Development](#development)
- [License](#license)

---

## Why jaigent

- **Two capabilities that matter.** Web access (search + page fetching) and a real filesystem, so the agent can research something and then write the result down.
- **Sandboxed by default.** Every file operation is confined to one workspace directory. Path traversal, absolute paths and escaping symlinks are all rejected.
- **No shell unless you ask.** Command execution is opt-in behind an explicit flag.
- **Provider agnostic.** OpenAI and Anthropic natively; anything OpenAI-compatible (OpenRouter, Groq, Together, Ollama, vLLM, LM Studio) by changing one URL.
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

Verify it:

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
| `jaigent run <prompt>` | Run one task and exit. |
| `jaigent chat` | Interactive session with memory. |
| `jaigent tools` | List the tools available to the agent. |
| `jaigent config` | Show resolved settings; exits `1` if no API key is set. |

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

Steering behaviour and observing tool calls:

```python
agent = Agent(
    Settings.from_env(),
    instructions="Always cite sources. Prefer primary documentation.",
    on_tool_call=lambda name, args, out: print(f"[{name}] {args}"),
)
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

## Using other providers

Any OpenAI-compatible endpoint works by setting `JAIGENT_BASE_URL`:

```bash
# OpenRouter
export JAIGENT_BASE_URL=https://openrouter.ai/api/v1
export JAIGENT_API_KEY=sk-or-...
export JAIGENT_MODEL=anthropic/claude-3.5-sonnet

# Groq
export JAIGENT_BASE_URL=https://api.groq.com/openai/v1
export JAIGENT_API_KEY=gsk_...
export JAIGENT_MODEL=llama-3.3-70b-versatile

# Ollama, running locally (any placeholder key)
export JAIGENT_BASE_URL=http://localhost:11434/v1
export JAIGENT_API_KEY=ollama
export JAIGENT_MODEL=qwen2.5:14b
```

Whichever you choose, the model must support **tool / function calling** — without it the agent can only chat.

Want to try the loop without spending anything? `examples/mock_llm_server.py` is a fake OpenAI-compatible server that replays a scripted plan:

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
