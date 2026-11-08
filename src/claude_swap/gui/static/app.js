"use strict";
// Same layout and wording as the terminal dashboard (aswap tui), and the same way of working:
// a cursor row, keys, a log of what happened. Every value is placed with textContent: emails,
// aliases and org names come from outside this page and must never be parsed as markup.
// Nothing here builds a command: it only sends plain values to the API.

// Wording matches SENTINEL_NOTES in switcher.py.
const STATUS = {
  token_expired: "token expired — refreshed when needed (aswap list refreshes it now)",
  api_key: "API key (no quota)",
  keychain_unavailable: "keychain unavailable — locked or in use; try again",
  relogin_required: "re-login needed — refresh token dead; sign in again with add account",
  foreign_credential: "live credential belongs to another account — a switch repairs it",
  no_credentials: "no stored credentials",
  unavailable: "usage unavailable",
};
const PROBLEM = {
  revoked: "OpenAI says this login was revoked. Sign in to it again (add account).",
  unauthorized: "OpenAI rejected this login. Sign in to it again (add account).",
  failed: "Could not read this account's usage from Codex just now.",
};
const WARN_PCT = 70, CRIT_PCT = 90, BAR_WIDTH = 24, STALE_S = 300, FETCH_MS = 15000;
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
let settings = null, busy = false, lastSig = "", lastJobKey = null, seq = 0, holdUntil = 0, lastGood = null;
const state = { claude: null, codex: null, autoState: { claude: null, codex: null } };
const sel = { claude: null, codex: null };   // the selected account id per panel
let focus = "claude";                        // the panel the keys act on
const entries = [];                          // the activity log

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
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), FETCH_MS);
  opts.signal = ctl.signal;
  let res;
  try {
    res = await fetch(path, opts);  // a stopped server throws here, before any status
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
const sev = (pct) => (pct >= CRIT_PCT ? "crit" : pct >= WARN_PCT ? "warn" : "ok");
const clock = () => new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });

// -- the activity log: what happened stays on screen (a one-line notice used to vanish) ------

function say(text, kind) {
  entries.push({ ts: clock(), text: String(text), kind: kind || "info" });
  if (entries.length > 6) entries.shift();
  renderLog();
}

function renderLog() {
  const box = $("log");
  box.replaceChildren();
  if (!entries.length) box.append(el("div", "nothing yet", "muted"));
  entries.forEach((e) => {
    const row = el("div");
    const mark = e.kind === "bad" ? "✗ " : e.kind === "good" ? "✓ " : "· ";
    row.append(el("span", e.ts, "t"), el("span", mark + e.text, e.kind === "bad" ? "warn" : e.kind === "good" ? "ok" : "muted"));
    box.append(row);
  });
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
  const clockText = new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  return "resets " + left + " · " + clockText;
}

// A window is named by its length, not assumed to be five hours: Codex's "go" plan has a
// month-long primary window.
function winLabel(minutes, fallback) {
  if (!minutes) return fallback;
  if (minutes === 300) return "5h";
  if (minutes === 10080) return "7d";
  const h = minutes / 60;
  return h < 48 ? Math.round(h) + "h" : Math.round(h / 24) + "d";
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

// A command is text; only a primary action in a dialog or the selected account is boxed.
function cmd(text, onclick, o) {
  const opts = o || {};
  const b = el("button", text, (opts.boxed ? "btn" : "link") + (opts.primary ? " primary" : "") + (opts.danger ? " danger" : ""));
  b.type = "button";
  b.disabled = !!opts.disabled;
  if (opts.title) b.title = opts.title;
  b.onclick = (ev) => { ev.stopPropagation(); onclick(ev); };
  return b;
}

function line(label, items) {
  const row = el("div", null, "cmd");
  row.append(el("span", label, "k"));
  items.forEach((it, i) => {
    if (i) row.append(el("span", "·", "sep"));
    row.append(it);
  });
  return row;
}

// -- the page's own dialog (the TUI's modal): one box, a few fields, ok / cancel ----------------

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
    dlg.onclose = () => { lastSig = ""; if (dlg.returnValue !== "ok") resolve(null); };  // cancel or Escape
    dlg.returnValue = "";
    dlg.showModal();
    const first = box.querySelector("input, select");
    (first || ok).focus();
  });
}

