/* The jAIgent panel.
 *
 * One module, no build step, no framework, no dependencies: the browser runs
 * this file as it is served. It talks to the server that served it, over
 * relative URLs, so the same page works whether it came from `jaigent web` on
 * this machine or from the Cloudflare Worker this folder deploys as.
 *
 * Two promises this file keeps:
 *
 *   - Nothing a model, a filename or a session title contributed is ever
 *     parsed as HTML. Every string becomes a text node through `textContent`,
 *     so a transcript containing markup cannot draw itself. There is no
 *     `innerHTML` in this file, and the server's Content-Security-Policy would
 *     refuse an inline script even if one crept in.
 *   - No telemetry. Nothing is sent anywhere except the panel's own API and,
 *     if and only if you fill in `firebase-config.js`, your own Firebase
 *     project.
 */

import { cloudState, followSnapshot, initCloud, loadSnapshot, publishSnapshot } from "./firebase.js";
import { readFirebaseConfig } from "./firebase-config.js";

/* ------------------------------------------------------------------ state */

/** Where the panel keeps what it needs between reloads, on this device only. */
const STORE_KEY = "jaigent.panel";

const state = {
  token: "",
  base: "",
  /* "local" when the page was served by `jaigent web` on this machine and the
     agent is attached; "cloud" when it was served by a deployment that only
     hosts the panel and reads what another machine published. */
  mode: "local",
  status: null,
  sessions: [],
  current: "", // session id loaded in the chat view, "" for a new one
  view: "chat",
  busy: false,
  abort: null, // AbortController for the turn in flight
  cloud: null, // whatever initCloud() resolved to, or null
  unfollow: null, // stops the cloud subscriptions, if any were opened
};

/* -------------------------------------------------------------- small DOM */

/**
 * Build an element with text and classes. The only way this file makes nodes.
 *
 * @param {string} tag Element name.
 * @param {string} [text] Text content, never markup.
 * @param {...string} classes Class names to add.
 * @returns {HTMLElement}
 */
function el(tag, text = "", ...classes) {
  const node = document.createElement(tag);
  if (text !== "") {
    node.textContent = text;
  }
  if (classes.length) {
    node.classList.add(...classes);
  }
  return node;
}

/** @param {string} id @returns {HTMLElement} */
function byId(id) {
  const node = document.getElementById(id);
  if (!node) {
    throw new Error(`the panel is missing #${id}`);
  }
  return node;
}

/** Replace a container's contents in one step, so it never flickers empty. */
function fill(container, ...children) {
  container.replaceChildren(...children);
}

/* ------------------------------------------------------- the pairing token */

/**
 * Find the token: in the address the CLI printed, then in this device's
 * storage. A fragment is used rather than a query string because the browser
 * never sends a fragment to a server, so it cannot reach a log line.
 */
