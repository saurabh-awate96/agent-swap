"use strict";
// agent-swap control panel. Two jobs, two places: "Active now" answers "what am I on and how
// much is left" (one card per product, limits read like Claude's own usage page), and the
// accounts table is where you act on any account. Every value is placed with textContent;
// nothing here builds markup from data or a command from input.

const SENTINEL = {
  token_expired: "Token expired. It is refreshed when needed; Refresh does it now.",
  api_key: "API key: no usage limits to show.",
  keychain_unavailable: "Keychain locked or busy. Try again.",
  relogin_required: "This login is dead. Sign in to it again (Add account).",
  foreign_credential: "The live login belongs to another account. A switch repairs it.",
  no_credentials: "No stored login.",
  unavailable: "Usage unavailable right now.",
};
const PROBLEM = {
  revoked: "OpenAI revoked this login. Sign in to it again (Add account).",
  unauthorized: "OpenAI rejected this login. Sign in to it again (Add account).",
  failed: "Could not read this account's usage from Codex just now.",
};
const WARN_PCT = 70, CRIT_PCT = 90, FETCH_MS = 15000;
const STRATEGIES = ["best", "weekly-first", "consume-first", "priority-consume-first"];
const NAMES = { claude: "Claude Code", codex: "Codex" };
const BASE = { claude: "/api/claude", codex: "/api/codex" };

// -- plumbing ------------------------------------------------------------------------------

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
const state = { claude: null, codex: null, auto: { claude: null, codex: null } };
let settings = null, busy = false, lastSig = "", seq = 0, holdUntil = 0, lastGood = null, lastJobKey = null;
let filter = "all", selected = null, linkToasted = null;            // selected = "claude:2" / "codex:1"
const seen = {};                                 // row key -> its numbers at the last redraw (for the flash)
const log = [];

function el(tag, text, cls) {
  const n = document.createElement(tag);
  if (text !== undefined && text !== null) n.textContent = text;
  if (cls) n.className = cls;
  return n;
}

async function api(path, body, ms) {
  const opts = { headers: { Authorization: "Bearer " + token } };
  if (body !== undefined) {
    opts.method = "POST";
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), ms || FETCH_MS);
  opts.signal = ctl.signal;
  let res;
  try {
    res = await fetch(path, opts);
  } catch (e) {
    const err = new Error(e.name === "AbortError" ? "timed out" : "unreachable");
    err.status = 0;
    throw err;
  } finally {
    clearTimeout(timer);
  }
  let data = null;
  try { data = await res.json(); } catch (e) { /* not JSON */ }
  if (!res.ok) {
    const err = new Error((data && data.error && data.error.message) || res.statusText || "request failed");
    err.status = res.status;
    throw err;
  }
  return data;
}

const writable = () => !!(settings && settings.writable);
const loginOpen = () => [state.claude, state.codex].some((d) => d && d.job && d.job.state === "running");
const sev = (pct) => (pct == null ? "" : pct >= CRIT_PCT ? "crit" : pct >= WARN_PCT ? "warn" : "");
const clock = () => new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });

// -- time, Claude-style: "Resets 7:40 PM", "Resets Wed 3:30 PM", "Resets Nov 13" ---------------------

function resetsText(ms) {
  if (!ms || isNaN(ms)) return "";
  const d = new Date(ms), now = new Date(), diff = ms - now.getTime();
  if (diff <= 0) return "Reset since this reading";
  const time = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  if (d.toDateString() === now.toDateString()) return "Resets " + time;
  if (diff < 6 * 86400000) return "Resets " + d.toLocaleDateString([], { weekday: "short" }) + " " + time;
  return "Resets " + d.toLocaleDateString([], { month: "short", day: "numeric" });
}

function inText(ms) {
  const s = Math.round((ms - Date.now()) / 1000);
  if (s <= 0) return "";
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return "in " + (d ? d + "d " + h + "h" : h ? h + "h " + m + "m" : m + "m");
}

function ago(ms) {
  const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
  if (s < 60) return "just now";
  const m = Math.round(s / 60);
  return m < 60 ? m + "m ago" : Math.round(m / 60) + "h ago";
}

// -- one model for both products -----------------------------------------------------------------------

function windowName(minutes) {
  if (minutes === 300) return ["session", "Current session"];
  if (minutes === 10080) return ["week", "Weekly"];
  if (minutes >= 20000) return ["month", Math.round(minutes / 1440) + "-day limit"];
  return ["other", Math.round(minutes / 60) + "h limit"];
}

function globalLine(p) { const a = state.auto[p]; return a && a.settings ? a.settings.threshold : null; }