// -- doing things ----------------------------------------------------------------------------------

function describe(r) {
  if (r && r.message) return String(r.message);
  if (r && r.switched) return "Switched.";
  return r && r.reason ? "Stayed on the current account: " + r.reason : "Nothing to do.";
}

// What a switch means for a Codex that is already running: say it, do not leave it as a surprise.
function codexHint(path) {
  if (!/^\/api\/codex\/(switch|rotate)$/.test(path)) return "";
  const c = state.codex;
  if (!c || !c.running) return "";
  return c.supervised
    ? " " + c.supervised + " session(s) started with `aswap codex run` are reopening on the new login."
    : " A Codex already running outside `aswap codex run` keeps its account until restarted.";
}

function working(on) {
  $("working").textContent = on ? "working…" : "";
  document.body.classList.toggle("busy", on);
}

async function act(path, body, label) {
  if (busy) return;
  busy = true;
  working(true);
  try {
    const r = await api(path, body);
    const codexSwitch = /^\/api\/codex\/(switch|rotate)$/.test(path);
    // For a Codex switch the hint below says what matters; the server's generic line would repeat it.
    const text = codexSwitch ? (r && r.switched ? "Switched." : "Already on that account.") : describe(r);
    say((label ? label + ": " : "") + text + codexHint(path), r && r.switched === false && !r.message ? "info" : "good");
  } catch (e) {
    say((label ? label + ": " : "") + e.message, "bad");
  }
  busy = false;
  working(false);
  lastSig = "";
  refresh();
}

async function confirmThen(opts, path, body, label) {
  if (await ask(opts)) act(path, body, label);
}

// -- one panel, built twice -------------------------------------------------------------------------
//
// Claude and Codex get the same parts in the same order from the same code, so they cannot drift
// apart: the commands, the accounts (one selected, with its actions), then auto-switch.

const PROVIDERS = {
  claude: {
    title: "claude code", base: "/api/claude", noun: "Claude",
    tokenLabel: "token", tokenTitle: "Add from a token or API key",
    tokenText: "A setup-token (from claude setup-token) or a Console API key. It is stored like any account and never shown again.",
    tokenFields: [
      { name: "token", label: "Token or key", type: "password" },
      { name: "email", label: "Label (optional)", placeholder: "defaults to setup-token-N@token.local" },
    ],
  },
  codex: {
    title: "codex", base: "/api/codex", noun: "Codex",
    tokenLabel: "API key", tokenTitle: "Add an OpenAI API key",
    tokenText: "The key is handed to `codex login --with-api-key` on its standard input, never on a command line, and stored like any account.",
    tokenFields: [{ name: "token", label: "OpenAI API key", type: "password" }],
  },
};

function header(num, alias, email, tag, active) {
  const head = el("div", null, "head");
  head.append(el("span", (active ? "▸" : " ") + String(num).padStart(2), "num"));
  if (alias) {
    head.append(el("span", alias, "alias"), document.createTextNode(email ? " (" + email + ")" : ""));
  } else {
    head.append(document.createTextNode(email || ""));
  }
  if (tag) head.append(el("span", "[" + tag + "]", "badge"));
  if (active) head.append(el("span", "● active", "on badge"));
  return head;
}

function normalize(p, a) {
  if (p === "claude") {
    // "x@y's Organization" is just the personal workspace of x@y: not worth repeating.
    const org = a.organizationName || "";
    const tag = !a.isOrganization ? "personal" : (org.endsWith("'s Organization") ? "personal" : org || "org");
    return {
      id: String(a.number), alias: a.alias, email: a.email, active: a.active, disabled: a.disabled,
      threshold: typeof a.threshold === "number" ? a.threshold : null, raw: a, tag,
      extra: a.plan && a.plan.label ? a.plan.label : null,
    };
  }
  return {
    id: String(a.slot), alias: a.alias, email: a.email, active: a.active, disabled: a.disabled,
    threshold: typeof a.threshold === "number" ? a.threshold : null, raw: a, tag: a.plan, extra: null,
  };
}

