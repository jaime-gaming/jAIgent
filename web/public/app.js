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
  stream: null, // the turn in flight: its bubble, its buffer, its tool lines
  cloud: null, // whatever initCloud() resolved to, or null
  unfollow: null, // stops the cloud subscriptions, if any were opened
  auth: null, // the Firebase sign-in state, when a hosted panel has one
  authProviders: null, // which sign-in buttons the config turned on
  revealed: false, // a gated cloud panel has been shown once signed in
  busyLabel: "", // the last status line drawn, so a repeat costs nothing
  approvalId: "", // the change waiting for an answer, if one is
  filesPath: ".", // which folder of the workspace the file list shows
  editing: null, // {path, text, modified, editable} for the open file
  confirmSave: false, // the "write it?" card under the editor is showing
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
  } else if (name === "files") {
    void loadFiles().catch(reportError);
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
        : "This panel was started read-only, with --no-chat. Restart it without that flag " +
          "to send from here.";
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

/** Put a line under an answer, creating the rack the first time. */
function appendToolLine(node, line) {
  if (!node.tools) {
    node.tools = el("div", "", "turn-tools");
    node.turn.appendChild(node.tools);
  }
  node.tools.appendChild(line);
}

/**
 * A line about something other than a tool call: a refusal, a stop, an error.
 *
 * @param {{turn: HTMLElement, tools: HTMLElement|null}} node
 */
function toolLine(node, text, ok = true) {
  const line = el("div", text, "tool-line", ...(ok ? [] : ["tool-line-bad"]));
  appendToolLine(node, line);
  scrollToEnd();
  return line;
}

/**
 * A call beginning: one line, marked as running.
 *
 * @param {object} stream @param {{tool: string, call?: string}} data
 */
function toolStart(stream, data) {
  const call = String(data.call || "");
  const line = el("div", `\u203a ${data.tool}`, "tool-line", "tool-line-running");
  appendToolLine(stream.node, line);
  if (call) {
    stream.lines.set(call, line);
  }
  scrollToEnd();
}

/**
 * The same call ending: the line it already has, settled into what came back.
 *
 * This is the half of "one line per call" that matters. Appending a second line
 * for the ending is what made a single write to a file read as two things
 * happening, and made a long turn look twice as busy as it was.
 *
 * @param {object} stream @param {{tool: string, call?: string, detail?: string, ok?: boolean}} data
 */
function toolEnd(stream, data) {
  const call = String(data.call || "");
  let line = call ? stream.lines.get(call) : undefined;
  if (line) {
    stream.lines.delete(call);
  } else {
    // An ending with no beginning this tab saw: a server from before calls had
    // ids, or a tab opened mid-turn. Drawn rather than dropped, because the
    // alternative is a result with nothing to say it happened.
    line = el("div", "", "tool-line");
    appendToolLine(stream.node, line);
  }
  line.classList.remove("tool-line-running");
  if (data.ok === false) {
    line.classList.add("tool-line-bad");
  }
  line.textContent = `\u00b7 ${data.tool}${data.detail ? `: ${data.detail}` : ""}`;
  scrollToEnd();
}

/**
 * Draw the text that has arrived since the last frame, in one go.
 *
 * An answer comes as hundreds of small events. Writing each one to the DOM and
 * then reading the height back to scroll costs a layout per chunk, which is
 * where a streaming answer gets its stutter, and on a slow machine the later
 * chunks queue behind the rendering of the earlier ones. One append and one
 * scroll per frame instead: the same text, drawn at the rate the screen can
 * actually show it.
 *
 * @param {object} stream
 */
function flushText(stream) {
  if (stream.frame) {
    window.cancelAnimationFrame(stream.frame);
    stream.frame = 0;
  }
  if (!stream.buffer) {
    return;
  }
  stream.wrote = true;
  stream.node.bubble.classList.add("turn-bubble-streaming");
  stream.node.bubble.textContent += stream.buffer;
  stream.buffer = "";
  scrollToEnd();
}

/** @param {object} stream @param {string} chunk */
function queueText(stream, chunk) {
  stream.buffer += String(chunk ?? "");
  if (!stream.frame) {
    stream.frame = window.requestAnimationFrame(() => flushText(stream));
  }
}

/**
 * Fold one save into what the panel is already showing.
 *
 * The server says so the moment a conversation reaches the disk, which is what
 * lets the rail be updated in place instead of refetched: a list rebuilt on
 * every turn moves the row under the pointer, loses the scroll position and
 * briefly shows a title that has already been replaced.
 *
 * @param {{id: string, title: string, turns: number, updated: number, tokens: number}} saved
 */
function applySaved(saved) {
  const known = state.sessions.find((session) => session.id === saved.id);
  if (known) {
    known.title = saved.title || known.title;
    known.turns = saved.turns;
    known.updated = saved.updated;
    known.tokens = saved.tokens;
    renderSessions();
  } else {
    // A conversation this tab has never seen, started elsewhere or new here:
    // the summary does not carry the model or the workspace, so ask.
    void loadSessions().catch(reportError);
  }
  if (!state.current || state.current === saved.id) {
    state.current = saved.id;
    if (saved.title) {
      setTitle(saved.title);
    }
  }
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

/**
 * Show or hide the "the agent is working" state.
 *
 * Called once per token and once per tool event, so it returns early when there
 * is nothing to redraw: a label written again is a style recalculated again, on
 * every chunk of an answer, for a line that says the same thing it said.
 */
function setBusy(busy, label = "") {
  if (busy === state.busy && label === state.busyLabel) {
    return;
  }
  state.busyLabel = label;
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
  const input = /** @type {HTMLTextAreaElement} */ (byId("prompt"));
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
  /* Everything one turn accumulates between the events and the screen. It is
     all here, in one object, so that the frame that draws, the line a tool call
     owns and whether anything has been saved are never guessed at from the
     order events happened to arrive in. */
  const stream = {
    node,
    buffer: "",
    frame: 0,
    lines: new Map(),
    wrote: false,
    saved: false,
  };
  state.stream = stream;
  setBusy(true, "Thinking");
  const controller = new AbortController();
  state.abort = controller;

  let tools = 0;
  let finished = false;
  const started = Date.now();

  /** The answer is over, however it ended: draw what is left, stop the caret. */
  const settle = () => {
    if (finished) {
      return;
    }
    finished = true;
    flushText(stream);
    stream.node.bubble.classList.remove("turn-bubble-streaming");
    hideApproval();
    setBusy(false);
  };

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
        if (!stream.wrote) {
          setBusy(true, "Writing");
        }
        queueText(stream, data.text);
      } else if (name === "tool") {
        if (data.state === "start") {
          tools += 1;
          toolStart(stream, data);
          setBusy(true, `Using ${data.tool}`);
        } else {
          toolEnd(stream, data);
          setBusy(true, data.ok === false ? `${data.tool} refused` : "Working");
        }
      } else if (name === "approval") {
        /* The turn is stopped on a question that only the reader can answer,
           and this tab is where they are. It is the reason a panel can run
           under an ask policy instead of being refused one. */
        showApproval(data);
        setBusy(true, `Waiting for you: ${data.tool}`);
      } else if (name === "ping") {
        if (state.approvalId) {
          byId("approval-wait").textContent = `still waiting, ${Math.round(data.seconds)}s`;
        }
      } else if (name === "saved") {
        stream.saved = true;
        applySaved(data);
      } else if (name === "error") {
        flushText(stream);
        toolLine(node, `! ${data.error}`, false);
      } else if (name === "done") {
        flushText(stream);
        if (!stream.wrote && data.output) {
          // Nothing streamed, so the whole answer arrives at once. Set rather
          // than appended: a second copy of an answer that did arrive is the
          // one duplication a chat panel cannot be forgiven for.
          node.bubble.textContent = data.output;
          stream.wrote = true;
        }
        node.foot = el(
          "div",
          `${tools} tool call(s) \u00b7 ${(data.elapsed || (Date.now() - started) / 1000).toFixed(1)}s` +
            (data.tokens ? ` \u00b7 ${data.tokens.toLocaleString()} tokens` : "") +
            (data.usd ? ` \u00b7 $${Number(data.usd).toFixed(4)}` : "") +
            (data.stopped_early ? " \u00b7 stopped early" : ""),
          "turn-foot",
        );
        node.turn.appendChild(node.foot);
        publishSnapshot({ kind: "turn", payload: { session: state.current, prompt, ...data } });
      }
    });
    settle();
    if (!stream.saved) {
      void loadSessions().catch(reportError);
    }
    if (state.view === "files") {
      // A turn can write files; the list should say so without being asked.
      void loadFiles().catch(reportError);
    }
  } catch (error) {
    settle();
    if (error && error.name === "AbortError") {
      toolLine(node, "\u00b7 stopped", true);
      report("stopped", "idle");
    } else {
      reportError(error);
      toolLine(node, `! ${error.message}`, false);
    }
    if (!stream.saved) {
      void loadSessions().catch(reportError);
    }
  } finally {
    state.abort = null;
    state.stream = null;
    byId("stop").hidden = true;
    byId("prompt").focus();
  }
}

