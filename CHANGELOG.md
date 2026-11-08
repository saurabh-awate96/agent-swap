# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/). This fork tracks
[realiti4/claude-swap](https://github.com/realiti4/claude-swap); everything below is on top of its `1.0.0`.

## Unreleased

### Renamed

- The project is now **agent-swap** (it switches Codex as well as Claude). The command is now `aswap`; `agent-swap`, `cswap` and `claude-swap` still work, and every doc and message uses `aswap`. The distribution is `agent-swap`, the repository `saurabh-awate96/agent-swap` (GitHub redirects the old URL). The Python package (`claude_swap`), the data folders, the Keychain service and the launchd label keep their old names so no saved login or running service is orphaned.

### Added

- **Refresh**: a button (and `f`) in the control panel that checks every account of both providers and lists each one's state in the activity log. For Codex it asks the real client about every stored login through a throwaway `CODEX_HOME`, without switching, saves back a token the client refreshed, and keeps `login ok / revoked` with its time on each card.
- **A sturdier control panel**: a cursor row instead of a wall of buttons (select an account; only it shows actions), a key for every action with a footer of hints, an activity log that stays on screen, a clear stale state when the dashboard stops (requests time out, out-of-order answers are ignored), no rebuild under an open dialog or a click, usage windows named by their real length (`30d` for Codex's go plan, not `5h`), the duplicate organisation tag dropped, and a revoked Codex login reported instead of silently showing stale numbers.
- **`aswap codex run`**: Codex that follows an account switch. It runs Codex as a child in the same terminal and, when the account is switched from anywhere, reopens it on the new login with `codex resume` of the same conversation. The Codex panel says how many sessions do this and warns about a Codex running outside it. POSIX only.
- **Claude and Codex are equal in the control panel**: two panels with the same parts in the same order and the same actions (`/api/<provider>/<action>`, pinned by a test). Codex gained alias, disable/enable, per-account threshold, `next-available`, an auto-switch engine and API-key login; `aswap codex alias|disable|enable|threshold` exist on the command line too.
- **Add account works on its own**: it saves the login in use first, takes an optional email for Claude, offers `codex login --device-auth`, and shows the sign-in link and device code in the panel when no browser window opens.
- **`aswap setup`**: a guided first-run screen. Checks Claude Code and Codex, shows how many logins are saved, and walks through saving the current login, adding more accounts, saving a Codex login, picking the auto-switch point and opening a dashboard. Arrow keys, or a number.
- **`install.sh` / `install.ps1`**: one-line install (uv, pipx, or fetch uv), PATH setup, then `aswap setup`. The Windows script is untested.
- **`aswap onboard`**: add several of your own accounts in one sitting, one `claude auth login` each.
- **`aswap codex add|list|switch|remove|status`**: OpenAI Codex CLI account switching from local files, with usage read from Codex's own session logs and `switch --strategy best`. [docs/codex.md](docs/codex.md)
- **`aswap gui`**: a local web control panel in the terminal dashboard's layout, showing Claude and Codex accounts, with buttons to switch, rotate, pick the best, disable, remove, alias, set a threshold, save the login in use, add an account through the browser sign-in, add a token, and start/stop/configure auto-switch. [docs/gui.md](docs/gui.md)
- **`autoswitch.keepalive`** (off by default): one tiny real `claude` request per idle account, at most every 8 hours, so its login and usage stay warm without aswap calling the token endpoint.
- [docs/safety.md](docs/safety.md), [SECURITY.md](SECURITY.md), this changelog.

- **Live Codex usage.** For the live account only, a stale or missing 5h/weekly reading (an idle account, or a window that reset since) is replaced by the current numbers from the real `codex app-server`, at most once every five minutes. Setting `usage.codexProbe` (default on) turns it off. The dashboard note says "read from Codex".
- The web dashboard shows the login you are using right now when it is not saved yet (Claude and Codex), instead of looking empty.

### Changed

- **`aswap gui` can change accounts by default**; `--read-only` gives a view only. `--write` is still accepted and does nothing. A read-only page had no buttons and nothing to do. A stopped server now says so on the page instead of "Failed to fetch".

- **Idle accounts are no longer refreshed in the background.** A lapsed idle token shows as "token expired" and is refreshed on demand (`aswap list`, `aswap switch`), or when auto-switch must leave the active account and no candidate has readable usage.
- The web dashboard polls without refreshing idle accounts' tokens, like the terminal dashboard (`list_accounts(refresh_idle=False)`).
- The "newer version on PyPI" notice is skipped for installs from a git URL or a folder, since PyPI's release is not theirs.
- The README leads with the one-line install and a screenshot, and links its own docs rather than upstream's.

### Fixed

- Codex usage: the newest reading is found even when a long session ends in one huge line (the scan used to read only the last 256 KB and silently fell back to an older session's number). A window that reset since the last reading shows as unknown in the web dashboard, not 0%, and the last-seen line says the numbers cover this machine only.
- A rotated refresh token survives a malformed refresh response; a non-numeric `expires_in` falls back to an hour.
- Auto-switch no longer overrides a manual switch made while it was deciding, and sequence.json writes take a lock.
- `aswap run` refuses to re-seed the profile of a live session; manual rotation skips accounts whose token is dead.
- `import --force` keeps a slot's disabled flag, threshold and priority and runs under the switch lock; `export` refuses a degraded credential read and adopts a session profile's newer token.
- On macOS a switch with a custom `CLAUDE_CONFIG_DIR` no longer writes the wrong Keychain item.
- Backward clock steps no longer freeze cooldowns, caches or usage ages; an absurd or non-finite `Retry-After` is capped at 7 days; a second 401 in a row backs off for at least an hour, and a 429 with no `Retry-After` for at least five minutes.
- Email validation matches the whole address; `purge` also removes retained `.prev` credential copies; the update check runs `uv` by absolute path.

### Known gaps

- Codex auto-switch runs only inside the web control panel (no `aswap auto` for it) and has a fixed five-minute cooldown, no hysteresis.
- The **add account** sign-in flows have never been run end to end for a second account on this machine (tests use stand-in commands).
- Plans of different sizes are compared by raw percent, not by headroom.
- A very large macOS credential is passed to `security` as an argument, visible to the same user's processes for a moment.
- The keepalive has not been run against a real account, and whether providers accept scheduled requests is not something aswap can know.
- Windows is untested for everything on this list.
