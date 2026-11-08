# Web control panel: how `cswap gui` is secured

Reference for the local web control panel. Everyday use is in the [README](../README.md#web-control-panel-gui).

A browser page that can switch accounts is an attack surface: any website you have open could try to talk to a local server. The dashboard is built so that it cannot be driven from outside the page it serves.

## What it is

A small server from the standard library (`http.server`, no new dependency) plus three static files (`index.html`, `app.css`, `app.js`). The page looks like the terminal dashboard on purpose: the same rows, bars, glyphs and wording, and the same thresholds (amber at 70%, red at 90%).

## Every request must pass, in order

1. **Bind**: `127.0.0.1` only, on a random free port (or `--port N`). Never `0.0.0.0`.
2. **Host header** must be exactly the address and port it was started on. This defeats DNS rebinding, where a hostile site points its own name at `127.0.0.1`.
3. **Origin** must match, and `Sec-Fetch-Site` must be `same-origin` or `none`. Cross-site requests are rejected.
4. **POST bodies** must be JSON with a JSON content type, and at most 4 KiB. A form post from another site cannot be JSON.
5. **Bearer token**: a random per-run secret. It travels in the URL **fragment** (`#...`), which browsers never send to a server or in a `Referer`; the page moves it to `sessionStorage` and clears the address bar.
6. **Writable by default, `--read-only` for a view.** With `--read-only` every write endpoint answers 403. (`--write` is still accepted and does nothing.)

## What the page can do

| Endpoint | Method | Does | Read-only |
|---|---|---|---|
| `/api/settings`, `/api/accounts`, `/api/codex`, `/api/auto` | GET | accounts, usage, the auto-switch state and its last events | works |
| `/api/switch`, `/api/rotate`, `/api/disable`, `/api/remove` | POST | switch to a slot, rotate (`next`, `best`, `next-available`), hold out of rotation, remove | 403 |
| `/api/alias`, `/api/threshold` | POST | an account's short name and its own switch-away line | 403 |
| `/api/add-current`, `/api/add-token` | POST | save the login in use; add a setup-token or API key | 403 |
| `/api/login` | POST | start `claude auth login` or `codex login` in the browser, then save the result | 403 |
| `/api/auto`, `/api/config` | POST | start/stop the auto-switch engine; change threshold, weekly line, strategy, keepalive | 403 |
| `/api/codex/switch`, `/api/codex/rotate`, `/api/codex/remove`, `/api/codex/add-current` | POST | the same for Codex | 403 |

Every value is checked on the server and the page never sends anything that could reach a shell:

- Account routes take a slot **number** (digits only), never an email, a path or a command.
- `/api/config` accepts four keys only (`autoswitch.threshold`, `.weeklyThreshold`, `.strategy`, `.keepalive`); the value goes through the same validation as `cswap config set`.
- `/api/login` runs one fixed command (`claude auth login --email <address>` or `codex login`), without a shell and with no input; the only variable part is an address checked against the same pattern `cswap add` uses. It is refused while the login in use is not saved yet, because signing in replaces it. While one is open, other changes answer 409 (auto-switch and its settings stay available), the process is stopped after five minutes, and the new login is saved only if it exited cleanly.
- `/api/add-token` takes the token in the request body; it is never logged and is removed from any message sent back.
- Only one write runs at a time. No endpoint returns a token or credential: the page receives emails, aliases, plans and percentages.

The auto-switch engine started from the page is the same one `cswap auto` runs, in this process: it stops when the dashboard stops. Run auto-switch in one place only.

## Logins that are not saved yet

If the login Claude Code (or Codex) is using is not stored in cswap, the page lists it first, marked `[not saved]`, with its address (and plan, for Codex) only. It has a **save** button (read-only: it says which command to run). For Claude this comes from `.claude.json`, for Codex from `auth.json`: no keychain read and no network.

## In the browser

The page refreshes on a timer, so like the terminal dashboard it never refreshes an idle account's token: a lapsed one shows as "token expired" until you use that account or run `cswap list`.

A strict `Content-Security-Policy` (own origin only, no inline script, no framing). Every value from outside the page (email, alias, organisation name) is placed with `textContent`, never parsed as HTML. There is no polling timer that rebuilds the page under your cursor: it refreshes data and re-renders in one step.

## Shared machines

The token protects against other websites, not against other users of the same computer who can read your terminal or process list. On a shared machine start it with `--no-open` and paste the link yourself, or do not use it.
