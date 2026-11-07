# Web dashboard: how `cswap gui` is secured

Reference for the local web dashboard. Everyday use is in the [README](../README.md#web-dashboard-gui).

A browser page that can switch accounts is an attack surface: any website you have open could try to talk to a local server. The dashboard is built so that it cannot be driven from outside the page it serves.

## What it is

A small server from the standard library (`http.server`, no new dependency) plus three static files (`index.html`, `app.css`, `app.js`). The page looks like the terminal dashboard on purpose: the same rows, bars, glyphs and wording, and the same thresholds (amber at 70%, red at 90%).

## Every request must pass, in order

1. **Bind**: `127.0.0.1` only, on a random free port (or `--port N`). Never `0.0.0.0`.
2. **Host header** must be exactly the address and port it was started on. This defeats DNS rebinding, where a hostile site points its own name at `127.0.0.1`.
3. **Origin** must match, and `Sec-Fetch-Site` must be `same-origin` or `none`. Cross-site requests are rejected.
4. **POST bodies** must be JSON with a JSON content type, and at most 4 KiB. A form post from another site cannot be JSON.
5. **Bearer token**: a random per-run secret. It travels in the URL **fragment** (`#...`), which browsers never send to a server or in a `Referer`; the page moves it to `sessionStorage` and clears the address bar.
6. **Read-only unless `--write`.** Without it, the write endpoints answer 403.

## What the page can do

| Endpoint | Method | Needs `--write` |
|---|---|---|
| `/api/settings` | GET | no |
| `/api/accounts` | GET (Claude accounts and usage) | no |
| `/api/codex` | GET (Codex accounts and usage) | no |
| `/api/switch`, `/api/disable`, `/api/add-current` | POST | yes |
| `/api/codex/switch`, `/api/codex/add-current` | POST | yes |

Writes carry only a slot **number**, validated as digits; they never carry an email, a path or a command. Only one write runs at a time. No endpoint returns a token or credential: the page receives emails, aliases, plans and percentages.

## In the browser

A strict `Content-Security-Policy` (own origin only, no inline script, no framing). Every value from outside the page (email, alias, organisation name) is placed with `textContent`, never parsed as HTML. There is no polling timer that rebuilds the page under your cursor: it refreshes data and re-renders in one step.

## Shared machines

The token protects against other websites, not against other users of the same computer who can read your terminal or process list. On a shared machine start it with `--no-open` and paste the link yourself, or do not use it.
