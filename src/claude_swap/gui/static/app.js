"use strict";
// Same layout and wording as the terminal dashboard (cswap tui), plus the controls
// the terminal has on its keys. Every value is placed with textContent: emails,
// aliases and org names come from outside this page and must never be parsed as
// markup. Nothing here builds a command: it only sends plain values to the API.

// Wording matches SENTINEL_NOTES in switcher.py.
const STATUS = {
  token_expired: "token expired — refreshed when needed (cswap list refreshes it now)",
  api_key: "API key (no quota)",
  keychain_unavailable: "keychain unavailable — locked or in use; try again",
  relogin_required: "re-login needed — refresh token dead; log in with Claude Code, then run: cswap add",
  foreign_credential: "live credential belongs to another account — a switch repairs it",
  no_credentials: "no stored credentials",
  unavailable: "usage unavailable",
};
const WARN_PCT = 70, CRIT_PCT = 90, BAR_WIDTH = 30, STALE_S = 300;
const STRATEGIES = ["best", "weekly-first", "consume-first", "priority-consume-first"];

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
let settings = null, claude = null, codex = null, auto = null;
let busy = false, lastSig = "", lastJobKey = null;

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
  const res = await fetch(path, opts);  // a stopped server throws here, before any status
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
const loginOpen = () => !!(claude && claude.job && claude.job.state === "running");
const sev = (pct) => (pct >= CRIT_PCT ? "crit" : pct >= WARN_PCT ? "warn" : "ok");

function notice(text, bad) {
  const n = $("notice");
  n.textContent = text;
  n.className = bad ? "warn" : "muted";
}

function age(seconds) {
  if (seconds == null) return "";
  const m = Math.round(seconds / 60);
  return m < 1 ? "just now" : m < 60 ? m + "m ago" : Math.round(m / 60) + "h ago";
}

function resetText(ms) {
  const s = Math.round((ms - Date.now()) / 1000);
  if (s <= 0) return "";
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  const left = d ? d + "d " + h + "h" : h ? h + "h " + m + "m" : m + "m";
  const clock = new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  return "resets " + left + " · " + clock;
}

// The same glyphs as the TUI: filled, half, track, and a tick at the switch line.
function bar(pct, line) {
  const box = el("span", null, "bar");
  if (pct == null) { box.append(el("span", "─".repeat(BAR_WIDTH), "track")); return box; }
  const cells = Math.min(Math.max(pct, 0), 100) / 100 * BAR_WIDTH;
  const full = Math.floor(cells), half = cells - full >= 0.5 && full < BAR_WIDTH;
  const tickAt = line == null ? -1 : Math.min(BAR_WIDTH - 1, Math.max(0, Math.round(line / 100 * BAR_WIDTH)));
  for (let i = 0; i < BAR_WIDTH; i++) {
    if (i === tickAt) box.append(el("span", "┃", "tick"));
    else if (i < full) box.append(el("span", "━", sev(pct)));
    else if (i === full && half) box.append(el("span", "╸", sev(pct)));
    else box.append(el("span", "─", "track"));
  }
  return box;
}

function usageRow(label, pct, suffix, line, dim, unknown) {
  const row = el("div", null, "row" + (dim ? " dim" : ""));
  row.append(el("span", label, "label"), bar(pct, line));
  row.append(pct == null ? el("span", unknown || "usage unknown", "muted")
                         : el("span", pct.toFixed(0) + "%", "pct " + sev(pct)));
  if (suffix) row.append(el("span", suffix, "muted"));
  return row;
}

function button(text, onclick, opts) {
  const o = opts || {};
  const b = el("button", text, "btn" + (o.primary ? " primary" : "") + (o.danger ? " danger" : ""));
  b.type = "button";
  b.disabled = !!o.disabled;
  if (o.title) b.title = o.title;
  b.onclick = onclick;
  return b;
}

// -- the page's own dialog (the TUI's modal): one box, a few fields, ok / cancel ----

const dlg = $("dlg");

