# Proposal: a local web UI for jAIgent

**Status: proposal only; no web UI is implemented.** A web UI is still on the
"discuss in an issue first" list in [AGENTS.md](../AGENTS.md) and
[CONTRIBUTING.md](../CONTRIBUTING.md). This document is planning material, not
an approved implementation specification.

This document keeps the original proposal for a browser UI hosted by the
user's own jAIgent process and adds the adjacent **ChatGPT Web** route. They
are complementary surfaces, not competing implementations: the local UI is a
readable dashboard and browser client; the ChatGPT Web app is a remote MCP
client connection to the owner's self-hosted jAIgent process. Neither creates
a hosted jAIgent service.

## Two web surfaces, one jAIgent

| Surface | Entry point | Who connects | Status and boundary |
| --- | --- | --- | --- |
| **Local jAIgent web UI** | Proposed `jaigent web` | The owner's browser to a loopback-only UI | Still a proposal; no dashboard, UI routes or browser assets are implemented. Preserve the existing phases and security questions below. |
| **ChatGPT Web** | `jaigent chatgpt` → remote Streamable HTTP MCP | ChatGPT Web initiates HTTPS/OAuth requests to the owner's public endpoint | Implemented in the 0.5.7 candidate as a separate, self-hosted MCP bridge; it is not a browser dashboard or a hosted relay. |

### ChatGPT Web connection in the broader web plan

