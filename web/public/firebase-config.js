/* Where your Firebase project's credentials go.
 *
 * This file ships **empty on purpose**. jAIgent is published, so anything
 * committed here would be public the moment it was pushed; the panel is
 * designed to be dropped into your own private repository, where this one file
 * is filled in and the rest of the folder is left alone.
 *
 * What to put here
 * ----------------
 * Firebase console → Project settings → General → "Your apps" → SDK setup and
 * configuration → Config. It is the *public* client config: an API key for a
 * Firebase web app is not a secret in the way a provider key is, it identifies
 * the project rather than authorising anything. What actually protects your
 * data is Firestore's security rules, and those live in the project, not here.
 *
 *     const FIREBASE_CONFIG = {
 *       apiKey: "AIzaSy…",
 *       authDomain: "your-project.firebaseapp.com",
 *       projectId: "your-project",
 *       storageBucket: "your-project.appspot.com",
 *       messagingSenderId: "123456789012",
 *       appId: "1:123456789012:web:abcdef0123456789",
 *     };
 *
 * Never put a service-account key, a provider API key or a database URL with
 * credentials in this folder. A browser page can only ever hold public values;
 * anything private belongs on the server or in the project's rules.
 *
 * Leaving it empty is a supported state, not a broken one: the panel then runs
 * entirely against the local jAIgent and never opens a network connection of
 * its own.
 */

/**
 * The Firebase web client configuration.
 *
 * Fill every field from your project's "Config" block, or leave them all empty
 * to keep the panel local-only. A partially filled config is treated as empty:
 * Firebase's own initialiser throws on a missing field, and a clear "not
 * configured" is better than a stack trace in the console.
 *
 * @type {{
 *   apiKey: string,
 *   authDomain: string,
 *   projectId: string,
 *   storageBucket: string,
 *   messagingSenderId: string,
 *   appId: string,
 * }}
 */
export const FIREBASE_CONFIG = {
  apiKey: "",
  authDomain: "",
  projectId: "",
  storageBucket: "",
  messagingSenderId: "",
  appId: "",
};

/**
 * The Firebase SDK version to load, and where from.
 *
 * Pinned rather than floating: a page that pulls `@latest` changes behaviour
 * under you, and a version that disappears breaks a panel nobody touched. The
 * SDK is loaded dynamically, and only once a config exists, so a local-only
 * panel never asks the network for anything.
 */
export const FIREBASE_SDK = {
  version: "10.12.5",
  origin: "https://www.gstatic.com/firebasejs",
};

/**
 * What the panel writes, and how long it keeps it.
 *
 * `collection` is the top-level Firestore collection. Under it the panel keeps
 * one document per browser (`deviceId`) with a `snapshots` subcollection holding
 * the latest sessions/checkpoints/settings it saw, and appends each completed
 * turn to `turns`. Nothing here contains a provider key or a file's contents:
 * transcripts are the one thing worth thinking about before you mirror them,
 * and `mirrorTranscripts` is off until you decide.
 */
export const FIREBASE_SYNC = {
  collection: "jaigent",
  deviceId: "", // empty means "derive one and keep it in localStorage"
  mirrorTranscripts: false,
};

/**
 * Whether the hosted panel asks who you are, and how.
 *
 * This does nothing on its own: it only takes effect once FIREBASE_CONFIG above
 * is filled in, because signing in needs a project to sign in to. Leave the
 * config empty and the panel shows no login, exactly as it opens no connection.
 * `google` needs the Google provider turned on in your Firebase console, and
 * `password` needs Email/Password turned on there; switch either off here to
 * hide its button. Sign-in gates the *hosted* panel, the one a Worker serves to
 * the open internet. A panel you opened with `jaigent web` stays on its loopback
 * token and never shows this.
 */
export const FIREBASE_AUTH = {
  enabled: true,
  providers: { google: true, password: true },
};

/**
 * Whether the config is complete enough to initialise Firebase with.
 *
 * @param {typeof FIREBASE_CONFIG} config
 * @returns {boolean}
 */
export function isConfigured(config) {
  const required = ["apiKey", "authDomain", "projectId", "appId"];
  return required.every((name) => typeof config[name] === "string" && config[name].trim() !== "");
}

/**
 * Everything the panel needs from this file, in one call.
 *
 * @returns {{config: typeof FIREBASE_CONFIG, sdk: typeof FIREBASE_SDK,
 *            sync: typeof FIREBASE_SYNC, configured: boolean}}
 */
export function readFirebaseConfig() {
  return {
    config: FIREBASE_CONFIG,
    sdk: FIREBASE_SDK,
    sync: FIREBASE_SYNC,
    auth: FIREBASE_AUTH,
    configured: isConfigured(FIREBASE_CONFIG),
  };
}
