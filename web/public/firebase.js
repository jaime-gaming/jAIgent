/* The cloud store behind the panel.
 *
 * Everything in here is optional. With `firebase-config.js` left as shipped,
 * `initCloud()` resolves to `null`, `publishSnapshot()` returns immediately,
 * and the panel never opens a connection of its own. Fill the config in and the
 * same code starts mirroring what the panel sees into your Firestore project,
 * with no other file changed and nothing about the panel's behaviour depending
 * on it.
 *
 * Why Firebase rather than a database driver
 * ------------------------------------------
 * A browser page cannot hold credentials, so whatever it talks to has to be a
 * service that authenticates the *page* and enforces authorisation in rules the
 * page cannot edit. Firestore is that, and it is what this folder is prepared
 * for. The local server keeps its own store on disk; the cloud copy is a mirror
 * for reading your sessions from another device, not a second source of truth.
 *
 * The SDK is loaded with a dynamic `import()` from a pinned version, only once
 * a config exists. The server's Content-Security-Policy allows that origin only
 * when the config has actually been filled in, so a shipped panel cannot load
 * remote code at all.
 */

const listeners = new Set();

/** What to write where, taken from the config at start-up. */
let syncOptions = { collection: "jaigent", deviceId: "", mirrorTranscripts: false };

/** Where the SDK is loaded from, so later imports ask for the same version. */
let sdkBase = "";

/** @type {{connected: boolean, label: string, db?: object}|null} */
let cloud = null;

let started = null;

/**
 * Tell the panel what the cloud is doing. Called on every change, and
 * immediately for anyone who registers later.
 *
 * @param {(state: typeof cloud) => void} listener
 * @returns {() => void} A function that stops the subscription.
 */
export function cloudState(listener) {
  listeners.add(listener);
  listener(cloud);
  return () => listeners.delete(listener);
}

function setState(next) {
  cloud = next;
  for (const listener of [...listeners]) {
    try {
      listener(cloud);
    } catch {
      /* One broken subscriber must not stop the others, or the panel. */
    }
  }
}

/**
 * This browser's identity, kept in localStorage so repeated visits from the
 * same device land on the same document instead of piling up.
 *
 * @param {string} wanted An explicit id from the config, or "".
 * @returns {string}
 */
function deviceId(wanted) {
  if (wanted) {
    return wanted;
  }
  const key = "jaigent.device";
  try {
    const existing = window.localStorage.getItem(key);
    if (existing) {
      return existing;
    }
    const fresh = `web-${crypto.randomUUID().replace(/-/g, "").slice(0, 16)}`;
    window.localStorage.setItem(key, fresh);
    return fresh;
  } catch {
    /* Storage refused: a per-tab id still keeps the writes consistent. */
    return `web-${Math.random().toString(36).slice(2, 10)}`;
  }
}

/**
 * Connect, if there is anything to connect to.
 *
 * @param {{config: object, sdk: {version: string, origin: string},
 *          sync: {collection: string, deviceId: string,
 *                 mirrorTranscripts: boolean}, configured: boolean}} bundle
 * @returns {Promise<{connected: boolean, label: string, publish: Function}|null>}
 *   `null` when the config is the shipped placeholder.
 */
export async function initCloud(bundle) {
  if (!bundle || !bundle.configured) {
    setState(null);
    return null;
  }
  if (started) {
    // A reload of the module, or boot() called twice: one connection is enough.
    return started;
  }
  syncOptions = { ...syncOptions, ...bundle.sync };
  started = connect(bundle).then(
    (result) => result,
    (error) => {
      started = null;
      setState({ connected: false, label: "cloud unreachable" });
      throw error;
    },
  );
  return started;
}

/**
 * Load the SDK, initialise the app, and confirm Firestore answers.
 *
 * The check is a real read of the panel's own document rather than a successful
 * `initializeApp`, which is a local operation and tells you nothing about
 * whether the project exists or the rules let this page in.
 */