function model(p, a) {
  if (p === "claude") {
    const org = a.organizationName || "";
    const line = typeof a.threshold === "number" ? a.threshold : globalLine("claude");
    const weeklyLine = (state.auto.claude && state.auto.claude.settings.weeklyThreshold) || line;
    const u = a.usage || {}, limits = [];
    const t = (w) => (w && w.resetsAt ? Date.parse(w.resetsAt) : null);
    if (u.fiveHour) limits.push({ kind: "session", name: "Current session", pct: u.fiveHour.pct, reset: t(u.fiveHour), line });
    if (u.sevenDay) limits.push({ kind: "week", name: "Weekly · all models", pct: u.sevenDay.pct, reset: t(u.sevenDay), line: weeklyLine, ahead: u.sevenDay.aheadOfPace });
    (u.scoped || []).forEach((w) => limits.push({ kind: "scoped", name: "Weekly · " + w.name, pct: w.pct, reset: t(w), line: weeklyLine }));
    if (u.spend) limits.push({ kind: "spend", name: "Extra usage", pct: u.spend.pct, reset: t(u.spend), line: null, extra: "$" + u.spend.used.toFixed(2) + " of $" + u.spend.limit.toFixed(2) });
    const problem = a.usageStatus && a.usageStatus !== "ok" ? (SENTINEL[a.usageStatus] || a.usageStatus) : null;
    return {
      p, id: String(a.number), alias: a.alias || "", email: a.email || "", active: !!a.active, disabled: !!a.disabled,
      threshold: typeof a.threshold === "number" ? a.threshold : null,
      plan: a.plan && a.plan.label ? a.plan.label : "", org: a.isOrganization && !org.endsWith("'s Organization") ? org : "",
      limits, problem, dead: a.usageStatus === "relogin_required", apikey: a.usageStatus === "api_key",
      read: a.usageAgeSeconds != null ? Date.now() - a.usageAgeSeconds * 1000 : null, live: true, health: null,
      sig: JSON.stringify([a.usage, a.usageStatus, a.disabled, a.active]),
    };
  }
  const u = a.usage || {}, limits = [];
  const line = typeof a.threshold === "number" ? a.threshold : globalLine("codex");
  ["primary", "secondary"].forEach((k) => {
    const w = u[k];
    if (!w) return;
    const [kind, name] = windowName(w.window_minutes);
    const reset = w.resets_at * 1000;
    limits.push({ kind, name, pct: reset > Date.now() ? w.used_percent : null, reset, line });
  });
  const longOnly = !u.secondary && u.primary && u.primary.window_minutes >= 20000;
  return {
    p, id: String(a.slot), alias: a.alias || "", email: a.email || "", active: !!a.active, disabled: !!a.disabled,
    threshold: typeof a.threshold === "number" ? a.threshold : null, plan: a.plan || "", org: "",
    limits, problem: a.problem ? PROBLEM[a.problem] || PROBLEM.failed : null, dead: a.problem === "revoked" || a.problem === "unauthorized",
    apikey: a.authMode === "apikey", read: u.seenAt ? Date.parse(u.seenAt) : null, live: !!u.live, health: a.health || null,
    note: longOnly ? "This plan has one " + windowName(u.primary.window_minutes)[1].toLowerCase() + " and no weekly limit." : null,
    sig: JSON.stringify([a.usage, a.health, a.problem, a.disabled, a.active]),
  };
}

const accounts = (p) => (state[p] ? state[p].accounts.map((a) => model(p, a)) : []);
const allAccounts = () => accounts("claude").concat(accounts("codex"));
const keyOf = (m) => m.p + ":" + m.id;
const label = (m) => m.alias || m.email || "slot " + m.id;
const byKey = (k) => allAccounts().find((m) => keyOf(m) === k) || null;

// -- small pieces ----------------------------------------------------------------------------------------

function btn(text, cls, onclick, opts) {
  const o = opts || {};
  const b = el("button", text, cls);
  b.type = "button";
  b.disabled = !!o.disabled;
  if (o.title) b.title = o.title;
  b.addEventListener("click", (ev) => { ev.stopPropagation(); onclick(ev); });
  return b;
}

function bar(pct, line, big) {
  const b = el("div", null, "bar " + (pct == null ? "unknown" : sev(pct)));
  if (big) b.style.height = "6px";
  if (pct != null) { const f = el("i"); f.style.width = Math.max(0, Math.min(100, pct)) + "%"; b.append(f); }
  if (line != null && pct != null) { const t = el("u"); t.style.left = "calc(" + Math.min(100, line) + "% - 1px)"; t.title = "switches away at " + line + "%"; b.append(t); }
  return b;
}

function limitRow(l) {
  const row = el("div", null, "limit");
  const left = el("div");
  left.append(el("div", l.name, "name"));
  const when = l.pct == null && l.reset && l.reset <= Date.now() ? "Reset since this reading" : resetsText(l.reset);
  const w = el("div", [when, l.extra, l.ahead ? "ahead of pace" : ""].filter(Boolean).join(" · "), "when");
  if (l.reset) w.title = inText(l.reset);
  left.append(w);
  const pct = el("div", null, "pct");
  if (l.pct == null) pct.textContent = "—";
  else { pct.append(el("b", Math.round(l.pct) + "%"), document.createTextNode(" used")); }
  row.append(left, bar(l.pct, l.line, true), pct);
  return row;
}