function ask(opts) {
  return new Promise((resolve) => {
    $("dlg-title").textContent = opts.title;
    $("dlg-text").textContent = opts.text || "";
    const box = $("dlg-fields");
    box.replaceChildren();
    const inputs = {};
    (opts.fields || []).forEach((f) => {
      const wrap = el("label", null, "field" + (f.type === "checkbox" ? " check" : ""));
      let input;
      if (f.type === "select") {
        input = el("select");
        f.options.forEach((o) => { const op = el("option", o); op.value = o; input.append(op); });
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
      if (f.help) wrap.append(el("small", f.help));
      box.append(wrap);
      inputs[f.name] = input;
    });
    const ok = $("dlg-ok");
    ok.textContent = opts.ok || "ok";
    ok.className = "btn " + (opts.danger ? "danger" : "primary");
    const submit = () => {
      const out = {};
      Object.keys(inputs).forEach((k) => { out[k] = inputs[k].type === "checkbox" ? inputs[k].checked : inputs[k].value; });
      dlg.close("ok");
      resolve(out);
    };
    ok.onclick = submit;
    box.onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); submit(); } };
    $("dlg-cancel").onclick = () => dlg.close("cancel");
    dlg.onclose = () => { if (dlg.returnValue !== "ok") resolve(null); };  // cancel or Escape
    dlg.returnValue = "";
    dlg.showModal();
    const first = box.querySelector("input, select");
    if (first) first.focus();
  });
}

// -- doing things ----------------------------------------------------------------------

function describe(r) {
  if (r && r.message) return String(r.message);
  if (r && r.switched) return "Switched. Running Claude Code picks it up within about 30s.";
  return r && r.reason ? "Stayed on the current account: " + r.reason : "Nothing to do.";
}

async function act(path, body) {
  if (busy) return;
  busy = true;
  notice("working…");
  try {
    notice(describe(await api(path, body)));
  } catch (e) {
    notice("Error: " + e.message, true);
  }
  busy = false;
  lastSig = "";
  refresh();
}

async function confirmThen(opts, path, body) {
  if (await ask(opts)) act(path, body);
}

// -- Claude --------------------------------------------------------------------------------

function header(num, alias, email, tag, active) {
  const head = el("div", null, "head");
  head.append(el("span", String(num).padStart(2), "num"));
  if (alias) {
    head.append(el("span", alias, "alias"), document.createTextNode(email ? " (" + email + ")" : ""));
  } else {
    head.append(document.createTextNode(email || ""));
  }
  if (tag) head.append(el("span", "[" + tag + "]", "badge"));
  if (active) head.append(el("span", "● active", "on"));
  return head;
}

async function editAlias(a) {
  const v = await ask({
    title: "Alias for account " + a.number, text: a.email, ok: "save",
    fields: [{ name: "alias", label: "Short name (blank removes it)", value: a.alias || "" }],
  });
  if (v) act("/api/alias", { number: String(a.number), alias: v.alias });
}

async function editThreshold(a) {
  const v = await ask({
    title: "Switch-away line for account " + a.number, text: a.email, ok: "save",
    fields: [{
      name: "pct", label: "Percent used (50 to 99.9; blank = the global line)", type: "number", step: "0.5",
      value: typeof a.threshold === "number" ? a.threshold : "",
    }],
  });
  if (!v) return;
  const raw = v.pct.trim();
  const value = raw === "" ? null : Number(raw);
  if (value !== null && Number.isNaN(value)) { notice("Error: that is not a number", true); return; }
  act("/api/threshold", { number: String(a.number), threshold: value });
}

function claudeCard(a) {
  const card = el("div", null, "acct" + (a.active ? " active" : ""));
  const head = header(a.number, a.alias, a.email, a.isOrganization ? (a.organizationName || "org") : "personal", a.active);
  if (a.disabled) head.append(el("span", "(disabled)", "badge"));
  if (typeof a.threshold === "number") head.append(el("span", "th " + a.threshold + "%", "badge"));
  if (a.plan && a.plan.label) head.append(el("span", "· " + a.plan.label, "badge"));
  if (a.usageAgeSeconds != null && a.usageAgeSeconds > STALE_S) head.append(el("span", age(a.usageAgeSeconds), "badge"));
  card.append(head);

  const line = auto && auto.settings ? auto.settings.threshold : (settings ? settings.autoswitch.threshold : null);
  const weekly = (auto && auto.settings && auto.settings.weeklyThreshold) || line;
  const u = a.usage;
  const dim = a.usageAgeSeconds != null && a.usageAgeSeconds > STALE_S;
  if (a.usageStatus !== "ok" || !u) {
    const status = a.usageStatus === "ok" ? "unavailable" : a.usageStatus;
    const text = (a.usageStatus === "api_key" ? "· " : "⚠ ") + (STATUS[status] || status);
    card.append(el("div", text, "note " + (a.usageStatus === "api_key" ? "muted" : "warn")));
  } else {
    const win = (w) => (w.resetsAt ? resetText(Date.parse(w.resetsAt)) : "");
    const pace = (w) => (w.aheadOfPace ? "  (ahead of pace)" : "");
    if (u.spend) card.append(usageRow("$$", u.spend.pct, win(u.spend) + "  $" + u.spend.used.toFixed(2) + " / $" + u.spend.limit.toFixed(2), line, dim));
    if (u.fiveHour) card.append(usageRow("5h", u.fiveHour.pct, win(u.fiveHour), line, dim));
    if (u.sevenDay) card.append(usageRow("7d", u.sevenDay.pct, win(u.sevenDay) + pace(u.sevenDay), weekly, dim));
    (u.scoped || []).forEach((w) => card.append(usageRow(w.name, w.pct, win(w) + (w.pct >= 100 ? "  (!)" : pace(w)), weekly, dim)));
  }
  if (writable()) {
    const n = String(a.number), lock = loginOpen();
    const acts = el("div", null, "acts");
    acts.append(
      button("switch", () => act("/api/switch", { number: n }), { primary: true, disabled: a.active || lock }),
      button(a.disabled ? "enable" : "disable", () => act("/api/disable", { number: n, disabled: !a.disabled }), { disabled: lock }),
      button("alias", () => editAlias(a), { disabled: lock }),
      button("threshold", () => editThreshold(a), { disabled: lock }),
      button("remove", () => confirmThen({
        title: "Remove account " + n, ok: "remove", danger: true,
        text: "Remove " + a.email + "? Its stored credentials and config backup are deleted.",
      }, "/api/remove", { number: n }), { danger: true, disabled: lock }),
    );
    card.append(acts);
  }
  return card;
}

