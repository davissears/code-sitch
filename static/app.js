"use strict";

const state = {
  sessions: [],          // from /api/sessions, newest first
  byId: new Map(),
  filter: "",
  provider: localStorage.getItem("csm_provider") || "all",
  mode: "instant",       // "instant" | "agentic"
  agentic: { query: "", results: [], loading: false, error: null },
  meta: { active_count: 0, total: 0, indexing: false, enrich: {} },
  busy: new Set(),       // session ids with an in-flight action
};

const $ = (s) => document.querySelector(s);
const el = (tag, cls, txt) => { const e = document.createElement(tag); if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; };

// ---------------------------------------------------------------- time
function relTime(epochSec) {
  if (!epochSec) return "—";
  const s = Math.max(0, Date.now() / 1000 - epochSec);
  if (s < 45) return "just now";
  if (s < 90) return "a minute ago";
  const m = s / 60;
  if (m < 60) return Math.round(m) + "m ago";
  const h = m / 60;
  if (h < 24) return Math.round(h) + "h ago";
  const d = h / 24;
  if (d < 7) return Math.round(d) + "d ago";
  const wk = d / 7;
  if (wk < 5) return Math.round(wk) + "w ago";
  return new Date(epochSec * 1000).toLocaleDateString();
}
function absTime(epochSec) {
  if (!epochSec) return "";
  return new Date(epochSec * 1000).toLocaleString();
}

// ---------------------------------------------------------------- numbers
function fmtNum(n) { return (n || 0).toLocaleString(); }
function fmtTokens(n) {
  n = n || 0;
  if (n >= 1e9) return (n / 1e9).toFixed(n >= 1e10 ? 0 : 1) + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(n >= 1e4 ? 0 : 1) + "K";
  return "" + n;
}

function renderUsage() {
  const u = $("#usage");
  const s = state.meta.stats;
  if (!s || !s.rows) { u.classList.add("hidden"); u.innerHTML = ""; return; }
  u.classList.remove("hidden");
  const tip = (b) => !b ? "" :
    `input ${fmtNum(b.input)} · cache write ${fmtNum(b.cache_creation)} · output ${fmtNum(b.output)}\n` +
    `cache reads ${fmtNum(b.cache_read)} (context replays — not counted in the total)`;

  const table = el("table", "usage-table");
  const thead = el("thead"), htr = el("tr");
  const thPrompts = el("th", "num", "Prompts sent");
  thPrompts.title = "Prompts you typed (excludes assistant replies and tool-use steps)";
  htr.append(el("th", "up", "Period"), el("th", "num", "Chats"), thPrompts, el("th", "num", "Tokens"));
  thead.append(htr);
  const tbody = el("tbody");
  for (const r of s.rows) {
    const tr = el("tr", r.key === "all" ? "alltime" : "");
    tr.append(el("td", "up", r.label),
              el("td", "num", fmtNum(r.chats)),
              el("td", "num", fmtNum(r.messages)));
    const tdtok = el("td", "num tok", r.tokens ? fmtTokens(r.tokens.headline) : "—");
    if (r.tokens) tdtok.title = tip(r.tokens);
    tr.append(tdtok);
    tbody.append(tr);
  }
  table.append(thead, tbody);
  u.innerHTML = "";
  u.append(table);
}

// ---------------------------------------------------------------- data
async function fetchSessions() {
  try {
    const r = await fetch("/api/sessions");
    const data = await r.json();
    state.sessions = data.sessions || [];
    state.byId = new Map(state.sessions.map((s) => [s.id, s]));
    state.meta = data;
    render();
  } catch (e) {
    /* transient — keep last good render */
  }
}

// ---------------------------------------------------------------- search
function tokens(q) {
  return q.toLowerCase().split(/\s+/).filter(Boolean);
}

function providerMatches(s) {
  return state.provider === "all" || (s.provider || "claude") === state.provider;
}

function providerSessions() {
  return state.sessions.filter(providerMatches);
}

