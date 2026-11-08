# Codex accounts: how `cswap codex` works

Reference for the OpenAI Codex CLI support. Everyday use is in the [README](../README.md#openai-codex-cli-accounts).

## What gets switched

Codex keeps its login in one file, `$CODEX_HOME/auth.json` (default `~/.codex/auth.json`). `cswap codex switch` replaces that file with the stored copy of another account. Nothing else of Codex's is touched: history, config and sessions stay put.

Only the file credential store is supported (`cli_auth_credentials_store = "file"` in `config.toml`, the default). With `keyring` or `auto` cswap refuses, rather than guess where Codex keeps the login.

## Storage

Each slot holds the **exact bytes** of `auth.json` as Codex wrote it, never re-serialised. They live in the platform credential store (macOS Keychain item `codex-N`, or a file under `~/.claude-swap-backup/codex/`), with the roster (`sequence.json`: slot, email, plan, alias) next to them. The identity comes from the claims inside the login's own tokens, so no request to OpenAI is ever needed.

## Switching safely

1. Take the same lock Codex takes (`auth.lock`), plus cswap's own.
2. Refuse while Codex may be mid-refresh (a Codex process is running and the access token expires within 10 minutes), unless `--force`.
3. Refuse if the current `auth.json` is a login cswap has not stored, so a login is never lost. Run `cswap codex add` first.
4. Write the target atomically with mode `0600`, then read it back. If it does not verify, the previous login is restored.

Because refresh tokens are single use, a login that exists in two places will break. Do not `codex logout` a stored account: it revokes the login server-side.

## A running Codex does not notice

A Codex process loads `auth.json` when it starts. A switch affects processes started afterwards; sessions already open keep their account until restarted.

## Usage numbers

Switching makes no request to OpenAI. Codex records rate-limit snapshots in its own session logs, `sessions/**/rollout-*.jsonl`, as `token_count` events: `primary` is the 5-hour window and `secondary` the weekly one. `cswap codex list` reads the newest snapshot, so an account you have not used lately shows the numbers from its last session, and a window whose reset time has passed shows as reset.

`switch --strategy best` picks the account with the most quota left by those numbers.

### The live account is read from Codex itself

Those files are stale for an account you have not used lately, and after a reset they hold nothing current. For the **live** account only, when its stored reading is older than five minutes or has a window that has reset since, `cswap codex list` and the web dashboard ask the real `codex app-server` for `account/rateLimits/read`. That is OpenAI's own client running with your own login (cswap never calls OpenAI's usage endpoint itself and identifies itself to the client as `agent-swap`), it answers in about a second, and the result is shown as "read from Codex". It is asked at most once every five minutes per process, a failed ask is not retried within that time, and any failure falls back to the local reading. It never switches or rewrites a login. Turn it off with `cswap config set usage.codexProbe false`; then Codex stays fully offline from cswap's side.

Accounts that are not live have no such reading, so they keep showing their last local numbers.

Two limits follow from that. Only usage **on this machine** is recorded, so use from another computer or the Codex app does not show, and the numbers can be lower than OpenAI's (the live reading above is what corrects this for the live account). And each reading is credited to the stored account whose id matches the session's `creator_account_id`: sessions of a login cswap has not stored are ignored, never guessed onto another account. A window that has reset since the last reading is shown as unknown in the web dashboard (and as `reset` on the command line), not as 0%.

## Not built

Auto-switching for Codex (the Claude engine's polling and thresholds) is not wired up: `cswap codex switch` is a manual or scripted action. Windows is untested.