The ChatGPT Web integration documented in the [root README](../README.md#mcp-and-chatgpt-web)
lets a user-created ChatGPT Web app invoke jAIgent directly. ChatGPT cannot
reach the user's `localhost`; the bridge binds to loopback and an operator
publishes it through a TLS reverse proxy at a public HTTPS URL. A user-managed
OAuth 2.1 authorization server is required; jAIgent validates its JWTs but does
not provide an identity service. Hosting, DNS, ChatGPT plan/workspace access
and identity-provider setup remain external requirements.

It reuses the existing `/v1` gateway, jAIgent tool registry and workspace,
checkpoint and session code. ChatGPT can choose between direct jAIgent tools,
`jaigent_chat` for a complete agent task, and isolated persistent sessions.
Read-only is the safe default; exposing mutations requires both an explicit
bridge opt-in and a writable gateway. Shell is never exposed. The optional
Plugin Creator/Codex packaging starter does not host the endpoint, create an
OAuth provider, make a Desktop-only plugin work on the Web, or replace the
remote MCP connection.

This boundary matters for the local UI proposal: `jaigent web` must remain a
separate local-only UI and must not proxy ChatGPT traffic or inherit the
remote bridge's public exposure. Both may reuse existing state and runtime
logic, but neither may add a second agent, tool implementation, session
store, central service or hidden tunnel.

## The idea

One command, `jaigent web`, starts a local page connected to the same config,
provider setup, tools, sessions, checkpoints and settings used by the CLI. The
page reads and drives existing code and files; it adds no second agent or
separate session database. No account, hosted backend or telemetry.

Three layers, each useful on its own, in proposed build order:

### Phase 1 — dashboard and history (read-only)

A local browser dashboard for saved sessions, transcripts, usage, checkpoints
and redacted settings/status. It reads the existing session store,
`CheckpointStore` and settings APIs. It introduces no sync layer, database or
filesystem mutations. A useful first slice should let the owner find a
conversation, inspect its transcript and see where its files/workspace live.

**Candidate first-release screens**

- **Sessions:** newest first; search by title/text; show age, model, turn count
  and workspace. Show a source/origin label when known (for example, CLI or
  ChatGPT); old sessions without origin metadata should display “unknown”.
- **Transcript:** read-only user/assistant messages, with tool payloads omitted
  just as the CLI's session viewer omits them. Never render untrusted message
  text as HTML.
- **Usage:** show usage that is actually stored on the session. Do not claim
  exact spend where the saved data or model price is incomplete.
- **Checkpoints:** browse the history and inspect file metadata/diffs if that
  can be done without restoring anything. No undo/rewind action in phase 1.
- **Settings/status:** show the effective, non-secret configuration. Provider
  keys, OAuth tokens, gateway keys and other secrets must remain redacted.

**Explicitly out of scope for phase 1:** starting inference, editing files,
restoring checkpoints, changing settings, invoking schedules, shell commands,
remote sharing or attaching to a running CLI. A read-only page must not expose
write routes that are merely hidden in its controls.

### Phase 2 — start and continue browser chats

The page starts a new jAIgent conversation, lists/selects several sessions and
continues the chosen one. Each session has its own history and model context;
creating two browser sessions must never merge their messages. The browser
calls the same existing agent/gateway and tool implementations as the CLI,
using the workspace's normal sandbox, checkpoints and approval policy.

The CLI's approval diff and `ask_user` picker become a diff card and button
list. A browser turn must keep the same meaning as a terminal turn: show what
will change, make the decision explicit and keep the user's option to undo.
Do not replace approvals with an unreviewed “auto” path merely because the
caller is a browser.

Session work should reuse `Session`, `list_sessions` and the existing
load/save path. Validate a session ID before loading it; do not pass an
untrusted URL segment straight to `Session.load()`. Serialize writes to one
session so two requests cannot overwrite each other's conversation. Different
session IDs may proceed independently, subject to the existing gateway's
provider/budget limits.

### Phase 3 — attach to a running CLI

An opt-in such as `jaigent chat --share` (or `/share` mid-chat) connects a live
terminal session to the browser. The CLI keeps doing the work; the page mirrors
it by listening to callbacks that already exist (`on_tool_start`,
`on_tool_call`, `on_text`, `on_approval` and ask events) and can answer
approvals/questions remotely. No async rewrite: the CLI stays single-threaded
and gains an event sink. Sharing is not part of the first release.

## Proposed architecture

- **Transport:** same-origin HTTP requests plus Server-Sent Events for
  progress/streaming, on the existing standard-library
  `ThreadingHTTPServer`. No WebSocket dependency or new framework unless a
  concrete requirement proves the stdlib route inadequate.
- **Frontend:** one packaged static HTML/CSS/JS interface, vanilla JS and no
  build step or `node_modules`. Keep browser calls relative to the page origin;
  do not make browser-facing code call another `localhost` URL.
- **Agent/runtime:** call jAIgent's existing session, tool registry, agent and
  gateway code. No second agent loop, provider client, sandbox or duplicate
  implementation of file operations.
- **Persistence:** existing JSON sessions and checkpoint/settings stores remain
  the source of truth. No database or migration in the initial version.
- **API shape (draft, not a commitment):**

  | Route | Phase | Candidate behaviour |
  | --- | --- | --- |
  | `GET /api/status` | 1 | Redacted runtime and workspace status. |
  | `GET /api/sessions` | 1 | Paginated/filterable session metadata. |
  | `GET /api/sessions/{id}` | 1 | Validated ID; read-only transcript/usage. |
  | `GET /api/checkpoints` | 1 | Read-only checkpoint metadata. |
  | `GET /api/settings` | 1 | Redacted effective settings. |
  | `POST /api/sessions` | 2 | Create a persistent session. |
  | `POST /api/sessions/{id}/messages` | 2 | Continue one session through the agent. |
  | `GET /api/events` | 2 | SSE progress for the active browser turn. |

  Keep API responses small, bounded and typed; validate every route parameter
  and request body on the server, not just in JavaScript.

## Security and local-only boundary

- Bind to `127.0.0.1` only. No `--host 0.0.0.0`, public URL, remote auth
  product or implicit tunnel. A user who wants remote access can choose their
  own SSH/VPN tunnel outside jAIgent.
- Treat loopback as a network boundary, not proof that every browser request is
  trustworthy. Validate `Host` and `Origin`; allow only the exact local
  same-origin UI; send no permissive CORS headers; defend state-changing routes
  against CSRF and DNS-rebinding-style requests.
- If browser authentication is needed, prefer a random, one-time pairing token
  printed by the CLI and delivered to the local page without putting it in
  server logs or persistent storage. Clear it from the address bar after use,
  keep it in memory/session storage, and require it on every API request.
  Decide whether phase 1 can be safely read-only without pairing before
  settling the mechanism; loopback alone is not a sufficient argument.
- Never return provider API keys, the MCP gateway key, OAuth access/refresh
  tokens, `.env` contents or secret-store contents. Redact configuration on
  the server before serializing it.
- Use the existing workspace sandbox for every path operation; reject `.git`,
  secret files, traversal and symlinks that resolve outside the workspace.
  Validate session IDs against the generated ID format before loading.
- Phase 1 has no mutation endpoints. Later writes must use the same approval,
  checkpoint and undo logic as the CLI, not a separate browser-only shortcut.
- Do not confuse this UI with ChatGPT Web. The ChatGPT custom app needs a
  public HTTPS MCP endpoint because ChatGPT's servers cannot call a user's
  localhost; this local dashboard must remain local and does not proxy
  ChatGPT.com.

## What this must not become

- A cloud relay, account system, hosted service or phone-home feature.
- A second agent implementation. The browser is another front door, like MCP
  and `serve`; the loop, tools, sandbox, sessions and approval policy stay in
  one place.
- A public web server or an internet-facing filesystem browser.
- A way to bypass the CLI's write approvals, checkpoints, undo or spend cap.

## Open questions for an implementation issue

1. Should phase 1 display every saved session (including ChatGPT-originated
   sessions) or offer a source filter? **Leaning:** show all to the local owner,
   with an explicit source label and no transcript export/share.
2. Should checkpoint diffs be in phase 1, or should it ship with metadata and
   phase-2 restore/undo controls later?
3. Can a strictly read-only phase 1 use a fresh local pairing token, or is a
   browser session cookie preferable for the cross-platform local UI?
4. Should phase 2 chat use the current CLI `Agent` instance API directly or
   route through the existing OpenAI-compatible gateway? The decision must
   preserve one shared agent/tool implementation and local ownership.
5. Does phase 3 attach only to chats explicitly started with `--share`? The
   current leaning is yes, opt-in per session.
6. Should the dashboard expose schedules, or stay focused on sessions,
   checkpoints and status through phase 2?

## Suggested next planning milestone

Before implementation, open an issue that accepts the read-only phase-1 scope,
local-only security boundary, route contract and session visibility questions.
Keep this proposal current until that decision exists. The 0.5.7 candidate has
a `jaigent chatgpt` remote MCP bridge, but that is not a UI. There is still no
`jaigent web` command, dashboard HTTP route, browser asset or hosted relay.