function readTokenFromAddress() {
  const hash = window.location.hash || "";
  const match = /[#&]token=([^&]+)/.exec(hash);
  if (!match) {
    return "";
  }
  const token = decodeURIComponent(match[1]);
  // Clear it straight away: a token in the address bar ends up in history, in
  // screenshots and in anything that reads window.location later.
  const rest = hash
    .replace(/[#&]token=[^&]*/, "")
    .replace(/^[#&]+/, "")
    .replace(/^&/, "");
  const url = window.location.pathname + window.location.search + (rest ? `#${rest}` : "");
  window.history.replaceState(null, "", url);
  return token;
}

function readStored() {
  try {
    const raw = window.localStorage.getItem(STORE_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch {
    return {}; // private mode, or a hand-edited value: start clean
  }
}

function writeStored() {
  try {
    window.localStorage.setItem(
      STORE_KEY,
      JSON.stringify({ token: state.token, base: state.base }),
    );
  } catch {
    /* Storage refused. The panel still works for this tab; it just will not
       remember, which is the correct failure mode. */
  }
}

/** Forget everything and go back to the gate. */
function disconnect() {
  try {
    window.localStorage.removeItem(STORE_KEY);
  } catch {
    /* nothing to forget */
  }
  state.token = "";
  byId("app").hidden = true;
  byId("gate").hidden = false;
  byId("gate-token").focus();
}

/* ------------------------------------------------------------------- the API */

/**
 * One authenticated request to the panel's own API.
 *
 * @param {string} path Such as "/api/status".
 * @param {RequestInit & {json?: unknown}} [options]
 * @returns {Promise<unknown>}
 */
async function api(path, options = {}) {
  const init = {
    method: options.method || "GET",
    headers: {
      ...(options.json === undefined ? {} : { "content-type": "application/json" }),
      ...(state.token ? { authorization: `Bearer ${state.token}` } : {}),
      ...(options.headers || {}),
    },
    signal: options.signal,
  };
  if (options.json !== undefined) {
    init.body = JSON.stringify(options.json);
  }
  const response = await fetch(`${state.base}${path}`, init);
  const text = await response.text();
  let payload = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = null; // a proxy or an error page answered; reported below
  }
  if (!response.ok) {
    const message =
      (payload && (payload.error || (payload.error && payload.error.message))) ||
      `${response.status} ${response.statusText}`;
    const error = new Error(typeof message === "string" ? message : "request failed");
    error.status = response.status;
    throw error;
  }
  return payload;
}

/* --------------------------------------------------------------- reporting */

/**
 * Put one line of truth in the top bar. The dot's colour is never the only
 * carrier of meaning; the words next to it say the same thing.
 *
 * @param {string} text
 * @param {"idle"|"ok"|"busy"|"bad"} [kind]
 */
function report(text, kind = "idle") {
  byId("status-text").textContent = text;
  const dot = byId("status-dot");
  dot.className = `dot dot-${kind}`;
}

function reportError(error) {
  const message = error && error.message ? error.message : String(error);
  if (error && error.status === 401) {
    report("wrong or missing token", "bad");
    byId("gate-error").textContent = message;
    byId("gate-error").hidden = false;
    disconnect();
    return;
  }
  report(message, "bad");
}

/* --------------------------------------------------------------- the views */

function showView(name) {
  state.view = name;
  for (const tab of document.querySelectorAll(".tab")) {
    const active = tab.dataset.view === name;
    tab.classList.toggle("tab-active", active);
    if (active) {
      tab.setAttribute("aria-current", "page");
    } else {
      tab.removeAttribute("aria-current");
    }
  }
  for (const view of document.querySelectorAll(".view")) {
    view.hidden = view.id !== `view-${name}`;
  }
  if (name === "sessions") {
    void loadSessions();
  } else if (name === "checkpoints") {
    void loadCheckpoints();
  } else if (name === "settings") {
    void loadSettings();
  } else if (name === "chat") {
    byId("prompt").focus();
  }
}

/** @param {string} text @param {number} [at] */
function formatWhen(text, at) {
  if (!at) {
    return text || "";
  }
  const seconds = (Date.now() - at * 1000) / 1000;
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

/** One row of a listing. */
function row(cells, { head = false, click = null } = {}) {
  const node = el("div", "", "row", ...(head ? ["row-head"] : []));
  if (click) {
    node.classList.add("row-clickable");
    node.setAttribute("role", "button");
    node.tabIndex = 0;
    const open = () => click();
    node.addEventListener("click", open);
    node.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        open();
      }
    });
  }
  for (const cell of cells) {
    if (cell instanceof HTMLElement) {
      node.appendChild(cell);
    } else {
      node.appendChild(el("div", String(cell ?? ""), "row-cell"));
    }
  }
  return node;
}

async function loadStatus() {
  const payload = await api("/api/status");
  state.status = payload;
  byId("brand-version").textContent = `v${payload.version}`;
  const chat = payload.chat ? "chat on" : "read-only";
  report(`${payload.model} · ${chat} · ${payload.workspace}`, "ok");
  byId("composer-meta").textContent = payload.chat
    ? `${payload.provider} · ${payload.model} · approval ${payload.approval}`
    : state.mode === "cloud"
      ? "hosted panel · the agent runs on your own machine, not here"
      : `read-only · ${payload.provider} · ${payload.model}`;
  byId("prompt").disabled = !payload.chat;
  byId("send").disabled = !payload.chat;
  if (!payload.chat) {
    byId("prompt").placeholder =
      state.mode === "cloud"
        ? "Nothing runs here. Open the panel with `jaigent web` on the machine that holds your " +
          "workspace to send a message."
        : "This panel is read-only. Start it with `jaigent web --chat` to send from here.";
  }
  return payload;
}

/**
 * Read one listing from the cloud instead of the local API.
 *
 * @param {string} kind
 * @returns {Promise<unknown|null>}
 */
async function fromCloud(kind) {
  if (state.mode !== "cloud") {
    return null;
  }
  return loadSnapshot(kind);
}

async function loadSessions() {
  const mirrored = await fromCloud("sessions");
  if (mirrored) {
    state.sessions = Array.isArray(mirrored) ? mirrored : [];
    renderSessions();
    return;
  }
  const needle = byId("search").value.trim();
  const path = `/api/sessions?limit=100${needle ? `&q=${encodeURIComponent(needle)}` : ""}`;
  const payload = await api(path);
  state.sessions = payload.sessions;
  renderSessions();
  publishSnapshot({ kind: "sessions", payload: state.sessions });
}

function renderSessions() {
  /* The sidebar list. */
  const items = state.sessions.map((session) => {
    const item = el("li", "", "session-item");
    if (session.id === state.current) {
      item.classList.add("session-item-active");
    }
    item.appendChild(el("div", session.title, "session-title"));
    const meta = el("div", "", "session-meta");
    meta.appendChild(el("span", formatWhen(session.age, session.updated)));
    meta.appendChild(el("span", `${session.turns} turns`));
    meta.appendChild(el("span", session.source));
    item.appendChild(meta);
    item.addEventListener("click", () => void openSession(session.id));
    return item;
  });
  fill(byId("session-list"), ...items);
  byId("session-empty").hidden = state.sessions.length > 0;

  /* The table view. */
  const rows = [
    row(["Title", "Model / workspace", "When"], { head: true }),
    ...state.sessions.map((session) =>
      row([session.title, `${session.model} · ${session.workspace}`, formatWhen("", session.updated)], {
        click: () => void openSession(session.id),
      }),
    ),
  ];
  fill(byId("session-table"), ...rows);
}

async function loadCheckpoints() {
  const mirrored = await fromCloud("checkpoints");
  const payload = mirrored
    ? { checkpoints: Array.isArray(mirrored) ? mirrored : [], size: 0, workspace: "" }
    : await api("/api/checkpoints?limit=50");
  const rows = [
    row(["Snapshot", "Files", "When"], { head: true }),
    ...payload.checkpoints.map((checkpoint) =>
      row([
        checkpoint.label || checkpoint.id,
        `${checkpoint.files.length} file(s) · ${checkpoint.summary}`,
        formatWhen(checkpoint.age, checkpoint.created),
      ]),
    ),
  ];
  if (!payload.checkpoints.length) {
    rows.push(row(["No checkpoints yet. jAIgent takes one before it writes to a file."]));
  }
  fill(byId("checkpoint-list"), ...rows);
  publishSnapshot({ kind: "checkpoints", payload: payload.checkpoints });
}

async function loadSettings() {
  const mirrored = await fromCloud("settings");
  const payload = mirrored ? { settings: mirrored } : await api("/api/settings");
  const settings = payload.settings;
  const rows = [row(["Setting", "Value"], { head: true })];
  for (const [name, value] of Object.entries(settings)) {
    if (typeof value === "boolean") {
      const badge = el("span", value ? "on" : "off", "pill");
      if (value) {
        badge.classList.add("pill-on");
      }
      rows.push(row([el("div", name, "row-cell"), badge]));
    } else {
      rows.push(row([name, String(value)], {}));
    }
  }
  fill(byId("settings-table"), ...rows);
  const cloudNote = byId("cloud-note");
  cloudNote.textContent = state.cloud
    ? `Mirroring this panel's state to ${state.cloud.label}. Credentials came from your own firebase-config.js; nothing is sent anywhere else.`
    : "Not connected to a remote store. Put your Firebase project's public config in " +
      "firebase-config.js to mirror the panel's state there; the folder ships without " +
      "credentials on purpose.";
  publishSnapshot({ kind: "settings", payload: settings });
}

/* ------------------------------------------------------------- the chat view */

/** Append one turn to the transcript. */
function addTurn(role, text) {
  const transcript = byId("transcript");
  const placeholder = transcript.querySelector(".empty");
  if (placeholder) {
    placeholder.remove();
  }
  const turn = el("div", "", "turn", `turn-${role}`);
  turn.appendChild(el("div", role === "user" ? "you" : "jAIgent", "turn-role"));
  const bubble = el("div", text, "turn-bubble");
  turn.appendChild(bubble);
  transcript.appendChild(turn);
  scrollToEnd();
  return { turn, bubble, tools: null, foot: null };
}

/** The tool lines under an answer, one per call. */
function toolLine(node, text, ok = true) {
  if (!node.tools) {
    node.tools = el("div", "", "turn-tools");
    node.turn.appendChild(node.tools);
  }
  const line = el("div", text, "tool-line", ...(ok ? [] : ["tool-line-bad"]));
  node.tools.appendChild(line);
  scrollToEnd();
  return line;
}

function scrollToEnd() {
  const transcript = byId("transcript");
  // Only follow the answer if the reader is already at the bottom: scrolling
  // someone who went back to re-read is the single most annoying thing a chat
  // panel can do.
  const atBottom = transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 80;
  if (atBottom) {
    transcript.scrollTop = transcript.scrollHeight;
  }
}

/** Show or hide the "the agent is working" state. */
function setBusy(busy, label = "") {
  state.busy = busy;
  byId("composer").classList.toggle("composer-busy", busy);
  byId("composer-status").hidden = !busy;
  byId("composer-status-text").textContent = label;
  byId("send").disabled = busy || !(state.status && state.status.chat);
  byId("stop").hidden = !busy;
  byId("prompt").disabled = !(state.status && state.status.chat);
  report(busy ? label || "working" : "ready", busy ? "busy" : "ok");
}

async function openSession(id) {
  const mirrored = state.mode === "cloud" ? await loadSnapshot("session") : null;
  const payload =
    mirrored && mirrored.id === id
      ? mirrored
      : await api(`/api/sessions/${encodeURIComponent(id)}`);
  state.current = id;
  const transcript = byId("transcript");
  fill(transcript);
  for (const message of payload.messages) {
    addTurn(message.role === "user" ? "user" : "assistant", message.text);
  }
  if (!payload.messages.length) {
    transcript.appendChild(el("p", "This conversation has no readable turns.", "empty"));
  }
  showView("chat");
  for (const item of document.querySelectorAll(".session-item")) {
    item.classList.remove("session-item-active");
  }
  const index = state.sessions.findIndex((session) => session.id === id);
  const items = document.querySelectorAll(".session-item");
  if (index >= 0 && items[index]) {
    items[index].classList.add("session-item-active");
  }
  publishSnapshot({ kind: "session", payload });
}

function newChat() {
  state.current = "";
  fill(byId("transcript"), el("p", "New conversation. Ask for something below.", "empty"));
  showView("chat");
  byId("prompt").focus();
}

/* ------------------------------------------------------- server-sent events */

/**
 * Read one `text/event-stream` body, calling `onEvent` per event.
 *
 * Written by hand rather than with `EventSource` because a turn is a POST with
 * a body, and EventSource only does GET.
 */
async function readEvents(response, onEvent) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) {
      break;
    }
    buffer += decoder.decode(value, { stream: true });
    let boundary = buffer.indexOf("\n\n");
    while (boundary !== -1) {
      const block = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      const parsed = parseEvent(block);
      if (parsed) {
        onEvent(parsed.name, parsed.data);
      }
      boundary = buffer.indexOf("\n\n");
    }
  }
  const tail = parseEvent(buffer);
  if (tail) {
    onEvent(tail.name, tail.data);
  }
}

/** @param {string} block @returns {{name: string, data: unknown}|null} */
function parseEvent(block) {
  let name = "message";
  const dataLines = [];
  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) {
      name = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).replace(/^ /, ""));
    }
  }
  if (!dataLines.length) {
    return null;
  }
  const raw = dataLines.join("\n");
  try {
    return { name, data: JSON.parse(raw) };
  } catch {
    return { name, data: raw };
  }
}