function meter(l, column) {
  const box = el("div", null, "meter");
  if (!l) { box.append(el("span", "—", "dash")); return box; }
  const top = el("span", null, l.kind === column ? "" : "other");
  // A different window in this column (a 30-day limit under "Weekly") always says what it is.
  top.append(el("span", l.kind === "session" ? "Session" : l.kind === "week" ? "Weekly" : l.kind === "month" ? l.name.replace(" limit", "") : l.name.replace("Weekly · ", ""), "lbl"),
             l.pct == null ? el("b", "—", "dash") : el("b", Math.round(l.pct) + "%"));
  box.append(top, bar(l.pct, l.line));
  return box;
}

function statusPill(m) {
  if (m.dead) return el("span", "Sign in again", "status bad");
  if (m.problem && !m.apikey) return el("span", "Check", "status bad");
  if (m.active) return el("span", "Active", "status active");
  if (m.disabled) return el("span", "Disabled", "status off");
  return el("span", "Ready", "status");
}

// -- doing things --------------------------------------------------------------------------------------------

function say(text, bad) {
  log.push({ ts: clock(), text: String(text), bad: !!bad });
  if (log.length > 30) log.shift();
  renderLog();
  const t = el("div", String(text), "toast" + (bad ? " bad" : ""));
  $("toasts").append(t);
  setTimeout(() => t.remove(), bad ? 7000 : 4000);
}

function renderLog() {
  const box = $("log");
  box.replaceChildren();
  log.slice().reverse().forEach((e) => { const r = el("div"); r.append(el("span", e.ts, "t"), el("span", e.text, e.bad ? "bad" : "")); box.append(r); });
  $("activity-toggle").textContent = "Activity" + (log.length ? " (" + log.length + ")" : "");
}

function describe(r) {
  if (r && r.message) return String(r.message);
  if (r && r.switched) return "Switched.";
  return r && r.reason ? "Stayed: " + r.reason : "Nothing to do.";
}

function codexNote() {
  const c = state.codex;
  if (!c || !c.running) return "";
  return c.supervised ? " Codex sessions started with `aswap codex run` reopen on it." : " A Codex that is already running keeps its account until restarted.";
}

async function act(path, body, what) {
  if (busy) return;
  busy = true;
  document.body.classList.add("busy");
  try {
    const r = await api(path, body, 90000);
    let text = describe(r);
    if (/^\/api\/codex\/(switch|rotate)$/.test(path)) text = (r && r.switched ? "Switched." : "Already on it.") + codexNote();
    say((what ? what + ": " : "") + text, false);
  } catch (e) {
    say((what ? what + ": " : "") + e.message, true);
  }
  busy = false;
  document.body.classList.remove("busy");
  lastSig = "";
  refresh();
}

// -- the dialog: fields, or a list of choices --------------------------------------------------------------------

const dlg = $("dlg");

function ask(o) {
  return new Promise((resolve) => {
    $("dlg-title").textContent = o.title;
    $("dlg-text").textContent = o.text || "";
    $("dlg-text").hidden = !o.text;
    const box = $("dlg-fields");
    box.replaceChildren();
    const inputs = {};
    let picked = null;
    (o.fields || []).forEach((f) => {
      const wrap = el("label", null, "field" + (f.type === "checkbox" ? " check" : ""));
      let input;
      if (f.type === "select") {
        input = el("select");
        f.options.forEach((v) => { const op = el("option", v); op.value = v; input.append(op); });
        input.value = f.value;
      } else {
        input = el("input");
        input.type = f.type || "text";
        if (f.type === "checkbox") input.checked = !!f.value;
        else input.value = f.value == null ? "" : String(f.value);
        if (f.step) input.step = f.step;
        if (f.placeholder) input.placeholder = f.placeholder;
        input.autocomplete = "off";
        input.spellcheck = false;
      }
      if (f.type === "checkbox") wrap.append(input, el("span", f.label));
      else wrap.append(el("span", f.label), input);
      box.append(wrap);
      inputs[f.name] = input;
    });
    if (o.choices) {
      const list = el("div", null, "choices");
      let group = null;
      o.choices.forEach((c) => {
        if (c.p !== group) { group = c.p; list.append(el("div", NAMES[c.p], "choice-group prov " + c.p)); }
        const b = el("button", null, "choice");
        b.type = "button";
        b.disabled = !!c.disabled;
        b.append(el("span", c.title), el("small", c.text));
        b.addEventListener("click", () => { picked = c.id; dlg.close("ok"); });
        list.append(b);
      });
      box.append(list);
    }
    if (o.keys) {
      const k = el("div", null, "keys");
      o.keys.forEach(([key, what]) => { const kb = el("span"); kb.append(el("kbd", key)); k.append(kb, el("span", what)); });
      box.append(k);
    }
    const ok = $("dlg-ok");
    ok.hidden = !!(o.choices || o.keys);
    ok.textContent = o.ok || "OK";
    ok.className = o.danger ? "danger" : "primary";
    $("dlg-cancel").textContent = o.keys ? "Close" : "Cancel";
    const submit = () => {
      const out = {};
      Object.keys(inputs).forEach((k) => { out[k] = inputs[k].type === "checkbox" ? inputs[k].checked : inputs[k].value; });
      picked = out;
      dlg.close("ok");
    };
    ok.onclick = submit;
    box.onkeydown = (e) => { if (e.key === "Enter" && !o.choices) { e.preventDefault(); submit(); } };
    $("dlg-cancel").onclick = () => dlg.close("cancel");
    dlg.onclose = () => { lastSig = ""; resolve(dlg.returnValue === "ok" ? picked : null); };
    dlg.returnValue = "";
    dlg.showModal();
    const first = box.querySelector("input, select, button.choice:not(:disabled)");
    (first || $("dlg-cancel")).focus();
  });
}

