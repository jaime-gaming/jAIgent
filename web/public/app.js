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

import {
  authState,
  cloudState,
  followSnapshot,
  initAuth,
  initCloud,
  loadSnapshot,
  publishSnapshot,
  signInWithGoogle,
  signInWithPassword,
  signOutCloud,
} from "./firebase.js";
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
  auth: null, // the Firebase sign-in state, when a hosted panel has one
  authProviders: null, // which sign-in buttons the config turned on
  revealed: false, // a gated cloud panel has been shown once signed in
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
  setDrawer(false);
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
  renderQuick();
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

/* Settings are shown here, never edited here: a browser tab is not where a key
 * or a workspace path belongs. So each row carries the value it has right now
 * and the exact flag or environment variable that changes it, grouped into
 * sections a person can scan, instead of a flat dump of internal names. */
const SETTINGS_GROUPS = [
  {
    title: "Agent",
    note: "How jAIgent thinks, and how far it will go on one turn.",
    keys: [
      "provider",
      "model",
      "base_url",
      "temperature",
      "max_tokens",
      "max_steps",
      "timeout",
      "retries",
      "failover",
      "budget",
    ],
  },
  {
    title: "Credentials",
    note: "Keys are masked before they leave the server, so a secret reads as set or unset, never as itself.",
    keys: ["api_key", "search_backend", "search_api_key"],
  },
  {
    title: "Safety",
    note: "What the agent may do without stopping to ask.",
    keys: ["approval", "allow_shell", "skills_enabled", "plugins_enabled"],
  },
  {
    title: "Workspace and history",
    note: "The one folder jAIgent can reach, and what it keeps.",
    keys: ["workspace", "checkpoints", "memory", "auto_compact"],
  },
  {
    title: "Output",
    note: "What the panel and the terminal show while it works.",
    keys: ["verbose", "stream", "show_cost"],
  },
];

const SETTINGS_FIELDS = {
  provider: { label: "Provider", help: "Which API speaks for the agent.", flag: "--provider", env: "JAIGENT_PROVIDER" },
  model: { label: "Model", help: "The model that answers.", flag: "--model", env: "JAIGENT_MODEL" },
  base_url: { label: "API address", help: "Where requests go. Empty means the provider default.", flag: "--base-url", env: "JAIGENT_BASE_URL", mono: true },
  temperature: { label: "Temperature", help: "How much the answers vary. Lower is steadier.", flag: "--temperature", env: "JAIGENT_TEMPERATURE" },
  max_tokens: { label: "Max tokens", help: "Ceiling on the length of one answer.", env: "JAIGENT_MAX_TOKENS" },
  max_steps: { label: "Max steps", help: "Tool calls allowed in one turn before it stops.", flag: "--max-steps", env: "JAIGENT_MAX_STEPS" },
  timeout: { label: "Timeout", help: "Seconds to wait on the provider.", env: "JAIGENT_TIMEOUT" },
  retries: { label: "Retries", help: "Attempts after a call fails.", flag: "--retries", env: "JAIGENT_RETRIES" },
  failover: { label: "Failover", help: "Fall back to another model when one fails.", flag: "--no-failover", env: "JAIGENT_FAILOVER" },
  budget: { label: "Budget", help: "Spend cap in USD for the run. Zero means no cap.", env: "JAIGENT_BUDGET" },

  api_key: { label: "API key", help: "Masked. Shown as set or unset, never as itself.", env: "JAIGENT_API_KEY", secret: true },
  search_backend: { label: "Search backend", help: "Where web searches run.", flag: "--search-backend", env: "JAIGENT_SEARCH_BACKEND" },
  search_api_key: { label: "Search key", help: "Masked. Only some backends need one.", env: "TAVILY_API_KEY", secret: true },

  approval: { label: "Approval", help: "ask, auto or dry-run for tools that change files.", flag: "--ask / --yes / --dry-run", env: "JAIGENT_APPROVAL", pill: true },
  allow_shell: { label: "Shell access", help: "Let the agent run shell commands.", flag: "--allow-shell", env: "JAIGENT_ALLOW_SHELL" },
  skills_enabled: { label: "Skills", help: "Load the bundled skills.", env: "JAIGENT_SKILLS" },
  plugins_enabled: { label: "Plugins", help: "Load plugins.", env: "JAIGENT_PLUGINS" },

  workspace: { label: "Workspace", help: "The one folder jAIgent can read and write.", flag: "--workspace", env: "JAIGENT_WORKSPACE", mono: true },
  checkpoints: { label: "Checkpoints", help: "Snapshot a file before writing, so a change can be undone.", flag: "--no-checkpoints", env: "JAIGENT_CHECKPOINTS" },
  memory: { label: "Memory", help: "Carry notes between sessions.", env: "JAIGENT_MEMORY" },
  auto_compact: { label: "Auto compact", help: "Trim a long history to fit the context window.", env: "JAIGENT_AUTO_COMPACT" },

  verbose: { label: "Verbose", help: "Show extra detail while it works.", flag: "--verbose", env: "JAIGENT_VERBOSE" },
  stream: { label: "Stream", help: "Show answers word by word as they arrive.", flag: "--no-stream", env: "JAIGENT_STREAM" },
  show_cost: { label: "Show cost", help: "Print what each turn cost.", flag: "--no-cost", env: "JAIGENT_SHOW_COST" },
};

