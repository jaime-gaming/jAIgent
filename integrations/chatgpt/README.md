# jAIgent for ChatGPT: remote MCP first

The supported route for **ChatGPT Web** is a custom remote MCP app created in
ChatGPT Web and pointed at the user's public HTTPS endpoint:

```text
https://<your-host>/mcp
```

That app connects inbound to `jaigent chatgpt`, which reuses the existing
jAIgent gateway, tool registry, sandbox and session store. ChatGPT cannot reach
a local `localhost`/`127.0.0.1` MCP process. Do not use
`jaigent mcp --print-config chatgpt` as the ChatGPT Web setup; that command
configures a local stdio client only.

## Configure ChatGPT Web

1. Install the optional dependency, configure OAuth and start the existing
   gateway plus the loopback-only MCP bridge. Follow the full setup and
   security instructions in the [root README](../../README.md#mcp-and-chatgpt-web).
2. In ChatGPT Web, enable Developer Mode/custom apps if your account and
   workspace permit it. Open **Settings (or Workspace Settings) → Apps →
   Create**, register `https://<your-host>/mcp`, configure OAuth and scan the
   tools. OpenAI's [MCP apps guide](https://help.openai.com/en/articles/12584461-developer-mode-and-mcp-apps-in-chatgpt)
   documents the current flow.
3. Test a read-only tool first. The plan/workspace controls whether write
   actions can be selected; OpenAI currently limits full MCP write actions to
   supported Business, Enterprise and Edu workspaces in beta. Verify the
   current plan matrix and administrator settings before enabling writes.

The existing [jAIgent plugin listing](https://chatgpt.com/plugins/plugins_6ac65df23b2081918dda77cba4e674ca)
is Desktop-only. A remote app created above is a separate Web connection; the
listing does not expose a local server to ChatGPT Web.

## Optional Plugin Creator / Codex starter

`plugin.json`, `.app.json`, `.codex-plugin/plugin.json` and `skills/jaigent/`
are a packaging starter for Plugin Creator or Codex workflows. They do **not**
host the service, create an OAuth provider, or make an app available on Web by
themselves. The empty `.app.json` is intentional: each user/workspace must map
their own remote app, and their connection identifier must not be committed.

If you package the skill, use the remote app created above—not local stdio or a
loopback URL. A plugin marked Desktop-only, or mapped to a local MCP process,
is not a ChatGPT Web solution. This packaging step is optional; the Web app can
be used directly without Plugin Creator.

## Available actions

The remote service makes the existing read-only jAIgent file/web tools
available directly, retains `jaigent_chat` for a full agent task, and adds
isolated persistent sessions through `jaigent_sessions_list`,
`jaigent_session_start` and `jaigent_session_chat`. Provider keys stay on the
jAIgent host, but any content returned by direct read tools or `load_skill` is
shared with ChatGPT as a tool result; do not put secrets in skill files. Delegated
`jaigent_chat` content is sent to the configured model provider. Session deletion and all
other non-read-only tools require both the explicit bridge write opt-in and a
writable gateway. Built-in direct file mutations use the existing checkpoint
store when checkpoints are enabled; user plugins are trusted local Python and
may operate outside that sandbox, so review them or set `JAIGENT_PLUGINS=0`.
ChatGPT's app-level confirmation is separate from the CLI's interactive diff
prompt. `run_command` is never exposed. See the
[skill](skills/jaigent/SKILL.md) for usage guidance.
