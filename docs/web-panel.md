# The web panel

`jaigent web` puts the same jAIgent you run in a terminal into a browser tab:
your saved conversations, your undo history, the configuration in force, a box
you can send a turn from, and the files in the workspace, which you can read and
write from the page.

```
jaigent web                 # opens http://127.0.0.1:8765, and you can write to it
jaigent web --no-chat       # the same panel, with no turns started from the browser
jaigent web --ask           # every change asks you first, in the page
jaigent web --dry-run       # changes are refused, so nothing on disk moves
jaigent web --port 9000 --no-browser
```

Writing is the default because a panel you can open but not write to is a panel
you open once. Taking it away is a flag, and there are two ways to: `--no-chat`
(also spelled `--read-only`, the word `jaigent serve` uses) stops turns from the
browser, and `--dry-run` lets the agent read and plan while refusing every
change, including a save from the editor.

Two documents cover this. This one is about the panel as part of jAIgent: what
it serves, what it refuses, and how a turn started in a browser relates to one
started in a terminal. [web/README.md](../web/README.md) is about the folder:
how to deploy it, and how to connect it to a store.

## One implementation, two front doors

Nothing here is a second agent. The panel reads the same session store, the
same checkpoint index and the same settings file that `jaigent chat` reads, and
a turn started from the browser runs through the same `Agent`, the same tool
registry, the same workspace sandbox and the same approval policy. There is no
panel-specific model call, no panel-specific file access and no panel-specific
copy of anything.

| | terminal | panel |
| --- | --- | --- |
| Entry point | `jaigent chat` | `jaigent web`, then a browser |
| Agent | `jaigent.agent.Agent` | the same class |
| Tools | `build_default_registry(interactive=True)` | `interactive=False`, so `ask_user` is not offered |
| Sessions | `~/.jaigent/sessions`, `source="cli"` | the same directory, `source="web"` |
| Checkpoints | `jaigent undo` | listed read-only; restoring stays in the terminal |
| Approvals | a diff and a `y`/`n` key | the same diff, as a card in the page, and four buttons |
| Files | `read_file`, `write_file` | the same sandbox, with an editor you drive by hand |

### Asking in the browser

`--ask` used to be refused outright, on the reasoning that an approval prompt is
printed on a terminal and waits for a key, and a turn started from a browser has
nobody at that terminal. That was true of the terminal and says nothing about the
panel, which knows exactly where the reader is: in the tab that started the turn.
So the same policy now works, and the prompt is drawn there.

```
event: approval
data: {"id": "RVLXe4TBv18", "tool": "write_file", "arguments": {"path": "notes.md"},
       "preview": "... the same diff the terminal prints ...", "timeout": 600}
```

The turn blocks. The card shows the diff and four answers, which are the four the
terminal has always had: allow this change, always allow this tool for the rest of
this conversation, decline it, or stop the turn. "Always" is as wide as the
conversation it was answered in and no wider, which is how wide it is on a
terminal: two tabs discussing two things do not share one "stop asking me", and a
conversation picked up again in a later panel asks rather than inheriting a grant
nobody remembers making. The answer comes back as
`POST /api/approve`, on another connection, and wakes the turn.

Three edges are handled rather than left to chance:

* **Nobody answers.** After `APPROVAL_TIMEOUT`, ten minutes, the answer is no, and
  the model is told nobody answered so it does not retry or assume.
* **The tab goes away.** While it waits, the turn writes a heartbeat every fifteen
  seconds. A closed tab is discovered in seconds rather than at the timeout, and
  every question still waiting is answered with "the panel went away".
* **The tab comes back.** `GET /api/approvals` lists the questions still open, so a
  reloaded page, or a second one, can answer a turn that is still waiting.

`ask_user` is still left out of the tool registry, and the model is still told
nobody can answer a question of that shape, because a picker is not a diff: it
needs a screen of its own and the turn is already holding one.

### Editing a file yourself

The Files tab lists the workspace and opens a file in an editor. This is not a
view of what the agent last said about a file: opening one reads it from disk and
saving one writes it back, in the same workspace, under the same sandbox the file
tools use.

