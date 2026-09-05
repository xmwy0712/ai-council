/* AI Council web UI.
 *
 * All model/attachment text is inserted with textContent — never innerHTML —
 * so a hostile answer or file can not run script in the browser either.
 */

"use strict";

/* ------------------------------------------------------------------ utils */

const $ = (id) => document.getElementById(id);
const h = (tag, cls) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  return node;
};
const text = (node, value) => { node.textContent = value == null ? "" : String(value); };

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    let detail = `${response.status}`;
    try { detail = (await response.json()).detail || detail; } catch (_) { /* keep */ }
    const err = new Error(detail);
    err.status = response.status;
    throw err;
  }
  return response.json();
}

/* --------------------------------------------------------------- i18n data */

const DYNAMIC = {
  zh: {
    status: { starting: "启动中", running: "运行中", paused: "已暂停", awaiting_user: "等人", stalled: "停滞", completed: "已完成", stopped: "已终止", error: "出错", cancelled: "已取消" },
    phase: { P0: "上下文", P1: "提案", P2: "选定", P3: "评审", P4: "辩论", P5: "裁定", P6: "修订", P7: "终稿" },
    kind: { network: "网络", rate_limit: "限流", timeout: "超时", auth: "鉴权", contract: "契约", content_refusal: "拒答" },
    themeMode: { light: "浅色", dark: "深色" },
  },
  en: {
    status: { starting: "starting", running: "running", paused: "paused", awaiting_user: "awaiting you", stalled: "stalled", completed: "completed", stopped: "stopped", error: "error", cancelled: "cancelled" },
    phase: { P0: "context", P1: "proposals", P2: "selection", P3: "review", P4: "debate", P5: "verdict", P6: "revision", P7: "final" },
    kind: { network: "network", rate_limit: "rate limit", timeout: "timeout", auth: "auth", contract: "contract", content_refusal: "refusal" },
    themeMode: { light: "light", dark: "dark" },
  },
};

let I18N = {};
let LANG = localStorage.getItem("council:lang") || "zh";

function pick(map, key) {
  const table = map || {};
  return Object.prototype.hasOwnProperty.call(table, key) ? table[key] : key;
}
function t(key) { return pick(I18N, key); }
function dStatus(value) { return pick(DYNAMIC[LANG].status, value); }
function dPhase(value) { return pick(DYNAMIC[LANG].phase, value); }
function dKind(value) { return pick(DYNAMIC[LANG].kind, value); }

function localize() {
  document.querySelectorAll("[data-i18n]").forEach((node) => {
    text(node, t(node.dataset.i18n));
  });
  document.querySelectorAll("[data-i18n-ph]").forEach((node) => {
    node.placeholder = t(node.dataset.i18nPh);
  });
  document.title = t("app.title");
}

async function loadLocale(lang) {
  const response = await fetch(`/i18n/${lang}.json`);
  I18N = await response.json();
  document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
  localize();
}

/* ---------------------------------------------------------------- themes */

const BUILTIN_THEMES = ["light", "dark", "high-contrast"];
let currentTheme = null; // {id, label, mode, colors}
let builtinThemes = [];

function themeLabel(theme) {
  if (!theme || !theme.label) return theme && theme.id ? theme.id : "";
  if (typeof theme.label === "object") return theme.label[LANG] || theme.label.en || "";
  return theme.label;
}

async function fetchThemes() {
  builtinThemes = [];
  for (const id of BUILTIN_THEMES) {
    try {
      const theme = await (await fetch(`/themes/${id}.json`)).json();
      builtinThemes.push(theme);
    } catch (_) { /* ignore one broken builtin */ }
  }
  const select = $("theme-select");
  select.replaceChildren();
  builtinThemes.forEach((theme) => {
    const option = h("option");
    option.value = theme.id;
    text(option, themeLabel(theme));
    select.appendChild(option);
  });
  const custom = h("option");
  custom.value = "__custom__";
  text(custom, t("theme.builtin") + " · …");
  select.appendChild(custom);
}

function applyTheme(theme) {
  currentTheme = theme;
  const root = document.documentElement;
  root.dataset.themeMode = theme.mode || "light";
  const style = root.style;
  Object.keys(theme.colors || {}).forEach((key) => {
    if (key.startsWith("--")) style.setProperty(key, theme.colors[key]);
  });
  try { localStorage.setItem("council:theme", JSON.stringify(theme)); } catch (_) { /* private mode */ }
  if (theme.mode) {
    const meta = document.querySelector('meta[name="color-scheme"]');
    if (meta) meta.content = theme.mode;
  }
}

