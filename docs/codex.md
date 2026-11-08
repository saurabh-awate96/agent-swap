# Codex accounts

Reference for `aswap codex`, the OpenAI Codex CLI side of agent-swap. Everyday use is in the [README](../README.md#codex-accounts).

## Commands

```bash
aswap codex add [--slot N] [--alias NAME] [-y]   # store the login Codex is using now
aswap codex list [--json]                        # stored accounts and their usage
aswap codex switch [NUM|EMAIL|ALIAS]             # rotate, or jump to one account
aswap codex switch --strategy best|next-available
aswap codex switch ... --force                   # also while Codex may be refreshing
aswap codex status [--json]                      # which stored account is live
aswap codex alias NUM|EMAIL|ALIAS [NAME]         # set a short name; omit NAME to clear
aswap codex disable|enable NUM|EMAIL|ALIAS
aswap codex threshold NUM|EMAIL|ALIAS PCT        # its own switch-away line, 50 to 99.9
aswap codex threshold NUM|EMAIL|ALIAS --unset    # back to the global line
aswap codex remove NUM|EMAIL|ALIAS [-y]          # forget a stored account (-y: also the live one)
aswap codex run [-- codex args]                  # Codex that follows a switch (POSIX only)
```

`switch` and `--strategy` never touch the network. `list` may ask the real Codex client for the live account's usage (see below).

## What gets switched

Codex keeps its login in one file, `$CODEX_HOME/auth.json` (default `~/.codex/auth.json`). `aswap codex switch` replaces that file with the stored copy of another account. Nothing else of Codex's is touched: history, config and sessions stay put.

Only the file credential store is supported, which is `cli_auth_credentials_store = "file"` in `config.toml` and the default. With `keyring` or `auto`, aswap refuses rather than guess where Codex keeps the login.

## Storage

Each slot holds the **exact bytes** of `auth.json` as Codex wrote it, never re-serialised. On macOS they live in the Keychain as items `codex-N`; elsewhere as files under `codex/credentials/` in the backup directory (see the README's [data locations](../README.md#data-locations)). The roster (slot, email, plan, alias, disabled flag, threshold) is `codex/sequence.json`. The identity comes from the claims inside the login's own tokens, so no request to OpenAI is needed to store one.

## Switching safely

1. Take the same lock Codex takes (`auth.lock`), plus agent-swap's own.
2. Refuse while Codex may be mid-refresh (a Codex process is running and the access token expires within 10 minutes), unless `--force`.
3. Refuse if the current `auth.json` is a login aswap has not stored, so a login is never lost. Run `aswap codex add` first.
4. Write the target atomically with mode `0600`, then read it back. If it does not verify, the previous login is restored.

Refresh tokens are single use, so a login that exists in two places breaks. Do not `codex logout` a stored account: it revokes the login on OpenAI's side.

## Running Codex and switches

A Codex process loads `auth.json` when it starts and keeps that account. Nothing outside the process can change it, so a plain `codex` that was already running keeps its account after a switch until it restarts.

`aswap codex run` closes that gap. It runs Codex as a child in the same terminal and watches a marker file that every real switch writes (from the dashboards, `aswap codex switch` or auto-switch). When the account changes, it stops Codex and starts `codex resume <session id>` on the new login: same terminal, same conversation, nothing to type.

- The session is found by working directory among the rollout files written since Codex started. If none exists yet, Codex simply restarts.
- The answer that was streaming at that moment is interrupted. Everything before it is kept.
- On restart only `resume <id>` is passed, not the options of the first start.
- To use it everywhere: `alias codex='aswap codex run'`.
- POSIX only. On Windows, `aswap codex run` exits with an error.

The web panel and the terminal dashboard say how many Codex sessions are supervised, and warn about a Codex running outside `aswap codex run`.

## Usage numbers

Switching makes no request to OpenAI. Codex records rate-limit snapshots in its own session logs, `sessions/**/rollout-*.jsonl`, as `token_count` events: `primary` is the 5-hour window and `secondary` the weekly one. `aswap codex list` reads the newest snapshot, so an account you have not used lately shows the numbers from its last session. A window whose reset time has passed shows as `reset`.

Windows are named by their real length, not assumed. Codex's `go` plan has one 30-day window and no weekly one; the panels show `30d` and say that the plan has no weekly limit.

`switch --strategy best` picks the account with the most quota left by those numbers. `next-available` rotates and skips accounts whose last reading is at their limit.

### The live account is read from Codex itself

Local files are stale for an account you have not used lately, and after a reset they hold nothing current. For the **live** account only, when its stored reading is older than five minutes or has a window that has reset since, `aswap codex list`, the web panel and the terminal dashboard ask the real `codex app-server` for `account/rateLimits/read`. That is OpenAI's own client running with your own login. aswap never calls OpenAI's usage endpoint itself, and it identifies itself to the client as `agent-swap`.

- It answers in about a second and is shown as "read from Codex".
- It is asked at most once every five minutes per process. A failed ask is not retried within that time, and any failure falls back to the local reading.
- It never switches or rewrites a login.
- Turn it off with `aswap config set usage.codexProbe false`. Codex is then fully offline from agent-swap's side.

Accounts that are not live have no such reading and keep their last local numbers.

Two limits follow. Only usage **on this machine** is recorded, so use from another computer or the Codex app does not show, and the numbers can be lower than OpenAI's (the live reading corrects this for the live account only). And each reading is credited to the stored account whose id matches the session's `creator_account_id`. Sessions of a login aswap has not stored are ignored, never guessed onto another account.

## Checking every stored login

The refresh button in the web panel (`/api/codex/refresh`) and the "check every login now" entry of the terminal dashboard ask the real client about **every** stored login, not only the live one.

- The live account is asked as it is.
- A stored one is asked through a throwaway `CODEX_HOME`: a 0700 directory holding a 0600 copy of that slot's `auth.json`, removed afterwards. Nothing is switched and the live `auth.json` is never touched.
- If the client refreshed that copy's token, the old refresh token in the stored copy is dead, so the new file is saved back into the slot.
- A login whose access token is within ten minutes of expiry is not asked, because the client would refresh it. It is reported as about to refresh and checked on its next real use.

Each result is kept on the slot (`login ok`, `revoked`, `unauthorized`, `failed`, with the time) and shown on its card.

## Revoked logins

If OpenAI has revoked a login, its stored copy can no longer be used, and the panels say so: `login revoked: sign in again`, with the sign-in entry to use. Saving a revoked login would only store a dead copy, so the web panel and the terminal dashboard refuse to save one (their "save current login" action) and say to sign in again. Signing in to another account does not save a revoked login first.

The command line `aswap codex add` does not run this check on the current `main` branch; it stores whatever login `auth.json` holds.

## Signing in to another account

- **Web panel:** `add → account` opens `codex login` in your browser and saves the result. "By code" uses `codex login --device-auth`, and the page shows the link and the device code if no browser window opens. An API key is `codex login --with-api-key` with the key on standard input.
- **Terminal dashboard:** "Sign in to another account" leaves the dashboard for `codex login` in the same terminal, then comes back.
- **Command line:** run `aswap codex add` for the login in use, then `codex login` yourself, then `aswap codex add` again.

Sign-in has been exercised by tests with stand-in commands only. Windows is untested for these flows.

## Removing Codex accounts

`aswap codex remove N` forgets a stored account and deletes its stored copy. It refuses the live account unless you pass `-y`.

`aswap purge` removes the backup directory, which holds the file-based Codex copies and the roster. Its own description does not mention Codex's Keychain items, so on macOS run `aswap codex remove N` for each Codex account before purging.

## JSON output

`aswap codex list --json` prints a JSON array with one object per account, and `aswap codex status --json` prints one object. Their fields are `slot`, `alias`, `email`, `plan`, `authMode`, `active`, `disabled`, `threshold`, `problem`, `health` and `usage` (for `status`: `state`, `slot`, `email`, `plan`, `authMode`, `lastRefresh`, `accessExpiresInSeconds`, `usage`). Unlike the Claude payloads, **these carry no `schemaVersion`** yet; treat the shape as provisional until it does. See [json-output.md](json-output.md).

## Not built

- Codex auto-switch exists only inside the web control panel. There is no `aswap auto` for Codex. It is a smaller engine than Claude's: it shares the global threshold, uses a fixed five-minute cooldown, and has no hysteresis or quarantine.
- `aswap codex run` is POSIX only.
- Windows is untested for the sign-in flows.