// The login the CLI is using right now, when cswap has not saved it.
function unsavedCard(u, kind) {
  const card = el("div", null, "acct");
  const head = header("·", null, u.email, "not saved", false);
  if (u.plan) head.append(el("span", "· " + u.plan, "badge"));
  card.append(head);
  card.append(el("div", "· logged in now, but not saved in cswap yet", "note muted"));
  if (writable()) {
    const acts = el("div", null, "acts");
    acts.append(button("save", () => act(kind === "codex" ? "/api/codex/add-current" : "/api/add-current", {}),
                       { primary: true, disabled: loginOpen() }));
    card.append(acts);
  } else {
    const cmd = kind === "codex" ? "cswap codex add" : "cswap add";
    card.append(el("div", "save it with: " + cmd, "note muted"));
  }
  return card;
}

function claudeTools() {
  const box = $("claude-tools");
  box.replaceChildren();
  box.hidden = !writable();
  if (!writable()) return;
  const lock = loginOpen(), unsaved = !!(claude && claude.unsaved);
  box.append(
    button("rotate", () => act("/api/rotate", { strategy: "next" }), { disabled: lock, title: "switch to the next account" }),
    button("best", () => act("/api/rotate", { strategy: "best" }), { disabled: lock, title: "the account with the most quota left" }),
    button("next available", () => act("/api/rotate", { strategy: "next-available" }), { disabled: lock, title: "next account that is not at its limit" }),
    button("save current login", async () => {
      if (await ask({
        title: "Save current login", ok: "save",
        text: "Back up the current Claude Code login as a managed account? If it is already managed, its stored credentials are refreshed in place.",
      })) act("/api/add-current", {});
    }, { primary: unsaved, disabled: lock }),
    button("add account…", async () => {
      const v = await ask({
        title: "Add an account", ok: "sign in",
        text: "Opens Claude Code's sign-in for this address in your browser. Your current login is replaced only once you finish signing in, so it must already be saved.",
        fields: [{ name: "email", label: "Email of the account to add", placeholder: "you@example.com" }],
      });
      if (v) act("/api/login", { kind: "claude", email: v.email.trim() });
    }, { disabled: lock || unsaved, title: unsaved ? "save the current login first" : "" }),
    button("add token…", async () => {
      const v = await ask({
        title: "Add from a token or API key", ok: "add",
        text: "A setup-token (from claude setup-token) or a Console API key. It is stored like any account and never shown again.",
        fields: [
          { name: "token", label: "Token or key", type: "password" },
          { name: "email", label: "Label (optional)", placeholder: "defaults to setup-token-N@token.local" },
        ],
      });
      if (!v) return;
      const body = { token: v.token.trim() };
      if (v.email.trim()) body.email = v.email.trim();
      act("/api/add-token", body);
    }, { disabled: lock }),
  );
}

// -- auto-switch -------------------------------------------------------------------------------

