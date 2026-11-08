# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/). This fork tracks
[realiti4/claude-swap](https://github.com/realiti4/claude-swap); everything below is on top of its `1.0.0`.

## Unreleased

### Added

- **Codex accounts** with `aswap codex`: `add`, `list`, `switch` (rotate, a named account, `--strategy best|next-available`), `remove`, `status`, `alias`, `disable`/`enable`, `threshold`, and `run`. Switching reads and writes local files only. [docs/codex.md](docs/codex.md)
- **`aswap codex run`**: runs Codex in the same terminal and, when the account is switched from anywhere, reopens it on the new login and resumes the same conversation. POSIX only.
- **Live Codex usage**: for the live account only, a stale or missing reading is replaced by the current numbers from the real `codex app-server`, at most once every five minutes. `usage.codexProbe` (default on) turns it off.
- **Codex refresh**: "check every login" asks the real client about every stored Codex login through a throwaway `CODEX_HOME`, without switching, and keeps each result (`login ok`, `revoked`, `failed`) with its time.
- **Web control panel** (`aswap gui`): a local browser panel with Claude on the left and Codex on the right, the same parts and actions in both, a key for every action and an activity log. It is secured as described in [docs/gui.md](docs/gui.md). Actions include switch, rotate, best, next available, disable, alias, threshold, remove, save login, add account through the browser sign-in, add a token or API key, refresh (all, or one account), and start, stop or dry-run auto-switch per product.
- **Refresh one account**: `u` in the panel, or the account's refresh button, checks that account only.
- **Terminal dashboard shows Codex**: a CODEX panel under the Claude one, and a *Codex accounts…* menu entry (key `c`) with switch, rotate, best pick, sign in to another account, save current login, check every login, disable/enable and remove. Codex uses its own blue, Claude terracotta. [docs/tui.md](docs/tui.md)
- **`aswap setup`**: a guided first-run screen that checks Claude Code and Codex, shows how many logins are saved, and walks through saving the login in use, adding accounts, saving a Codex login, choosing the auto-switch line and opening a dashboard.
- **`install.sh` and `install.ps1`**: one-line install (uv, else pipx, else fetch uv), PATH setup, then `aswap setup`. `install.sh` replaces an old `claude-swap` install. The Windows script is untested.
- **`aswap onboard`**: adds several Claude accounts in one sitting, one `claude auth login` each.
- **`autoswitch.keepalive`** (off by default): one tiny real `claude` request per idle account, at most once every 8 hours, so its login and usage stay warm. It spends a real request. [docs/auto-switch.md](docs/auto-switch.md)
- **`aswap config`** and `settings.json`, with validated keys (`autoswitch.*`, `run.shareHistory`, `ui.theme`, `updates.check`, `usage.headerProbe`, `usage.codexProbe`).
- Documentation: [docs/](docs/README.md) reference pages, [SECURITY.md](SECURITY.md), [CONTRIBUTING.md](CONTRIBUTING.md).

### Changed

- **The project is agent-swap, and the command is `aswap`.** It switches Codex as well as Claude, so it was renamed. `agent-swap`, `cswap` and `claude-swap` still run the same program. The distribution is `agent-swap` and the repository `saurabh-awate96/agent-swap` (GitHub redirects the old URL). The Python package (`claude_swap`), the data folders, the Keychain service and the launchd label keep their old names, so no saved login or running service is orphaned.
- **`aswap gui` can change accounts by default.** `--read-only` gives a view only. `--write` is still accepted and does nothing.
- **Claude and Codex are equal in the web panel**: the same actions under `/api/<provider>/<action>`, pinned by a test.
- **Idle accounts are no longer refreshed in the background.** A lapsed idle token shows as "token expired" and is refreshed on request (`aswap list`, `aswap switch`, the panel's refresh), or when auto-switch must leave the active account and no candidate has readable usage.
- The web panel polls without refreshing idle accounts' tokens, like the terminal dashboard.
- Web panel usage windows are named by their real length: `5h`, `weekly`, `30d` (Codex's `go` plan has one 30-day window and no weekly one).
- The "newer version on PyPI" notice is skipped for installs from a git URL or a folder, since PyPI's release is not theirs.
- The README leads with the one-line install and a screenshot, and links its own docs.

### Fixed

- Web panel: a usage row is a grid, so a long reset text wraps in its own column instead of breaking the row. A per-model window says `weekly · resets …`. A Codex plan with no weekly window says so instead of showing no weekly row.
- Codex usage: the newest reading is found even when a long session ends in one huge line (the scan used to read only the last 256 KB and could fall back to an older session's number). A window that reset since the last reading shows as unknown, not 0%.
- A revoked Codex login is reported as revoked instead of silently showing stale numbers. The terminal dashboard and the web panel refuse to save a revoked login, which would only store a dead copy, and say to sign in again.
- A rotated refresh token survives a malformed refresh response. A non-numeric `expires_in` falls back to an hour.
- Auto-switch no longer overrides a manual switch made while it was deciding. `sequence.json` writes take a lock.
- `aswap run` refuses to re-seed the profile of a live session. Manual rotation skips accounts whose token is dead.
- `import --force` keeps a slot's disabled flag, threshold and priority, and runs under the switch lock. `export` refuses a degraded credential read and adopts a session profile's newer token.
- On macOS a switch with a custom `CLAUDE_CONFIG_DIR` no longer writes the wrong Keychain item.
- Backward clock steps no longer freeze cooldowns, caches or usage ages. An absurd or non-finite `Retry-After` is capped at 7 days. A second 401 in a row backs off for at least an hour, and a 429 with no `Retry-After` for at least five minutes.
- Email validation matches the whole address. `purge` also removes retained `.prev` credential copies. The update check runs `uv` by its absolute path.

### Known gaps

- Codex auto-switch runs only inside the web control panel: there is no `aswap auto` for Codex. It has a fixed five-minute cooldown and no hysteresis.
- `aswap codex add` on the command line does not refuse a revoked login. Only the web panel and the terminal dashboard do.
- Codex's `--json` output (`aswap codex list --json`, `status --json`) has no `schemaVersion` yet.
- `aswap purge` lists no Codex items, so Codex Keychain items (`codex-N`) may remain on macOS. Run `aswap codex remove N` for each account first.
- `aswap codex run` is POSIX only.
- The **add account** sign-in flows have not been run end to end for a second account on a real machine; tests use stand-in commands.
- Plans of different sizes are compared by raw percent, not by headroom.
- A very large macOS credential is passed to `security` as an argument, visible to the same user's processes for a moment.
- The keepalive has not been run against a real account. Whether providers accept scheduled requests is not something aswap can know.
- Codex usage from local files covers this machine only.
- Windows is untested for the sign-in flows, `install.ps1`, and `aswap codex run`.