function stop() {
  if (state.abort) {
    state.abort.abort();
  }
  // A half-drawn answer is worse than a short one: whatever has arrived is put
  // on screen at once rather than left in the buffer of a turn that ended.
  if (state.stream) {
    flushText(state.stream);
    state.stream.node.bubble.classList.remove("turn-bubble-streaming");
  }
  hideApproval();
}

/* ------------------------------------------------------ asking in the panel */

/**
 * Put one change in front of the reader and wait for the answer.
 *
 * The preview is the diff the terminal would have printed, sent as text and
 * drawn into a `pre` through `textContent`, so a filename or a file's contents
 * can no more draw themselves here than anywhere else in the panel.
 *
 * @param {{id: string, tool: string, preview?: string, timeout?: number}} data
 */
function showApproval(data) {
  state.approvalId = String(data.id || "");
  byId("approval-title").textContent = `${data.tool} wants to make a change`;
  byId("approval-preview").textContent = String(data.preview || "");
  const minutes = data.timeout ? Math.round(Number(data.timeout) / 60) : 0;
  byId("approval-wait").textContent = minutes ? `waiting up to ${minutes} minutes` : "waiting";
  /* Named, and scoped, because "always" is the one answer here that keeps
     working after this card is gone. A reader who cannot tell how wide it is
     either does not use it or uses it and is surprised later. */
  const always = byId("approval-always");
  always.textContent = `Always allow ${data.tool}`;
  always.title = `Do not ask about ${data.tool} again in this conversation.`;
  byId("approval").hidden = false;
  /* Focused, because a card that answers a keystroke nobody meant to press is
     worse than one that needs a click: Enter is "allow this change". */
  byId("approval-allow").focus();
}