async function connect(bundle) {
  const { config, sdk, sync } = bundle;
  const base = `${sdk.origin}/${sdk.version}`;
  sdkBase = base;
  const [{ initializeApp }, { getFirestore, doc, getDoc, setDoc, collection, addDoc }] =
    await Promise.all([
      import(/* webpackIgnore: true */ `${base}/firebase-app.js`),
      import(/* webpackIgnore: true */ `${base}/firebase-firestore.js`),
    ]);

  const app = initializeApp(config);
  const db = getFirestore(app);
  const device = deviceId(sync.deviceId);
  const panel = doc(db, sync.collection, device);

  // Prove the connection before claiming it: a wrong project id or a rule that
  // denies this page both surface here, at start-up, rather than as a silent
  // failure an hour later.
  await setDoc(panel, { kind: "panel", seen: Date.now() }, { merge: true });
  await getDoc(panel);

  const label = config.projectId;
  setState({ connected: true, label });

  return {
    connected: true,
    label,
    db,
    device,
    sync,
    panel,
    /** @param {object} payload */
    publish: async (payload) => {
      const snapshots = collection(panel, "snapshots");
      await setDoc(doc(snapshots, payload.kind), { ...payload, at: Date.now() }, { merge: true });
    },
    /** @param {object} turn */
    append: async (turn) => {
      await addDoc(collection(panel, "turns"), { ...turn, at: Date.now() });
    },
  };
}

/**
 * Read back one thing the panel published earlier, from any device.
 *
 * This is what makes the hosted panel more than a static page: a Worker
 * deployment has no agent and no files, so its sessions, checkpoints and
 * settings come from whatever the local panel mirrored here.
 *
 * @param {string} kind "sessions", "checkpoints", "settings" or "session".
 * @returns {Promise<unknown|null>} The stored payload, or null.
 */
export async function loadSnapshot(kind) {
  if (!started) {
    return null;
  }
  try {
    const store = await started;
    if (!store || !store.connected) {
      return null;
    }
    const { doc, getDoc } = await import(
      /* webpackIgnore: true */ `${sdkUrl()}/firebase-firestore.js`
    );
    const snapshot = await getDoc(doc(store.panel, "snapshots", kind));
    return snapshot.exists() ? (snapshot.data().payload ?? null) : null;
  } catch {
    return null; // a cloud the panel cannot read is a cloud it does without
  }
}

/**
 * Follow one snapshot, so a second device updates while the tab is open.
 *
 * @param {string} kind
 * @param {(payload: unknown) => void} listener
 * @returns {Promise<() => void>} A function that stops the subscription.
 */
export async function followSnapshot(kind, listener) {
  if (!started) {
    return () => undefined;
  }
  try {
    const store = await started;
    if (!store || !store.connected) {
      return () => undefined;
    }
    const { doc, onSnapshot } = await import(
      /* webpackIgnore: true */ `${sdkUrl()}/firebase-firestore.js`
    );
    return onSnapshot(
      doc(store.panel, "snapshots", kind),
      (snapshot) => {
        if (snapshot.exists()) {
          listener(snapshot.data().payload ?? null);
        }
      },
      () => undefined, // a rule that denies the read is not worth an exception
    );
  } catch {
    return () => undefined;
  }
}

/** The pinned SDK base URL, remembered at start-up. */
function sdkUrl() {
  return sdkBase || "https://www.gstatic.com/firebasejs/10.12.5";
}

/**
 * Mirror one thing the panel learned. Never throws: a cloud that is down, slow
 * or unconfigured must not stop a conversation.
 *
 * @param {{kind: string, payload?: unknown}} snapshot
 * @returns {void}
 */
export function publishSnapshot(snapshot) {
  if (!cloud || !cloud.connected || !started) {
    return;
  }
  if (snapshot.kind === "session" && !syncOptions.mirrorTranscripts) {
    return; // a transcript is the one payload worth a deliberate opt-in
  }
  void started.then(
    (store) => {
      if (!store) {
        return;
      }
      if (snapshot.kind === "turn" && store.append) {
        return store.append(snapshot.payload);
      }
      return store.publish(snapshot);
    },
    () => undefined,
  );
}
