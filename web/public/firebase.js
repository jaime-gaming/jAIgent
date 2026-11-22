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

/* The sign-in for a hosted panel.
 *
 * A page served by `jaigent web` runs on your loopback and is already gated by a
 * pairing token, so it never signs in. A page a Worker serves to the open
 * internet has no token to hand out, and this is what stands in for one: it asks
 * your Firebase project who you are before the panel shows anything.
 *
 * Like the cloud mirror, it is entirely optional. With the config left empty, or
 * with `auth.enabled` off, `initAuth()` reports that no login exists and the
 * panel behaves exactly as it did before this file grew a sign-in. Nothing here
 * talks to your own backend; Firebase Auth is the only party, and it hands back
 * a user the panel keeps in memory for the tab's lifetime.
 */

const authListeners = new Set();

/** @type {{available: boolean, signedIn: boolean, user: {email: string, name: string, uid: string}|null, label: string}} */
let auth = { available: false, signedIn: false, user: null, label: "" };

let authInstance = null;
let authBundle = null;
let authReady = null;

/**
 * Tell the panel who is signed in. Called on every change, and immediately for
 * anyone who registers later, so a late subscriber is never left guessing.
 *
 * @param {(state: typeof auth) => void} listener
 * @returns {() => void} A function that stops the subscription.
 */
export function authState(listener) {
  authListeners.add(listener);
  listener(auth);
  return () => authListeners.delete(listener);
}

function setAuth(next) {
  auth = { ...auth, ...next };
  for (const listener of [...authListeners]) {
    try {
      listener(auth);
    } catch {
      /* One broken subscriber must not stop the others, or the panel. */
    }
  }
}

/** Whether a sign-in gate applies to this deployment at all. */
export function authEnabled() {
  return Boolean(authBundle && authBundle.configured && authBundle.auth && authBundle.auth.enabled);
}

/** The Firebase app, made once whether the mirror or the login asked first. */
async function ensureApp() {
  if (!authBundle) {
    throw new Error("auth not initialised");
  }
  const { config, sdk } = authBundle;
  const base = `${sdk.origin}/${sdk.version}`;
  const [{ getApps, initializeApp }, authModule] = await Promise.all([
    import(/* webpackIgnore: true */ `${base}/firebase-app.js`),
    import(/* webpackIgnore: true */ `${base}/firebase-auth.js`),
  ]);
  const apps = getApps();
  const app = apps.length ? apps[0] : initializeApp(config);
  if (!authInstance) {
    authInstance = authModule.getAuth(app);
    // One watcher for the whole tab: a sign-in, a sign-out and a token refresh
    // all land here, so the panel never has to poll for who you are. The first
    // callback resolves `authReady`, which is how initAuth() learns about a
    // session the browser already had before showing or hiding a gate.
    authReady = new Promise((resolve) => {
      let first = true;
      authModule.onAuthStateChanged(authInstance, (user) => {
        setAuth({
          signedIn: Boolean(user),
          user: user
            ? {
                email: user.email || "",
                name: user.displayName || (user.email ? user.email.split("@")[0] : ""),
                uid: user.uid || "",
              }
            : null,
          label: user ? (user.email || user.displayName || "signed in") : "",
        });
        if (first) {
          first = false;
          resolve();
        }
      });
    });
  }
  return { authModule, auth: authInstance };
}

/**
 * Decide whether this deployment needs a login, and start watching for one if so.
 *
 * Never throws: a config that is empty, a provider the project has not turned
 * on, or a network that will not reach Firebase all leave the panel usable
 * rather than stranded on a sign-in it cannot complete.
 *
 * @param {{config: object, sdk: {version: string, origin: string},
 *          auth: {enabled: boolean, providers: object}, configured: boolean}} bundle
 * @returns {Promise<boolean>} Whether a gate now applies.
 */
export async function initAuth(bundle) {
  authBundle = bundle || null;
  if (!authEnabled()) {
    setAuth({ available: false, signedIn: false, user: null, label: "" });
    return false;
  }
  try {
    await ensureApp();
    if (authReady) {
      await authReady; // settle on a session the browser already held, if any
    }
    setAuth({ available: true });
    return true;
  } catch {
    setAuth({ available: false, label: "sign-in unavailable" });
    return false;
  }
}

/**
 * Sign in with Google, in a popup the browser already trusts.
 *
 * @returns {Promise<{ok: boolean, message?: string}>}
 */
export async function signInWithGoogle() {
  if (!authEnabled()) {
    return { ok: false, message: "Sign-in is not configured for this panel." };
  }
  try {
    const { authModule, auth: instance } = await ensureApp();
    const provider = new authModule.GoogleAuthProvider();
    await authModule.signInWithPopup(instance, provider);
    return { ok: true };
  } catch (error) {
    return { ok: false, message: authMessage(error) };
  }
}

/**
 * Sign in with an email and password from your Firebase project.
 *
 * @param {string} email
 * @param {string} password
 * @returns {Promise<{ok: boolean, message?: string}>}
 */
export async function signInWithPassword(email, password) {
  if (!authEnabled()) {
    return { ok: false, message: "Sign-in is not configured for this panel." };
  }
  if (!email || !password) {
    return { ok: false, message: "Enter both an email and a password." };
  }
  try {
    const { authModule, auth: instance } = await ensureApp();
    await authModule.signInWithEmailAndPassword(instance, email.trim(), password);
    return { ok: true };
  } catch (error) {
    return { ok: false, message: authMessage(error) };
  }
}

/**
 * Sign out. Leaves the panel on its gate rather than throwing you to a blank one.
 *
 * @returns {Promise<void>}
 */
export async function signOutCloud() {
  if (!authInstance) {
    return;
  }
  try {
    const { authModule } = await ensureApp();
    await authModule.signOut(authInstance);
  } catch {
    /* A sign-out that fails still leaves the tab usable; say nothing. */
  }
}

/** Turn a Firebase error code into a sentence worth showing a person. */
function authMessage(error) {
  const code = (error && error.code) || "";
  const table = {
    "auth/popup-closed-by-user": "The sign-in window closed before you finished.",
    "auth/popup-blocked": "Your browser blocked the sign-in window. Allow popups for this site.",
    "auth/unauthorized-domain": "This domain is not on your Firebase project's authorised list.",
    "auth/invalid-email": "That email does not look right.",
    "auth/user-not-found": "No account uses that email.",
    "auth/wrong-password": "That password did not match.",
    "auth/invalid-credential": "Those details did not match an account.",
    "auth/too-many-requests": "Too many attempts. Wait a moment and try again.",
    "auth/network-request-failed": "Could not reach Firebase. Check the connection.",
    "auth/operation-not-allowed": "That sign-in method is off in your Firebase project.",
  };
  return table[code] || (error && error.message) || "Sign-in failed.";
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