async function applyBuiltin(id) {
  const theme = builtinThemes.find((item) => item.id === id) || currentTheme;
  if (theme) applyTheme(theme);
}

function initSavedTheme() {
  const meta = document.createElement("meta");
  meta.name = "color-scheme";
  document.head.appendChild(meta);
  try {
    const saved = JSON.parse(localStorage.getItem("council:theme") || "null");
    if (saved && saved.colors) applyTheme(saved);
  } catch (_) { /* fall through to CSS default */ }
}

function exportCurrentTheme() {
  const data = currentTheme || { id: "custom", label: { zh: "自定义", en: "custom" }, mode: "light", colors: {} };
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const link = h("a");
  link.href = URL.createObjectURL(blob);
  link.download = `ai-council-theme-${data.id || "custom"}.json`;
  link.click();
  URL.revokeObjectURL(link.href);
}

function importThemeFile(file) {
  const reader = new FileReader();
  reader.onload = () => {
    try {
      const theme = JSON.parse(String(reader.result));
      if (!theme.colors || typeof theme.colors !== "object" || !Object.keys(theme.colors).length) {
        throw new Error("colors missing");
      }
      theme.id = theme.id || "custom";
      theme.mode = theme.mode === "dark" ? "dark" : "light";
      applyTheme(theme);
      $("theme-select").value = "__custom__";
    } catch (_) {
      alert(t("theme.import") + ": JSON {colors:{…}}");
    }
  };
  reader.readAsText(file);
}

/* ---------------------------------------------------------------- session */

let SESSION_ID = null;
let WS = null;
let seenSeqs = new Set();
let digestTimer = null;
let lastStatus = "";

function showView(name) {
  $("view-new").classList.toggle("hidden", name !== "new");
  $("view-session").classList.toggle("hidden", name !== "session");
  $("btn-back").classList.toggle("hidden", name !== "session");
  if (name === "new") refreshList();
}

function banner(el, message) {
  if (!message) { el.classList.add("hidden"); text(el, ""); return; }
  text(el, message);
  el.classList.remove("hidden");
}

async function refreshDigest() {
  if (!SESSION_ID) return;
  try {
    const digest = await api(`/api/sessions/${SESSION_ID}`);
    renderDigest(digest);
  } catch (_) { /* session may have just vanished; ignore */ }
}

function scheduleDigestRefresh() {
  clearTimeout(digestTimer);
  digestTimer = setTimeout(refreshDigest, 120);
}

function renderDigest(d) {
  text($("session-question"), d.question || "—");
  const status = d.status || "starting";
  const chip = $("status-chip");
  chip.className = "status " + status;
  text(chip, dStatus(status));
  if (status === "error" && d.error) banner($("session-banner"), d.error);

  text($("phase-chip"), d.phase ? `${d.phase} · ${dPhase(d.phase)}` : "");
  text($("room-chip"), t("session.phase") + " R" + (d.round || 1) + " · V" + (d.version || 1));

  const finalish = status === "completed";
  const proposalHost = $("digest-proposal");
  const body = d.proposal ? (d.proposal.proposal || d.proposal.text || JSON.stringify(d.proposal)) : "";
  if (body) {
    const pre = h("div");
    pre.className = finalish ? "proposal final" : "proposal";
    text(pre, body);
    proposalHost.replaceChildren(pre);
  } else {
    proposalHost.replaceChildren(h("p", "dim"));
    text(proposalHost.firstChild, t("session.waiting"));
  }

  const extra = $("digest-extra");
  extra.replaceChildren();
  const sections = [
    ["steps", d.proposal && d.proposal.steps, t("session.steps")],
    ["assumptions", d.proposal && d.proposal.assumptions, t("session.assumptions")],
    ["risks", d.proposal && d.proposal.risks, t("session.risks")],
    ["open_questions", d.proposal && d.proposal.open_questions, t("session.questions")],
  ];
  sections.forEach(([key, items, label]) => {
    if (!Array.isArray(items) || !items.length) return;
    const title = h("h4");
    text(title, label);
    extra.appendChild(title);
    const ul = h("ul");
    items.forEach((item) => {
      const li = h("li");
      text(li, item);
      ul.appendChild(li);
    });
    extra.appendChild(ul);
  });

  const meta = $("meta-row");
  meta.replaceChildren();
  [["id", d.session_id], [t("session.selected"), d.selected], [t("session.version"), "V" + (d.version || 1)],
   [t("session.rounds"), d.rounds], [t("session.calls"), d.calls_done],
   [t("session.usage"), `${d.usage ? d.usage.input : 0}↓ ${d.usage ? d.usage.output : 0}↑`]]
    .forEach(([label, value]) => {
      if (value == null || value === "") return;
      const span = h("span");
      const code = h("code");
      text(code, `${label}: ${value}`);
      span.appendChild(code);
      meta.appendChild(span);
    });
  if (d.degraded && d.degraded.length) {
    const span = h("span");
    text(span, "⚠ " + t("session.degraded") + ": " + d.degraded.join(", "));
    meta.appendChild(span);
  }

  setLiveControls(status);
}