function globalLine(p) {
  const a = state.autoState[p];
  return a && a.settings ? a.settings.threshold : null;
}

function usageBody(p, card, n) {
  const a = n.raw, lineAt = n.threshold != null ? n.threshold : globalLine(p);
  if (p === "claude") {
    const weekly = (state.autoState.claude && state.autoState.claude.settings.weeklyThreshold) || lineAt;
    const u = a.usage, dim = a.usageAgeSeconds != null && a.usageAgeSeconds > STALE_S;
    if (a.usageStatus !== "ok" || !u) {
      const status = a.usageStatus === "ok" ? "unavailable" : a.usageStatus;
      const text = (a.usageStatus === "api_key" ? "· " : "⚠ ") + (STATUS[status] || status);
      card.append(el("div", text, "note " + (a.usageStatus === "api_key" ? "muted" : "warn")));
      return;
    }
    const win = (w) => (w.resetsAt ? resetText(Date.parse(w.resetsAt)) : "");
    const pace = (w) => (w.aheadOfPace ? "  (ahead of pace)" : "");
    if (u.spend) card.append(usageRow("$$", u.spend.pct, win(u.spend) + "  $" + u.spend.used.toFixed(2) + " / $" + u.spend.limit.toFixed(2), lineAt, dim));
    if (u.fiveHour) card.append(usageRow("5h", u.fiveHour.pct, win(u.fiveHour), lineAt, dim));
    if (u.sevenDay) card.append(usageRow("7d", u.sevenDay.pct, win(u.sevenDay) + pace(u.sevenDay), weekly, dim));
    (u.scoped || []).forEach((w) => card.append(usageRow(w.name, w.pct, win(w) + (w.pct >= 100 ? "  (!)" : pace(w)), weekly, dim)));
    return;
  }
  if (a.problem) card.append(el("div", "⚠ " + (PROBLEM[a.problem] || PROBLEM.failed), "note warn"));
  const u = a.usage, now = Date.now();
  if (a.authMode === "apikey") { card.append(el("div", "· API key (no quota)", "note muted")); return; }
  if (!u) { if (!a.problem) card.append(el("div", "no usage seen yet (it appears after you use Codex)", "note muted")); return; }
  // A window that has reset since the reading is unknown (use elsewhere is not in the local
  // files), not 0%.
  const win = (w, fallback) => {
    if (!w) return;
    const label = winLabel(w.window_minutes, fallback), resetMs = w.resets_at * 1000;
    if (resetMs <= now) card.append(usageRow(label, null, "", null, true, "reset since last seen"));
    else card.append(usageRow(label, w.used_percent, resetText(resetMs), lineAt, !u.live || !!a.problem));
  };
  win(u.primary, "5h");
  win(u.secondary, "7d");
  const seen = Date.parse(u.seenAt);
  if (!isNaN(seen)) {
    card.append(el("div", u.live ? "└ read from Codex " + age((now - seen) / 1000)
                                 : "└ last seen " + age((now - seen) / 1000) + " · this machine only", "note muted"));
  }
}

async function editAlias(p, n) {
  const v = await ask({
    title: "Alias for " + PROVIDERS[p].noun + " account " + n.id, text: n.email, ok: "save",
    fields: [{ name: "alias", label: "Short name (blank removes it)", value: n.alias || "" }],
  });
  if (v) act(PROVIDERS[p].base + "/alias", { number: n.id, alias: v.alias }, "alias");
}

async function editThreshold(p, n) {
  const v = await ask({
    title: "Switch-away line for " + PROVIDERS[p].noun + " account " + n.id, text: n.email, ok: "save",
    fields: [{ name: "pct", label: "Percent used (50 to 99.9; blank = the global line)", type: "number", step: "0.5", value: n.threshold == null ? "" : n.threshold }],
  });
  if (!v) return;
  const raw = v.pct.trim(), value = raw === "" ? null : Number(raw);
  if (value !== null && Number.isNaN(value)) { say("that is not a number", "bad"); return; }
  act(PROVIDERS[p].base + "/threshold", { number: n.id, threshold: value }, "threshold");
}