/* ------------------------------------------------------------- sending a turn */

async function send() {
  const input = byId("prompt");
  const prompt = input.value.trim();
  if (!prompt || state.busy) {
    return;
  }
  if (!(state.status && state.status.chat)) {
    report("this panel is read-only", "bad");
    return;
  }
  input.value = "";
  autoGrow(input);
  addTurn("user", prompt);

  const node = addTurn("assistant", "");
  setBusy(true, "Thinking");
  const controller = new AbortController();
  state.abort = controller;

  let wrote = false;
  let tools = 0;
  const started = Date.now();

  try {
    const response = await fetch(`${state.base}/api/chat`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        accept: "text/event-stream",
        ...(state.token ? { authorization: `Bearer ${state.token}` } : {}),
      },
      body: JSON.stringify({ prompt, session: state.current }),
      signal: controller.signal,
    });
    if (!response.ok) {
      const text = await response.text();
      let message = `${response.status}`;
      try {
        message = JSON.parse(text).error || message;
      } catch {
        /* an HTML error page; the status is enough */
      }
      throw new Error(message);
    }
    await readEvents(response, (name, data) => {
      if (name === "session") {
        state.current = data.id;
        publishSnapshot({ kind: "session-start", payload: data });
      } else if (name === "token") {
        if (!wrote) {
          setBusy(true, "Writing");
        }
        wrote = true;
        node.bubble.textContent += data.text;
        scrollToEnd();
      } else if (name === "tool") {
        tools += data.state === "start" ? 1 : 0;
        setBusy(true, `Using ${data.tool}`);
        toolLine(
          node,
          `${data.state === "start" ? "›" : "·"} ${data.tool}${data.detail ? `: ${data.detail}` : ""}`,
          data.ok !== false,
        );
      } else if (name === "error") {
        toolLine(node, `! ${data.error}`, false);
      } else if (name === "done") {
        if (!wrote && data.output) {
          node.bubble.textContent = data.output;
        }
        node.foot = el(
          "div",
          `${tools} tool call(s) · ${(data.elapsed || (Date.now() - started) / 1000).toFixed(1)}s` +
            (data.tokens ? ` · ${data.tokens.toLocaleString()} tokens` : "") +
            (data.usd ? ` · $${Number(data.usd).toFixed(4)}` : "") +
            (data.stopped_early ? " · stopped early" : ""),
          "turn-foot",
        );
        node.turn.appendChild(node.foot);
        publishSnapshot({ kind: "turn", payload: { session: state.current, prompt, ...data } });
      }
    });
    setBusy(false);
    void loadSessions();
  } catch (error) {
    if (error && error.name === "AbortError") {
      toolLine(node, "· stopped", true);
      setBusy(false);
      report("stopped", "idle");
    } else {
      setBusy(false);
      reportError(error);
      toolLine(node, `! ${error.message}`, false);
    }
  } finally {
    state.abort = null;
    byId("stop").hidden = true;
  }
}