function hideApproval() {
  state.approvalId = "";
  byId("approval").hidden = true;
  byId("approval-preview").textContent = "";
  byId("approval-wait").textContent = "";
}

/**
 * Answer the question the turn is blocked on.
 *
 * @param {"allow"|"always"|"deny"|"quit"} decision
 */
async function answerApproval(decision) {
  const id = state.approvalId;
  if (!id) {
    return;
  }
  hideApproval();
  try {
    await api("/api/approve", { method: "POST", json: { id, decision } });
  } catch (error) {
    reportError(error);
  }
}

/**
 * Pick up a question asked while this tab was away.
 *
 * A turn keeps waiting for its answer, and a tab that was reloaded, or a second
 * tab opened on the same panel, is where that answer can still be given.
 */
async function resumeApprovals() {
  if (state.mode !== "local") {
    return;
  }
  try {
    const payload = /** @type {{approvals?: unknown[]}} */ (await api("/api/approvals"));
    const waiting = Array.isArray(payload.approvals) ? payload.approvals : [];
    if (waiting.length && !state.approvalId) {
      showApproval(/** @type {any} */ (waiting[0]));
    }
  } catch {
    /* A panel that cannot answer a question can still read everything else. */
  }
}

/* ------------------------------------------------------- files, for real */
/*
 * The workspace on this machine, listed and edited. This is not a view of what
 * the agent last said about a file: opening one reads it from disk and saving
 * one writes it back, taking the same checkpoint the agent takes, so an edit
 * made in a browser tab is an edit `jaigent undo` can revert.
 */