const doSwitch = (p, n) => act(PROVIDERS[p].base + "/switch", { number: n.id }, "switch to " + (n.alias || n.email || n.id));
const doDisable = (p, n) => act(PROVIDERS[p].base + "/disable", { number: n.id, disabled: !n.disabled }, n.disabled ? "enable" : "disable");
const doRemove = (p, n) => confirmThen({
  title: "Remove " + PROVIDERS[p].noun + " account " + n.id, ok: "remove", danger: true,
  text: "Remove " + (n.email || "this API-key login") + "? Its stored copy is deleted"
    + (p === "codex" ? "; Codex itself is not touched." : ", with its config backup."),
}, PROVIDERS[p].base + "/remove", { number: n.id }, "remove");

function accountCard(p, a) {
  const n = normalize(p, a);
  const card = el("div", null, "acct" + (n.active ? " active" : "") + (sel[p] === n.id ? " sel" : ""));
  card.dataset.key = p + ":" + n.id;
  card.tabIndex = 0;
  card.setAttribute("role", "option");
  card.setAttribute("aria-selected", sel[p] === n.id ? "true" : "false");
  const head = header(n.id, n.alias, n.email, n.tag, n.active);
  if (n.disabled) head.append(el("span", "(disabled)", "badge"));
  if (n.threshold != null) head.append(el("span", "th " + n.threshold + "%", "badge"));
  if (n.extra) head.append(el("span", "· " + n.extra, "badge"));
  if (p === "claude" && a.usageAgeSeconds != null && a.usageAgeSeconds > STALE_S) head.append(el("span", age(a.usageAgeSeconds), "badge"));
  card.append(head);
  usageBody(p, card, n);
  if (writable() && sel[p] === n.id) {
    const lock = loginOpen(), acts = el("div", null, "acts");
    acts.append(
      cmd("switch ⏎", () => doSwitch(p, n), { boxed: true, primary: true, disabled: n.active || lock }),
      cmd(n.disabled ? "enable" : "disable", () => doDisable(p, n), { disabled: lock }),
      cmd("alias", () => editAlias(p, n), { disabled: lock }),
      cmd("threshold", () => editThreshold(p, n), { disabled: lock }),
      cmd("remove", () => doRemove(p, n), { danger: true, disabled: lock }),
    );
    card.append(acts);
  }
  card.onclick = () => { sel[p] = n.id; focus = p; lastSig = ""; render(); };
  return card;
}

// The login the CLI is using right now, when agent-swap has not saved it.
function unsavedCard(p, u) {
  const card = el("div", null, "acct sel");
  const head = header("·", null, u.email, "not saved", false);
  if (u.plan) head.append(el("span", "· " + u.plan, "badge"));
  card.append(head);
  card.append(el("div", "· logged in now, but not saved in agent-swap yet", "note muted"));
  if (writable()) {
    const acts = el("div", null, "acts");
    acts.append(cmd("save", () => act(PROVIDERS[p].base + "/save", {}, "save login"), { boxed: true, primary: true, disabled: loginOpen() }));
    card.append(acts);
  } else {
    card.append(el("div", "save it with: " + (p === "codex" ? "aswap codex add" : "aswap add"), "note muted"));
  }
  return card;
}

// -- the commands of a panel (also bound to keys) ---------------------------------------------------

const rotate = (p, strategy) => act(PROVIDERS[p].base + "/rotate", { strategy }, strategy === "next" ? "rotate" : strategy);

