# Architecture

How jAIgent is put together, for people changing it. The README covers what a
user can do; this covers where it happens in the code. Nothing here is needed
to *use* jAIgent.

The one-sentence version: an agent loop asks a provider for either text or
tool calls, executes the tool calls inside a sandbox, and repeats until the
model answers — with every mutation snapshotted, approved and reversible.

## The layer map

```
                    CLI (cli.py)          MCP server (mcp.py)     gateway (serve)
                       │                       │                       │
                       └───────────────┬───────┴───────────┬───────────┘
                                       │   Agent (agent.py) │
                                       │  ┌────────────────┴──────────────┐
                                       │  │ route → provider → tools → …  │
                                       │  └───────┬───────────────┬───────┘
                                       │          │               │
                              FailoverProvider    ToolRegistry
                                       │               │
                        llm/ adapters (openai,      tools/ (files, web,
                        anthropic, gemini)           shell, ask, plugins)
```

One `Agent`, one registry of tools, one approval policy. The three front
doors — terminal, MCP, OpenAI-compatible HTTP — all drive the same loop. That
is the whole trick behind "all your agents in one place": nothing about the
tools or the sandbox knows which door was used.

## The agent loop (`agent.py`)

`Agent.run(prompt)` is the entire product, in one function:

1. **Route.** `model="auto"` picks a model for this prompt; `model="free"`
   may also switch provider. Non-routing models skip this.
2. **Build messages:** system prompt, history, the user's prompt.
3. **Call the provider.** The reply is either plain text (done) or tool
   calls (continue). Streaming chunks are forwarded through `on_text` as
   they arrive.
4. **Execute each tool call** through `_execute`:
   snapshot → approval check → run → append the result for the model.
5. Repeat until the model answers without tool calls, or the step budget
   (`max_steps`, default 12) runs out — in which case one final call is made
   with the tools taken away, forcing an answer.

Everything the loop observes is reported through assignable callbacks:

| Callback | Fires | Used by the CLI for |
| --- | --- | --- |
| `on_tool_start` | just before a tool runs | naming the action on the status line |
| `on_approval` | just before an approval prompt | pausing the status animation |
| `on_tool_call` | after a tool runs | the quiet trace line |
| `on_text` | per streamed chunk | printing the answer live |
| `on_route` | when `auto`/`free` picks a model | the routing notice |
| `on_failover` | when a provider fails over | the retry announcement |
| `on_provider` | when the answering provider changes | "Continuing on …" |

These are the integration point for anything that wants to watch or decorate
a run — including a future web front end (see
[web-ui-proposal.md](web-ui-proposal.md)).

## Providers (`llm/`)

- `llm/base.py` — the `LLMProvider` ABC, plus `AssistantMessage` and
  `ToolCall`, the two shapes everything downstream speaks.
- `llm/openai.py` — the chat-completions wire format. Eight of the ten
  providers speak it, so adding one is a config entry, not a class.
- `llm/anthropic.py` and `llm/gemini.py` — the two genuinely different
  protocols.

`failover.py` wraps a provider in `FailoverProvider`: classify the error
(retryable or not), retry, then walk down the chain of providers that have a
key. The chain is built from the keys you actually have — nothing is
configured, it is discovered.

## Tools (`tools/`)

- `base.py` — `Tool` descriptors and the `ToolRegistry`. The registry's
  contract: **any exception becomes an `ERROR: …` string for the model**.
  A tool may fail; a run may not crash.
- `sandbox.py` — `resolve_in_workspace()`. Security-critical; every
  filesystem path in every tool goes through it. Traversal, absolute paths
  and symlink escapes are rejected there, once, for everyone.
- `files.py`, `web.py`, `shell.py`, `ask.py` — the built-ins. `shell.py` is
  the only one behind a settings flag (`--allow-shell`) and the only tool
  that cannot be snapshotted for undo.
- Plugins (`plugins.py`) add local Python tools from `./.jaigent/plugins`
  and are never fetched from the network.

## Approval and undo (`approval.py`, `checkpoint.py`)

Two independent gates protect the workspace:

1. **Before:** `Approver.check()` runs for every tool in `MUTATING_TOOLS`.
   In `ask` mode it renders a diff preview (`preview()`) and waits; `auto`
   allows; `dry-run` refuses with a message the model can relay. The
   snapshot is taken *before* this prompt, so approving and then changing
   your mind is still one `undo` away.
2. **After:** `CheckpointStore` (content-addressed, deduplicated) records
   the before-state of every path a tool will touch. `jaigent undo`,
   `/revert`, `/rewind <id>` and `/checkpoints` are all views on that store.

A tool that writes files and is not registered in `paths_for_tool()` breaks
`undo` — that mapping is the price of admission for new mutating tools.

## Configuration (`config.py`, `settings_store.py`, `paths.py`)

Five layers, highest wins: CLI flags → environment and `.env` →
`./.jaigent/settings.json` → `~/.jaigent/settings.json` → built-in defaults.
`Settings` is a frozen value object; `merged_with(**changes)` is how every
mid-session switch (`/model`, `/provider`, `/workspace`) produces a new one.

`paths.py` decides where everything lives per platform (`JAIGENT_HOME`
always wins, then XDG on Linux, `%APPDATA%` on Windows). No module calls
`Path.home()` directly — they all ask `paths`, so one root governs every
store.

## State on disk

| Path | Contents |
| --- | --- |
| `~/.jaigent/secrets.env` | provider keys, owner-only permissions |
| `~/.jaigent/settings.json` | user-level settings (never secrets) |
| `~/.jaigent/sessions/` | saved conversations |
| `./.jaigent/` | project settings, skills, plugins, commands, checkpoints, memory |
| `./.jaigent/checkpoints/` | the undo store, content-addressed |

## The front doors

- **CLI** (`cli.py`) — argparse plus rich rendering: the splash, chat REPL,
  status line, approvals, and every subcommand. All decoration lives in
  `ui.py` / `branding.py` / `picker.py`, with ASCII fallbacks so Windows
  consoles never crash on a glyph.
- **MCP** (`mcp.py`) — exposes the same tools to ChatGPT and Claude Desktop
  as tools, resources and prompts. `run_command` and `ask_user` are blocked
  here by design: no shell over MCP, and nobody is at the other end to
  answer a question.
- **The gateway** (`gateway.py`) — `jaigent serve`, an OpenAI-compatible
  `/v1` with hashed `jgt-` keys, one fresh agent per request. Approval is
  forced to `auto` because there is no terminal to ask.

## Where to start reading

The loop first (`agent.py`, ~500 lines), then `tools/sandbox.py`, then
`approval.py`. Between those three files is every guarantee the README
makes. [AGENTS.md](../AGENTS.md) has the conventions for changing each of
them safely.