async function editAutoSettings() {
  const s = auto.settings;
  const v = await ask({
    title: "Auto-switch settings", ok: "save",
    text: "Takes effect at once; a running auto-switch restarts with the new values.",
    fields: [
      { name: "threshold", label: "Switch away at (percent used)", type: "number", step: "0.5", value: s.threshold },
      { name: "weekly", label: "Weekly line (blank = same as above)", type: "number", step: "0.5", value: s.weeklyThreshold == null ? "" : s.weeklyThreshold },
      { name: "strategy", label: "Strategy", type: "select", options: STRATEGIES, value: s.strategy },
      { name: "keepalive", label: "Keepalive: one tiny real request per idle account (uses a little quota)", type: "checkbox", value: s.keepalive },
    ],
  });
  if (!v) return;
  const threshold = Number(v.threshold), weekly = v.weekly.trim() === "" ? null : Number(v.weekly);
  if (Number.isNaN(threshold) || Number.isNaN(weekly)) { notice("Error: a line must be a number", true); return; }
  const changes = [];
  if (threshold !== s.threshold) changes.push(["autoswitch.threshold", threshold]);
  if (weekly !== s.weeklyThreshold) changes.push(["autoswitch.weeklyThreshold", weekly]);
  if (v.strategy !== s.strategy) changes.push(["autoswitch.strategy", v.strategy]);
  if (v.keepalive !== s.keepalive) changes.push(["autoswitch.keepalive", v.keepalive]);
  if (!changes.length) return;
  busy = true;
  try {
    for (const [key, value] of changes) await api("/api/config", { key, value });
    notice("Saved." + (auto.running ? " Auto-switch restarted with the new values." : ""));
  } catch (e) {
    notice("Error: " + e.message, true);
  }
  busy = false;
  lastSig = "";
  refresh();
}

function renderAuto() {
  if (!auto) return;
  $("auto-panel").hidden = false;
  const state = $("auto-state");
  state.replaceChildren(auto.running
    ? el("span", "● running" + (auto.dryRun ? " (dry run: it only reports)" : ""), "on")
    : el("span", "stopped", "muted"));
  const tools = $("auto-tools");
  tools.replaceChildren();
  if (writable()) {
    if (auto.running) {
      tools.append(button("stop", () => act("/api/auto", { action: "stop" }), { danger: true }));
    } else {
      tools.append(
        button("start", () => act("/api/auto", { action: "start" }), { primary: true }),
        button("start (dry run)", () => act("/api/auto", { action: "start", dryRun: true }), { title: "decide and report, never switch" }),
      );
    }
    tools.append(button("settings", editAutoSettings));
  }
  const s = auto.settings;
  $("auto-summary").textContent = "threshold " + s.threshold + "%  ·  weekly " +
    (s.weeklyThreshold == null ? "same" : s.weeklyThreshold + "%") + "  ·  strategy " + s.strategy +
    "  ·  keepalive " + (s.keepalive ? "on" : "off") + "  ·  every " + Math.round(s.intervalSeconds) + "s";
  const log = $("auto-log");
  log.replaceChildren();
  const events = auto.events.filter((e) => e.kind !== "poll").slice(-8);
  if (!events.length) log.append(el("div", auto.running ? "watching… nothing to report yet" : "no events", "muted"));
  events.forEach((e) => {
    const row = el("div");
    const cls = e.kind === "switch" ? "on" : e.kind === "error" || e.kind === "account-quarantined" ? "warn" : "muted";
    row.append(el("span", e.ts, "t"), el("span", e.text, cls));
    log.append(row);
  });
}

// -- Codex ----------------------------------------------------------------------------------------

function codexCard(a) {
  const card = el("div", null, "acct" + (a.active ? " active" : ""));
  card.append(header(a.slot, a.alias, a.email, a.plan, a.active));
  const u = a.usage, now = Date.now();
  if (a.authMode === "apikey") {
    card.append(el("div", "· API key (no quota)", "note muted"));
  } else if (!u) {
    card.append(el("div", "no usage seen yet (it appears after you use Codex)", "note muted"));
  } else {
    // A window that has reset since the reading is unknown (use elsewhere is not in
    // the local files), not 0%.
    const win = (w, label) => {
      if (!w) return;
      const resetMs = w.resets_at * 1000;
      if (resetMs <= now) card.append(usageRow(label, null, "", null, true, "reset since last seen"));
      else card.append(usageRow(label, w.used_percent, resetText(resetMs), null, !u.live));
    };
    win(u.primary, "5h");
    win(u.secondary, "7d");
    const seen = Date.parse(u.seenAt);
    if (!isNaN(seen)) {
      const note = u.live ? "└ read from Codex " + age((now - seen) / 1000)
                          : "└ last seen " + age((now - seen) / 1000) + " · this machine only";
      card.append(el("div", note, "note muted"));
    }
  }
  if (writable()) {
    const n = String(a.slot), lock = loginOpen();
    const acts = el("div", null, "acts");
    acts.append(
      button("switch", () => act("/api/codex/switch", { number: n }), { primary: true, disabled: a.active || lock }),
      button("remove", () => confirmThen({
        title: "Remove Codex slot " + n, ok: "remove", danger: true,
        text: "Remove " + (a.email || "this API-key login") + " from cswap? Its stored copy is deleted; Codex itself is not touched.",
      }, "/api/codex/remove", { number: n }), { danger: true, disabled: lock }),
    );
    card.append(acts);
  }
  return card;
}