async function addAccount(p) {
  const cfg = PROVIDERS[p];
  if (p === "claude") {
    const v = await ask({
      title: "Add a Claude account", ok: "open sign-in",
      text: "Opens Claude Code's sign-in in your browser. The login you are using now is saved first if it is not saved yet, so nothing is lost.",
      fields: [{ name: "email", label: "Email to pre-fill (optional)", placeholder: "you@example.com" }],
    });
    if (v) act(cfg.base + "/login", v.email.trim() ? { email: v.email.trim() } : {}, "add account");
  } else if (await ask({
    title: "Add a Codex account", ok: "open sign-in",
    text: "Opens Codex's sign-in in your browser. The login you are using now is saved first if it is not saved yet. Start Codex with `aswap codex run` and it follows a switch by itself.",
  })) act(cfg.base + "/login", {}, "add account");
}

async function codeLogin() {
  if (await ask({
    title: "Sign in to Codex with a code", ok: "start",
    text: "For a machine without a browser, or when the browser does not open: Codex prints a link and a code to enter there. They appear in the panel.",
  })) act(PROVIDERS.codex.base + "/login", { device: true }, "sign in by code");
}

async function saveLogin(p) {
  if (await ask({
    title: "Save current " + PROVIDERS[p].noun + " login", ok: "save",
    text: "Back up the " + PROVIDERS[p].noun + " login in use as a managed account. If it is already managed, its stored copy is refreshed in place.",
  })) act(PROVIDERS[p].base + "/save", {}, "save login");
}

async function addToken(p) {
  const cfg = PROVIDERS[p];
  const v = await ask({ title: cfg.tokenTitle, ok: "add", text: cfg.tokenText, fields: cfg.tokenFields });
  if (!v) return;
  const body = { token: v.token.trim() };
  if (v.email && v.email.trim()) body.email = v.email.trim();
  act(cfg.base + "/token", body, "add " + cfg.tokenLabel);
}

function commandRows(p) {
  const lock = loginOpen(), data = state[p], unsaved = !!(data && data.unsaved);
  const wrap = el("div", null, "cmds col");
  wrap.append(line("switch", [
    cmd("rotate", () => rotate(p, "next"), { disabled: lock, title: "the next account  (r)" }),
    cmd("best", () => rotate(p, "best"), { disabled: lock, title: "the account with the most quota left  (b)" }),
    cmd("next available", () => rotate(p, "next-available"), { disabled: lock, title: "the next account not at its limit  (n)" }),
  ]));
  const add = [cmd("account", () => addAccount(p), { disabled: lock, primary: true, title: "sign in to another account  (+)" })];
  if (p === "codex") add.push(cmd("by code", codeLogin, { disabled: lock, title: "for a machine without a browser" }));
  add.push(
    cmd(PROVIDERS[p].tokenLabel, () => addToken(p), { disabled: lock }),
    cmd("save login", () => saveLogin(p), { disabled: lock, primary: unsaved, title: "back up the login in use  (v)" }),
  );
  wrap.append(line("add", add));
  return wrap;
}

// -- auto-switch, per panel -----------------------------------------------------------------------------

async function editAutoSettings(p) {
  const a = state.autoState[p], s = a.settings;
  const fields = [{ name: "threshold", label: "Switch away at (percent used)", type: "number", step: "0.5", value: s.threshold }];
  if (p === "claude") {
    fields.push(
      { name: "weekly", label: "Weekly line (blank = same as above)", type: "number", step: "0.5", value: s.weeklyThreshold == null ? "" : s.weeklyThreshold },
      { name: "strategy", label: "Strategy", type: "select", options: STRATEGIES, value: s.strategy },
      { name: "keepalive", label: "Keepalive: one tiny real request per idle account (uses a little quota)", type: "checkbox", value: s.keepalive },
    );
  }
  const v = await ask({
    title: PROVIDERS[p].noun + " auto-switch settings", ok: "save",
    text: p === "codex" ? "The line is shared with Claude's. Takes effect at once; a running auto-switch restarts."
                        : "Takes effect at once; a running auto-switch restarts with the new values.",
    fields,
  });
  if (!v) return;
  const threshold = Number(v.threshold);
  const weekly = p === "claude" ? (v.weekly.trim() === "" ? null : Number(v.weekly)) : null;
  if (Number.isNaN(threshold) || Number.isNaN(weekly)) { say("a line must be a number", "bad"); return; }
  const changes = [];
  if (threshold !== s.threshold) changes.push(["autoswitch.threshold", threshold]);
  if (p === "claude") {
    if (weekly !== s.weeklyThreshold) changes.push(["autoswitch.weeklyThreshold", weekly]);
    if (v.strategy !== s.strategy) changes.push(["autoswitch.strategy", v.strategy]);
    if (v.keepalive !== s.keepalive) changes.push(["autoswitch.keepalive", v.keepalive]);
  }
  if (!changes.length) return;
  busy = true;
  working(true);
  try {
    for (const [key, value] of changes) await api(PROVIDERS[p].base + "/config", { key, value });
    say("auto-switch settings saved." + (a.running ? " It restarted with the new values." : ""), "good");
  } catch (e) {
    say(e.message, "bad");
  }
  busy = false;
  working(false);
  lastSig = "";
  refresh();
}

