"use strict";
// Same layout and wording as the terminal dashboard (aswap tui), plus the controls
// the terminal has on its keys. Every value is placed with textContent: emails,
// aliases and org names come from outside this page and must never be parsed as
// markup. Nothing here builds a command: it only sends plain values to the API.

// Wording matches SENTINEL_NOTES in switcher.py.
const STATUS = {
  token_expired: "token expired — refreshed when needed (aswap list refreshes it now)",
  api_key: "API key (no quota)",
  keychain_unavailable: "keychain unavailable — locked or in use; try again",
  relogin_required: "re-login needed — refresh token dead; log in with Claude Code, then run: aswap add",
  foreign_credential: "live credential belongs to another account — a switch repairs it",
  no_credentials: "no stored credentials",
  unavailable: "usage unavailable",
};
const WARN_PCT = 70, CRIT_PCT = 90, BAR_WIDTH = 24, STALE_S = 300;
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
let settings = null;
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
const loginOpen = () => [state.claude, state.codex].some((d) => d && d.job && d.job.state === "running");
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

// -- one panel, built twice -----------------------------------------------------------
//
// Claude and Codex get the same parts in the same order, from the same code, so they
// cannot drift apart: login and saving, rotating, the accounts with the same five
// buttons each, then auto-switch. Only labels and the usage rows differ.

const PROVIDERS = {
  claude: {
    title: "claude code", base: "/api/claude", noun: "Claude",
    tokenButton: "add token…", tokenTitle: "Add from a token or API key",
    tokenText: "A setup-token (from claude setup-token) or a Console API key. It is stored like any account and never shown again.",
    tokenFields: [
      { name: "token", label: "Token or key", type: "password" },
      { name: "email", label: "Label (optional)", placeholder: "defaults to setup-token-N@token.local" },
    ],
  },
  codex: {
    title: "codex", base: "/api/codex", noun: "Codex",
    tokenButton: "add API key…", tokenTitle: "Add an OpenAI API key",
    tokenText: "The key is handed to `codex login --with-api-key` on its standard input, never on a command line, and stored like any account.",
    tokenFields: [{ name: "token", label: "OpenAI API key", type: "password" }],
  },
};
const state = { claude: null, codex: null, autoState: { claude: null, codex: null } };

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