function setLiveControls(status) {
  const running = status === "running" || status === "starting" || status === "awaiting_user" || status === "stalled";
  const paused = status === "paused";
  $("btn-pause").disabled = !running;
  $("btn-resume").disabled = !paused;
  $("btn-stop").disabled = !(running || paused);
  lastStatus = status;
}

/* -------------------------------------------------------------- timeline */

const kindClass = (kind) => {
  if (kind === "PhaseStarted" || kind === "PhaseCompleted") return "phase";
  if (kind === "CallCompleted") return "call-ok";
  if (kind === "CallFailed") return "call-fail";
  if (kind === "NodeStateChanged" || kind === "SessionStatusChanged") return "status";
  if (kind === "Convergence" || kind === "ConfigChanged" || kind === "SessionCreated") return "system";
  return "";
};

function describeEvent(event) {
  const p = event.payload;
  switch (event.type) {
    case "SessionCreated":
      return t("ev.created") + (p.question ? ` — ${p.question}` : "");
    case "PhaseStarted": return `→ ${p.phase} ${dPhase(p.phase)}` + (p.round ? ` (R${p.round})` : "");
    case "PhaseCompleted": return `✓ ${p.phase} ${dPhase(p.phase)}` + (p.round ? ` (R${p.round})` : "");
    case "CallIssued": return `${p.node_id} @ ${p.phase} 请求 ${p.model}`;
    case "CallCompleted": return `${p.node_id} @ ${p.phase} ${p.latency_ms}ms` + (p.repaired ? ` · 修复${p.repaired}` : "");
    case "CallFailed": return `${p.node_id} @ ${p.phase} ✗ ${dKind(p.kind)} ${p.message}`;
    case "NodeStateChanged": return `${p.node_id} ${p.state}: ${p.reason || ""}`;
    case "SessionStatusChanged": return `${dStatus(p.status)}: ${p.reason || ""}`;
    case "Convergence": return t("ev.convergence");
    case "ConfigChanged": return t("ev.config") + (p.diff && p.diff.length ? ` — ${p.diff.join("; ")}` : "");
    case "UserDecision": return `${t("ev.decision")}: ${p.decision || ""}`;
    default: return event.type;
  }
}

function appendEvent(event) {
  const seq = Number(event.seq) || 0;
  if (seq > 0) {
    if (seenSeqs.has(seq)) return false; // replay vs live duplicate
    seenSeqs.add(seq);
  }
  const list = $("timeline");
  const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 60;
  const li = h("li");
  const seqNode = h("span", "t-seq");
  text(seqNode, seq ? `#${seq}` : "");
  const bodyNode = h("span", "t-body");
  const kindNode = h("span", "t-kind " + kindClass(event.type));
  text(kindNode, describeEvent(event));
  bodyNode.appendChild(kindNode);
  const raw = event.payload && (event.payload.raw_text || event.payload.text);
  if (event.type === "CallCompleted") {
    const detail = h("div", "raw");
    const parsed = event.payload.parsed;
    const snippet = parsed && parsed.proposal ? parsed.proposal : (raw || "");
    text(detail, typeof snippet === "string" ? snippet.slice(0, 1200) : JSON.stringify(snippet).slice(0, 1200));
    bodyNode.appendChild(detail);
  }
  li.appendChild(seqNode);
  li.appendChild(bodyNode);
  list.appendChild(li);
  while (list.childElementCount > 1500) list.removeChild(list.firstChild);
  if (atBottom) list.scrollTop = list.scrollHeight;
  return true;
}

