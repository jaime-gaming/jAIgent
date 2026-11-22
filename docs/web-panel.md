# The web panel

`jaigent web` puts the same jAIgent you run in a terminal into a browser tab:
your saved conversations, your undo history, the configuration in force, and,
if you ask for it, a box you can send a turn from.

```
jaigent web                 # read-only, opens http://127.0.0.1:8765
jaigent web --chat --yes    # sending allowed, no approval prompt to answer
jaigent web --port 9000 --no-browser
```

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
| Approvals | a diff and a `y`/`n` key | whatever policy you started the server with |

That last row is why `--chat` refuses to run under `--ask`. An approval prompt
is printed on a terminal and waits for a key; a turn started from a browser has
nobody at that terminal, so the choice would be between blocking forever and
answering itself. Neither is acceptable, so the server will not start:

```
$ jaigent web --chat
configuration error: The web panel cannot answer an approval prompt: it would be
printed on a terminal nobody is watching.
  Run it read-only:  jaigent web
  Or allow the work:  jaigent web --chat --yes
  Or refuse writes:   jaigent web --chat --dry-run
```

`ask_user` is left out of the registry for the same reason, and the model is
told nobody can answer, so it uses its own judgement instead of waiting.

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
| `POST /api/chat` | one turn, streamed as server-sent events. Only with `--chat` |

The streaming reply is the interesting one. A turn is a `POST` with a body, so
`EventSource` cannot be used; the panel reads the response body itself and
parses the events:

```
event: session
data: {"id": "20261121-183002-a1b2c3d4", "title": ""}

event: tool
data: {"state": "start", "tool": "read_file"}

event: tool
data: {"state": "end", "tool": "read_file", "ok": true, "detail": "# Notes"}

event: token
data: {"text": "Two files "}

event: done
data: {"output": "…", "session": "…", "tokens": 4120, "usd": 0.0008, "elapsed": 6.2}
```

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
  → Access   read-only (--chat to run turns from the browser)
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
| `This panel is read-only` | you did not pass `--chat` |
| `The panel's files are not installed` | a broken install: the files live in `web/public` in a checkout and in `jaigent/data/web` in a wheel |
| The page is blank and the console shows a CSP error | you edited `index.html` to load something from another origin |
| `403` behind your own proxy | the proxy's hostname was not named. `--allow-origin https://your-host` |
| The panel will not appear in a frame | `frame-ancestors 'none'` by design. `--allow-frame https://your-host` |
