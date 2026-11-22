/**
 * The jAIgent panel as a Cloudflare Worker.
 *
 * `npx wrangler deploy` from this folder publishes `public/` at the hostname
 * bound to the Worker, and this file answers the handful of routes that are not
 * files. There is no build step: what is in `public/` is what ships.
 *
 * What this deployment is, and what it is not
 * -------------------------------------------
 * It is a **host for the panel**. The agent does not run here and cannot: a
 * Worker on the public internet has no access to your machine, your workspace
 * or your provider key, and it must not. A turn is run by `jaigent web` on the
 * machine where the work lives; this Worker serves the page that reads what
 * that machine published to your Firebase project.
 *
 * So the split is deliberate:
 *
 *   - local (`jaigent web`)   the agent, the sessions, the checkpoints, chat;
 *   - here                    the panel's files, a health route, and whatever
 *                             your Firebase project holds.
 *
 * Nothing in this file holds a credential, and nothing here talks to Firebase
 * either: the browser does, with your project's own rules deciding what it may
 * read. A Worker secret would be a second place for keys to leak from, and the
 * client config is public by design.
 */

/** Bumped with the release; the panel shows it next to the wordmark. */
const VERSION = "0.6.0";

/**
 * The headers every response carries.
 *
 * The Content-Security-Policy here is looser than the local server's in exactly
 * one way: it allows the Firebase SDK's origin and the endpoints the SDK calls.
 * That is the point of this deployment, since a panel in the cloud exists to
 * talk to a cloud store, and it is still `default-src 'none'`, so anything not
 * named here is refused. Inline scripts and styles stay forbidden: the panel has none.
 */
const CSP = [
  "default-src 'none'",
  "script-src 'self' https://www.gstatic.com",
  "style-src 'self'",
  "img-src 'self' data:",
  "font-src 'self'",
  "manifest-src 'self'",
  "worker-src 'self' blob:",
  "connect-src 'self' https://firestore.googleapis.com https://*.firebaseio.com",
  "https://identitytoolkit.googleapis.com https://securetoken.googleapis.com",
  "https://*.cloudfunctions.net https://*.run.app wss://*.firebaseio.com",
  "base-uri 'none'",
  "frame-ancestors 'none'",
  "form-action 'none'",
].join(" ");

/**
 * @param {Request} request
 * @param {Env} env
 * @returns {Promise<Response>}
 */
export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === "/api/health") {
      return json({
        status: "ok",
        service: "jAIgent",
        version: VERSION,
        deployment: "worker",
        chat: false,
      });
    }

    if (url.pathname === "/api/status") {
      // The same shape the local server returns, so one panel serves both. It
      // says plainly that no agent is attached and that nothing is stored here.
      return json({
        service: "jAIgent",
        version: VERSION,
        deployment: "worker",
        provider: "cloud",
        model: "n/a",
        workspace: "n/a",
        approval: "ask",
        chat: false,
        stream: false,
        sessions: 0,
        panel: true,
      });
    }

    if (url.pathname === "/api/chat") {
      return json(
        {
          error:
            "This deployment hosts the panel; it has no agent to run. Start one on the machine " +
            "that holds your workspace with `jaigent web --chat`, or mirror your sessions here " +
            "through firebase-config.js and read them.",
        },
        501,
      );
    }

    if (url.pathname.startsWith("/api/")) {
      return json(
        {
          error:
            `No API route named ${url.pathname}. ` +
            "The panel's data comes from your Firebase project.",
        },
        404,
      );
    }

    // Everything else is a file. `env.ASSETS` is wrangler's static binding; the
    // fallback keeps the Worker useful if it is ever deployed without one.
    if (env && env.ASSETS) {
      const response = await env.ASSETS.fetch(request);
      return withSecurityHeaders(response);
    }
    return json({ error: "No static assets are bound to this Worker." }, 500);
  },
};

/**
 * One JSON response, with the security headers attached.
 *
 * @param {object} payload
 * @param {number} [status]
 * @returns {Response}
 */
function json(payload, status = 200) {
  return withSecurityHeaders(
    new Response(JSON.stringify(payload), {
      status,
      headers: { "content-type": "application/json; charset=utf-8" },
    }),
  );
}

/**
 * Copy a response with the panel's headers added.
 *
 * Immutable, because a deployed panel is versioned by deploy and a stale
 * `app.js` against a fresh `index.html` is the one failure mode worth preventing
 * here. `no-store` would be wrong for assets; `no-cache` with an ETag from the
 * asset binding gets revalidation without refetching.
 *
 * @param {Response} response
 * @returns {Response}
 */
function withSecurityHeaders(response) {
  const headers = new Headers(response.headers);
  headers.set("content-security-policy", CSP);
  headers.set("x-content-type-options", "nosniff");
  headers.set("referrer-policy", "no-referrer");
  headers.set("permissions-policy", "camera=(), microphone=(), geolocation=(), interest-cohort=()");
  if (!headers.has("cache-control")) {
    headers.set("cache-control", "public, max-age=0, must-revalidate");
  }
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

/**
 * @typedef {object} Env
 * @property {{fetch(request: Request): Promise<Response>}} ASSETS
 *   Wrangler's static-asset binding, from `[assets]` in wrangler.toml.
 */