/* -------------------------------------------------------------- websocket */

function connectWS(sessionId) {
  SESSION_ID = sessionId;
  seenSeqs = new Set();
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  WS = new WebSocket(`${scheme}://${location.host}/api/sessions/${sessionId}/ws`);
  WS.onopen = () => WS.send(JSON.stringify({ kind: "hello", since: 0 }));
  WS.onmessage = (raw) => {
    let message;
    try { message = JSON.parse(raw.data); } catch (_) { return; }
    handleWS(message);
  };
  WS.onclose = () => {
    if (SESSION_ID !== sessionId) return;
    banner($("session-banner"), t("error.network"));
  };
  WS.onerror = () => { /* onclose will surface */ };
}

function handleWS(message) {
  switch (message.kind) {
    case "replay":
      (message.events || []).forEach(appendEvent);
      scheduleDigestRefresh();
      break;
    case "event": {
      const changed = appendEvent(message.event);
      const type = message.event.type;
      if (changed && (type === "PhaseCompleted" || type === "PhaseStarted" || type === "SessionStatusChanged" || type === "Convergence")) {
        scheduleDigestRefresh();
      }
      break;
    }
    case "session_done":
      scheduleDigestRefresh();
      refreshList();
      if (message.status === "stopped" || message.status === "error" || message.status === "cancelled") {
        banner($("session-banner"), message.reason || message.status);
      }
      break;
    case "drift":
      banner($("session-banner"), t("drift.note") + " " + (message.diff || []).join("; "));
      break;
    case "ask":
      showAsk(message);
      break;
    case "error":
      banner($("session-banner"), message.message || "");
      break;
    default:
      break;
  }
}

function wsAnswer(askId, payload) {
  if (!WS || WS.readyState !== WebSocket.OPEN) return;
  WS.send(JSON.stringify({ kind: "answer", ask_id: askId, payload }));
}

function wsAction(action, extra = {}) {
  if (!WS || WS.readyState !== WebSocket.OPEN) return;
  WS.send(JSON.stringify({ kind: "action", action, ...extra }));
}

/* ------------------------------------------------------------------- asks */

let activeAsk = null;

const ACTION_LABELS = ["retry", "wait", "switch_model", "switch_adapter", "drop_node", "abort"];

function renderAskButtons(ask) {
  const box = $("ask-body");
  box.replaceChildren();
  const options = h("div", "ask-options");
  const inputs = h("div", "ask-actions");
  const input = h("input");
  input.type = "text";
  input.className = "hidden";
  const send = h("button", "primary");
  text(send, t("ask.send"));
  const cancel = h("button", "chip");
  text(cancel, t("act.cancel"));

  function enableInput() {
    input.classList.remove("hidden");
    send.classList.remove("hidden");
    cancel.classList.add("hidden");
    input.focus();
  }

  if (ask.type === "failure") {
    const map = { retry: "act.retry", wait: "act.wait", switch_model: "act.switch_model", switch_adapter: "act.switch_adapter", drop_node: "act.drop_node", abort: "act.abort" };
    (ask.ask.available || []).forEach((value) => {
      const button = h("button");
      text(button, t(map[value] || value));
      button.addEventListener("click", () => {
        if (value === "switch_model" || value === "switch_adapter") {
          input.placeholder = value === "switch_model" ? t("ask.switch_model.ph") : t("ask.switch_adapter.ph");
          activeAsk.pendingValue = value;
          enableInput();
          return;
        }
        wsAnswer(activeAsk.ask_id, { action: value });
        closeAsk();
      });
      options.appendChild(button);
    });
  } else if (ask.type === "select") {
    (ask.ask.candidates || []).forEach((nodeId) => {
      const button = h("button");
      text(button, nodeId);
      button.addEventListener("click", () => { wsAnswer(ask.ask_id, { node_id: nodeId }); closeAsk(); });
      options.appendChild(button);
    });
  } else if (ask.type === "decision") {
    input.classList.remove("hidden");
    input.placeholder = t("ask.decision.title") + "…";
    send.classList.remove("hidden");
  }

  send.addEventListener("click", () => {
    const value = input.value.trim();
    if (!value) { banner($("ask-error"), t("ask.error.missing")); return; }
    if (activeAsk.pendingValue) {
      const payload = { action: activeAsk.pendingValue };
      if (activeAsk.pendingValue === "switch_model") payload.model = value;
      else payload.adapter = value;
      wsAnswer(activeAsk.ask_id, payload);
    } else {
      wsAnswer(activeAsk.ask_id, { text: value });
    }
    closeAsk();
  });
  cancel.addEventListener("click", closeAsk);

  box.appendChild(options);
  box.appendChild(inputs);
  inputs.appendChild(input);
  inputs.appendChild(send);
  if (ask.type !== "decision") inputs.appendChild(cancel);
  banner($("ask-error"), "");
}

