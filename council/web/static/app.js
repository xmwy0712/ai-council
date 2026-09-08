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
  document.querySelectorAll("[data-i18n-tip]").forEach((node) => {
    node.dataset.tip = t(node.dataset.i18nTip);
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

/* ------------------------------------------------------- meta / roster */

let META = null;   // /api/meta 缓存
let MODELS = null; // /api/models 注册表目录

const OVERRIDES_KEY = "council:roster";

function loadOverrides() {
  try {
    const raw = JSON.parse(localStorage.getItem(OVERRIDES_KEY) || "{}");
    return raw && typeof raw === "object" ? raw : {};
  } catch (_) { return {}; }
}
let OVERRIDES = loadOverrides();

const TUNING_KEY = "council:tuning";

function loadTuning() {
  try {
    const raw = JSON.parse(localStorage.getItem(TUNING_KEY) || "{}");
    return raw && typeof raw === "object" ? raw : {};
  } catch (_) { return {}; }
}
let TUNING = loadTuning();

function saveTuning() {
  try { localStorage.setItem(TUNING_KEY, JSON.stringify(TUNING)); } catch (_) { /* private mode */ }
}

function collectTuning() {
  const out = {};
  if (TUNING.idle_s > 0) out.idle_s = TUNING.idle_s;
  if (TUNING.max_retries >= 0) out.max_retries = TUNING.max_retries;
  return Object.keys(out).length ? out : null;
}

function saveOverrides() {
  try { localStorage.setItem(OVERRIDES_KEY, JSON.stringify(OVERRIDES)); } catch (_) { /* private mode */ }
}

// 配置里写死的档位优先如实显示，不误导用户
function nodeLabel(node) {
  const base = node.display || node.id;
  if (node.thinking) return `${base} · ${t("meta.thinking").replace("{level}", node.thinking)}`;
  return base;
}

function renderRoster() {
  const host = $("roster");
  if (!host || !META) return;
  const nodes = META.judge_node
    ? [...META.participants, Object.assign({}, META.judge_node, { is_judge: true })]
    : META.participants;
  const names = nodes.map((n) => nodeLabel(n) + (n.is_judge ? " ⚖" : "")).join("、");
  const judge = META.judge_node
    ? " · " + t("meta.judge").replace("{name}", META.judge_node.display || META.judge_node.id)
    : "";
  text(host, t("meta.roster").replace("{n}", String(nodes.length)).replace("{names}", names) + judge);
}

function renderRosterEditor() {
  const host = $("roster-editor");
  if (!host || !META) return;
  host.replaceChildren();
  const nodes = META.judge_node
    ? [...META.participants, Object.assign({}, META.judge_node, { is_judge: true })]
    : META.participants;
  nodes.forEach((node) => {
    const saved = OVERRIDES[node.id] || {};
    const row = h("div", "roster-row");

    const name = h("span", "r-name" + (node.is_judge ? " r-judge" : ""));
    text(name, nodeLabel(node));
    row.appendChild(name);
    if (node.is_judge) {
      // Judge 行旁的「?」：解释独立裁定，以及未配置/失败时转人工审核
      const tip = h("span", "help-tip");
      tip.tabIndex = 0;
      tip.dataset.i18nTip = "roster.judge.help";
      tip.dataset.tip = t("roster.judge.help");
      text(tip, "?");
      row.appendChild(tip);
    }

    const modelWrap = h("label", "select-wrap r-model-wrap");
    const modelSel = h("select", "r-model");
    modelSel.dataset.node = node.id;
    modelSel.setAttribute("aria-label", t("roster.model"));
    // 空 title：压制 Chromium 对 select 的自动截断文字提示（移出残留的框）
    modelSel.setAttribute("title", "");
    const keep = h("option");
    keep.value = "";
    text(keep, t("roster.keep").replace("{model}", node.model_display || node.model));
    modelSel.appendChild(keep);
    (MODELS ? MODELS.providers : []).forEach((provider) => {
      const group = h("optgroup");
      group.label = provider.display;
      provider.models.forEach((m) => {
        const opt = h("option");
        opt.value = m.id;
        text(opt, m.display);
        group.appendChild(opt);
      });
      modelSel.appendChild(group);
    });
    modelSel.value = saved.model || "";
    modelWrap.appendChild(modelSel);

    const thinkWrap = h("label", "select-wrap r-think-wrap");
    const thinkSel = h("select", "r-thinking");
    thinkSel.dataset.node = node.id;
    thinkSel.setAttribute("aria-label", t("roster.thinking"));
    thinkSel.setAttribute("title", "");
    // 档位由注册表决定：各厂商数量与命名都不同（GPT-6 五档、GLM-5.3 三档…）
    const levelsFor = (modelOverride) => {
      if (!MODELS) return node.thinking_levels || [];
      if (!modelOverride) return node.thinking_levels || [];
      for (const provider of MODELS.providers) {
        const info = provider.models.find((m) => m.id === modelOverride);
        if (info) return info.thinking ? info.thinking_levels || [] : [];
      }
      return [];
    };
    const levelLabel = (level) => {
      const key = "roster.level." + level;
      const label = t(key);
      return label === key ? level : label; // 未收录的档位直接显示原名
    };
    const buildThinkingOptions = () => {
      thinkSel.replaceChildren();
      const unset = h("option");
      unset.value = "";
      text(unset, t("roster.thinking.none"));
      thinkSel.appendChild(unset);
      levelsFor(modelSel.value || node.model).forEach((level) => {
        const opt = h("option");
        opt.value = level;
        text(opt, levelLabel(level));
        thinkSel.appendChild(opt);
      });
    };
    thinkWrap.appendChild(thinkSel);

    const syncThinking = () => {
      buildThinkingOptions();
      const levels = levelsFor(modelSel.value);
      if (!levels.length) {
        // 不可调档：只读显示当前配置（可能是某档位或空）。
        thinkSel.disabled = true;
        const current = node.thinking || "";
        // 当前值不在选项里时补一个只读选项，否则 select 显示空白
        if (current && ![...thinkSel.options].some((o) => o.value === current)) {
          const opt = h("option");
          opt.value = current;
          text(opt, levelLabel(current));
          thinkSel.appendChild(opt);
        }
        thinkSel.value = current;
      } else {
        thinkSel.disabled = false;
        const current = saved.thinking !== undefined ? saved.thinking : node.thinking || "";
        thinkSel.value = levels.includes(current) ? current : "";
      }
    };
    syncThinking();

    const persist = () => {
      const entry = {
        model: modelSel.value,
        thinking: thinkSel.disabled ? "" : thinkSel.value,
      };
      if (entry.model || entry.thinking) OVERRIDES[node.id] = entry;
      else delete OVERRIDES[node.id];
      saveOverrides();
    };
    modelSel.addEventListener("change", () => { syncThinking(); persist(); });
    thinkSel.addEventListener("change", persist);

    row.appendChild(modelWrap);
    row.appendChild(thinkWrap);
    host.appendChild(row);
  });
}

function collectOverrides() {
  const out = [];
  document.querySelectorAll("#roster-editor .roster-row").forEach((row) => {
    const modelSel = row.querySelector("select.r-model");
    const thinkSel = row.querySelector("select.r-thinking");
    if (!modelSel) return;
    const item = {
      node_id: modelSel.dataset.node,
      model: modelSel.value || null,
      thinking: thinkSel && !thinkSel.disabled ? thinkSel.value : null,
    };
    if (item.model || item.thinking !== null) out.push(item);
  });
  return out;
}

function renderConfigWarnings() {
  const host = $("config-warnings");
  if (!host || !META) return;
  const warnings = Array.isArray(META.warnings) ? META.warnings : [];
  if (!warnings.length) { host.classList.add("hidden"); text(host, ""); return; }
  // CLI `council config-check` 的网页等价物：启动配置的软警告逐条列出。
  text(host, warnings.map((line) => "⚠ " + line).join("\n"));
  host.classList.remove("hidden");
}

/* ------------------------------------------------------- cursor trail */

let fx = null; // 光效句柄：null 表示关闭

function applyFxSetting() {
  const enabled = localStorage.getItem("council:fx") !== "off";
  const toggle = $("fx-toggle");
  if (toggle) toggle.checked = enabled;
  if (enabled && !fx) fx = initCursorTrail();
  else if (!enabled && fx) { fx.stop(); fx = null; }
}

function initCursorTrail() {
  const canvas = $("fx-canvas");
  if (!canvas) return null;
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
  if (reduced.matches) return null;
  const ctx = canvas.getContext("2d");
  if (!ctx) return null;

  let raf = 0;
  const trail = [];            // 连续光拖尾（记录恒星轨迹点）
  const TRAIL_MS = 500;        // 拖尾渐隐时长
  let accent = "#4f7cff";
  let accentRGB = [79, 124, 255];
  let cx = null;
  let cy = null;               // 光标位置
  let sx = null;
  let sy = null;               // 恒星平滑跟随位置
  let frame = 0;

  const hexToRgb = (hex) => {
    const m = /^#?([0-9a-f]{6})$/i.exec(hex);
    if (!m) return [79, 124, 255];
    const n = parseInt(m[1], 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  };

  const readAccent = () => {
    const raw = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim();
    if (raw) { accent = raw; accentRGB = hexToRgb(raw); }
  };

  const resize = () => {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(canvas.clientWidth * dpr);
    canvas.height = Math.round(canvas.clientHeight * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  };

  // 行星：淡色、适中大小、各自轨道（轨道本身不画出）
  const planets = [
    { color: "#e8a6a6", r: 4.2, orbitR: 26, theta: 0.4, omega: 0.75 },
    { color: "#e0cfa4", r: 2.6, orbitR: 36, theta: 3.3, omega: 0.9 },
    { color: "#a6bfe8", r: 5.0, orbitR: 48, theta: 2.4, omega: -0.55 },
    { color: "#a8d8b4", r: 3.4, orbitR: 64, theta: 4.2, omega: 0.45 },
    { color: "#c2c2cc", r: 3.0, orbitR: 82, theta: 5.5, omega: -0.35 },
  ].map((p) => ({ ...p, x: null, y: null, vx: 0, vy: 0 }));

  // 恒星锚点限制在安全区内（边缘留出最大行星轨道 + 光晕余量）：
  // 否则光标移到视口边缘时，行星公转到画布外被裁掉，贴边留下一竖条
  // 被切开的彩色弧段（视觉上像鼠标在页面边缘留下了痕迹）。
  const EDGE = 100; // 最大行星轨道 82 + 光晕/行星半径余量
  const clampX = (v) => {
    const w = canvas.clientWidth;
    if (w <= EDGE * 2) return w / 2;
    return Math.min(Math.max(v, EDGE), w - EDGE);
  };
  const clampY = (v) => {
    const h = canvas.clientHeight;
    if (h <= EDGE * 2) return h / 2;
    return Math.min(Math.max(v, EDGE), h - EDGE);
  };

  // 恒星与行星始终跟随光标
  window.addEventListener("pointermove", (event) => {
    const x = event.clientX;
    const y = event.clientY;
    if (cx === null) {
      cx = x;
      cy = y;
      sx = clampX(x);
      sy = clampY(y);
      for (const p of planets) {
        p.x = sx + Math.cos(p.theta) * p.orbitR;
        p.y = sy + Math.sin(p.theta) * p.orbitR;
      }
      return;
    }
    cx = x;
    cy = y;
  }, { passive: true });

  window.addEventListener("pointerleave", () => { /* 恒星留在原地继续发光 */ });

    // 可交互卡片区域：行星绕行到这些框内会被「挡住」（每帧擦除其覆盖像素）
    const drawOccluders = () => {
      document.querySelectorAll(".card, .topbar").forEach((el) => {
        const r = el.getBoundingClientRect();
        if (r.width > 0 && r.height > 0) ctx.clearRect(r.x, r.y, r.width, r.height);
      });
    };

    const step = () => {
    frame += 1;
    ctx.clearRect(0, 0, canvas.clientWidth, canvas.clientHeight);
    const [r, g, b] = accentRGB;

    if (cx !== null) {
      // 恒星平滑跟随光标
      sx += (cx - sx) * 0.35;
      sy += (cy - sy) * 0.35;
      sx = clampX(sx);
      sy = clampY(sy);

      // 连续光拖尾：每帧记录恒星位置，形成不断渐隐的一条光线
      const now = performance.now();
      trail.push({ x: sx, y: sy, t: now });
      while (trail.length && now - trail[0].t > TRAIL_MS) trail.shift();
      if (trail.length > 1) {
        ctx.lineCap = "round";
        ctx.lineJoin = "round";
        for (let i = 1; i < trail.length; i += 1) {
          const a = trail[i - 1];
          const c = trail[i];
          const fade = 1 - (now - c.t) / TRAIL_MS;
          // 浅蓝外辉 + 亮芯，双层描边模拟发光线条
          ctx.strokeStyle = `rgba(158,196,255,${(fade * 0.14).toFixed(3)})`;
          ctx.lineWidth = 1 + 5 * fade;
          ctx.beginPath();
          ctx.moveTo(a.x, a.y);
          ctx.lineTo(c.x, c.y);
          ctx.stroke();
          ctx.strokeStyle = `rgba(158,196,255,${(fade * 0.6).toFixed(3)})`;
          ctx.lineWidth = 0.5 + 2 * fade;
          ctx.beginPath();
          ctx.moveTo(a.x, a.y);
          ctx.lineTo(c.x, c.y);
          ctx.stroke();
        }
      }

      // 行星：锚点沿轨道公转，弹簧引力把行星拉向锚点——
      // 引力随轨道半径衰减（近紧远松，更有真实感）；
      // 快速移动时行星被大幅甩在身后，骤停时冲过头再缓慢回摆
      for (const p of planets) {
        p.theta += p.omega / 60;
        const ax = sx + Math.cos(p.theta) * p.orbitR;
        const ay = sy + Math.sin(p.theta) * p.orbitR;
        if (p.x === null) { p.x = ax; p.y = ay; }
        const gpull = 0.009 * (36 / p.orbitR); // 更弱的吸引 + 距离衰减
        p.vx += (ax - p.x) * gpull;
        p.vy += (ay - p.y) * gpull;
        p.vx *= 0.96;               // 低阻尼：惯性滑行更远、回摆更久
        p.vy *= 0.96;
        p.x += p.vx;
        p.y += p.vy;
      }

      // 恒星：淡蓝色柔光，无实心星核（光标本体）
      const pulse = 1 + Math.sin(frame * 0.03) * 0.06;
      const haloR = 16 * pulse;
      const halo = ctx.createRadialGradient(sx, sy, 0, sx, sy, haloR);
      halo.addColorStop(0, `rgba(${r},${g},${b},0.45)`);
      halo.addColorStop(0.4, `rgba(${r},${g},${b},0.16)`);
      halo.addColorStop(1, `rgba(${r},${g},${b},0)`);
      ctx.fillStyle = halo;
      ctx.beginPath();
      ctx.arc(sx, sy, haloR, 0, Math.PI * 2);
      ctx.fill();

      // 行星：淡色柔光 + 实心圆
      for (const p of planets) {
        ctx.fillStyle = p.color;
        ctx.globalAlpha = 0.22;
        ctx.beginPath();
        ctx.arc(p.x, p.y, p.r * 2.1, 0, Math.PI * 2);
        ctx.fill();
        ctx.globalAlpha = 1;
        ctx.beginPath();
        ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
        ctx.fill();
      }
    }

    ctx.globalAlpha = 1;
    // 卡片遮挡：所有元素画完后，把可交互框范围内的像素擦除——
    // 行星/拖尾/恒星进入卡片即被「挡住」
    drawOccluders();
    raf = window.requestAnimationFrame(step);
  };

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) window.cancelAnimationFrame(raf);
    else if (!reduced.matches) raf = window.requestAnimationFrame(step);
  });
  window.addEventListener("resize", resize);
  // 主题切换会改 --accent，低成本周期性跟随
  window.setInterval(readAccent, 2000);

  readAccent();
  resize();
  raf = window.requestAnimationFrame(step);
  return {
    stop: () => {
      window.cancelAnimationFrame(raf);
      ctx.clearRect(0, 0, canvas.clientWidth, canvas.clientHeight);
    },
  };
}

/* 问号气泡显隐：JS 委托控制——原生 select 弹出层会卡住 CSS :hover，
   只靠 hover 会在鼠标移出后残留一个框。此处保证移出/点击即隐藏。 */
function bindHelpTips() {
  const hideAll = () => {
    document.querySelectorAll(".help-tip.show").forEach((node) => node.classList.remove("show"));
  };
  document.addEventListener("mouseover", (event) => {
    const tip = event.target instanceof Element ? event.target.closest(".help-tip") : null;
    if (tip) tip.classList.add("show");
    else hideAll();
  });
  document.addEventListener("mouseout", (event) => {
    if (event.target instanceof Element && event.target.closest(".help-tip")) hideAll();
  });
  document.addEventListener("pointerdown", hideAll);
  document.addEventListener("focusin", (event) => {
    hideAll();
    const tip = event.target instanceof Element ? event.target.closest(".help-tip") : null;
    if (tip) tip.classList.add("show");
  });
  document.addEventListener("focusout", hideAll);
}

/* ---------------------------------------------------------------- session */

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

// 流式状态行：CallChunk 每秒可达数十条，逐条追加只会刷屏——
// 同一调用只保留一行实时状态，CallCompleted 时移除
const streamLines = new Map();
const streamCounts = new Map();

function updateStreamLine(event) {
  const key = event.payload.key || "stream";
  let li = streamLines.get(key);
  if (!li) {
    li = h("li");
    const seqNode = h("span", "t-seq");
    const kind = h("span", "t-kind streaming");
    const body = h("span", "t-body stream-text");
    li.appendChild(seqNode);
    li.appendChild(kind);
    kind.textContent = "⏳ 流式接收中";
    li.appendChild(body);
    const list = $("timeline");
    const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 60;
    list.appendChild(li);
    if (atBottom) list.scrollTop = list.scrollHeight;
    streamLines.set(key, li);
    streamCounts.set(key, 0);
  }
  streamCounts.set(key, (streamCounts.get(key) || 0) + 1);
  const n = streamCounts.get(key);
  if (n % 5 === 1) {
    const body = li.querySelector(".stream-text");
    if (body) text(body, `${key} · 已接收 ${n} 段`);
  }
}

function clearStreamLine(key) {
  const li = streamLines.get(key);
  if (li) { li.remove(); streamLines.delete(key); streamCounts.delete(key); }
}

function appendEvent(event) {
  const seq = Number(event.seq) || 0;
  if (seq > 0) {
    if (seenSeqs.has(seq)) return false; // replay vs live duplicate
    seenSeqs.add(seq);
  }
  if (event.type === "CallChunk") {
    updateStreamLine(event);
    return true;
  }
  if (event.type === "CallCompleted") clearStreamLine(event.payload.key);
  if (event.type === "CallFailed") clearStreamLine(event.payload.key);
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
  streamLines.clear();
  streamCounts.clear();
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
    // 暂停/恢复按钮已覆盖「等待」语义；这里不再提供以免与会话级暂停混淆
    const map = { retry: "act.retry", switch_model: "act.switch_model", switch_adapter: "act.switch_adapter", drop_node: "act.drop_node", abort: "act.abort" };
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
  // 选主案：必须选一个候选——30 分钟后自动兜底取首个；不再提供「保持等待」按钮
  // 失败弹窗：动作按钮里已有「终止会议」，取消按钮冗余——一并隐藏
  if (ask.type === "select" || ask.type === "failure") {
    cancel.classList.add("hidden");
    // 选主案需要阅读各候选方案：弹窗浮动左下角、去掉全屏遮罩
    if (ask.type === "select") {
      const overlay = $("ask-overlay");
      overlay.classList.add("ask-select-mode");
    }
  }

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
  $("ask-overlay").classList.remove("ask-select-mode");
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
      const deleteBtn = h("button", "chip danger");
      text(deleteBtn, t("list.delete"));
      deleteBtn.addEventListener("click", async () => {
        if (!confirm(t("list.delete.confirm"))) return;
        try {
          await api(`/api/sessions/${row.session_id}`, { method: "DELETE" });
          if (SESSION_ID === row.session_id) {
            SESSION_ID = null;
            if (WS) { WS.close(); WS = null; }
            showView("new");
          }
          refreshList();
        } catch (err) {
          banner($("session-banner"), err.message);
        }
      });
      actions.appendChild(deleteBtn);
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
      body: JSON.stringify({ question, files, overrides: collectOverrides(), tuning: collectTuning() }),
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
  $("settings-overlay").classList.remove("hidden");
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

/* ------------------------------------------------------------ CLI diagnostics */

function renderDiagnostics(items) {
  const host = $("diag-list");
  if (!host) return;
  if (!items || !items.length) {
    text(host, "");
    return;
  }
  host.replaceChildren();
  for (const it of items) {
    const row = h("div", "diag-row");
    const name = h("span", "diag-name");
    text(name, it.name);
    row.appendChild(name);
    if (!it.installed) {
      const tag = h("span", "diag-tag bad");
      text(tag, t("diag.missing"));
      row.appendChild(tag);
    } else {
      const tag = h("span", "diag-tag good");
      text(tag, `${t("diag.installed")}${it.version ? " · " + it.version : ""}`);
      row.appendChild(tag);
      if (it.authenticated === true) {
        const a = h("span", "diag-tag good");
        text(a, t("diag.authed"));
        row.appendChild(a);
      } else if (it.authenticated === false) {
        const a = h("span", "diag-tag bad");
        text(a, t("diag.unauth") + (it.auth_detail ? " · " + it.auth_detail : ""));
        row.appendChild(a);
      } else if (it.authenticated === null && it.auth_detail) {
        const a = h("span", "diag-tag warn");
        text(a, t("diag.auth_unknown") + " · " + it.auth_detail);
        row.appendChild(a);
      }
      if (it.error) {
        const e = h("div", "diag-error");
        text(e, it.error);
        row.appendChild(e);
      }
    }
    host.appendChild(row);
  }
}

async function runDiagnostics() {
  const btn = $("btn-diagnostics");
  btn.disabled = true;
  try {
    const data = await api("/api/diagnostics");
    renderDiagnostics(data.clis || []);
  } catch (err) {
    const host = $("diag-list");
    text(host, err.message);
  } finally {
    btn.disabled = false;
  }
}

/* ------------------------------------------------------------------- boot */

function switchLang(lang) {
  LANG = lang === "en" ? "en" : "zh";
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
    renderRoster();
    renderRosterEditor();
    renderConfigWarnings();
  });
}

