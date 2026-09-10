# Proposal: a web page linked to the CLI

**Status: proposal. Nothing here is built.** A web UI is on the
"discuss in an issue first" list in both [AGENTS.md](../AGENTS.md) and
[CONTRIBUTING.md](../CONTRIBUTING.md) — this page is the material for that
discussion, not a decision. If it is built, this page is where the design
lives until the README takes over.

## The idea

One command, `jaigent web`, starts a local web page that is connected to the
same things the CLI uses: the same config, keys, sessions, checkpoints and
settings. Everything already lives on disk in `~/.jaigent` and `./.jaigent`;
the page reads and drives what is there. No account, no hosted backend, no
telemetry — the same rules the CLI ships under.

Three layers, each useful on its own, in build order:

### Phase 1 — the dashboard (read-only)

Sessions, transcripts, spend over time, checkpoints, settings — everything
the CLI already stores, shown in a browser. Cheapest layer: it only *reads*
the same files `session.py`, `checkpoint.py` and `settings_store.py` already
read. No sync layer, no database, nothing new to keep consistent. It is also
the natural place for things the terminal cannot show well: spend charts,
diff history, session search.

### Phase 2 — chat in the browser

The page talks to the same agent loop the terminal does. Start a chat in the
terminal, continue it in the browser — same session store, same `/resume`
semantics. The approval diff and the `ask_user` picker become a diff card and
a button list; the picker's key mapping carries over one-to-one. The agent
runs inside the web process, the same way it runs inside `serve` today.

### Phase 3 — attach to a running CLI

`jaigent chat --share` (or `/share` mid-chat) connects a live terminal
session to the bridge. The CLI keeps doing the work; the browser mirrors the
run by listening to the callbacks that already exist — `on_tool_start`,
`on_tool_call`, `on_text`, `on_approval`, ask events — and can answer
approvals and questions remotely. No async rewrite: the CLI stays
single-threaded and just gains an event sink.

## Design decisions (argued, not settled)

- **Transport:** Server-Sent Events plus POST, on stdlib's
  `ThreadingHTTPServer`. No websocket dependency; the dependency ceiling in
  AGENTS.md is `httpx` + `rich` and this keeps it.
- **Frontend:** one static HTML file, vanilla JS, shipped inside the package.
  No build step, no node_modules, nothing to bit-rot between releases.
- **Security:** bind `127.0.0.1` only, always. A random pairing token printed
  in the terminal gates the browser session — approving a file write
  remotely is exactly as powerful as it sounds, so the terminal that started
  the run vouches for the page. Provider keys never reach the page; the
  bridge signs provider calls itself, exactly like `serve` does for `jgt-`
  keys.
- **MCP parity:** `ask_user` is blocked over MCP because nobody is at the
  other end. A paired browser page is someone at the other end — phase 3 is
  what makes remote answers legitimate.

## What this must not become

- A cloud relay, an account system, or any phone-home behaviour.
- A second agent implementation. The browser is a front door, like MCP and
  `serve`; the loop, tools, sandbox and approval policy stay in one place.
- Remote access over the open internet. If someone needs that, a Tailscale
  or SSH tunnel covers it without this project shipping crypto or auth.

## Open questions

1. Does phase 3 attach to *any* running chat, or only ones started with
   `--share`? (Leaning: opt-in per session.)
2. Should the dashboard expose the schedule runner (`schedule list` /
   `run`), or stay strictly read-only until phase 2?
3. One pairing token per `jaigent web` process, or per browser session?

## Suggested first step

Open the issue, paste the "The idea" section, and build phase 1 behind it.
Phase 1 touches nothing interactive — it is a reader of files that already
exist — so it is the low-risk slice that proves the shape.