function codexTools() {
  const box = $("codex-tools");
  box.replaceChildren();
  box.hidden = !writable();
  if (!writable()) return;
  const lock = loginOpen(), unsaved = !!(codex && codex.unsaved);
  box.append(
    button("rotate", () => act("/api/codex/rotate", { strategy: "next" }), { disabled: lock }),
    button("best", () => act("/api/codex/rotate", { strategy: "best" }), { disabled: lock, title: "the account with the most quota left" }),
    button("save current login", () => act("/api/codex/add-current", {}), { primary: unsaved, disabled: lock }),
    button("add account…", () => confirmThen({
      title: "Add a Codex account", ok: "sign in",
      text: "Opens Codex's sign-in in your browser. Your current Codex login is replaced only once you finish, so it must already be saved. Running Codex sessions keep their account until restarted.",
    }, "/api/login", { kind: "codex" }), { disabled: lock || unsaved, title: unsaved ? "save the current login first" : "" }),
  );
}

// -- the page ------------------------------------------------------------------------------------------

function trackJob(job) {
  const key = job ? job.kind + "|" + job.state + "|" + job.message : "";
  $("job").textContent = job && job.state === "running" ? "● " + job.message : "";
  if (job && job.state !== "running" && key !== lastJobKey) notice(job.message, job.state === "error");
  lastJobKey = key;
}

function render() {
  if (claude) {
    const cards = claude.accounts.map(claudeCard);
    if (claude.unsaved) cards.unshift(unsavedCard(claude.unsaved, "claude"));
    const main = $("accounts");
    main.replaceChildren(...cards);
    if (!cards.length) main.append(el("p", "No accounts yet. Run cswap onboard in a terminal.", "muted"));
    claudeTools();
    trackJob(claude.job);
  }
  renderAuto();
  if (codex && (codex.accounts.length || codex.unsaved || codex.error || (writable() && codex.installed))) {
    $("codex").hidden = false;
    $("codex-note").textContent = codex.error || "";
    const cards = codex.accounts.map(codexCard);
    if (codex.unsaved) cards.unshift(unsavedCard(codex.unsaved, "codex"));
    $("codex-accounts").replaceChildren(...cards);
    codexTools();
  }
}

function showConnection(ok, e) {
  const c = $("conn");
  if (ok) { c.textContent = ""; return; }
  c.textContent = e && (e.status === 401 || e.status === 403)
    ? "This page is from an earlier run. Open the link that `aswap gui` printed, or run it again."
    : "The dashboard stopped or is restarting. Run `aswap gui` again; this page keeps trying.";
}

async function refresh() {
  try {
    claude = await api("/api/accounts");
    codex = await api("/api/codex");
    auto = await api("/api/auto");
  } catch (e) {
    showConnection(false, e);
    return;
  }
  showConnection(true);
  $("updated").textContent = "updated " + new Date().toLocaleTimeString([], { hour12: false });
  // Skip identical data: a rebuild mid-click would swallow the click. Reset countdowns
  // still move, so a minute-old picture is redrawn anyway.
  const sig = JSON.stringify([claude, codex, auto, Math.floor(Date.now() / 30000)]);
  if (sig === lastSig) return;
  lastSig = sig;
  render();
}

async function loop() {
  if (!document.hidden) await refresh();
  setTimeout(loop, ((settings && settings.pollSeconds) || 3) * 1000);
}

async function start() {
  if (!token) { $("conn").textContent = "Open the link that `aswap gui` printed."; return; }
  try {
    settings = await api("/api/settings");
  } catch (e) {
    showConnection(false, e);
    setTimeout(start, 3000);  // the server may just be starting
    return;
  }
  $("mode").textContent = writable() ? "· can change accounts" : "· read-only";
  loop();
}

start();