// -- account actions ----------------------------------------------------------------------------------------------

const doSwitch = (m) => act(BASE[m.p] + "/switch", { number: m.id }, "Switch to " + label(m));
const doDisable = (m) => act(BASE[m.p] + "/disable", { number: m.id, disabled: !m.disabled }, (m.disabled ? "Enable " : "Disable ") + label(m));
const rotate = (p, strategy) => act(BASE[p] + "/rotate", { strategy }, NAMES[p] + " · " + (strategy === "next" ? "next account" : strategy));

async function refreshOne(m) {
  if (busy) return;
  busy = true;
  try {
    const r = await api(BASE[m.p] + "/refresh", { number: m.id }, 90000);
    r.accounts.forEach((a) => say(NAMES[m.p] + " · " + a.label + ": " + a.status + (a.detail ? " · " + a.detail : ""), !a.ok));
  } catch (e) { say(label(m) + ": " + e.message, true); }
  busy = false;
  lastSig = "";
  refresh();
}

async function refreshAll() {
  if (busy || !writable()) return;
  busy = true;
  $("refresh").disabled = true;
  $("refresh").textContent = "↻ Checking…";
  for (const p of ["claude", "codex"]) {
    try {
      const r = await api(BASE[p] + "/refresh", {}, 90000);
      const bad = r.accounts.filter((a) => !a.ok);
      say(NAMES[p] + ": " + r.message, bad.length > 0);
      bad.forEach((a) => say(NAMES[p] + " · " + a.label + ": " + a.status, true));
    } catch (e) { say(NAMES[p] + ": " + e.message, true); }
  }
  $("refresh").disabled = false;
  $("refresh").textContent = "↻ Refresh";
  busy = false;
  lastSig = "";
  refresh();
}

async function editAlias(m) {
  const v = await ask({ title: "Short name", text: label(m), ok: "Save", fields: [{ name: "alias", label: "Alias (blank removes it)", value: m.alias }] });
  if (v) act(BASE[m.p] + "/alias", { number: m.id, alias: v.alias }, "Alias");
}

async function editThreshold(m) {
  const v = await ask({
    title: "Switch-away line", text: label(m), ok: "Save",
    fields: [{ name: "pct", label: "Percent used, 50 to 99.9 (blank = the global line)", type: "number", step: "0.5", value: m.threshold == null ? "" : m.threshold }],
  });
  if (!v) return;
  const raw = v.pct.trim(), value = raw === "" ? null : Number(raw);
  if (value !== null && Number.isNaN(value)) { say("That is not a number.", true); return; }
  act(BASE[m.p] + "/threshold", { number: m.id, threshold: value }, "Threshold");
}

async function removeAcct(m) {
  const v = await ask({
    title: "Remove " + label(m) + "?", ok: "Remove", danger: true,
    text: "Its stored login is deleted from agent-swap" + (m.p === "codex" ? ". Codex itself is not touched." : ", with its config backup."),
  });
  if (v) act(BASE[m.p] + "/remove", { number: m.id }, "Remove");
}

async function addAccount(only) {
  const unsavedC = state.claude && state.claude.unsaved, unsavedX = state.codex && state.codex.unsaved;
  const deadX = unsavedX && (unsavedX.problem === "revoked" || unsavedX.problem === "unauthorized");
  const choices = [
    { id: "claude-login", p: "claude", title: "Sign in with the browser", text: "Claude Code's sign-in. The login in use is saved first." },
    { id: "claude-save", p: "claude", title: "Save the login in use", text: unsavedC ? unsavedC.email + " is not saved yet" : "Back it up or refresh its stored copy", },
    { id: "claude-token", p: "claude", title: "Use a token or API key", text: "A setup-token or Console API key" },
    { id: "codex-login", p: "codex", title: "Sign in with the browser", text: "Codex's sign-in. The login in use is saved first unless it is revoked." },
    { id: "codex-code", p: "codex", title: "Sign in with a code", text: "For a machine without a browser" },
    { id: "codex-save", p: "codex", title: "Save the login in use", text: deadX ? "Not possible: OpenAI revoked it" : unsavedX ? unsavedX.email + " is not saved yet" : "Back it up or refresh its stored copy", disabled: deadX },
    { id: "codex-key", p: "codex", title: "Use an OpenAI API key", text: "Handed to codex on its standard input" },
  ].filter((c) => !only || c.p === only);
  const pick = await ask({ title: "Add an account", choices });
  if (!pick) return;
  const [p, how] = pick.split("-");
  if (how === "save") return act(BASE[p] + "/save", {}, NAMES[p] + " · save login");
  if (how === "code") return act(BASE.codex + "/login", { device: true }, "Codex · sign in by code");
  if (how === "login" && p === "codex") return act(BASE.codex + "/login", {}, "Codex · sign in");
  if (how === "login") {
    const v = await ask({ title: "Sign in to Claude Code", ok: "Open sign-in", fields: [{ name: "email", label: "Email to pre-fill (optional)", placeholder: "you@example.com" }] });
    if (v) act(BASE.claude + "/login", v.email.trim() ? { email: v.email.trim() } : {}, "Claude · sign in");
    return;
  }
  const fields = [{ name: "token", label: p === "codex" ? "OpenAI API key" : "Token or key", type: "password" }];
  if (p === "claude") fields.push({ name: "email", label: "Label (optional)", placeholder: "setup-token-N@token.local" });
  const v = await ask({ title: p === "codex" ? "Add an OpenAI API key" : "Add a token or API key", ok: "Add", fields,
                        text: "Stored like any account and never shown again." });
  if (!v) return;
  const body = { token: v.token.trim() };
  if (v.email && v.email.trim()) body.email = v.email.trim();
  act(BASE[p] + "/token", body, NAMES[p] + " · add key");
}

