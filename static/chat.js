"use strict";
/* chat.js — remote chat with one running code assistant session.
   Read: poll /api/chat (incremental, by transcript byte offset).
   Write: /api/chat/send types into the live terminal; the message then echoes
   back from the transcript like any typed prompt, which confirms delivery. */

const SID = new URLSearchParams(location.search).get("session");
const $ = (s) => document.querySelector(s);

const state = {
  offset: 0,
  seen: new Set(),       // message ids already rendered
  pending: [],           // optimistic sends awaiting transcript echo
  active: false,
  activity: null,
  providerLabel: "session",
  firstLoad: true,
  polling: false,
};

if (!SID) {
  $("#ctitle").textContent = "No session selected";
  $("#box").disabled = true; $("#send").disabled = true;
}

// ------------------------------------------------------------- mini markdown
function esc(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function md(text) {
  // split on ``` fences: even chunks are prose, odd chunks are code
  const parts = text.split("```");
  let out = "";
  for (let i = 0; i < parts.length; i++) {
    if (i % 2) {
      let c = parts[i];
      const nl = c.indexOf("\n");
      if (nl !== -1 && /^[\w+-]*\s*$/.test(c.slice(0, nl))) c = c.slice(nl + 1);
      out += "<pre>" + esc(c.replace(/\n$/, "")) + "</pre>";
    } else {
      let h = esc(parts[i]);
      h = h.replace(/`([^`\n]+)`/g, "<code>$1</code>");
      h = h.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
      out += h.replace(/\n/g, "<br>");
    }
  }
  return out;
}

// ------------------------------------------------------------- rendering
function nearBottom() {
  const m = $("#msgs");
  return m.scrollHeight - m.scrollTop - m.clientHeight < 140;
}
function scrollDown() {
  const m = $("#msgs");
  m.scrollTop = m.scrollHeight;
}

function bubble(msg) {
  const d = document.createElement("div");
  d.className = "msg " + (msg.role === "user" ? "user" : "assistant");
  d.innerHTML = md(msg.text || "");
  return d;
}

function toolChip(msg) {
  const c = document.createElement("div");
  c.className = "toolchip";
  const b = document.createElement("b");
  b.textContent = msg.name || "tool";
  c.append(b);
  if (msg.text) c.append(document.createTextNode(" · " + msg.text));
  c.title = (msg.name || "tool") + (msg.text ? " — " + msg.text : "");
  return c;
}

function appendMessage(msg) {
  const msgs = $("#msgs");
  if (msg.role === "tool") {
    // group consecutive tool chips into one compact block
    let g = msgs.lastElementChild;
    if (!g || !g.classList.contains("toolgroup")) {
      g = document.createElement("div");
      g.className = "toolgroup";
      msgs.append(g);
    }
    g.append(toolChip(msg));
  } else {
    msgs.append(bubble(msg));
  }
}

function matchPending(text) {
  const i = state.pending.findIndex((p) => p.text === text);
  if (i === -1) return false;
  state.pending[i].el.remove();
  state.pending.splice(i, 1);
  return true;
}

function setStatus(active, activity) {
  state.active = active; state.activity = activity;
  const st = $("#cstatus"), lbl = $("#cstatelabel"), dot = st.querySelector(".dot");
  const act = active ? (activity || "active") : "inactive";
  st.className = "cstatus " + act;
  dot.className = "dot " + ({ working: "live", waiting: "wait", active: "live", inactive: "idle" }[act]);
  lbl.textContent = { working: "working", waiting: "waiting for you",
                      active: "running", inactive: "not running" }[act];
  $("#typing").classList.toggle("hidden", act !== "working");
  $("#deadbar").classList.toggle("hidden", active);
  $("#box").disabled = !active;
  $("#send").disabled = !active;
  if (!active) $("#box").placeholder = "Resume the session to chat…";
  else $("#box").placeholder = "Message this " + state.providerLabel + "…";
}

// ------------------------------------------------------------- polling
async function poll() {
  if (state.polling || !SID) return;
  state.polling = true;
  try {
    const r = await fetch(`/api/chat?session_id=${encodeURIComponent(SID)}&offset=${state.offset}`);
    if (r.status === 401) { location.reload(); return; }
    if (!r.ok) return;
    const data = await r.json();
    state.providerLabel = data.provider_label || "session";
    $("#ctitle").textContent = data.title || "(untitled session)";
    $("#cproj").textContent = data.project || data.cwd || "";
    document.title = (data.title || "Chat") + " · " + state.providerLabel;
    if (data.truncated) $("#histnote").classList.remove("hidden");

    const stick = nearBottom() || state.firstLoad;
    for (const m of data.messages || []) {
      if (m.id && state.seen.has(m.id)) continue;
      if (m.id) state.seen.add(m.id);
      if (m.role === "user" && matchPending(m.text)) { /* replaced optimistic */ }
      appendMessage(m);
    }
    state.offset = data.offset || state.offset;
    setStatus(!!data.active, data.activity);
    if (stick && (data.messages || []).length) scrollDown();
    if (state.firstLoad) { scrollDown(); state.firstLoad = false; }
  } catch { /* transient */ }
  finally { state.polling = false; }
}

// ------------------------------------------------------------- sending
async function send() {
  const box = $("#box");
  const text = box.value.trim();
  if (!text || !SID) return;
  box.value = ""; autosize();

  const el = document.createElement("div");
  el.className = "msg user pending";
  el.innerHTML = md(text);
  $("#msgs").append(el);
  scrollDown();
  const entry = { text, el };
  state.pending.push(entry);

  try {
    const r = await fetch("/api/chat/send", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: SID, text }),
    });
    if (r.status === 401) { location.reload(); return; }
    const data = await r.json().catch(() => ({}));
    if (!r.ok || !data.ok) {
      el.classList.remove("pending");
      el.classList.add("failed");
      const note = document.createElement("span");
      note.className = "sendnote";
      note.textContent = data.inactive
        ? "not delivered — session isn't running (tap to retry)"
        : "not delivered — " + (data.detail || "send failed") + " (tap to retry)";
      el.append(note);
      el.onclick = () => { el.remove(); box.value = text; autosize(); box.focus(); };
      state.pending.splice(state.pending.indexOf(entry), 1);
      if (data.inactive) setStatus(false, null);
    }
    // on success: leave the pale bubble; the transcript echo solidifies it
  } catch {
    el.classList.add("failed");
  }
}

// ------------------------------------------------------------- resume
async function resume() {
  const btn = $("#resumeBtn");
  btn.disabled = true; btn.textContent = "Resuming…";
  try {
    const r = await fetch("/api/resume", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: SID,
        dangerously: localStorage.getItem("csm_dangerous") !== "0",
      }),
    });
    const data = await r.json().catch(() => ({}));
    if (!data.ok) { btn.textContent = "Resume failed — retry"; btn.disabled = false; return; }
    // a fresh terminal is opening on the laptop; poll until it reports active
    const t0 = Date.now();
    const waiter = setInterval(async () => {
      await poll();
      if (state.active) { clearInterval(waiter); btn.disabled = false; btn.textContent = "▷ Resume on laptop"; }
      else if (Date.now() - t0 > 30000) {
        clearInterval(waiter); btn.disabled = false; btn.textContent = "▷ Resume on laptop";
      }
    }, 1500);
  } catch {
    btn.textContent = "Resume failed — retry"; btn.disabled = false;
  }
}

// ------------------------------------------------------------- wiring
function autosize() {
  const box = $("#box");
  box.style.height = "auto";
  box.style.height = Math.min(box.scrollHeight, 132) + "px";
}

$("#box").addEventListener("input", autosize);
$("#box").addEventListener("keydown", (e) => {
  // desktop: Enter sends, Shift+Enter = newline. touch: Enter = newline, use the button
  const coarse = matchMedia("(pointer: coarse)").matches;
  if (e.key === "Enter" && !e.shiftKey && !coarse) { e.preventDefault(); send(); }
});
$("#send").addEventListener("click", send);
$("#resumeBtn").addEventListener("click", resume);

let timer = setInterval(poll, 1500);
document.addEventListener("visibilitychange", () => {
  clearInterval(timer);
  if (!document.hidden) { poll(); timer = setInterval(poll, 1500); }
});
poll();