function instantMatches() {
  const q = state.filter.trim();
  const base = providerSessions();
  if (!q) return base;                       // already newest-first
  const terms = tokens(q);
  const scored = [];
  for (const s of base) {
    const hay = s.haystack || "";
    let ok = true, score = 0;
    for (const t of terms) {
      if (hay.indexOf(t) === -1) { ok = false; break; }
      const title = (s.title || "").toLowerCase();
      if (title.indexOf(t) !== -1) score += 5;
      else if ((s.keywords || []).some((k) => k.indexOf(t) !== -1)) score += 3;
      else score += 1;
    }
    if (ok) scored.push([score, s.updated || 0, s]);
  }
  scored.sort((a, b) => (b[0] - a[0]) || (b[1] - a[1]));
  return scored.map((x) => x[2]);
}

function displayList() {
  if (state.mode === "agentic") {
    const out = [];
    for (const r of state.agentic.results) {
      const s = state.byId.get(r.session_id);
      if (s && providerMatches(s)) out.push(Object.assign({}, s, { _reason: r.reason }));
    }
    return out;
  }
  return instantMatches();
}

function providerBadge(s) {
  return el("span", "provider provider-" + (s.provider || "session"), s.provider_label || s.provider || "Session");
}

// ---------------------------------------------------------------- agentic
async function runAgentic(query) {
  state.mode = "agentic";
  state.agentic = { query, results: [], loading: true, error: null };
  render();
  try {
    const r = await fetch("/api/search", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query }),
    });
    const data = await r.json();
    state.agentic.loading = false;
    if (data.error) { state.agentic.error = data.error; }
    else { state.agentic.results = data.results || []; }
  } catch (e) {
    state.agentic.loading = false;
    state.agentic.error = "search failed";
  }
  render();
}

function clearAgentic() {
  state.mode = "instant";
  state.agentic = { query: "", results: [], loading: false, error: null };
  render();
}

// ---------------------------------------------------------------- actions
function openChat(s) {
  location.href = "/chat?session=" + encodeURIComponent(s.id);
}