function showAsk(ask) {
  activeAsk = ask;
  $("ask-overlay").classList.remove("hidden");
  const title = ask.type === "failure" ? t("ask.failure.title") : ask.type === "select" ? t("ask.select.title") : t("ask.decision.title");
  text($("ask-title"), title);
  const detail = ask.ask || {};
  const line = ask.type === "failure"
    ? `${detail.node_id} @ ${detail.phase}：${dKind(detail.kind)} — ${detail.message}`
    : ask.type === "decision" ? detail.question || "" : detail.reason || "";
  text($("ask-detail"), line);
  renderAskButtons(ask);
}

function closeAsk() {
  activeAsk = null;
  $("ask-overlay").classList.add("hidden");
}

/* ----------------------------------------------------------------- actions */

async function postAction(action, body = {}) {
  try {
    await api(`/api/sessions/${SESSION_ID}/actions`, { method: "POST", body: JSON.stringify({ action, ...body }) });
  } catch (err) {
    banner($("session-banner"), err.message);
  }
}

function bindActions() {
  $("btn-pause").addEventListener("click", () => postAction("pause"));
  $("btn-resume").addEventListener("click", () => postAction("resume"));
  $("btn-stop").addEventListener("click", () => { if (confirm(t("act.stop") + "?")) postAction("stop"); });
  $("policy-select").addEventListener("change", () => postAction("policy", { policy: $("policy-select").value }));
  $("btn-export").addEventListener("click", () => {
    $("btn-export").href = `/api/sessions/${SESSION_ID}/export?raw=${$("raw-toggle").checked ? "true" : "false"}`;
  });
}

/* ------------------------------------------------------------------- lists */

async function refreshList() {
  try {
    const data = await api("/api/sessions");
    const list = $("session-list");
    list.replaceChildren();
    if (!data.sessions.length) {
      const empty = h("p", "dim");
      text(empty, t("list.empty"));
      list.appendChild(empty);
      return;
    }
    data.sessions.forEach((row) => {
      const div = h("div", "session-row");
      const q = h("span", "q");
      text(q, row.question || row.session_id);
      const chip = h("span", "status " + (row.status || ""));
      text(chip, dStatus(row.status || "running"));
      const when = h("span", "when");
      text(when, (row.updated_at || "").replace("T", " ").slice(0, 16));
      const actions = h("span", "row gap");
      const openBtn = h("button", "chip");
      text(openBtn, t("list.open"));
      openBtn.addEventListener("click", () => openSession(row.session_id));
      actions.appendChild(openBtn);
      if (row.status === "paused" || row.status === "stopped" || row.status === "error") {
        const resumeBtn = h("button", "chip");
        text(resumeBtn, t("list.resume"));
        resumeBtn.addEventListener("click", async () => {
          try {
            await api(`/api/sessions/${row.session_id}/resume`, { method: "POST", body: JSON.stringify({ question: "" }) });
            openSession(row.session_id);
          } catch (err) {
            banner($("session-banner"), err.message);
            showView("session");
          }
        });
        actions.appendChild(resumeBtn);
      }
      div.appendChild(q);
      div.appendChild(chip);
      div.appendChild(when);
      div.appendChild(actions);
      list.appendChild(div);
    });
  } catch (_) { /* backend may be starting */ }
}

function openSession(sessionId) {
  showView("session");
  banner($("session-banner"), "");
  $("timeline").replaceChildren();
  renderDigest({ status: "starting", question: "…", session_id: sessionId, rounds: 0, calls_done: 0, usage: { input: 0, output: 0 } });
  connectWS(sessionId);
}