Four things happen on a save that a plain `Path.write_text` would not do:

* A checkpoint is taken first, labelled `web panel <path>`, so `jaigent undo` in
  the terminal reverts an edit made in a browser exactly as it reverts one made by
  the agent.
* The modification time the editor read is sent back with the text. If the file
  changed underneath, the save is refused with a `409` and the editor says so,
  rather than replacing an agent's edit with a copy older than it.
* The write goes to a temporary file in the same folder and is renamed over the
  target, carrying the original permissions, so an interrupted save leaves the old
  file readable and a saved script still runs.
* The file's own line endings are put back. A text box holds `\n` for every line
  whatever the file had, and `write_text` would then write the serving platform's
  ending over the top of that: CRLF to LF on Linux, LF to CRLF on Windows, where a
  repository whose `.gitattributes` asks for LF would show a one-line edit as a
  change to every line in the file. The editor says `CRLF kept` when that is what
  it is doing. A file with no line breaks, or one that mixes the two, gets the
  platform's own ending, which is what it got before. The rule is one helper,
  `jaigent.tools.sandbox.line_ending_of`, and the `write_file` and `edit_file`
  tools use the same one, so a file edited from the panel and then edited by the
  agent keeps one convention throughout.

Paths outside the workspace are refused, and so are the ones the file tools will
not write: `.git`, secrets, and plugin files, which the next run would import. A
file that is not UTF-8 text, or holds a NUL byte, is refused rather than shown,
because an editor that displayed it would offer to save it back mangled. Under
`--dry-run` the editor is read-only and says why; under `--ask` it confirms the
write with you before posting it.

## What it serves

| Route | What comes back |
| --- | --- |
| `/` | the panel: `index.html`, `app.js`, `styles.css`, the icon and the manifest |
| `/api/health` | that something is listening. No token, no details |
| `/api/status` | version, provider, model, workspace, whether chat is on |
| `/api/sessions?limit=&q=` | the saved conversations, newest first |
| `/api/sessions/{id}` | one conversation, with its transcript and token count |
| `/api/checkpoints?limit=` | the workspace's undo history, without file contents |
| `/api/settings` | the effective configuration, secrets masked |
| `/api/files?path=&limit=` | one folder of the workspace: names, sizes, times, and which the panel may write |
| `/api/file?path=` | one text file, with the modification time a save has to match and the line ending it uses |
| `/api/approvals` | the changes still waiting for an answer |
| `POST /api/chat` | one turn, streamed as server-sent events. Not with `--no-chat` |
| `POST /api/file` | write one file, taking a checkpoint first |
| `POST /api/approve` | answer one waiting change: `allow`, `always`, `deny` or `quit` |

The streaming reply is the interesting one. A turn is a `POST` with a body, so
`EventSource` cannot be used; the panel reads the response body itself and
parses the events:

```
event: session
data: {"id": "20261121-183002-a1b2c3d4", "title": ""}

event: saved
data: {"id": "20261121-183002-a1b2c3d4", "title": "summarise notes.md", "turns": 1}

event: tool
data: {"state": "start", "tool": "read_file", "call": "c1", "arguments": {"path": "notes.md"}}

event: tool
data: {"state": "end", "tool": "read_file", "call": "c1", "ok": true, "detail": "# Notes"}

event: token
data: {"text": "Two files "}

event: saved
data: {"id": "20261121-183002-a1b2c3d4", "title": "summarise notes.md", "turns": 1, "tokens": 4120}

event: done
data: {"output": "…", "session": "…", "tokens": 4120, "usd": 0.0008, "elapsed": 6.2}
```

