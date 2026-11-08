# Codex accounts: how `aswap codex` works

Reference for the OpenAI Codex CLI support. Everyday use is in the [README](../README.md#openai-codex-cli-accounts).

## What gets switched

Codex keeps its login in one file, `$CODEX_HOME/auth.json` (default `~/.codex/auth.json`). `aswap codex switch` replaces that file with the stored copy of another account. Nothing else of Codex's is touched: history, config and sessions stay put.

Only the file credential store is supported (`cli_auth_credentials_store = "file"` in `config.toml`, the default). With `keyring` or `auto` aswap refuses, rather than guess where Codex keeps the login.

## Storage

Each slot holds the **exact bytes** of `auth.json` as Codex wrote it, never re-serialised. They live in the platform credential store (macOS Keychain item `codex-N`, or a file under `~/.claude-swap-backup/codex/`), with the roster (`sequence.json`: slot, email, plan, alias) next to them. The identity comes from the claims inside the login's own tokens, so no request to OpenAI is ever needed.

## Switching safely

1. Take the same lock Codex takes (`auth.lock`), plus agent-swap's own.
2. Refuse while Codex may be mid-refresh (a Codex process is running and the access token expires within 10 minutes), unless `--force`.
3. Refuse if the current `auth.json` is a login aswap has not stored, so a login is never lost. Run `aswap codex add` first.
4. Write the target atomically with mode `0600`, then read it back. If it does not verify, the previous login is restored.

Because refresh tokens are single use, a login that exists in two places will break. Do not `codex logout` a stored account: it revokes the login server-side.

## A running Codex does not notice, unless it was started with `aswap codex run`

A Codex process loads `auth.json` when it starts and keeps that account; nothing outside the process can change it (the app-server's login calls need a browser login or are marked internal-only). So a plain `codex` that was already running keeps its account after a switch.

`aswap codex run` closes that gap. It runs Codex as a child in the same terminal and watches a marker file that every real switch writes (from the dashboard, `aswap codex switch` or auto-switch). When the account changes it stops Codex and starts `codex resume <session id>` on the new login: same terminal, same conversation, nothing to type. The session is found by working directory among the rollout files written since it started; if none exists yet it simply restarts. The answer that was streaming at that moment is interrupted, and only `resume <id>` is passed on restart, not the options of the first start. To use it everywhere: `alias codex='aswap codex run'`. POSIX only for now.

## Usage numbers

Switching makes no request to OpenAI. Codex records rate-limit snapshots in its own session logs, `sessions/**/rollout-*.jsonl`, as `token_count` events: `primary` is the 5-hour window and `secondary` the weekly one. `aswap codex list` reads the newest snapshot, so an account you have not used lately shows the numbers from its last session, and a window whose reset time has passed shows as reset.

`switch --strategy best` picks the account with the most quota left by those numbers.

### The live account is read from Codex itself

Those files are stale for an account you have not used lately, and after a reset they hold nothing current. For the **live** account only, when its stored reading is older than five minutes or has a window that has reset since, `aswap codex list` and the web dashboard ask the real `codex app-server` for `account/rateLimits/read`. That is OpenAI's own client running with your own login (aswap never calls OpenAI's usage endpoint itself and identifies itself to the client as `agent-swap`), it answers in about a second, and the result is shown as "read from Codex". It is asked at most once every five minutes per process, a failed ask is not retried within that time, and any failure falls back to the local reading. It never switches or rewrites a login. Turn it off with `aswap config set usage.codexProbe false`; then Codex stays fully offline from agent-swap's side.

Accounts that are not live have no such reading, so they keep showing their last local numbers.

Two limits follow from that. Only usage **on this machine** is recorded, so use from another computer or the Codex app does not show, and the numbers can be lower than OpenAI's (the live reading above is what corrects this for the live account). And each reading is credited to the stored account whose id matches the session's `creator_account_id`: sessions of a login aswap has not stored are ignored, never guessed onto another account. A window that has reset since the last reading is shown as unknown in the web dashboard (and as `reset` on the command line), not as 0%.

## Alias, disable, threshold, auto-switch, API keys

Codex slots have what Claude's have: `aswap codex alias`, `disable` / `enable` (a disabled slot is skipped by `rotate`, `--strategy best`, `next-available` and auto-switch, but an explicit `switch` still works), and `threshold` (a slot's own switch-away line, 50 to 99.9). `switch --strategy next-available` rotates and skips slots whose last reading is at their limit. `aswap gui` adds a Codex auto-switch and a log-in-with-an-API-key button (`codex login --with-api-key`, the key on stdin).

## Not built

Codex auto-switch exists only inside `aswap gui` (there is no `aswap auto` for Codex), and it is a small engine: no hysteresis or quarantine like Claude's. Windows is untested for the sign-in flows.