/* ------------------------------------------------------------------- start */

async function submitNewSession(event) {
  event.preventDefault();
  const question = $("question").value.trim();
  const files = [...$("files").files].map((file) => ({ name: file.name, content: "" }));
  $("btn-start").disabled = true;
  text($("btn-start"), t("new.running"));
  banner($("new-error"), "");
  try {
    // Read file contents with the text reader; binary/encoding failures are
    // rejected by the server-side gate anyway.
    for (let i = 0; i < files.length; i += 1) {
      files[i].content = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result || ""));
        reader.onerror = () => reject(new Error(files[i].name));
        reader.readAsText($("files").files[i]);
      });
    }
    const created = await api("/api/sessions", {
      method: "POST",
      body: JSON.stringify({ question, files }),
    });
    $("question").value = "";
    $("files").value = "";
    renderFileList([]);
    openSession(created.session_id);
  } catch (err) {
    banner($("new-error"), `${t("new.error.http")}: ${err.message}`);
    showView("new");
  } finally {
    $("btn-start").disabled = false;
    text($("btn-start"), t("new.start"));
  }
}

function renderFileList(files) {
  const list = $("file-list");
  list.replaceChildren();
  files.forEach((file) => {
    const li = h("li");
    text(li, `${file.name}（${(file.size / 1024).toFixed(1)} KiB）`);
    list.appendChild(li);
  });
}

/* ------------------------------------------------------------------- keys */

const CUSTOM_KEYS_KEY = "council:customkeys";

function customKeyNames() {
  try {
    const list = JSON.parse(localStorage.getItem(CUSTOM_KEYS_KEY) || "[]");
    return Array.isArray(list) ? list.filter((name) => typeof name === "string") : [];
  } catch (_) { return []; }
}

function storeCustomNames(names) {
  try { localStorage.setItem(CUSTOM_KEYS_KEY, JSON.stringify(names)); } catch (_) { /* private mode */ }
}

function sourceLabel(source) { return t("keys.source." + source); }

async function keyStatus(name) {
  try {
    const status = await api(`/api/keys/${encodeURIComponent(name)}`);
    return status.source || "unset";
  } catch (_) { return "unset"; }
}

async function openKeys() {
  banner($("keys-error"), "");
  $("keys-overlay").classList.remove("hidden");
  const rows = [];
  try {
    const overview = await api("/api/keys");
    (overview.presets || []).forEach((preset) => {
      rows.push({
        name: preset.name,
        label: t(preset.label || "keys.vendor.custom"),
        status: (overview.statuses || {})[preset.name] || "unset",
      });
    });
  } catch (err) {
    banner($("keys-error"), `${t("keys.error.save")}: ${err.message}`);
  }
  for (const name of customKeyNames()) {
    if (rows.some((row) => row.name === name)) continue;
    rows.push({ name, status: await keyStatus(name) });
  }
  renderKeyRows(rows);
}

function renderKeyRows(rows) {
  const host = $("keys-list");
  host.replaceChildren();
  rows.forEach((row) => {
    const div = h("div", "key-row");
    const nameBox = h("div", "k-name");
    text(nameBox, row.name);
    if (row.label) {
      const small = h("small");
      text(small, row.label);
      nameBox.appendChild(small);
    }
    const input = h("input");
    input.type = "password";
    input.autocomplete = "new-password";
    input.placeholder = "sk-…";
    const source = h("span", "source " + row.status);
    text(source, sourceLabel(row.status));
    const save = h("button", "chip");
    text(save, t("keys.save"));
    const del = h("button", "chip danger");
    text(del, t("keys.delete"));
    const shadow = h("div", "shadow-note hidden");
    text(shadow, t("keys.shadowed"));
    save.addEventListener("click", async () => {
      const value = input.value.trim();
      if (!value) return;
      save.disabled = true;
      try {
        const result = await api("/api/keys", {
          method: "POST",
          body: JSON.stringify({ name: row.name, value }),
        });
        const status = result.source || "keyring";
        source.className = "source " + status;
        text(source, sourceLabel(status));
        shadow.classList.toggle("hidden", !result.shadowed);
        banner($("keys-error"), "");
        input.value = "";
      } catch (err) {
        banner($("keys-error"), `${t("keys.error.save")}: ${err.message}`);
      } finally {
        save.disabled = false;
      }
    });
    del.addEventListener("click", async () => {
      if (!confirm(t("keys.delete.confirm"))) return;
      try {
        await api(`/api/keys/${encodeURIComponent(row.name)}`, { method: "DELETE" });
        storeCustomNames(customKeyNames().filter((name) => name !== row.name));
        await openKeys();
      } catch (err) {
        banner($("keys-error"), `${t("keys.error.save")}: ${err.message}`);
      }
    });
    div.appendChild(nameBox);
    div.appendChild(source);
    div.appendChild(input);
    div.appendChild(save);
    div.appendChild(del);
    div.appendChild(shadow);
    host.appendChild(div);
  });
}