function normalize(p, a) {
  if (p === "claude") {
    return {
      id: String(a.number), alias: a.alias, email: a.email, active: a.active, disabled: a.disabled,
      threshold: typeof a.threshold === "number" ? a.threshold : null, raw: a,
      tag: a.isOrganization ? (a.organizationName || "org") : "personal",
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
  const a = n.raw, line = n.threshold != null ? n.threshold : globalLine(p);
  if (p === "claude") {
    const weekly = (state.autoState.claude && state.autoState.claude.settings.weeklyThreshold) || line;
    const u = a.usage, dim = a.usageAgeSeconds != null && a.usageAgeSeconds > STALE_S;
    if (a.usageStatus !== "ok" || !u) {
      const status = a.usageStatus === "ok" ? "unavailable" : a.usageStatus;
      const text = (a.usageStatus === "api_key" ? "· " : "⚠ ") + (STATUS[status] || status);
      card.append(el("div", text, "note " + (a.usageStatus === "api_key" ? "muted" : "warn")));
      return;
    }
    const win = (w) => (w.resetsAt ? resetText(Date.parse(w.resetsAt)) : "");
    const pace = (w) => (w.aheadOfPace ? "  (ahead of pace)" : "");
    if (u.spend) card.append(usageRow("$$", u.spend.pct, win(u.spend) + "  $" + u.spend.used.toFixed(2) + " / $" + u.spend.limit.toFixed(2), line, dim));
    if (u.fiveHour) card.append(usageRow("5h", u.fiveHour.pct, win(u.fiveHour), line, dim));
    if (u.sevenDay) card.append(usageRow("7d", u.sevenDay.pct, win(u.sevenDay) + pace(u.sevenDay), weekly, dim));
    (u.scoped || []).forEach((w) => card.append(usageRow(w.name, w.pct, win(w) + (w.pct >= 100 ? "  (!)" : pace(w)), weekly, dim)));
    return;
  }
  const u = a.usage, now = Date.now();
  if (a.authMode === "apikey") { card.append(el("div", "· API key (no quota)", "note muted")); return; }
  if (!u) { card.append(el("div", "no usage seen yet (it appears after you use Codex)", "note muted")); return; }
  // A window that has reset since the reading is unknown (use elsewhere is not in the
  // local files), not 0%.
  const win = (w, label) => {
    if (!w) return;
    const resetMs = w.resets_at * 1000;
    if (resetMs <= now) card.append(usageRow(label, null, "", null, true, "reset since last seen"));
    else card.append(usageRow(label, w.used_percent, resetText(resetMs), line, !u.live));
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
  if (v) act(PROVIDERS[p].base + "/alias", { number: n.id, alias: v.alias });
}

async function editThreshold(p, n) {
  const v = await ask({
    title: "Switch-away line for " + PROVIDERS[p].noun + " account " + n.id, text: n.email, ok: "save",
    fields: [{ name: "pct", label: "Percent used (50 to 99.9; blank = the global line)", type: "number", step: "0.5", value: n.threshold == null ? "" : n.threshold }],
  });
  if (!v) return;
  const raw = v.pct.trim(), value = raw === "" ? null : Number(raw);
  if (value !== null && Number.isNaN(value)) { notice("Error: that is not a number", true); return; }
  act(PROVIDERS[p].base + "/threshold", { number: n.id, threshold: value });
}

function accountCard(p, a) {
  const n = normalize(p, a), cfg = PROVIDERS[p];
  const card = el("div", null, "acct" + (n.active ? " active" : ""));
  const head = header(n.id, n.alias, n.email, n.tag, n.active);
  if (n.disabled) head.append(el("span", "(disabled)", "badge"));
  if (n.threshold != null) head.append(el("span", "th " + n.threshold + "%", "badge"));
  if (n.extra) head.append(el("span", "· " + n.extra, "badge"));
  if (p === "claude" && a.usageAgeSeconds != null && a.usageAgeSeconds > STALE_S) head.append(el("span", age(a.usageAgeSeconds), "badge"));
  card.append(head);
  usageBody(p, card, n);
  if (writable()) {
    const lock = loginOpen(), acts = el("div", null, "acts");
    acts.append(
      button("switch", () => act(cfg.base + "/switch", { number: n.id }), { primary: true, disabled: n.active || lock }),
      button(n.disabled ? "enable" : "disable", () => act(cfg.base + "/disable", { number: n.id, disabled: !n.disabled }), { disabled: lock }),
      button("alias", () => editAlias(p, n), { disabled: lock }),
      button("threshold", () => editThreshold(p, n), { disabled: lock }),
      button("remove", () => confirmThen({
        title: "Remove " + cfg.noun + " account " + n.id, ok: "remove", danger: true,
        text: "Remove " + (n.email || "this API-key login") + "? Its stored copy is deleted"
          + (p === "codex" ? "; Codex itself is not touched." : ", with its config backup."),
      }, cfg.base + "/remove", { number: n.id }), { danger: true, disabled: lock }),
    );
    card.append(acts);
  }
  return card;
}

// The login the CLI is using right now, when aswap has not saved it.
function unsavedCard(p, u) {
  const card = el("div", null, "acct");
  const head = header("·", null, u.email, "not saved", false);
  if (u.plan) head.append(el("span", "· " + u.plan, "badge"));
  card.append(head);
  card.append(el("div", "· logged in now, but not saved in aswap yet", "note muted"));
  if (writable()) {
    const acts = el("div", null, "acts");
    acts.append(button("save", () => act(PROVIDERS[p].base + "/save", {}), { primary: true, disabled: loginOpen() }));
    card.append(acts);
  } else {
    card.append(el("div", "save it with: " + (p === "codex" ? "aswap codex add" : "aswap add"), "note muted"));
  }
  return card;
}

function toolsFor(p) {
  const cfg = PROVIDERS[p], lock = loginOpen(), data = state[p], unsaved = !!(data && data.unsaved);
  const login = [
    button("add account…", async () => {
      if (p === "claude") {
        const v = await ask({
          title: "Log in to another Claude account", ok: "open sign-in",
          text: "Opens Claude Code's sign-in in your browser. The login you are using now is saved first if it is not saved yet, so nothing is lost.",
          fields: [{ name: "email", label: "Email to pre-fill (optional)", placeholder: "you@example.com" }],
        });
        if (v) act(cfg.base + "/login", v.email.trim() ? { email: v.email.trim() } : {});
      } else if (await ask({
        title: "Log in to another Codex account", ok: "open sign-in",
        text: "Opens Codex's sign-in in your browser. The login you are using now is saved first if it is not saved yet. Running Codex sessions keep their account until restarted.",
      })) act(cfg.base + "/login", {});
    }, { primary: true, disabled: lock }),
  ];
  if (p === "codex") {
    login.push(button("sign in by code…", async () => {
      if (await ask({
        title: "Sign in to Codex with a code", ok: "start",
        text: "For a machine without a browser, or when the browser does not open: Codex prints a link and a code to enter there. They appear here.",
      })) act(cfg.base + "/login", { device: true });
    }, { disabled: lock }));
  }
  login.push(
    button("save current login", async () => {
      if (await ask({
        title: "Save current " + cfg.noun + " login", ok: "save",
        text: "Back up the " + cfg.noun + " login in use as a managed account. If it is already managed, its stored copy is refreshed in place.",
      })) act(cfg.base + "/save", {});
    }, { primary: unsaved, disabled: lock }),
    button(cfg.tokenButton, async () => {
      const v = await ask({ title: cfg.tokenTitle, ok: "add", text: cfg.tokenText, fields: cfg.tokenFields });
      if (!v) return;
      const body = { token: v.token.trim() };
      if (v.email && v.email.trim()) body.email = v.email.trim();
      act(cfg.base + "/token", body);
    }, { disabled: lock }),
  );
  const rotate = [
    button("rotate", () => act(cfg.base + "/rotate", { strategy: "next" }), { disabled: lock, title: "switch to the next account" }),
    button("best", () => act(cfg.base + "/rotate", { strategy: "best" }), { disabled: lock, title: "the account with the most quota left" }),
    button("next available", () => act(cfg.base + "/rotate", { strategy: "next-available" }), { disabled: lock, title: "next account that is not at its limit" }),
  ];
  return { login, rotate };
}

// -- auto-switch, per panel ----------------------------------------------------------------------

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
  if (Number.isNaN(threshold) || Number.isNaN(weekly)) { notice("Error: a line must be a number", true); return; }
  const changes = [];
  if (threshold !== s.threshold) changes.push(["autoswitch.threshold", threshold]);
  if (p === "claude") {
    if (weekly !== s.weeklyThreshold) changes.push(["autoswitch.weeklyThreshold", weekly]);
    if (v.strategy !== s.strategy) changes.push(["autoswitch.strategy", v.strategy]);
    if (v.keepalive !== s.keepalive) changes.push(["autoswitch.keepalive", v.keepalive]);
  }
  if (!changes.length) return;
  busy = true;
  try {
    for (const [key, value] of changes) await api(PROVIDERS[p].base + "/config", { key, value });
    notice("Saved." + (a.running ? " Auto-switch restarted with the new values." : ""));
  } catch (e) {
    notice("Error: " + e.message, true);
  }
  busy = false;
  lastSig = "";
  refresh();
}

function renderAuto(p, box) {
  const a = state.autoState[p];
  box.replaceChildren();
  if (!a) return;
  const head = el("div", null, "section-head sub");
  head.append(el("span", "auto-switch"), a.running
    ? el("span", "● running" + (a.dryRun ? " (dry run: it only reports)" : ""), "on")
    : el("span", "stopped", "muted"));
  box.append(head);
  if (writable()) {
    const tools = el("div", null, "tools");
    if (a.running) {
      tools.append(button("stop", () => act(PROVIDERS[p].base + "/auto", { action: "stop" }), { danger: true }));
    } else {
      tools.append(
        button("start", () => act(PROVIDERS[p].base + "/auto", { action: "start" }), { primary: true }),
        button("start (dry run)", () => act(PROVIDERS[p].base + "/auto", { action: "start", dryRun: true }), { title: "decide and report, never switch" }),
      );
    }
    tools.append(button("settings", () => editAutoSettings(p)));
    box.append(tools);
  }
  const s = a.settings;
  box.append(el("div", p === "claude"
    ? "threshold " + s.threshold + "%  ·  weekly " + (s.weeklyThreshold == null ? "same" : s.weeklyThreshold + "%") +
      "  ·  strategy " + s.strategy + "  ·  keepalive " + (s.keepalive ? "on" : "off") + "  ·  every " + Math.round(s.intervalSeconds) + "s"
    : "threshold " + s.threshold + "%  ·  every " + Math.round(s.intervalSeconds) + "s  ·  a running Codex keeps its account until restarted",
    "note muted"));
  const log = el("div", null, "log");
  const events = a.events.filter((e) => e.kind !== "poll").slice(-8);
  if (!events.length) log.append(el("div", a.running ? "watching… nothing to report yet" : "no events", "muted"));
  events.forEach((e) => {
    const row = el("div"), cls = e.kind === "switch" ? "on" : e.kind === "error" || e.kind === "account-quarantined" ? "warn" : "muted";
    row.append(el("span", e.ts, "t"), el("span", e.text, cls));
    log.append(row);
  });
  box.append(log);
}

// -- the panel and the page --------------------------------------------------------------------------

function buildPanels() {
  const root = $("panels");
  ["claude", "codex"].forEach((p) => {
    const panel = el("section", null, "panel");
    panel.id = "panel-" + p;
    panel.append(el("h2", PROVIDERS[p].title));
    ["state", "progress", "login", "rotate", "accounts", "auto"].forEach((part) => {
      const d = el(part === "accounts" ? "main" : "div", null, part === "login" || part === "rotate" ? "tools" : part);
      d.dataset.part = part;
      panel.append(d);
    });
    root.append(panel);
  });
}

const part = (p, name) => document.querySelector("#panel-" + p + " [data-part=" + name + "]");

function liveName(p) {
  const data = state[p];
  if (!data) return "";
  if (data.unsaved) return data.unsaved.email + " (not saved yet)";
  const live = data.accounts.find((a) => a.active);
  return live ? live.email + " (saved)" : "";
}

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

function renderPanel(p) {
  const data = state[p], cfg = PROVIDERS[p];
  if (!data) return;
  const st = part(p, "state");
  st.replaceChildren();
  st.className = "state muted";
  if (p === "codex" && data.installed === false && !data.accounts.length) {
    st.append(document.createTextNode("Codex is not installed (`codex` was not found on PATH)."));
  } else {
    const who = liveName(p);
    st.append(document.createTextNode("logged in now: "), el("b", who || "no login found"));
    if (p === "codex" && data.running) {
      st.append(el("div", data.supervised
        ? "● " + data.supervised + " Codex session(s) started with `aswap codex run` follow a switch by themselves."
        : "! Codex is running outside agent-swap: it keeps its account until restarted. Start it with `aswap codex run` and a switch reaches it at once.", data.supervised ? "on" : "warn"));
    }
  }
  renderProgress(p);
  const tools = toolsFor(p);
  [["login", tools.login], ["rotate", tools.rotate]].forEach(([name, buttons]) => {
    const box = part(p, name);
    box.replaceChildren(...(writable() ? buttons : []));
    box.hidden = !writable();
  });
  const cards = data.accounts.map((a) => accountCard(p, a));
  if (data.unsaved) cards.unshift(unsavedCard(p, data.unsaved));
  const main = part(p, "accounts");
  main.replaceChildren(...cards);
  if (data.error) main.prepend(el("p", data.error, "warn"));
  if (!cards.length) main.append(el("p", "No " + cfg.noun + " accounts yet." + (writable() ? " Use “add account” above." : ""), "muted"));
  renderAuto(p, part(p, "auto"));
}

function trackJob(job) {
  const key = job ? job.kind + "|" + job.state + "|" + job.message : "";
  if (job && job.state !== "running" && key !== lastJobKey) notice(job.message, job.state === "error");
  lastJobKey = key;
}

function render() {
  renderPanel("claude");
  renderPanel("codex");
  trackJob((state.claude && state.claude.job) || (state.codex && state.codex.job));
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
    const [claude, codex, ac, ax] = await Promise.all([
      api("/api/claude"), api("/api/codex"), api("/api/claude/auto"), api("/api/codex/auto"),
    ]);
    state.claude = claude; state.codex = codex; state.autoState = { claude: ac, codex: ax };
  } catch (e) {
    showConnection(false, e);
    return;
  }
  showConnection(true);
  $("updated").textContent = "updated " + new Date().toLocaleTimeString([], { hour12: false });
  // Skip identical data: a rebuild mid-click would swallow the click. Reset countdowns
  // still move, so a half-minute-old picture is redrawn anyway.
  const sig = JSON.stringify([state, Math.floor(Date.now() / 30000)]);
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
  buildPanels();
  loop();
}

start();