// -- auto-switch ------------------------------------------------------------------------------------------------

function toggleAuto(p, dry) {
  const a = state.auto[p];
  if (!a) return;
  if (a.running) act(BASE[p] + "/auto", { action: "stop" }, NAMES[p] + " · auto-switch");
  else act(BASE[p] + "/auto", dry ? { action: "start", dryRun: true } : { action: "start" }, NAMES[p] + " · auto-switch");
}

async function autoSettings(p) {
  const s = state.auto[p].settings;
  const fields = [{ name: "threshold", label: "Switch away at (% used)", type: "number", step: "0.5", value: s.threshold }];
  if (p === "claude") fields.push(
    { name: "weekly", label: "Weekly line (blank = same)", type: "number", step: "0.5", value: s.weeklyThreshold == null ? "" : s.weeklyThreshold },
    { name: "strategy", label: "Strategy", type: "select", options: STRATEGIES, value: s.strategy },
    { name: "keepalive", label: "Keepalive: a tiny real request per idle account (uses a little quota)", type: "checkbox", value: s.keepalive },
  );
  const v = await ask({ title: NAMES[p] + " auto-switch", ok: "Save", fields,
                        text: p === "codex" ? "The line is shared with Claude Code." : "A running auto-switch restarts with the new values." });
  if (!v) return;
  const threshold = Number(v.threshold), weekly = p === "claude" ? (v.weekly.trim() === "" ? null : Number(v.weekly)) : null;
  if (Number.isNaN(threshold) || Number.isNaN(weekly)) { say("A line must be a number.", true); return; }
  const changes = [];
  if (threshold !== s.threshold) changes.push(["autoswitch.threshold", threshold]);
  if (p === "claude") {
    if (weekly !== s.weeklyThreshold) changes.push(["autoswitch.weeklyThreshold", weekly]);
    if (v.strategy !== s.strategy) changes.push(["autoswitch.strategy", v.strategy]);
    if (v.keepalive !== s.keepalive) changes.push(["autoswitch.keepalive", v.keepalive]);
  }
  if (!changes.length) return;
  try {
    for (const [key, value] of changes) await api(BASE[p] + "/config", { key, value });
    say(NAMES[p] + " · auto-switch settings saved.", false);
  } catch (e) { say(e.message, true); }
  lastSig = "";
  refresh();
}

// -- rendering: banners -------------------------------------------------------------------------------------------

// Copy from a click (the browser allows it then); the old way for browsers without the API.
async function copyText(text, what) {
  try {
    await navigator.clipboard.writeText(text);
  } catch (e) {
    const t = el("textarea");
    t.value = text;
    t.setAttribute("readonly", "");
    t.style.position = "fixed";
    t.style.opacity = "0";
    document.body.append(t);
    t.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (err) { ok = false; }
    t.remove();
    if (!ok) { say("Could not copy the " + what + ". Select it and copy it by hand.", true); return; }
  }
  say(what.charAt(0).toUpperCase() + what.slice(1) + " copied to your clipboard.", false);
}

function isHttps(u) { try { return new URL(u).protocol === "https:"; } catch (e) { return false; } }

function renderBanners(conn) {
  const box = $("banners");
  box.replaceChildren();
  if (conn) box.append(el("div", conn, "banner warn"));
  const job = [state.claude, state.codex].map((d) => d && d.job).find((j) => j && j.state === "running");
  if (job) {
    const b = el("div", null, "banner info");
    b.append(el("span", NAMES[job.kind], "prov " + job.kind), el("span", job.message));
    if (job.url && isHttps(job.url)) {
      const a = el("a", "Open the sign-in page");
      a.href = job.url; a.target = "_blank"; a.rel = "noopener noreferrer";
      b.append(a, btn("Copy link", "ghost small", () => copyText(job.url, "sign-in link")));
    }
    if (job.code) {
      const c = el("span", "Code ");
      c.append(el("span", job.code, "code"));
      b.append(c, btn("Copy code", "ghost small", () => copyText(job.code, "code"), { title: "Copy the device code" }));
    }
    box.append(b);
  }
}