function renderTuningInputs() {
  const idle = $("tune-idle");
  const retries = $("tune-retries");
  if (!idle || !retries) return;
  idle.value = TUNING.idle_s > 0 ? TUNING.idle_s : 90;
  retries.value = TUNING.max_retries >= 0 ? TUNING.max_retries : 3;
}

function bindTuningInputs() {
  const idle = $("tune-idle");
  const retries = $("tune-retries");
  if (idle) idle.addEventListener("change", () => {
    TUNING.idle_s = Math.max(10, Math.min(3600, Number(idle.value) || 90));
    idle.value = TUNING.idle_s;
    saveTuning();
  });
  if (retries) retries.addEventListener("change", () => {
    TUNING.max_retries = Math.max(0, Math.min(10, Number(retries.value) || 3));
    retries.value = TUNING.max_retries;
    saveTuning();
  });
}

function bindBoot() {
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

  // Settings panel: cursor effect / theme / language / keys
  $("btn-settings").addEventListener("click", openKeys);
  $("settings-overlay").addEventListener("click", (event) => {
    if (event.target === $("settings-overlay")) $("settings-overlay").classList.add("hidden");
  });
  $("fx-toggle").addEventListener("change", () => {
    localStorage.setItem("council:fx", $("fx-toggle").checked ? "on" : "off");
    applyFxSetting();
  });
  $("lang-select").value = LANG;
  $("lang-select").addEventListener("change", () => switchLang($("lang-select").value));

  // Keys section (lives inside settings)
  $("btn-keys-custom").addEventListener("click", addCustomKey);
  $("keys-custom-value").addEventListener("keydown", (event) => {
    if (event.key === "Enter") addCustomKey();
  });

  $("btn-diagnostics").addEventListener("click", runDiagnostics);

  bindActions();
}

async function boot() {
  initSavedTheme();
  bindBoot();
  bindHelpTips();
  applyFxSetting();
  bindTuningInputs();
  renderTuningInputs();
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
    META = await api("/api/meta");
    try { MODELS = await api("/api/models"); } catch (_) { MODELS = null; }
    renderRoster();
    renderRosterEditor();
    renderConfigWarnings();
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