function stop() {
  if (state.abort) {
    state.abort.abort();
  }
}

/* ------------------------------------------------------------ the composer */

function autoGrow(input) {
  input.style.setProperty("height", "auto");
  const wanted = Math.min(input.scrollHeight, window.innerHeight * 0.4);
  input.style.setProperty("height", `${wanted}px`);
}

/* ------------------------------------------------------------------- startup */

async function boot() {
  byId("gate-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const token = byId("gate-token").value.trim();
    const base = byId("gate-origin").value.trim().replace(/\/+$/, "");
    if (!token) {
      byId("gate-error").textContent = "A token is required.";
      byId("gate-error").hidden = false;
      return;
    }
    state.token = token;
    state.base = base;
    writeStored();
    byId("gate-error").hidden = true;
    await start();
  });
  byId("disconnect").addEventListener("click", disconnect);
  byId("send").addEventListener("click", () => void send());
  byId("stop").addEventListener("click", stop);
  byId("new-chat").addEventListener("click", newChat);

  const prompt = byId("prompt");
  prompt.addEventListener("input", () => autoGrow(prompt));
  prompt.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void send();
    }
  });

  let timer = 0;
  byId("search").addEventListener("input", () => {
    window.clearTimeout(timer);
    timer = window.setTimeout(() => void loadSessions().catch(reportError), 220);
  });

  for (const tab of document.querySelectorAll(".tab")) {
    tab.addEventListener("click", () => showView(tab.dataset.view));
  }

  document.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      byId("search").focus();
      byId("search").select();
    } else if (event.key === "Escape" && document.activeElement === byId("search")) {
      byId("search").blur();
    }
  });

  /* The cloud store is optional and never blocks the panel: if the config is
     still the shipped placeholder, or the project cannot be reached, the panel
     runs entirely on this machine. */
  try {
    state.cloud = await initCloud(readFirebaseConfig());
  } catch (error) {
    state.cloud = null;
    console.warn("cloud sync is off:", error && error.message ? error.message : error);
  }
  renderCloud();
  cloudState(() => renderCloud());

  const stored = readStored();
  state.token = readTokenFromAddress() || stored.token || "";
  state.base = (stored.base || "").replace(/\/+$/, "");

  /* Health needs no token on either server, which is what makes it the right
     place to ask "is there an agent attached to this page?" before showing
     anyone a gate they may not need. */
  try {
    const health = await api("/api/health");
    state.mode = health && health.deployment === "worker" ? "cloud" : "local";
  } catch {
    state.mode = "local"; // unreachable: the gate explains it below
  }
  if (state.token || state.mode === "cloud") {
    await start();
  } else {
    byId("gate").hidden = false;
    byId("gate-token").focus();
  }
}