Two details of that stream are load-bearing. A tool call carries the same `call`
id at its start and at its end, which is what lets the panel settle the one line
it drew instead of adding a second, so one write to a file does not read as two
things happening. And `saved` arrives twice: once with the prompt on disk before
the provider is even called, and once with the finished conversation. See
[How a conversation is saved](#how-a-conversation-is-saved).

Closing the tab, or pressing Stop, closes the connection; the server notices on
the next write and stops feeding events to nobody. Two turns on the *same*
conversation are serialised, because otherwise both would load the same history
and the second save would delete what the first had just added. Two different
conversations run in parallel.

## Two boundaries

### Loopback only

There is no `--host`. The panel binds `127.0.0.1` and refuses to be configured
otherwise, because it shows your transcripts and can start a turn that writes to
your workspace:

```
$ jaigent web --host 0.0.0.0     # not a flag that exists
$ # and WebConfig(host="0.0.0.0") raises ConfigurationError
```

Reach it from another machine with your own tunnel or VPN, where the other end
is authenticated by something you chose.

A tunnel that forwards requests under its own hostname needs one thing named
first, because the panel refuses a `Host` or an `Origin` it does not recognise:

```
jaigent web --allow-origin https://panel.example.com
JAIGENT_WEB_ALLOWED_ORIGINS=https://panel.example.com jaigent web
```

That widens which *page* may talk to the panel and nothing else: the socket is
still loopback, so only something you already put in front of it can reach the
port, and every request still needs the token. A wildcard is refused rather than
interpreted, since `*` would undo the whole check. If you also want to embed the
panel in a frame on your own dashboard, `--allow-frame ORIGIN` names who may;
the default is `frame-ancestors 'none'`, because a panel nobody can see is a
panel somebody is clickjacking.

### One token per run

Loopback is a boundary, not proof that whoever is asking is you: everything
else running on your machine can reach this port too. So `jaigent web`
generates a token, prints it inside the address it opens, and requires it on
every API request.

```
  → Panel    http://127.0.0.1:8765
  → Access   chat on (approval: ask, answered in the browser)
  → Scope    this machine only (/home/you/project)

  Open this: http://127.0.0.1:8765/#token=Xy9…
```

The token rides in the *fragment*, which the browser never sends to a server,
so it cannot end up in a log line or a `Referer` header; the panel reads it
once and clears it from the address bar. It is never written to disk, never
logged, and compared with `secrets.compare_digest`. `--token` lets you fix one
for a bookmarked address, which is a deliberate trade: a token in a bookmark
outlives the run that made it.

Two more checks run before any route is dispatched:

- **`Host`.** DNS rebinding is the threat: `evil.example` resolves to
  `127.0.0.1` and the browser then treats this server as same-origin with a
  page that has no business here. The one header an attacker cannot rewrite is
  the `Host` the browser sends, so a `Host` that does not name this machine is
  refused.
- **`Origin`.** A request carrying an `Origin` that is not this panel's own is
  refused, including `null`, which is what a sandboxed iframe sends. The panel
  is served from here, so there is no legitimate cross-origin caller. Preflight
  is answered with no CORS headers at all for anyone else, which makes the
  attempt fail in the browser before the request is sent.

## What the page may do

The response headers are the same promise the code makes:

```
content-security-policy: default-src 'none'; script-src 'self'; style-src 'self';
  img-src 'self' data:; connect-src 'self'; font-src 'self'; manifest-src 'self';
  base-uri 'none'; frame-ancestors 'none'; form-action 'none'
cache-control: no-store
x-content-type-options: nosniff
referrer-policy: no-referrer
```

A transcript is model output. If any of it were parsed as markup, one crafted
answer would run in the page that displays it, so the panel builds text nodes
and never assigns to `innerHTML`; there is a test that fails if that changes.
`no-store` because your conversations are not something to leave in a shared
cache. `frame-ancestors 'none'` because a panel nobody can see is a panel
somebody is clickjacking.

The one thing that can widen the policy is Firebase. `web/public/firebase-config.js`
ships with every field empty, and while it stays that way the panel may not
load remote code at all. Put your project's public client config in it and the
server adds the SDK's origin and the endpoints it calls, and nothing else. The
check is on the file the server itself serves, so it cannot be tripped by a
header, a query parameter or an environment variable.

## Signing in to a panel you host

A panel you open with `jaigent web` runs on this machine and is gated by the
pairing token the command prints. There is nothing to sign in to: the token is
the gate, and it never leaves the tab.

A panel a Worker serves to the open internet has no token to hand out, so it
asks who you are instead. That sign-in is Firebase Auth, and it lives entirely
in the page: `web/public/firebase.js` loads `firebase-auth.js` from the same
pinned SDK as the cloud mirror, watches `onAuthStateChanged` for the whole tab,
and shows a gate before the panel reveals anything. Google and email sign-in
are offered; switch either off under `FIREBASE_AUTH.providers` in
`firebase-config.js` to hide its button.

It is optional in the same way the mirror is. With `firebase-config.js` left
empty, or with `FIREBASE_AUTH.enabled` set to `false`, `initAuth()` reports that
no login applies and the hosted panel opens straight through, exactly as it did
before there was a sign-in. A sign-in needs a project to sign in to, so an
unfilled config never enables the gate whatever the flag says.

`jaigent web login` is the companion on the terminal. It reads the same
`firebase-config.js` the panel loads and tells you whether a gate applies,
which project it points at, and which providers are on. Give it `--url
https://your-panel.example` (or set `JAIGENT_WEB_URL`) and it opens that page
for you to sign in; without one it says how to name the panel rather than
guessing an address. It stores no credential and writes no file. A Firebase web
config is public by design, what protects your data is the project's rules and
the enabled providers, and none of that is the CLI's to keep.

## How a conversation is saved

A conversation is written to the session store twice per turn, and the first of
the two happens before the provider is called:

1. **The prompt, as promised.** The history the turn will run with, plus the
   message just sent, is on disk before anything can fail. A turn that dies
   halfway, is stopped, or loses its tab used to take the whole exchange with it,
   which reads as a panel that loses your work.
2. **The conversation, however it ended.** On the way out, whatever the agent
   recorded: the full history, or a partial answer and the calls that produced
   it. Failures, the Stop button and a closed tab all end here rather than
   nowhere.

The second save keeps the agent's own history unless it is *exactly* what it was
before the run, which is how "nothing was recorded" is told apart from "less was
recorded". Length is not the test: a turn that compacted a long conversation
ends with a shorter history than it started with, and saving the longer one would
undo the compaction on disk and make the next turn pay for it twice.

Each save is announced as a `saved` event carrying the id, the title, the turn
count and the tokens, so the rail updates the row it already has instead of
refetching the list. A list rebuilt on every turn moves the row under the
pointer, loses the scroll position, and can show a title the server has already
replaced.

The store itself is the terminal's: one JSON file per conversation under
`~/.jaigent/sessions`, written atomically and owner-only, listed by
`jaigent sessions` and resumable with `jaigent chat --resume <id>`.

## The panel and the terminal at the same time

They do not share a screen, so they do't fight over one, but they do share the
session directory. A conversation started in the browser appears in
`jaigent sessions`, carries `source="web"`, and can be resumed in the terminal
with `jaigent chat --resume <id>`; the reverse works the same way. Checkpoints
taken by a browser turn are `jaigent undo` material like any other.

The gateway (`jaigent serve`, port 8787) and the panel (`jaigent web`, port
8765) are independent and can run at once. The gateway is an API for other
programs with `jgt-` keys; the panel is a page for you with a per-run token.

## If it does not start

| Symptom | Cause |
| --- | --- |
| `Could not bind 127.0.0.1:8765` | something else has the port. `--port 8766` |
| The page loads and asks for a token | you opened it without the printed address, or the tab outlived the server |
| `401` on every request | the server was restarted, so the token changed |
| `This panel was started read-only` | you passed `--no-chat` or `--read-only` |
| `This panel is in dry-run mode` | `--dry-run` refuses every write, including a save from the editor |
| `Nothing is waiting on that approval` | the turn timed out, was stopped, or was answered from another tab |
| `changed on disk since this tab read it` | reload the file: something wrote to it while you were editing |
| `The panel's files are not installed` | a broken install: the files live in `web/public` in a checkout and in `jaigent/data/web` in a wheel |
| The page is blank and the console shows a CSP error | you edited `index.html` to load something from another origin |
| `403` behind your own proxy | the proxy's hostname was not named. `--allow-origin https://your-host` |
| The panel will not appear in a frame | `frame-ancestors 'none'` by design. `--allow-frame https://your-host` |