function renderSettingValue(value, field) {
  if (field.secret) {
    const set = value === "<set>";
    return el("span", set ? "set" : "unset", "pill", set ? "pill-on" : "pill-off");
  }
  if (typeof value === "boolean") {
    return el("span", value ? "on" : "off", "pill", value ? "pill-on" : "pill-off");
  }
  if (field.pill) {
    return el("span", String(value), "pill", "pill-on");
  }
  const text = String(value ?? "");
  return el("span", text === "" ? "default" : text, field.mono ? "setting-value-mono" : "setting-value");
}

function settingRow(key, value) {
  const field = SETTINGS_FIELDS[key] || { label: key, help: "" };
  const node = el("div", "", "setting");
  const main = el("div", "", "setting-main");
  main.appendChild(el("div", field.label, "setting-label"));
  if (field.help) {
    main.appendChild(el("div", field.help, "setting-help"));
  }
  node.appendChild(main);
  node.appendChild(renderSettingValue(value, field));
  const change = [];
  if (field.flag) {
    change.push(field.flag);
  }
  if (field.env) {
    change.push(field.env);
  }
  if (change.length) {
    const hint = el("div", "", "setting-change");
    hint.appendChild(el("span", "change with", "setting-change-label"));
    for (const token of change) {
      hint.appendChild(el("code", token));
    }
    node.appendChild(hint);
  }
  return node;
}

function settingsCard(title, note, keys, settings) {
  const present = keys.filter((key) => key in settings);
  if (!present.length) {
    return null;
  }
  const card = el("section", "", "settings-group");
  card.appendChild(el("h3", title, "settings-group-title"));
  if (note) {
    card.appendChild(el("p", note, "settings-group-note"));
  }
  const rows = el("div", "", "settings-rows");
  for (const key of present) {
    rows.appendChild(settingRow(key, settings[key]));
  }
  card.appendChild(rows);
  return card;
}