// -- rendering: "Active now" --------------------------------------------------------------------------------------

function readNote(m) {
  if (!m.read) return null;
  if (m.p === "claude") return "Usage read " + ago(m.read);
  return m.live ? "Read from Codex " + ago(m.read) : "Last seen " + ago(m.read) + " on this machine";
}

function nowCard(p) {
  const data = state[p], card = el("article", null, "card");
  const head = el("div", null, "card-head");
  const title = el("span", NAMES[p], "prov " + p);
  head.append(title);
  const rows = accounts(p), live = rows.find((m) => m.active);
  if (live && live.plan) head.append(el("span", live.plan, "plan"));
  card.append(head);
  if (!data) { card.append(el("div", "Loading…", "empty")); return card; }
  if (p === "codex" && data.installed === false && !rows.length) { card.append(el("div", "Codex is not installed (codex was not found on PATH).", "empty")); return card; }

  if (live) {
    const who = el("div", null, "who");
    if (live.alias) who.append(el("span", live.alias, "alias"));
    who.append(el("span", live.email, live.alias ? "email" : ""));
    card.append(who);
    if (live.problem) card.append(el("div", live.problem, "note warn"));
    if (live.limits.length) {
      const lim = el("div", null, "limits");
      live.limits.forEach((l) => lim.append(limitRow(l)));
      card.append(lim);
    }
    const notes = [live.note, readNote(live)].filter(Boolean);
    if (notes.length) card.append(el("div", notes.join(" · "), "note"));
  } else if (!data.unsaved) {
    card.append(el("div", rows.length ? "No " + NAMES[p] + " account is active." : "No " + NAMES[p] + " accounts yet.", "empty"));
  }

  if (data.unsaved) {
    const u = data.unsaved, dead = u.problem === "revoked" || u.problem === "unauthorized";
    const box = el("div", null, "unsaved" + (dead ? " bad" : ""));
    box.append(el("span", dead ? "Logged in as " + u.email + ", but " + PROBLEM[u.problem].toLowerCase() : "Logged in as " + u.email + ", not saved in agent-swap yet."));
    if (writable() && !dead) box.append(btn("Save", "primary small", () => act(BASE[p] + "/save", {}, NAMES[p] + " · save login"), { disabled: loginOpen() }));
    if (writable() && dead) box.append(btn("Sign in again", "ghost small", () => addAccount(p)));
    card.append(box);
  }

  const foot = el("div", null, "card-foot");
  const a = state.auto[p], autoBox = el("div", null, "auto");
  if (a) {
    const t = el("button", null, "toggle");
    t.type = "button";
    t.setAttribute("role", "switch");
    t.setAttribute("aria-checked", a.running ? "true" : "false");
    t.setAttribute("aria-label", NAMES[p] + " auto-switch");
    t.disabled = !writable();
    t.addEventListener("click", () => toggleAuto(p, false));
    const words = el("div");
    words.append(el("div", "Auto-switch " + (a.running ? (a.dryRun ? "on · dry run" : "on") : "off")),
                 el("div", a.running ? lastEvent(a) || "Watching…" : "Switches at " + a.settings.threshold + "% used when on", "last-event"));
    autoBox.append(t, words);
  }
  foot.append(autoBox);
  if (writable()) {
    const acts = el("div", null, "row-actions");
    if (a && !a.running) acts.append(btn("Dry run", "ghost small", () => toggleAuto(p, true), { title: "Decide and report, never switch" }));
    if (a) acts.append(btn("⚙", "ghost small icon", () => autoSettings(p), { title: "Auto-switch settings" }));
    if (rows.length > 1) acts.append(btn("Best account", "ghost small", () => rotate(p, "best"), { disabled: loginOpen(), title: "The one with the most quota left (b)" }));
    foot.append(acts);
  }
  card.append(foot);
  return card;
}

function lastEvent(a) {
  const e = a.events.filter((x) => x.kind !== "poll").slice(-1)[0];
  return e ? e.ts + " · " + e.text : "";
}

// -- rendering: the accounts table -----------------------------------------------------------------------------------

function renderFilters() {
  const box = $("filters");
  const counts = { all: allAccounts().length, claude: accounts("claude").length, codex: accounts("codex").length };
  box.replaceChildren(...["all", "claude", "codex"].map((f) => {
    const b = el("button", (f === "all" ? "All" : NAMES[f]) + " " + counts[f]);
    b.type = "button";
    b.setAttribute("role", "tab");
    b.setAttribute("aria-selected", filter === f ? "true" : "false");
    b.addEventListener("click", () => { filter = f; lastSig = ""; render(); });
    return b;
  }));
}

function shownRows() { return allAccounts().filter((m) => filter === "all" || m.p === filter); }

