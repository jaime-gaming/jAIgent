---
name: jaigent
description: Use the user's existing jAIgent MCP app for direct workspace tools, research, or isolated persistent agent sessions.
---

Use the connected **remote OAuth-protected jAIgent MCP app** when the user asks
you to work in their jAIgent workspace. This skill assumes the user configured
a remote HTTP MCP endpoint; it does not make a local stdio server reachable
from ChatGPT Web.

- Use `jaigent_status` to check the existing jAIgent gateway and its advisory
  model catalogue.
- For a small, explicit action, use jAIgent's existing tools directly: inspect
  with `list_files`, `read_file` or `search_files`; research with `web_search`
  and `fetch_page`. Cite sources you actually fetched.
- Use `jaigent_chat` when the user asks jAIgent to carry out a complete task
  through its existing agent loop. Prefer `auto` unless they request a model.
- For independent or long-running conversations, call
  `jaigent_session_start`, keep its returned `session_id`, and use
  `jaigent_session_chat` for later turns. Call `jaigent_sessions_list` to find
  ChatGPT-created sessions. Do not reuse one session ID for unrelated work.
- Only call file, memory or session mutation tools when the connected app
  exposes them and the user has authorized the change. Confirm before
  destructive or irreversible actions, especially `jaigent_session_delete`.
- Never claim a file changed, a tool ran or research succeeded unless the tool
  result confirms it. Provider credentials stay on the jAIgent host; never ask
  for or include them in a tool call.
- The connected instance uses one owner-managed workspace. Do not treat it as
  isolated storage for multiple users. Do not call `localhost` or `127.0.0.1`
  from ChatGPT; Web clients need a publicly reachable HTTPS endpoint.
