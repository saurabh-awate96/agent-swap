"use strict";
// Every value is placed with textContent: emails, aliases and org names come
// from outside this page and must never be parsed as markup.

const STATUS = {
  token_expired: "token expired, refreshed when needed",
  api_key: "API key (no quota)",
  keychain_unavailable: "keychain unavailable, try again",
  relogin_required: "login expired: log in and run cswap add --slot N",
  foreign_credential: "live login belongs to another account, a switch repairs it",
  no_credentials: "no stored credentials",
  unavailable: "usage unavailable",
};

let token = null;
try {
  token = sessionStorage.getItem("cswap-token");
  if (location.hash.length > 1) {
    token = location.hash.slice(1);
    sessionStorage.setItem("cswap-token", token);
  }
} catch (e) { /* storage blocked: the token then lives in this page only */ }
if (location.hash) history.replaceState(null, "", location.pathname);

const $ = (id) => document.getElementById(id);
let settings = null;
let latest = null;
let busy = false;

function el(tag, text, cls) {
  const n = document.createElement(tag);
  if (text !== undefined && text !== null) n.textContent = text;
  if (cls) n.className = cls;
  return n;
}

async function api(path, body) {
  const opts = { headers: { Authorization: "Bearer " + token } };
  if (body !== undefined) {
    opts.method = "POST";
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  const data = await res.json();
  if (!res.ok) throw new Error((data.error && data.error.message) || res.statusText);
  return data;
}

function countdown(iso) {
  if (!iso) return "";
  const s = Math.round((Date.parse(iso) - Date.now()) / 1000);
  if (s <= 0) return "resets now";
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return "resets in " + (d ? d + "d " + h + "h" : h ? h + "h " + m + "m" : m + "m");
}

function windowEl(label, w, line) {
  const box = el("div", null, "win");
  const head = el("div", null, "label");
  const pct = typeof w.pct === "number" ? w.pct : null;
  head.append(el("span", label), el("span", (pct === null ? "n/a" : pct.toFixed(0) + "%") + " " + countdown(w.resetsAt)));
  const bar = el("div", null, "bar");
  const fill = el("div", null, "fill" + (pct >= line ? " bad" : pct >= line - 15 ? " warn" : ""));
  fill.style.width = Math.min(100, pct || 0) + "%";
  bar.append(fill);
  if (line) {
    const tick = el("div", null, "tick");
    tick.style.left = "calc(" + line + "% - 1px)";
    bar.append(tick);
  }
  box.append(head, bar);
  return box;
}

function card(a) {
  const c = el("section", null, "card");
  c.append(el("h2", a.number + "  " + (a.alias || a.email)));
  const chips = el("div", null, "row");
  if (a.active) chips.append(el("span", "active", "chip active"));
  if (a.disabled) chips.append(el("span", "disabled", "chip bad"));
  if (a.plan && a.plan.label) chips.append(el("span", a.plan.label, "chip"));
  if (a.organizationName) chips.append(el("span", a.organizationName, "chip"));
  c.append(chips);

  const line = settings ? settings.autoswitch.threshold : null;
  const weekly = settings && settings.autoswitch.weeklyThreshold || line;
  const u = a.usage || a.lastGoodUsage;
  if (u) {
    if (u.fiveHour) c.append(windowEl("5 hours", u.fiveHour, line));
    if (u.sevenDay) c.append(windowEl("7 days", u.sevenDay, weekly));
    (u.scoped || []).forEach((w) => c.append(windowEl(w.name, w, weekly)));
  }
  if (a.usageStatus !== "ok") {
    c.append(el("p", STATUS[a.usageStatus] || a.usageStatus, "muted"));
  }
  if (settings && settings.writable) {
    const row = el("div", null, "actions");
    const sw = el("button", "Switch");
    sw.disabled = !!a.active;
    sw.onclick = () => act("/api/switch", { number: String(a.number) });
    const dis = el("button", a.disabled ? "Enable" : "Disable");
    dis.onclick = () => act("/api/disable", { number: String(a.number), disabled: !a.disabled });
    row.append(sw, dis);
    c.append(row);
  }
  return c;
}

function render() {
  if (!latest) return;
  const main = $("accounts");
  main.replaceChildren(...latest.accounts.map(card));
  if (!latest.accounts.length) main.append(el("p", "No accounts yet. Run cswap onboard in a terminal.", "muted"));
}

async function act(path, body) {
  if (busy) return;
  busy = true;
  $("notice").textContent = "working...";
  try {
    const r = await api(path, body);
    $("notice").textContent = (r.message || (r.switched ? "Switched. Running Claude Code picks it up within about 30s." : "Nothing to do.")).toString();
  } catch (e) {
    $("notice").textContent = "Error: " + e.message;
  }
  busy = false;
  refresh();
}

async function refresh() {
  try {
    latest = await api("/api/accounts");
    $("updated").textContent = "updated " + new Date().toLocaleTimeString();
    render();
  } catch (e) {
    $("notice").textContent = token ? "Error: " + e.message : "Reopen the URL printed by cswap gui.";
  }
}

async function loop() {
  if (!document.hidden) await refresh();
  setTimeout(loop, ((settings && settings.pollSeconds) || 3) * 1000);
}

async function start() {
  if (!token) { $("notice").textContent = "Reopen the URL printed by cswap gui."; return; }
  try {
    settings = await api("/api/settings");
  } catch (e) { $("notice").textContent = "Error: " + e.message; return; }
  $("mode").textContent = settings.writable ? "can change accounts" : "read-only";
  $("mode").className = "chip" + (settings.writable ? " write" : "");
  if (settings.writable) {
    $("add").hidden = false;
    $("add").onclick = () => act("/api/add-current", {});
  }
  loop();
}

start();