function accountRow(m) {
  const k = keyOf(m), wrap = el("div", null, "rowwrap" + (selected === k ? " sel" : ""));
  if (seen[k] !== undefined && seen[k] !== m.sig) wrap.classList.add("flash");
  seen[k] = m.sig;
  const row = el("div", null, "row");
  row.setAttribute("role", "row");
  row.tabIndex = 0;
  row.dataset.key = k;
  const acct = el("div", null, "acct");
  const av = el("div", (m.alias || m.email || "?").slice(0, 1), "avatar " + m.p);
  av.title = NAMES[m.p];
  const text = el("div", null, "acct-text");
  text.append(el("div", label(m), "acct-name"));
  text.append(el("div", [m.alias ? m.email : "", m.plan, m.org, m.threshold != null ? "switches at " + m.threshold + "%" : ""].filter(Boolean).join(" · ") || NAMES[m.p], "acct-sub"));
  acct.append(av, text);
  const session = m.limits.find((l) => l.kind === "session");
  const weekly = m.limits.find((l) => l.kind === "week") || m.limits.find((l) => l.kind === "month") || m.limits.find((l) => l.kind === "scoped");
  const acts = el("div", null, "row-actions");
  if (writable()) {
    if (!m.active) acts.append(btn("Switch", "ghost small", () => doSwitch(m), { disabled: loginOpen() || busy, title: "Make this the account in use (Enter)" }));
    acts.append(btn(selected === k ? "Close" : "More", "ghost small", () => select(selected === k ? null : k)));
  }
  row.append(acct, meter(session, "session"), meter(weekly, "week"), statusPill(m), acts);
  row.addEventListener("click", () => select(selected === k ? null : k));
  wrap.append(row);
  if (selected === k) wrap.append(detail(m));
  return wrap;
}

function detail(m) {
  const d = el("div", null, "detail");
  if (m.problem) d.append(el("div", m.problem, "note warn"));
  if (m.limits.length) {
    const lim = el("div", null, "limits");
    m.limits.forEach((l) => lim.append(limitRow(l)));
    d.append(lim);
  } else if (!m.problem) {
    d.append(el("div", m.apikey ? "API key: no usage limits to show." : "No usage seen yet. It appears after this account is used.", "note"));
  }
  const notes = [m.note, readNote(m)];
  if (m.health && m.health.at) notes.push("Login " + (m.health.status === "ok" ? "checked " : m.health.status + ", checked ") + ago(Date.parse(m.health.at)));
  if (notes.filter(Boolean).length) d.append(el("div", notes.filter(Boolean).join(" · "), "note"));
  if (writable()) {
    const a = el("div", null, "actions"), lock = loginOpen();
    if (!m.active) a.append(btn("Switch", "primary small", () => doSwitch(m), { disabled: lock }));
    a.append(
      btn("↻ Refresh", "ghost small", () => refreshOne(m), { disabled: lock, title: "Check just this account (u)" }),
      btn("Alias", "ghost small", () => editAlias(m), { disabled: lock }),
      btn("Switch-away line", "ghost small", () => editThreshold(m), { disabled: lock }),
      btn(m.disabled ? "Enable" : "Disable", "ghost small", () => doDisable(m), { disabled: lock, title: "Hold out of rotate, best and auto-switch" }),
      btn("Remove", "danger small", () => removeAcct(m), { disabled: lock }),
    );
    if (m.dead) a.prepend(btn("Sign in again", "primary small", () => addAccount(m.p)));
    d.append(a);
  }
  return d;
}

function select(k) {
  selected = k;
  lastSig = "";
  render();
  if (k) { const r = document.querySelector('[data-key="' + k + '"]'); if (r) { r.focus({ preventScroll: true }); r.scrollIntoView({ block: "nearest" }); } }
}

// -- render ----------------------------------------------------------------------------------------------------------

function render() {
  if (dlg.open) return;                                           // never under an open dialog
  if (Date.now() < holdUntil) { setTimeout(render, 450); return; } // nor under a click in progress
  renderBanners($("banners").dataset.conn || "");
  $("now").replaceChildren(nowCard("claude"), nowCard("codex"));
  renderFilters();
  const rows = shownRows();
  if (selected && !rows.some((m) => keyOf(m) === selected)) selected = null;
  $("rows").replaceChildren(...rows.map(accountRow));
  if (!rows.length) $("rows").append(el("div", "No accounts yet. Use + Add account.", "empty row"));
  const live = [state.claude, state.codex].map((d) => d && d.job).find((j) => j && j.state === "running" && j.url);
  if (live && live.url !== linkToasted) {
    linkToasted = live.url;
    say(live.copied
      ? "Sign-in link copied to your clipboard. Open it in the browser where the account you want is signed in."
      : "Sign-in link ready: use Copy link in the banner to paste it into the browser you want.", false);
  }
  const job = [state.claude, state.codex].map((d) => d && d.job).find((j) => j && j.state !== "running");
  const jk = job ? job.kind + job.state + job.message : "";
  if (job && jk !== lastJobKey && lastJobKey !== null) say(job.message, job.state === "error");
  lastJobKey = jk;
}

// -- keys ---------------------------------------------------------------------------------------------------------------