/** @param {number} bytes */
function formatSize(bytes) {
  if (!bytes) {
    return "";
  }
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/**
 * List one folder of the workspace.
 *
 * @param {string} [path] Defaults to the folder already on screen.
 */
async function loadFiles(path) {
  const wanted = path === undefined ? state.filesPath : path;
  const payload = /** @type {any} */ (
    await api(`/api/files?path=${encodeURIComponent(wanted)}&limit=200`)
  );
  state.filesPath = payload.path || ".";
  renderFiles(payload);
  return payload;
}

/** @param {any} payload */
function renderFiles(payload) {
  const parts = String(payload.path || ".").split("/").filter((part) => part && part !== ".");
  const crumbs = [];
  const home = el("button", "workspace", "crumb", ...(parts.length ? [] : ["crumb-here"]));
  home.type = "button";
  if (parts.length) {
    home.addEventListener("click", () => void loadFiles(".").catch(reportError));
  } else {
    home.setAttribute("aria-current", "location");
  }
  crumbs.push(home);

  let walked = "";
  parts.forEach((part, index) => {
    walked = walked ? `${walked}/${part}` : part;
    crumbs.push(el("span", "/", "crumb-sep"));
    const here = index === parts.length - 1;
    const crumb = el("button", part, "crumb", ...(here ? ["crumb-here"] : []));
    crumb.type = "button";
    if (here) {
      crumb.setAttribute("aria-current", "location");
    } else {
      const target = walked;
      crumb.addEventListener("click", () => void loadFiles(target).catch(reportError));
    }
    crumbs.push(crumb);
  });
  fill(byId("crumbs"), ...crumbs);

  const entries = Array.isArray(payload.entries) ? payload.entries : [];
  const rows = [row(["Name", "Size", "Changed"], { head: true })];
  for (const entry of entries) {
    const name = el("div", "", "row-cell", "file-name");
    name.appendChild(el("span", entry.kind === "dir" ? "\u25b8" : "\u00b7", "file-kind"));
    name.appendChild(el("span", String(entry.name)));
    if (entry.kind === "file" && !entry.editable) {
      name.appendChild(el("span", "not written from here", "file-locked"));
    }
    const target = String(entry.path);
    const node = row(
      [name, formatSize(Number(entry.size) || 0), formatWhen("", Number(entry.modified))],
      {
        click: () => {
          if (entry.kind === "dir") {
            void loadFiles(target).catch(reportError);
          } else {
            void openFile(target).catch(reportError);
          }
        },
      },
    );
    node.classList.add("file-row");
    rows.push(node);
  }
  if (!entries.length) {
    rows.push(row(["This folder is empty."]));
  }
  if (payload.truncated) {
    rows.push(row(["More files than one listing shows. Narrow it down from the terminal."]));
  }
  fill(byId("file-list"), ...rows);
}

/**
 * Read one file from disk into the editor.
 *
 * @param {string} path Workspace-relative.
 */
async function openFile(path) {
  if (editorDirty() && state.editing) {
    const discard = window.confirm(
      `${state.editing.path} has changes that are not saved. Open ${path} anyway and lose them?`,
    );
    if (!discard) {
      return;
    }
  }
  const file = /** @type {any} */ (await api(`/api/file?path=${encodeURIComponent(path)}`));
  const writes = Boolean(state.status && state.status.writes);
  state.editing = {
    path: String(file.path),
    text: String(file.text),
    modified: Number(file.modified),
    editable: Boolean(file.editable) && writes,
    lines: Number(file.lines) || 0,
    /* The ending the file on disk uses. A text box holds LF for every line, so
       this is what the save puts back, and saying it out loud is what stops a
       reader wondering why a Windows script did not come back re-ended. */
    eol: String(file.eol || "\n"),
  };
  const area = /** @type {HTMLTextAreaElement} */ (byId("editor-text"));
  area.value = state.editing.text;
  area.readOnly = !state.editing.editable;
  area.scrollTop = 0;
  byId("editor").hidden = false;
  byId("editor-path").textContent = state.editing.path;
  byId("editor-confirm").hidden = true;
  refreshEditorState();
  area.focus();
}

/** Whether the box holds something the file on disk does not. */
function editorDirty() {
  if (!state.editing) {
    return false;
  }
  return byId("editor-text").value !== state.editing.text;
}

/** @param {string} text @param {""|"saved"|"bad"|"dirty"} [kind] */
function setEditorState(text, kind = "") {
  const node = byId("editor-state");
  node.className = `editor-state${kind ? ` editor-state-${kind}` : ""}`;
  node.textContent = text;
}

/** The line under the editor, the save button and the state, kept in step. */
function refreshEditorState() {
  if (!state.editing) {
    return;
  }
  const area = /** @type {HTMLTextAreaElement} */ (byId("editor-text"));
  const dirty = editorDirty();
  const lines = area.value.split("\n").length;
  const ending = state.editing.eol === "\r\n" ? " \u00b7 CRLF kept" : "";
  if (dirty) {
    setEditorState(`unsaved \u00b7 ${lines} lines${ending}`, "dirty");
  } else {
    setEditorState(`${lines} lines${ending} \u00b7 saved on this machine`, "saved");
  }
  byId("editor-save").disabled = !dirty || !state.editing.editable;
  byId("editor-hint").textContent = state.editing.editable
    ? "Ctrl+S saves. What you write goes to the real file in this workspace, and a " +
      "checkpoint is taken first, so `jaigent undo` reverts it."
    : "Read-only here: this panel was started with --dry-run, or this is a file " +
      "jAIgent will not write to.";
}

/**
 * Write the editor's text back to the file on this machine.
 *
 * @param {boolean} [confirmed] The reader has already said yes to this exact
 *   write, which is what the panel asks for before posting under an `ask`
 *   policy.
 */
async function saveFile(confirmed = false) {
  const editing = state.editing;
  if (!editing) {
    return;
  }
  const area = /** @type {HTMLTextAreaElement} */ (byId("editor-text"));
  const text = area.value;
  const asks = Boolean(state.status && state.status.approval === "ask");
  if (asks && !confirmed) {
    state.confirmSave = true;
    byId("editor-confirm-text").textContent =
      `Write ${text.length.toLocaleString()} characters to ${editing.path} on this machine?`;
    byId("editor-confirm").hidden = false;
    byId("editor-confirm-yes").focus();
    return;
  }
  state.confirmSave = false;
  byId("editor-confirm").hidden = true;
  setEditorState("saving\u2026");
  byId("editor-save").disabled = true;
  try {
    const result = /** @type {any} */ (
      await api("/api/file", {
        method: "POST",
        json: {
          path: editing.path,
          text,
          base_modified: editing.modified,
          ...(asks ? { approved: true } : {}),
        },
      })
    );
    editing.text = text;
    editing.modified = Number(result.modified);
    editing.lines = Number(result.lines) || 0;
    refreshEditorState();
    toast(
      result.checkpoint
        ? `Saved ${result.path}. Undo it with jaigent undo (${result.checkpoint}).`
        : `Saved ${result.path}.`,
      "ok",
    );
    void loadFiles().catch(() => {});
  } catch (error) {
    if (error && error.status === 409) {
      setEditorState("changed on disk since this tab read it", "bad");
      toast("That file changed on disk. Reload it, then save again.", "bad");
      byId("editor-save").disabled = false;
    } else {
      setEditorState(error && error.message ? error.message : "could not save", "bad");
      reportError(error);
      byId("editor-save").disabled = false;
    }
  }
}

function closeEditor() {
  if (editorDirty() && state.editing) {
    const discard = window.confirm(`Discard the changes to ${state.editing.path}?`);
    if (!discard) {
      return;
    }
  }
  state.editing = null;
  state.confirmSave = false;
  byId("editor").hidden = true;
  byId("editor-confirm").hidden = true;
}

async function reloadFile() {
  if (!state.editing) {
    return;
  }
  if (editorDirty()) {
    const discard = window.confirm(
      `Throw away the changes to ${state.editing.path} and read it from disk again?`,
    );
    if (!discard) {
      return;
    }
  }
  const path = state.editing.path;
  state.editing.text = ""; // so openFile does not ask a second time
  await openFile(path);
  toast(`Read ${path} from disk again`, "ok");
}

/* ------------------------------------------------------------ the composer */

/**
 * Grow the composer to hold what is in it.
 *
 * The ceiling is most of the window rather than a fixed few lines: a reader who
 * pastes a page of a log or a whole file should see what they are about to send,
 * and the transcript above gives up the room instead of the text being cut off
 * and scrolled inside a box one line high. Below the ceiling the height is
 * exactly the content, so an ordinary one-line message still looks like one.
 *
 * @param {HTMLTextAreaElement} input
 */
function autoGrow(input) {
  // Measured from scratch each time: a height left over from a longer message
  // would otherwise become the new floor and the box would never shrink back.
  input.style.setProperty("height", "auto");
  const ceiling = Math.max(140, Math.round(window.innerHeight * 0.7));
  const wanted = Math.min(input.scrollHeight, ceiling);
  input.style.setProperty("height", `${wanted}px`);
}

/** Whether a keystroke belongs to a field that is already being typed into. */
function isField(node) {
  if (!(node instanceof HTMLElement)) {
    return false;
  }
  const tag = node.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || node.isContentEditable;
}

/**
 * Text from a paste event, or null when the clipboard holds something else.
 *
 * Null matters: a paste of a file or an image has no text to insert, and
 * calling preventDefault on it would swallow the one thing the reader meant to
 * do. Line endings are normalised because a paste from Windows carries CRLF,
 * and a message box that keeps them sends a prompt full of characters nobody
 * asked for.
 *
 * @param {ClipboardEvent} event
 * @returns {string|null}
 */
function pasteText(event) {
  const data = event.clipboardData;
  if (!data) {
    return null;
  }
  const text = data.getData("text/plain");
  if (!text) {
    return null;
  }
  return text.replace(/\r\n?/g, "\n");
}

/**
 * Put text into the composer at the caret, the way a paste should.
 *
 * `setRangeText` rather than assigning to `value`: the caret ends up after what
 * was inserted, a selection is replaced rather than appended to, and the
 * browser's own undo still undoes the paste instead of the whole box.
 *
 * @param {string} text
 * @returns {HTMLTextAreaElement}
 */
function insertIntoPrompt(text) {
  const input = /** @type {HTMLTextAreaElement} */ (byId("prompt"));
  input.focus();
  const from = input.selectionStart ?? input.value.length;
  const to = input.selectionEnd ?? input.value.length;
  input.setRangeText(text, from, to, "end");
  autoGrow(input);
  return input;
}

/**
 * Say where the typing will land.
 *
 * The caret is the browser's own and is left alone. What the panel adds is a
 * ring round the box, a lift, and the words under it, because "which of these
 * things is the field" should not need a blinking line to answer, and a reader
 * arriving with the keyboard should be able to see the focus without looking
 * for it.
 *
 * @param {boolean} focused
 */
function setComposerFocus(focused) {
  byId("composer").classList.toggle("composer-focused", focused);
  byId("composer-focus").hidden = !focused;
  byId("composer-keys").textContent = focused
    ? "Enter to send \u00b7 Shift+Enter for a new line \u00b7 paste with Ctrl+V"
    : "Click the box, or just start typing \u00b7 Enter to send";
}

/** One short line about something that worked, in and out on its own. */
let toastTimer = 0;
function toast(text, kind = "") {
  const node = byId("toast");
  node.textContent = text;
  node.className = `toast${kind ? ` toast-${kind}` : ""}`;
  node.hidden = false;
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => {
    node.hidden = true;
  }, 2600);
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
  const order = ["chat", "sessions", "files", "checkpoints", "settings"];
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

  const prompt = /** @type {HTMLTextAreaElement} */ (byId("prompt"));
  prompt.addEventListener("input", () => autoGrow(prompt));
  prompt.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void send();
    }
  });
  /* Where the typing will land, said out loud and drawn: the ring and the lift
     on the box, and the word under it. */
  prompt.addEventListener("focus", () => setComposerFocus(true));
  prompt.addEventListener("blur", () => setComposerFocus(false));
  /* Pasting is handled rather than left alone so that a paste from Windows
     arrives with Unix line endings, the box grows to hold it in the same
     moment, and a long one says how long it was. A paste of a file or an image
     has no text and is left to the browser, which is the only thing that can
     do anything with it. */
  prompt.addEventListener("paste", (event) => {
    const text = pasteText(event);
    if (text === null) {
      return;
    }
    event.preventDefault();
    insertIntoPrompt(text);
    const lines = text.split("\n").length;
    if (lines > 1) {
      toast(`Pasted ${lines} lines into the message box`, "ok");
    }
  });

  /* Ctrl+V anywhere in the chat lands in the message box. Somebody reading a
     transcript who copies a path or an error is not going to click the field
     first, and a paste that goes nowhere reads as a panel that cannot take one. */
  document.addEventListener("paste", (event) => {
    if (state.view !== "chat" || isField(event.target)) {
      return;
    }
    if (!(state.status && state.status.chat)) {
      return;
    }
    const text = pasteText(event);
    if (!text) {
      return;
    }
    event.preventDefault();
    showView("chat");
    insertIntoPrompt(text);
    toast("Pasted into the message box", "ok");
  });

  /* The editor's own wiring. */
  const area = /** @type {HTMLTextAreaElement} */ (byId("editor-text"));
  area.addEventListener("input", refreshEditorState);
  area.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      void saveFile();
    }
  });
  byId("editor-save").addEventListener("click", () => void saveFile());
  byId("editor-reload").addEventListener("click", () => void reloadFile().catch(reportError));
  byId("editor-close").addEventListener("click", closeEditor);
  byId("editor-confirm-yes").addEventListener("click", () => void saveFile(true));
  byId("editor-confirm-no").addEventListener("click", () => {
    state.confirmSave = false;
    byId("editor-confirm").hidden = true;
  });

  /* A change the agent wants to make is answered here, in the tab that asked
     for the turn. Four buttons, the same four answers the terminal's prompt
     has always had. */
  byId("approval-allow").addEventListener("click", () => void answerApproval("allow"));
  byId("approval-always").addEventListener("click", () => void answerApproval("always"));
  byId("approval-deny").addEventListener("click", () => void answerApproval("deny"));
  byId("approval-quit").addEventListener("click", () => void answerApproval("quit"));

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
      return;
    }
    if (event.key === "Escape") {
      if (state.confirmSave) {
        state.confirmSave = false;
        byId("editor-confirm").hidden = true;
        return;
      }
      if (document.activeElement === byId("search")) {
        byId("search").blur();
      }
      return;
    }
    /* Just start typing. A chat that needs a click before the first character
       is a chat that looks broken the first time somebody uses it, so a
       printable key anywhere in the chat view goes into the message box,
       caret where it was left. Shortcuts, fields and buttons are left alone:
       this is for the reader who has read the transcript and wants to answer
       it, not for somebody driving the panel by keyboard. */
    if (state.view !== "chat" || state.busy) {
      return;
    }
    if (event.metaKey || event.ctrlKey || event.altKey || event.key.length !== 1) {
      return;
    }
    if (!(state.status && state.status.chat)) {
      return;
    }
    const target = event.target;
    if (isField(target)) {
      return;
    }
    if (target instanceof HTMLElement && target.closest("button, a, [role=button]")) {
      return;
    }
    event.preventDefault();
    insertIntoPrompt(event.key);
  });

  /* The ceiling the composer grows to is a fraction of the window, so a
     rotated phone or a resized window leaves it set for the old one. */
  window.addEventListener("resize", () => {
    autoGrow(prompt);
    scrollToEnd();
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
    /* A turn already running in another tab, or one that was waiting when this
       tab was reloaded, is still waiting: pick its question up. */
    void resumeApprovals();
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
