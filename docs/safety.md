# Safety: your logins and your accounts

Reference for what cswap does with your logins, what it sends over the network, and what nobody can promise. Everyday use is in the [README](../README.md).

## What cswap stores

It keeps a copy of each login you add (`cswap add`, `cswap onboard`, `cswap codex add`) in the platform store: the macOS Keychain, or files under the backup directory elsewhere (see [Data locations](../README.md#data-locations)). It never reads another app's login and never sends a login anywhere except to the service it belongs to.

## One live copy of a login

Claude and Codex refresh tokens are **single use**: every refresh hands back a new one and kills the old one. Two programs holding the same login will eventually break each other.

- Use `cswap export` / `import` to *move* an account to another machine, then stop using it on the old one.
- Do not copy credential files around by hand, and never put a login someone else is using into cswap.
- Do not `/logout` (Claude) or `codex logout` an account cswap manages. Both revoke the login on the server, so the stored copy dies too.

cswap only manages accounts you own. It has no feature for taking over someone else's session, and `onboard` goes through Claude Code's own `claude auth login` in your browser.

## What it sends over the network

| What | When | To |
|---|---|---|
| Usage read (`cswap list`, dashboards, auto-switch) | every few minutes, adaptive, a couple of accounts per check | Anthropic usage endpoint |
| Profile / plan | every six hours | Anthropic profile endpoint |
| Token refresh | **only on demand**: `cswap list`/`switch` for that account, or when auto-switch must leave the active account and no candidate has readable usage | Anthropic OAuth endpoint |
| Update check | at most once a day (an hour after a failure); skipped for git/folder installs | PyPI |
| Keepalive (`autoswitch.keepalive`, **off by default**) | idle account whose token is about to lapse, at most once per 8 hours | Anthropic, through the real `claude` binary |
| Codex | never | nothing: Codex usage is read from Codex's own session files |
| Web dashboard | never | nothing outside your machine (`127.0.0.1` only) |

Idle accounts are **not** refreshed in the background. An idle account whose access token lapsed shows "token expired" until you use it, which is deliberate: every token refresh by cswap is a request made with Claude Code's client id from a different program, and the fewer of those there are, the better.

## Ban risk, honestly

Nothing in this repo can promise that a provider will never object to multiple accounts, automatic switching or scheduled requests. What it does to stay on the quiet side:

- It acts as little as possible: no background refreshes of idle accounts, adaptive polling that stays flat however many accounts you have, a cooldown and hysteresis so auto-switch does not flap.
- It uses the real client where a request has to look like a request from the real client (keepalive runs `claude`; it does not impersonate it).
- It never creates accounts, never touches billing, never bypasses a limit: it moves you to an account that still has quota.

What it cannot know: how Anthropic or OpenAI classify a given pattern, or whether their terms allow it for your plan. Read them. The keepalive in particular spends a real, tiny request per ping and starts that account's 5-hour window, so it is off until you turn it on.

## What a failure looks like

- A usage check fails: the last numbers are trusted for a bounded time while retries back off.
- A token refresh fails or its credential is rejected: the account is quarantined and reported, never retried in a loop.
- A write is interrupted: credentials are replaced atomically, and the previous copy is kept (`.prev`) until the new one is confirmed.
- `cswap codex` would replace a Codex login it has not stored: it refuses and tells you to `cswap codex add` first.

## Reporting a problem

Security issues: see [SECURITY.md](../SECURITY.md).
