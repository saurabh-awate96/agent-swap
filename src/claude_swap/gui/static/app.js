"use strict";
// Same layout and wording as the terminal dashboard (cswap tui). Every value is
// placed with textContent: emails, aliases and org names come from outside this
// page and must never be parsed as markup.

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
let settings = null, claude = null, codex = null, busy = false;

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

const sev = (pct) => (pct >= CRIT_PCT ? "crit" : pct >= WARN_PCT ? "warn" : "ok");

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

function button(text, onclick, disabled) {
  const b = el("button", text);
  b.disabled = !!disabled;
  b.onclick = onclick;
  return b;
}

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

// The login the CLI is using right now, when cswap has not saved it.
function unsavedCard(u, kind) {
  const card = el("div", null, "acct");
  const head = header("·", null, u.email, "not saved", false);
  if (u.plan) head.append(el("span", "· " + u.plan, "badge"));
  card.append(head);
  card.append(el("div", "· logged in now, but not saved in cswap yet", "note muted"));
  if (settings && settings.writable) {
    const acts = el("div", null, "acts");
    acts.append(button("save", () => act(kind === "codex" ? "/api/codex/add-current" : "/api/add-current", {})));
    card.append(acts);
  } else {
    const cmd = kind === "codex" ? "cswap codex add" : "cswap add";
    card.append(el("div", "save it with: " + cmd + "  (or start the dashboard with: cswap gui --write)", "note muted"));
  }
  return card;
}

function claudeCard(a) {
  const card = el("div", null, "acct" + (a.active ? " active" : ""));
  const head = header(a.number, a.alias, a.email, a.isOrganization ? (a.organizationName || "org") : "personal", a.active);
  if (a.disabled) head.append(el("span", "(disabled)", "badge"));
  if (typeof a.threshold === "number") head.append(el("span", "th " + a.threshold + "%", "badge"));
  if (a.plan && a.plan.label) head.append(el("span", "· " + a.plan.label, "badge"));
  if (a.usageAgeSeconds != null && a.usageAgeSeconds > STALE_S) head.append(el("span", age(a.usageAgeSeconds), "badge"));
  card.append(head);

  const line = settings ? settings.autoswitch.threshold : null;
  const weekly = (settings && settings.autoswitch.weeklyThreshold) || line;
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
  if (settings && settings.writable) {
    const acts = el("div", null, "acts");
    acts.append(
      button("switch", () => act("/api/switch", { number: String(a.number) }), a.active),
      button(a.disabled ? "enable" : "disable", () => act("/api/disable", { number: String(a.number), disabled: !a.disabled })),
    );
    card.append(acts);
  }
  return card;
}

function codexCard(a) {
  const card = el("div", null, "acct" + (a.active ? " active" : ""));
  card.append(header(a.slot, a.alias, a.email, a.plan, a.active));
  const u = a.usage, now = Date.now();
  if (a.authMode === "apikey") {
    card.append(el("div", "· API key (no quota)", "note muted"));
  } else if (!u) {
    card.append(el("div", "no usage seen yet (it appears after you use Codex)", "note muted"));
  } else {
    // Local snapshots, so they are always shown as aged readings. A window that
    // has reset since the reading is unknown (use elsewhere is not in the
    // local files), not 0%.
    const win = (w, label) => {
      if (!w) return;
      const resetMs = w.resets_at * 1000;
      if (resetMs <= now) card.append(usageRow(label, null, "", null, true, "reset since last seen"));
      else card.append(usageRow(label, w.used_percent, resetText(resetMs), null, true));
    };
    win(u.primary, "5h");
    win(u.secondary, "7d");
    const seen = Date.parse(u.seenAt);
    if (!isNaN(seen)) card.append(el("div", "└ last seen " + age((now - seen) / 1000) + " · this machine only", "note muted"));
  }
  if (settings && settings.writable) {
    const acts = el("div", null, "acts");
    acts.append(button("switch", () => act("/api/codex/switch", { number: String(a.slot) }), a.active));
    card.append(acts);
  }
  return card;
}

function render() {
  if (claude) {
    const main = $("accounts");
    const cards = claude.accounts.map(claudeCard);
    if (claude.unsaved) cards.unshift(unsavedCard(claude.unsaved, "claude"));
    main.replaceChildren(...cards);
    if (!cards.length) main.append(el("p", "No accounts yet. Run cswap onboard in a terminal.", "muted"));
  }
  if (codex && (codex.accounts.length || codex.unsaved || codex.error || (settings.writable && codex.installed))) {
    $("codex").hidden = false;
    $("codex-note").textContent = codex.error || "";
    const cards = codex.accounts.map(codexCard);
    if (codex.unsaved) cards.unshift(unsavedCard(codex.unsaved, "codex"));
    $("codex-accounts").replaceChildren(...cards);
  }
}

async function act(path, body) {
  if (busy) return;
  busy = true;
  $("notice").textContent = "working...";
  try {
    const r = await api(path, body);
    $("notice").textContent = String(r.message || (r.switched ? "Switched. Running Claude Code picks it up within about 30s." : "Nothing to do."));
  } catch (e) {
    $("notice").textContent = "Error: " + e.message;
  }
  busy = false;
  refresh();
}

async function refresh() {
  try {
    claude = await api("/api/accounts");
    codex = await api("/api/codex");
    $("updated").textContent = "updated " + new Date().toLocaleTimeString([], { hour12: false });
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
  $("mode").textContent = settings.writable ? "· can change accounts" : "· read-only";
  if (settings.writable) {
    $("add").hidden = false;
    $("add").onclick = () => act("/api/add-current", {});
    $("codex-add").hidden = false;
    $("codex-add").onclick = () => act("/api/codex/add-current", {});
  }
  loop();
}

start();