const toggleAuto = (p, dry) => {
  const a = state.autoState[p];
  if (!a) return;
  const path = PROVIDERS[p].base + "/auto";
  if (a.running) act(path, { action: "stop" }, "auto-switch");
  else act(path, dry ? { action: "start", dryRun: true } : { action: "start" }, "auto-switch");
};

function renderAuto(p, box) {
  const a = state.autoState[p];
  box.replaceChildren();
  if (!a) return;
  const top = el("div", null, "line");
  top.append(el("span", "auto-switch", "muted"), a.running
    ? el("span", "● running" + (a.dryRun ? " (dry run: it only reports)" : ""), "on")
    : el("span", "○ stopped", "muted"));
  if (writable()) {
    if (a.running) top.append(cmd("stop", () => toggleAuto(p), { danger: true }));
    else top.append(cmd("start", () => toggleAuto(p, false), { primary: true }), cmd("dry run", () => toggleAuto(p, true), { title: "decide and report, never switch" }));
    top.append(cmd("settings", () => editAutoSettings(p)));
  }
  box.append(top);
  const s = a.settings;
  box.append(el("div", p === "claude"
    ? "switch at " + s.threshold + "%  ·  weekly " + (s.weeklyThreshold == null ? "same" : s.weeklyThreshold + "%") +
      "  ·  " + s.strategy + "  ·  keepalive " + (s.keepalive ? "on" : "off") + "  ·  every " + Math.round(s.intervalSeconds) + "s"
    : "switch at " + s.threshold + "%  ·  every " + Math.round(s.intervalSeconds) + "s", "note muted"));
  const events = a.events.filter((e) => e.kind !== "poll").slice(-3);
  if (events.length) {
    const log = el("div", null, "log");
    events.forEach((e) => {
      const row = el("div"), cls = e.kind === "switch" ? "on" : e.kind === "error" || e.kind === "account-quarantined" ? "warn" : "muted";
      row.append(el("span", e.ts, "t"), el("span", e.text, cls));
      log.append(row);
    });
    box.append(log);
  }
}

// -- the panel and the page ------------------------------------------------------------------------------

function buildPanels() {
  const root = $("panels");
  ["claude", "codex"].forEach((p) => {
    const panel = el("section", null, "panel");
    panel.id = "panel-" + p;
    const h = el("h2");
    h.append(el("span", PROVIDERS[p].title), el("small", "", "muted"));
    panel.append(h);
    ["progress", "cmds", "accounts", "auto"].forEach((name) => {
      const d = el(name === "accounts" ? "main" : "div", null, name);
      d.dataset.part = name;
      if (name === "accounts") d.setAttribute("role", "listbox");
      panel.append(d);
    });
    panel.onclick = () => { if (focus !== p) { focus = p; lastSig = ""; render(); } };
    root.append(panel);
  });
}

const part = (p, name) => document.querySelector("#panel-" + p + " [data-part=" + name + "]");

// A link to the sign-in page, only ever an https one (the server already filters, this is the second check).
function isHttps(u) {
  try { return new URL(u).protocol === "https:"; } catch (e) { return false; }
}