async function loadSettings() {
  const mirrored = await fromCloud("settings");
  const payload = mirrored ? { settings: mirrored } : await api("/api/settings");
  const settings = payload.settings || {};

  const cards = [];
  const seen = new Set();
  for (const group of SETTINGS_GROUPS) {
    for (const key of group.keys) {
      seen.add(key);
    }
    const card = settingsCard(group.title, group.note, group.keys, settings);
    if (card) {
      cards.push(card);
    }
  }
  const rest = Object.keys(settings).filter((key) => !seen.has(key));
  const other = settingsCard("Other", "", rest, settings);
  if (other) {
    cards.push(other);
  }
  fill(byId("settings-table"), ...cards);

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
/* The conversation column keeps its greeting until the first turn lands, so the
 * starter cards stay in the DOM (byId would throw if they were removed) and
 * simply hide once there is something to read. */
function renderEmpty(show) {
  byId("empty-state").hidden = !show;
  byId("transcript").classList.toggle("is-empty", show);
}

function clearTurns() {
  for (const node of Array.from(byId("transcript").querySelectorAll(".turn"))) {
    node.remove();
  }
}

function setTitle(text) {
  byId("conv-title").textContent = text || "New chat";
}

/* The rail of conversations is a drawer on a narrow screen; everywhere else the
 * class and the scrim are simply inert. */
function setDrawer(open) {
  byId("app").classList.toggle("sidebar-open", open);
  byId("menu").setAttribute("aria-expanded", open ? "true" : "false");
  byId("scrim").hidden = !open;
}

function addTurn(role, text) {
  renderEmpty(false);
  const transcript = byId("transcript");
  const turn = el("div", "", "turn", `turn-${role}`);
  const label = el("div", "", "turn-role");
  if (role === "assistant") {
    const avatar = document.createElement("img");
    avatar.className = "turn-avatar";
    avatar.src = "./icon.svg";
    avatar.width = 22;
    avatar.height = 22;
    avatar.alt = "";
    label.appendChild(avatar);
    label.appendChild(document.createTextNode("jAIgent"));
  } else {
    label.textContent = "you";
  }
  turn.appendChild(label);
  turn.appendChild(el("div", text, "turn-bubble"));
  transcript.appendChild(turn);
  scrollToEnd();
  const bubble = turn.querySelector(".turn-bubble");
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
  clearTurns();
  for (const message of payload.messages) {
    addTurn(message.role === "user" ? "user" : "assistant", message.text);
  }
  renderEmpty(payload.messages.length === 0);
  setTitle(payload.title || "Conversation");
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
  clearTurns();
  renderEmpty(true);
  setTitle("New chat");
  showView("chat");
  setDrawer(false);
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
  if (!state.current) {
    setTitle(prompt.length > 46 ? `${prompt.slice(0, 46)}...` : prompt);
  }
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
        if (data.title) {
          setTitle(data.title);
        }
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

/* ------------------------------------------------------- touch and shortcuts */

/* Tappable starters. They fill the composer rather than firing it, so a thumb
 * on a phone can read what it is about to ask, and edit it, before sending. */
const QUICK_ACTIONS = [
  "List the files here",
  "Summarise this folder",
  "What changed recently?",
  "Explain what this project does",
];

function renderQuick() {
  const quick = byId("quick");
  const enabled = Boolean(state.status && state.status.chat);
  quick.hidden = !enabled;
  if (!enabled) {
    fill(quick);
    return;
  }
  if (quick.childElementCount) {
    return; // built once; a status refresh must not stack a second row
  }
  const chips = QUICK_ACTIONS.map((text) => {
    const chip = el("button", text, "chip");
    chip.type = "button";
    chip.addEventListener("click", () => {
      const prompt = byId("prompt");
      prompt.value = text;
      autoGrow(prompt);
      prompt.focus();
    });
    return chip;
  });
  fill(quick, ...chips);
}

/* On a touch device a clear sideways swipe moves between the tabs. It stays out
 * of the way of scrolling and of selecting text: only a mostly horizontal drag
 * counts, and only where the pointer is coarse, so a mouse never triggers it. */
function enableSwipe() {
  if (!(window.matchMedia && window.matchMedia("(pointer: coarse)").matches)) {
    return;
  }
  const main = document.querySelector(".main");
  if (!main) {
    return;
  }
  const order = ["chat", "sessions", "checkpoints", "settings"];
  let startX = 0;
  let startY = 0;
  let tracking = false;
  main.addEventListener(
    "touchstart",
    (event) => {
      if (event.touches.length !== 1) {
        tracking = false;
        return;
      }
      startX = event.touches[0].clientX;
      startY = event.touches[0].clientY;
      tracking = true;
    },
    { passive: true },
  );
  main.addEventListener(
    "touchend",
    (event) => {
      if (!tracking) {
        return;
      }
      tracking = false;
      const touch = event.changedTouches[0];
      const dx = touch.clientX - startX;
      const dy = touch.clientY - startY;
      if (Math.abs(dx) < 70 || Math.abs(dy) > 50) {
        return;
      }
      const index = order.indexOf(state.view);
      if (index < 0) {
        return;
      }
      const next = dx < 0 ? (index + 1) % order.length : (index - 1 + order.length) % order.length;
      showView(next);
    },
    { passive: true },
  );
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
  byId("menu").addEventListener("click", () =>
    setDrawer(!byId("app").classList.contains("sidebar-open")),
  );
  byId("scrim").addEventListener("click", () => setDrawer(false));
  enableSwipe();

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
  const bundle = readFirebaseConfig();
  try {
    state.cloud = await initCloud(bundle);
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
  /* A hosted panel has no loopback token to hand out, so it asks who you are
     instead. When Firebase is not configured this is a no-op and the panel
     opens straight through, exactly as it did before there was a sign-in. */
  if (state.mode === "cloud") {
    state.authProviders = (bundle.auth && bundle.auth.providers) || {
      google: true,
      password: true,
    };
    const gated = await enterCloud(bundle);
    if (gated) {
      const signed = Boolean(state.auth && state.auth.signedIn);
      state.revealed = signed;
      if (signed) {
        await start();
      } else {
        showAuthGate();
      }
      return;
    }
  }

  if (state.token || state.mode === "cloud") {
    await start();
  } else {
    byId("gate").hidden = false;
    byId("gate-form").hidden = false;
    byId("gate-token").focus();
  }
}

function renderCloud() {
  const cloud = byId("cloud");
  const auth = state.auth || {};
  const active = Boolean(state.cloud && state.cloud.connected);
  cloud.hidden = !(active || auth.signedIn);
  // A signed-in hosted panel names the person; a local mirror names the project.
  byId("cloud-text").textContent = auth.signedIn
    ? auth.label
    : active
      ? state.cloud.label
      : "";
  byId("signout").hidden = !(auth.available && auth.signedIn);
}

/**
 * The Firebase sign-in for a hosted panel. Does nothing when the deployment is
 * local, or when the config is the shipped placeholder, so the loopback panel a
 * reader opened with `jaigent web` keeps its token gate and never sees this.
 *
 * @param {object} bundle The readFirebaseConfig() bundle.
 * @returns {Promise<boolean>} Whether a login gate now stands between the reader
 *   and the panel.
 */
async function enterCloud(bundle) {
  let gated = false;
  try {
    gated = await initAuth(bundle);
  } catch (error) {
    gated = false;
    console.warn("cloud sign-in is off:", error && error.message ? error.message : error);
  }
  state.auth = state.auth || { available: false, signedIn: false, user: null, label: "" };
  authState((next) => {
    state.auth = next || state.auth;
    renderCloud();
    if (!state.auth.available) {
      return;
    }
    if (state.auth.signedIn) {
      if (!state.revealed) {
        state.revealed = true;
        void start();
      }
    } else if (state.revealed) {
      // Signed out, or the session lapsed: put the gate back rather than leave a
      // panel showing data the reader is no longer allowed to see.
      state.revealed = false;
      showAuthGate();
    }
  });
  wireAuthButtons();
  return gated;
}

/** Show the sign-in card and hide the panel behind it. */
function showAuthGate() {
  const providers = state.authProviders || { google: true, password: true };
  byId("gate").hidden = false;
  byId("gate-form").hidden = true;
  byId("gate-auth").hidden = false;
  byId("auth-google").hidden = !providers.google;
  byId("auth-signin").hidden = !providers.password;
  byId("auth-email").hidden = !providers.password;
  byId("auth-password").hidden = !providers.password;
  byId("app").hidden = true;
  if (providers.google) {
    byId("auth-google").focus();
  } else if (providers.password) {
    byId("auth-email").focus();
  }
}

/** Put a readable message under the sign-in buttons, or clear it. */
function authError(message) {
  const box = byId("auth-error");
  if (!message) {
    box.hidden = true;
    box.textContent = "";
    return;
  }
  box.textContent = message;
  box.hidden = false;
}

/** Attach the sign-in handlers once. */
let authWired = false;
function wireAuthButtons() {
  if (authWired) {
    return;
  }
  authWired = true;
  byId("auth-google").addEventListener("click", async () => {
    authError("");
    const result = await signInWithGoogle();
    if (!result.ok) {
      authError(result.message || "Sign-in failed.");
    }
  });
  byId("auth-signin").addEventListener("click", async () => {
    authError("");
    const result = await signInWithPassword(byId("auth-email").value, byId("auth-password").value);
    if (!result.ok) {
      authError(result.message || "Sign-in failed.");
    }
  });
  byId("auth-password").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      void byId("auth-signin").click();
    }
  });
  byId("signout").addEventListener("click", () => void signOutCloud());
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