function renderCloud() {
  const cloud = byId("cloud");
  const active = Boolean(state.cloud && state.cloud.connected);
  cloud.hidden = !active;
  byId("cloud-text").textContent = active ? state.cloud.label : "";
}

async function start() {
  byId("gate").hidden = true;
  byId("app").hidden = false;
  report("connecting…", "idle");
  try {
    await loadStatus();
    await loadSessions();
    setBusy(false);
    if (state.mode === "cloud") {
      // No token to forget, so no reason to offer the button.
      byId("disconnect").hidden = true;
      byId("gate-token").required = false;
      void followCloud();
    }
    focusPrompt();
  } catch (error) {
    reportError(error);
    if (error && error.status === 401) {
      return; // disconnect() already ran; the gate is showing
    }
    // Anything else is usually "the server is not running": say so plainly and
    // let the reader fix it, rather than showing an empty panel.
    byId("gate").hidden = false;
    byId("app").hidden = true;
    byId("gate-error").textContent =
      `Could not reach jAIgent (${error.message}). Start it with \`jaigent web\` and open the ` +
      "address it prints.";
    byId("gate-error").hidden = false;
  }
}

/**
 * Keep the hosted panel up to date while the tab is open.
 *
 * Only the cloud mode subscribes: locally, the panel asks the server when a
 * view is opened, and a listener that fires on every write from the same tab
 * would just redraw what it already drew.
 */
async function followCloud() {
  const watching = await Promise.all([
    followSnapshot("sessions", (payload) => {
      state.sessions = Array.isArray(payload) ? payload : [];
      renderSessions();
    }),
    followSnapshot("checkpoints", () => {
      if (state.view === "checkpoints") void loadCheckpoints().catch(reportError);
    }),
  ]);
  state.unfollow = () => watching.forEach((stop) => stop());
}

function focusPrompt() {
  if (state.status && state.status.chat) {
    byId("prompt").focus();
  }
}

void boot();