async function addCustomKey() {
  const name = $("keys-custom-name").value.trim();
  const value = $("keys-custom-value").value.trim();
  if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(name)) {
    banner($("keys-error"), t("keys.error.badname"));
    return;
  }
  if (!value) return;
  try {
    await api("/api/keys", { method: "POST", body: JSON.stringify({ name, value }) });
    const names = customKeyNames();
    if (!names.includes(name)) {
      names.push(name);
      storeCustomNames(names);
    }
    $("keys-custom-name").value = "";
    $("keys-custom-value").value = "";
    banner($("keys-error"), "");
    await openKeys();
  } catch (err) {
    banner($("keys-error"), `${t("keys.error.save")}: ${err.message}`);
  }
}

/* ------------------------------------------------------------------- boot */

function bindBoot() {
  $("btn-lang").addEventListener("click", () => {
    LANG = LANG === "zh" ? "en" : "zh";
    localStorage.setItem("council:lang", LANG);
    loadLocale(LANG).then(() => {
      const select = $("theme-select");
      const id = currentTheme ? currentTheme.id : select.value;
      if (id && id !== "__custom__") {
        const theme = builtinThemes.find((item) => item.id === id);
        if (theme) select.value = id;
      }
      fetchThemes();
      refreshList();
    });
  });
  $("btn-home").addEventListener("click", () => { if (SESSION_ID) closeAsk(); showView("new"); });
  $("btn-back").addEventListener("click", () => { closeAsk(); showView("new"); });
  $("theme-select").addEventListener("change", () => {
    if ($("theme-select").value === "__custom__") return;
    applyBuiltin($("theme-select").value);
  });
  $("btn-export-theme").addEventListener("click", exportCurrentTheme);
  $("btn-import-theme").addEventListener("click", () => $("theme-file").click());
  $("theme-file").addEventListener("change", () => {
    if ($("theme-file").files.length) importThemeFile($("theme-file").files[0]);
  });
  $("files").addEventListener("change", () => renderFileList([...$("files").files]));
  $("new-form").addEventListener("submit", submitNewSession);
  $("btn-start").disabled = false;

  // Keys panel
  $("btn-keys").addEventListener("click", openKeys);
  $("btn-keys-custom").addEventListener("click", addCustomKey);
  $("keys-custom-value").addEventListener("keydown", (event) => {
    if (event.key === "Enter") addCustomKey();
  });
  $("keys-overlay").addEventListener("click", (event) => {
    if (event.target === $("keys-overlay")) $("keys-overlay").classList.add("hidden");
  });

  bindActions();
}

async function boot() {
  initSavedTheme();
  bindBoot();
  await loadLocale(LANG);
  await fetchThemes();
  // Re-apply the persisted theme label once builtins are loaded.
  const saved = currentTheme;
  if (saved && saved.id) {
    const select = $("theme-select");
    const match = builtinThemes.find((item) => item.id === saved.id);
    if (match) select.value = match.id;
    else select.value = "__custom__";
  }
  try {
    const meta = await api("/api/meta");
    const room = $("room");
    const names = meta.participants.map((p) => p.display || p.id).join("、");
    text(room, `${meta.participants.length} 名参与者：${names}` + (meta.judge ? ` · judge: ${meta.judge}` : ""));
    const select = $("policy-select");
    ["continue", "pause", "ask_user"].forEach((value) => {
      const option = h("option");
      option.value = value;
      text(option, t("ask.policy." + value));
      select.appendChild(option);
    });
  } catch (_) { /* meta unavailable */ }
  refreshList();
  showView("new");
}

document.addEventListener("DOMContentLoaded", boot);