async function activate(s) {
  if (state.busy.has(s.id)) return;
  const caps = s.capabilities || {};
  state.busy.add(s.id);
  const verb = s.active ? "focus" : "resume";
  if (!caps[verb]) {
    toast(`${s.provider_label || "Provider"} does not support ${verb}`, "err");
    state.busy.delete(s.id);
    return;
  }
  try {
    const body = { session_id: s.id };
    if (verb === "resume") body.dangerously = $("#optDangerous").checked;
    const r = await fetch("/api/" + verb, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await r.json();
    if (data.ok) {
      toast(verb === "focus" ? "Focused window" : "Resuming in new terminal…", "ok");
      if (verb === "resume") setTimeout(fetchSessions, 1500);
    } else {
      toast((data.detail || "action failed"), "err");
      if (verb === "focus") fetchSessions(); // status may be stale
    }
  } catch (e) {
    toast("request failed", "err");
  } finally {
    state.busy.delete(s.id);
  }
}

// ---------------------------------------------------------------- render
function highlight(text, terms) {
  const frag = document.createDocumentFragment();
  if (!terms || !terms.length) { frag.appendChild(document.createTextNode(text)); return frag; }
  const lo = text.toLowerCase();
  let i = 0;
  while (i < text.length) {
    let best = -1, bestLen = 0;
    for (const t of terms) {
      const idx = lo.indexOf(t, i);
      if (idx !== -1 && (best === -1 || idx < best)) { best = idx; bestLen = t.length; }
    }
    if (best === -1) { frag.appendChild(document.createTextNode(text.slice(i))); break; }
    if (best > i) frag.appendChild(document.createTextNode(text.slice(i, best)));
    const m = el("mark", null, text.slice(best, best + bestLen));
    frag.appendChild(m);
    i = best + bestLen;
  }
  return frag;
}

function render() {
  renderUsage();
  renderProviderTabs();
  // stats
  const m = state.meta;
  const stats = $("#stats");
  stats.innerHTML = "";
  const live = el("span", "pill");
  live.append(el("span", "dot live"), el("span", null, `${m.active_count || 0} active`));
  stats.append(live);
  const working = state.sessions.filter((s) => s.active && s.activity === "working").length;
  if (working) {
    const wk = el("span", "pill");
    wk.append(el("span", "dot live"), el("span", null, `${working} working`));
    wk.title = "The assistant is actively generating in these sessions right now";
    stats.append(wk);
  }
  stats.append(el("span", "pill", `${m.total || 0} sessions`));
  const en = m.enrich || {};
  if (m.online === false) {
    const off = el("span", "pill offline", "AI offline");
    off.title = "No connection to the Anthropic API. Instant filter, focus and resume still work; AI keywords and AI search resume automatically when you're back online.";
    stats.append(off);
  } else if (m.indexing) stats.append(el("span", null, "indexing…"));
  else if (en.running) stats.append(el("span", null, `AI keywords ${en.done || 0}/${(en.done || 0) + (en.pending || 0)}`));

  // banner
  const banner = $("#banner");
  if (state.mode === "agentic") {
    banner.className = "banner" + (state.agentic.loading ? " loading" : "");
    banner.innerHTML = "";
    if (state.agentic.loading) {
      banner.append(el("span", "bspin"), el("span", null, `AI search is looking for “${state.agentic.query}”…`));
    } else if (state.agentic.error) {
      const msg = state.agentic.error === "offline"
        ? "AI search needs a connection — you appear to be offline. Instant keyword filter still works."
        : `Search error: ${state.agentic.error}`;
      banner.append(el("span", "bspark", "✳"), el("span", null, msg));
    } else {
      banner.append(el("span", "bspark", "✳"),
        el("span", null, `${state.agentic.results.length} result${state.agentic.results.length === 1 ? "" : "s"} for “${state.agentic.query}”`));
    }
    const clear = el("button", "clear", "Clear ✕");
    clear.onclick = clearAgentic;
    banner.append(clear);
    banner.classList.remove("hidden");
  } else {
    banner.classList.add("hidden");
  }

  // rows
  const list = displayList();
  const tbody = $("#rows");
  tbody.innerHTML = "";
  const terms = state.mode === "instant" ? tokens(state.filter) : [];

  // on touch devices the row opens remote chat (you can't focus a Mac window
  // from a phone); on the desktop it keeps focusing/resuming the terminal
  const coarse = matchMedia("(pointer: coarse)").matches;

  for (const s of list) {
    // three live states (working / waiting / active-unknown) plus inactive
    const act = s.active ? (s.activity || "active") : "inactive";
    const tr = el("tr", s.active ? "active state-" + act : "");
    tr.onclick = () => coarse ? openChat(s) : activate(s);

    // status
    const tdS = el("td", "c-status");
    const st = el("span", "status");
    const LABEL = { working: "working", waiting: "waiting", active: "active", inactive: "inactive" };
    const DOT = { working: "live", waiting: "wait", active: "live", inactive: "idle" };
    const dot = el("span", "dot " + DOT[act]);
    if (act === "waiting") dot.title = "The assistant is waiting for your input";
    else if (act === "working") dot.title = "The assistant is working right now";
    st.append(dot, el("span", null, LABEL[act]));
    tdS.append(st);

    // main
    const tdM = el("td", "c-main");
    const title = el("div", "title");
    title.appendChild(highlight(s.title || "(untitled session)", terms));
    title.title = s.first_prompt || s.title || "";
    tdM.append(title);
    if (s._reason) {
      tdM.append(el("div", "reason", "↳ " + s._reason));
    } else {
      const meta = el("div", "metaline");
      meta.append(providerBadge(s));
      meta.append(el("span", "sep", "·"));
      if (s.branch && s.branch !== "HEAD") { meta.append(el("span", "branch", "⎇ " + s.branch)); meta.append(el("span", "sep", "·")); }
      meta.append(el("span", null, `${s.messages} repl${s.messages === 1 ? "y" : "ies"}`));
      if (s.last_prompt) { meta.append(el("span", "sep", "·")); meta.append(el("span", null, "“" + truncate(s.last_prompt, 60) + "”")); }
      tdM.append(meta);
    }
    if (s._reason) {
      const meta = el("div", "metaline");
      meta.append(providerBadge(s));
      tdM.append(meta);
    }

    // project
    const tdP = el("td", "c-project");
    const pj = el("div", "project", s.project || "—");
    pj.title = s.cwd || "";
    tdP.append(pj);

    // replies (assistant response events, incl. tool-use steps — not your prompts)
    const tdC = el("td", "c-msgs msgs", String(s.messages || 0));
    tdC.title = "Assistant responses (incl. tool-use steps)";

    // time
    const tdT = el("td", "c-time");
    const tm = el("span", "time", relTime(s.updated));
    tm.title = absTime(s.updated);
    tdT.append(tm);

    // action
    const tdA = el("td", "c-action action");
    const caps = s.capabilities || {};
    const chat = el("span", "chip chat", "💬 chat");
    chat.title = "Chat with this session from here (works remotely)";
    if (caps.chat === false) {
      chat.classList.add("disabled");
      chat.title = "Chat is not supported for this provider";
    } else {
      chat.onclick = (e) => { e.stopPropagation(); openChat(s); };
    }
    const chip = el("span", "chip " + (s.active ? "focus" : "resume"),
      s.active ? "⤢ focus" : "▷ resume");
    const action = s.active ? "focus" : "resume";
    if (!caps[action]) {
      chip.classList.add("disabled");
      chip.title = `${s.provider_label || "Provider"} does not support ${action}`;
    }
    tdA.append(chat, chip);

    tr.append(tdS, tdM, tdP, tdC, tdT, tdA);
    tbody.append(tr);
  }

  // empty state
  const empty = $("#empty");
  if (list.length === 0) {
    empty.textContent = state.mode === "agentic"
      ? (state.agentic.loading ? "" : "No relevant sessions found.")
      : (state.filter ? "No sessions match your filter." : "No sessions yet.");
    empty.classList.toggle("hidden", state.agentic.loading);
    $("#grid").classList.add("hidden");
  } else {
    empty.classList.add("hidden");
    $("#grid").classList.remove("hidden");
  }

  $("#footstatus").textContent =
    `showing ${list.length} of ${state.meta.total || 0}` +
    (state.meta.generated_at ? " · updated " + new Date(state.meta.generated_at * 1000).toLocaleTimeString() : "");
}

function truncate(s, n) { s = s.replace(/\s+/g, " ").trim(); return s.length > n ? s.slice(0, n - 1) + "…" : s; }

function renderProviderTabs() {
  const tabs = $("#providerTabs");
  if (!tabs) return;
  for (const btn of tabs.querySelectorAll("button")) {
    const active = btn.dataset.provider === state.provider;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-selected", active ? "true" : "false");
  }
}

// ---------------------------------------------------------------- toast
let toastTimer = null;
function toast(msg, kind) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show " + (kind || "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.className = "toast hidden " + (kind || ""); }, 2600);
}