function renderProgress(p) {
  const box = part(p, "progress"), job = state[p] && state[p].job;
  box.replaceChildren();
  box.className = "progress";
  if (!job || job.kind !== p || job.state !== "running") { box.hidden = true; return; }
  box.hidden = false;
  box.append(el("div", "● " + job.message));
  if (job.url && isHttps(job.url)) {
    const link = el("a", job.url);
    link.href = job.url; link.target = "_blank"; link.rel = "noopener noreferrer";
    const row = el("div", "If no window opened, go to ");
    row.append(link);
    box.append(row);
  }
  if (job.code) { const row = el("div", "Enter this code there: "); row.append(el("span", job.code, "code")); box.append(row); }
}

function accountsOf(p) {
  const data = state[p];
  return data ? data.accounts.map((a) => normalize(p, a)) : [];
}

function ensureSelection(p) {
  const rows = accountsOf(p);
  if (!rows.length) { sel[p] = null; return; }
  if (!rows.some((r) => r.id === sel[p])) sel[p] = (rows.find((r) => r.active) || rows[0]).id;
}

function renderPanel(p) {
  const data = state[p], cfg = PROVIDERS[p];
  if (!data) return;
  ensureSelection(p);
  document.getElementById("panel-" + p).classList.toggle("focus", focus === p);
  const note = document.querySelector("#panel-" + p + " h2 small");
  note.textContent = p === "codex" && data.supervised ? "▸ " + data.supervised + " session(s) follow a switch" : "";
  renderProgress(p);
  const cmds = part(p, "cmds");
  cmds.replaceChildren();
  cmds.hidden = !writable();
  if (writable()) cmds.append(commandRows(p));
  const cards = data.accounts.map((a) => accountCard(p, a));
  if (data.unsaved) cards.unshift(unsavedCard(p, data.unsaved));
  const main = part(p, "accounts");
  main.replaceChildren(...cards);
  if (data.error) main.prepend(el("p", data.error, "warn"));
  if (p === "codex" && data.installed === false && !data.accounts.length) {
    main.append(el("p", "Codex is not installed (`codex` was not found on PATH).", "muted empty"));
  } else if (!cards.length) {
    main.append(el("p", "No " + cfg.noun + " accounts yet." + (writable() ? " Use add → account." : ""), "muted empty"));
  }
  renderAuto(p, part(p, "auto"));
}

function renderKeys() {
  const k = $("keys");
  k.replaceChildren();
  const hint = (key, what) => { const s = el("span"); s.append(el("kbd", key), document.createTextNode(" " + what + "   ")); return s; };
  k.append(hint("↑↓", "select"), hint("←→", "panel"));
  if (writable()) {
    k.append(hint("⏎", "switch"), hint("d", "disable"), hint("a", "alias"), hint("t", "threshold"), hint("x", "remove"),
             hint("r", "rotate"), hint("b", "best"), hint("n", "next"), hint("+", "add"), hint("v", "save login"), hint("g", "auto"));
  }
}

function trackJob(job) {
  const key = job ? job.kind + "|" + job.state + "|" + job.message : "";
  if (job && job.state !== "running" && key !== lastJobKey) say(job.message, job.state === "error" ? "bad" : "good");
  lastJobKey = key;
}

function render() {
  if (dlg.open) return;                          // never rebuild under an open dialog
  if (Date.now() < holdUntil) { setTimeout(render, 450); return; }  // nor under a click in progress
  const keep = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.key : null;
  renderPanel("claude");
  renderPanel("codex");
  trackJob((state.claude && state.claude.job) || (state.codex && state.codex.job));
  if (keep) {
    const again = document.querySelector('[data-key="' + keep + '"]');
    if (again) again.focus({ preventScroll: true });
  }
}

// -- keys ---------------------------------------------------------------------------------------------------

function move(delta) {
  const rows = accountsOf(focus);
  if (!rows.length) return;
  const i = Math.max(0, rows.findIndex((r) => r.id === sel[focus]));
  sel[focus] = rows[(i + delta + rows.length) % rows.length].id;
  lastSig = "";
  render();
  const card = document.querySelector('[data-key="' + focus + ":" + sel[focus] + '"]');
  if (card) card.scrollIntoView({ block: "nearest" });
}