const KEYS = [
  ["↑ ↓  j k", "Select an account"], ["Enter", "Switch to the selected account"], ["Esc", "Close the details"],
  ["u", "Refresh the selected account"], ["f", "Refresh every account"], ["d", "Disable / enable"],
  ["a", "Alias"], ["t", "Switch-away line"], ["x", "Remove"], ["b", "Best account (selected product)"],
  ["n", "Next account that is not at its limit"], ["+", "Add an account"], ["g", "Auto-switch on / off"],
  ["1 2 3", "Show all / Claude Code / Codex"], ["?", "This list"],
];

document.addEventListener("pointerdown", () => { holdUntil = Date.now() + 400; });
document.addEventListener("keydown", (e) => {
  if (dlg.open || e.ctrlKey || e.metaKey || e.altKey) return;
  if (/^(input|select|textarea)$/i.test(e.target.tagName)) return;
  const k = e.key, rows = shownRows(), m = selected ? byKey(selected) : null;
  const go = (fn) => { e.preventDefault(); fn(); };
  const move = (d) => {
    if (!rows.length) return;
    const i = rows.findIndex((r) => keyOf(r) === selected);
    select(keyOf(rows[i < 0 ? (d > 0 ? 0 : rows.length - 1) : (i + d + rows.length) % rows.length]));
  };
  if (k === "ArrowDown" || k === "j") return go(() => move(1));
  if (k === "ArrowUp" || k === "k") return go(() => move(-1));
  if (k === "Escape" && selected) return go(() => select(null));
  if (k === "?") return go(() => ask({ title: "Keyboard shortcuts", keys: KEYS }));
  if (k === "1" || k === "2" || k === "3") return go(() => { filter = ["all", "claude", "codex"][Number(k) - 1]; lastSig = ""; render(); });
  if (!writable() || busy || loginOpen()) return;
  const p = m ? m.p : filter === "codex" ? "codex" : "claude";
  if (k === "Enter" && m && !m.active) return go(() => doSwitch(m));
  if (k === "u" && m) return go(() => refreshOne(m));
  if (k === "d" && m) return go(() => doDisable(m));
  if (k === "a" && m) return go(() => editAlias(m));
  if (k === "t" && m) return go(() => editThreshold(m));
  if (k === "x" && m) return go(() => removeAcct(m));
  if (k === "f") return go(refreshAll);
  if (k === "b") return go(() => rotate(p, "best"));
  if (k === "n") return go(() => rotate(p, "next-available"));
  if (k === "r") return go(() => rotate(p, "next"));
  if (k === "+") return go(() => addAccount(m ? m.p : null));
  if (k === "g") return go(() => toggleAuto(p, false));
});

// -- data --------------------------------------------------------------------------------------------------------------

function connection(ok, e) {
  document.body.classList.toggle("stale", !ok);
  $("banners").dataset.conn = ok ? "" : e && (e.status === 401 || e.status === 403)
    ? "This page is from an earlier run. Open the link that aswap gui printed, or run it again."
    : "No answer from the dashboard (" + (e ? e.message : "unreachable") + "). Showing the last data. Run aswap gui again if it stopped; this page keeps trying.";
}

async function refresh() {
  const mine = ++seq;
  try {
    const [c, x, ac, ax] = await Promise.all([api("/api/claude"), api("/api/codex"), api("/api/claude/auto"), api("/api/codex/auto")]);
    if (mine !== seq) return;
    state.claude = c; state.codex = x; state.auto = { claude: ac, codex: ax };
  } catch (e) {
    if (mine !== seq) return;
    connection(false, e);
    $("updated").textContent = lastGood ? "Last data " + lastGood : "";
    renderBanners($("banners").dataset.conn);
    return;
  }
  const wasStale = document.body.classList.contains("stale");
  connection(true);
  lastGood = clock();
  $("updated").textContent = "Updated " + lastGood;
  const sig = JSON.stringify([state, filter, selected, Math.floor(Date.now() / 30000)]);
  if (sig === lastSig && !wasStale) return;
  lastSig = sig;
  render();
}

async function loop() {
  if (!document.hidden) await refresh();
  setTimeout(loop, ((settings && settings.pollSeconds) || 3) * 1000);
}

document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });

async function start() {
  if (!token) { $("banners").append(el("div", "Open the link that aswap gui printed.", "banner warn")); return; }
  try {
    settings = await api("/api/settings");
  } catch (e) {
    connection(false, e);
    renderBanners($("banners").dataset.conn);
    setTimeout(start, 3000);
    return;
  }
  connection(true);
  $("mode").textContent = writable() ? "" : "read-only";
  $("refresh").hidden = !writable();
  $("add").hidden = !writable();
  $("refresh").addEventListener("click", refreshAll);
  $("add").addEventListener("click", () => addAccount(null));
  $("help").addEventListener("click", () => ask({ title: "Keyboard shortcuts", keys: KEYS }));
  $("activity-toggle").addEventListener("click", () => {
    const box = $("log"), open = box.hidden;
    box.hidden = !open;
    $("activity-toggle").setAttribute("aria-expanded", open ? "true" : "false");
  });
  renderLog();
  loop();
}

start();