// ---------------------------------------------------------------- input wiring
function wire() {
  const search = $("#search");
  search.addEventListener("input", () => {
    state.filter = search.value;
    if (state.mode === "agentic") clearAgentic(); // typing returns to instant filter
    else render();
  });
  search.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      const q = search.value.trim();
      if (q) runAgentic(q);
    } else if (e.key === "Escape") {
      if (state.mode === "agentic") clearAgentic();
      else if (search.value) { search.value = ""; state.filter = ""; render(); }
      else search.blur();
    }
  });
  // "/" focuses search from anywhere
  document.addEventListener("keydown", (e) => {
    if (e.key === "/" && document.activeElement !== search) { e.preventDefault(); search.focus(); }
  });
  $("#optDangerous").checked = localStorage.getItem("csm_dangerous") !== "0"; // default ON
  $("#optDangerous").addEventListener("change", (e) => {
    localStorage.setItem("csm_dangerous", e.target.checked ? "1" : "0");
  });
  $("#providerTabs").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-provider]");
    if (!btn) return;
    state.provider = btn.dataset.provider;
    localStorage.setItem("csm_provider", state.provider);
    render();
  });
}

// ---------------------------------------------------------------- boot
wire();
fetchSessions();
setInterval(fetchSessions, 3000);           // live status + new sessions
setInterval(() => { if (state.mode === "instant") render(); }, 20000); // refresh relative times