function selected() {
  return accountsOf(focus).find((r) => r.id === sel[focus]) || null;
}

document.addEventListener("pointerdown", () => { holdUntil = Date.now() + 400; });

document.addEventListener("keydown", (e) => {
  if (dlg.open || e.ctrlKey || e.metaKey || e.altKey) return;
  const t = e.target;
  if (t && /^(input|select|textarea)$/i.test(t.tagName)) return;
  const k = e.key, n = selected(), p = focus, can = writable() && !busy && !loginOpen();
  const go = (fn) => { e.preventDefault(); fn(); };
  if (k === "ArrowDown" || k === "j") return go(() => move(1));
  if (k === "ArrowUp" || k === "k") return go(() => move(-1));
  if (k === "ArrowLeft" || k === "h" || k === "ArrowRight" || k === "l") {
    return go(() => { focus = focus === "claude" ? "codex" : "claude"; ensureSelection(focus); lastSig = ""; render(); });
  }
  if (!can) return;
  if (k === "Enter" && n && (t === document.body || (t.dataset && t.dataset.key))) return go(() => { if (!n.active) doSwitch(p, n); });
  if (k === "d" && n) return go(() => doDisable(p, n));
  if (k === "a" && n) return go(() => editAlias(p, n));
  if (k === "t" && n) return go(() => editThreshold(p, n));
  if (k === "x" && n) return go(() => doRemove(p, n));
  if (k === "r") return go(() => rotate(p, "next"));
  if (k === "b") return go(() => rotate(p, "best"));
  if (k === "n") return go(() => rotate(p, "next-available"));
  if (k === "+") return go(() => addAccount(p));
  if (k === "v") return go(() => saveLogin(p));
  if (k === "g") return go(() => toggleAuto(p, false));
});

// -- data -------------------------------------------------------------------------------------------------------

function showConnection(ok, e) {
  const c = $("conn");
  document.body.classList.toggle("stale", !ok);
  if (ok) { c.textContent = ""; return; }
  c.textContent = e && (e.status === 401 || e.status === 403)
    ? "This page is from an earlier run. Open the link that `aswap gui` printed, or run it again."
    : "No answer from the dashboard (" + (e ? e.message : "unreachable") + "). What you see is the last data it sent. Run `aswap gui` again if it stopped; this page keeps trying.";
}

async function refresh() {
  const mine = ++seq;
  try {
    const [claude, codex, ac, ax] = await Promise.all([
      api("/api/claude"), api("/api/codex"), api("/api/claude/auto"), api("/api/codex/auto"),
    ]);
    if (mine !== seq) return;                    // a newer request already answered
    state.claude = claude; state.codex = codex; state.autoState = { claude: ac, codex: ax };
  } catch (e) {
    if (mine !== seq) return;
    showConnection(false, e);
    $("updated").textContent = lastGood ? "last data " + lastGood + " · retrying" : "";
    return;
  }
  showConnection(true);
  lastGood = clock();
  $("updated").textContent = "updated " + lastGood;
  // Skip identical data: a rebuild mid-click would swallow the click. Reset countdowns still
  // move, so a half-minute-old picture is redrawn anyway.
  const sig = JSON.stringify([state, focus, Math.floor(Date.now() / 30000)]);
  if (sig === lastSig) return;
  lastSig = sig;
  render();
}

async function loop() {
  if (!document.hidden) await refresh();
  setTimeout(loop, ((settings && settings.pollSeconds) || 3) * 1000);
}

document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
window.addEventListener("online", refresh);

async function start() {
  if (!token) { $("conn").textContent = "Open the link that `aswap gui` printed."; return; }
  try {
    settings = await api("/api/settings");
  } catch (e) {
    showConnection(false, e);
    setTimeout(start, 3000);  // the server may just be starting
    return;
  }
  showConnection(true);
  $("mode").textContent = writable() ? "· can change accounts" : "· read-only";
  buildPanels();
  renderKeys();
  renderLog();
  loop();
}

start();
