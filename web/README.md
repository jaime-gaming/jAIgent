# The jAIgent web panel

One folder. Lift it out, deploy it, or run it from the CLI: there is no build
step, no framework and no dependency to install. Three ES modules, one
stylesheet, one HTML file, and a Cloudflare Worker that can host them.

```
web/
├── public/                 the panel itself, served as written
│   ├── index.html          the page: a gate, a top bar, a sidebar, a chat view
│   ├── app.js              what the page does: the API client, the views, the stream reader
│   ├── styles.css          how it looks, in the brand's four colours
│   ├── firebase.js         the optional cloud store: reads and mirrors snapshots
│   ├── firebase-config.js  ← the only file you are meant to edit
│   ├── manifest.webmanifest
│   ├── icon.svg
│   └── robots.txt          disallow everything: this is a private page
├── worker.js               the Cloudflare Worker: /api/health, /api/status, then assets
├── wrangler.toml           how it deploys
├── package.json            three scripts, one dev dependency (wrangler)
└── README.md               this file
```

The Python side of the story, the local server that gives this page its data,
is documented in [../docs/web-panel.md](../docs/web-panel.md).

## Run it

From a jAIgent checkout or an installed release:

```
jaigent web                    # read-only panel on http://127.0.0.1:8765
jaigent web --chat --yes       # sending allowed
```

That command serves `public/` and the API behind it, prints an address carrying
a pairing token, and opens your browser. Without jAIgent installed, you can
still look at the page:

```
npm run preview                # python3 -m http.server on 127.0.0.1:8790
```

which is useful for layout work and useless for everything else, since there is
no API behind it: the page will sit on its connect screen.

## Deploy it

```
cd web
npx wrangler login             # once
npx wrangler deploy            # publishes public/ and worker.js
```

Then bind the hostname on the Worker's Triggers tab, or in `wrangler.toml`:

```toml
routes = [{ pattern = "jaigent.jaime-gaming.com", custom_domain = true }]
```

A custom domain needs the zone (`jaime-gaming.com`) in the same Cloudflare
account. Nothing in this folder is secret, so there is no `wrangler secret` to
set and no environment variable to configure: the Worker serves files and two
JSON routes.

### What a deployed panel is

It hosts the page. **It does not and cannot run your agent.** A Worker on the
public internet has no route to your machine, your workspace or your provider
key, and `worker.js` says so rather than pretending: `POST /api/chat` answers
`501` with an explanation, and `/api/status` reports `chat: false`.

So a deployed panel is a *reader*. It shows whatever your local jAIgent
published to your Firebase project, which is what the next section is for. This
also means the browser will not let a page on `https://` call
`http://127.0.0.1`: mixed content and private-network access both block it. The
supported way to drive the agent from a browser is `jaigent web`, which serves
the page and the API from the same origin on your own machine.

## Connect it to Firebase

`public/firebase-config.js` is the one file meant to be edited, and it ships
empty on purpose. jAIgent is published, so anything committed here would be
public the moment it was pushed; this folder is designed to be dropped into
your own private repository, where that one file is filled in and nothing else
changes.

Firebase console, Project settings, General, "Your apps", SDK setup and
configuration, Config:

```js
export const FIREBASE_CONFIG = {
  apiKey: "AIza…",
  authDomain: "your-project.firebaseapp.com",
  projectId: "your-project",
  storageBucket: "your-project.appspot.com",
  messagingSenderId: "123456789012",
  appId: "1:123456789012:web:abcdef0123456789",
};
```

Those are *public* values. A Firebase web API key identifies a project rather
than authorising anything; what actually protects your data is Firestore's
security rules, and those live in the project, not here. Never put a
service-account key, a provider API key or a database URL with credentials in
this folder. A browser page can only ever hold public values.

With a config in place:

- the panel loads the Firebase SDK (pinned to one version, from `www.gstatic.com`)
  and confirms Firestore answers before it claims to be connected;
- it writes one document per browser, `jaigent/{deviceId}`, with a `snapshots`
  subcollection holding the latest sessions, checkpoints and settings it saw,
  and appends each completed turn to `turns`;
- a deployed panel reads those snapshots back, so what your machine published
  is what you see from another device;
- the local server notices the config is no longer the placeholder and widens
  its content policy by exactly the SDK origin and the endpoints it calls.

Transcripts are behind a second switch, because they are the one payload worth
thinking about before it leaves the machine:

```js
export const FIREBASE_SYNC = {
  collection: "jaigent",
  deviceId: "",              // empty means "derive one, keep it in localStorage"
  mirrorTranscripts: false,  // ← set true only if you want conversations mirrored
};
```

A minimal ruleset that lets one signed-in device write its own document and read
it back, and nobody else in:

```
rules_version = '2';
service cloud.firestore {
  match /databases/{database}/documents {
    match /jaigent/{deviceId}/{document=**} {
      allow read, write: if request.auth != null
        && request.auth.uid == deviceId;
    }
  }
}
```

Leaving the config empty is a supported state, not a broken one: the panel then
runs entirely against the local server and opens no connection of its own.

## What it does with your data

- **No telemetry.** Nothing is sent anywhere except the panel's own API and, if
  you fill in the config, your own Firebase project. There is no analytics tag,
  no font from a CDN, no third-party request of any kind.
- **No markup from a model.** Every string, including transcripts, filenames and
  session titles, becomes a text node. A crafted answer cannot draw itself.
- **The token stays in the tab.** It is read from the address fragment, kept in
  `localStorage` on that device, and cleared from the address bar immediately.
  Disconnecting removes it.
- **`robots.txt` disallows everything**, and the CSP sets `frame-ancestors
  'none'`, so the page is neither indexed nor frameable.

## Working on it

There is nothing to compile, which means the checks are the ones you can run by
hand:

```
node --check public/app.js     # it parses
npm run preview                # it draws
jaigent web                    # it works against a real server
```

`tests/test_webui.py` in the repository covers the server side, and four of its
tests read this folder as text: that the page loads one script and no inline
anything, that the code never builds HTML out of a string, that no absolute URL
points anywhere unexpected, and that no credentials ship. Editing a file here
can fail a test there, on purpose.

Two conventions worth keeping:

- Colours come from the four custom properties at the top of `styles.css`, which
  are the same four `src/jaigent/branding.py` uses. A fifth colour is a fifth
  brand.
- Motion means "the agent is working" and nothing else. The border light and the
  typing dots run only while a turn is in flight, every state is also written in
  words, and `prefers-reduced-motion` turns the movement off while leaving the
  meaning.
