# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/). This fork tracks
[realiti4/claude-swap](https://github.com/realiti4/claude-swap); everything below is on top of its `1.0.0`.

## Unreleased

### Renamed

- The project is now **agent-swap** (it switches Codex as well as Claude). New command `aswap`; `agent-swap`, `cswap` and `claude-swap` still work. The distribution is `agent-swap`, the repository `saurabh-awate96/agent-swap` (GitHub redirects the old URL). The Python package (`claude_swap`), the data folders, the Keychain service and the launchd label keep their old names so no saved login or running service is orphaned.

### Added

- **`cswap setup`**: a guided first-run screen. Checks Claude Code and Codex, shows how many logins are saved, and walks through saving the current login, adding more accounts, saving a Codex login, picking the auto-switch point and opening a dashboard. Arrow keys, or a number.
- **`install.sh` / `install.ps1`**: one-line install (uv, pipx, or fetch uv), PATH setup, then `cswap setup`. The Windows script is untested.
- **`cswap onboard`**: add several of your own accounts in one sitting, one `claude auth login` each.
- **`cswap codex add|list|switch|remove|status`**: OpenAI Codex CLI account switching from local files, with usage read from Codex's own session logs and `switch --strategy best`. [docs/codex.md](docs/codex.md)
- **`cswap gui`**: a local web control panel in the terminal dashboard's layout, showing Claude and Codex accounts, with buttons to switch, rotate, pick the best, disable, remove, alias, set a threshold, save the login in use, add an account through the browser sign-in, add a token, and start/stop/configure auto-switch. [docs/gui.md](docs/gui.md)
- **`autoswitch.keepalive`** (off by default): one tiny real `claude` request per idle account, at most every 8 hours, so its login and usage stay warm without cswap calling the token endpoint.
- [docs/safety.md](docs/safety.md), [SECURITY.md](SECURITY.md), this changelog.

- **Live Codex usage.** For the live account only, a stale or missing 5h/weekly reading (an idle account, or a window that reset since) is replaced by the current numbers from the real `codex app-server`, at most once every five minutes. Setting `usage.codexProbe` (default on) turns it off. The dashboard note says "read from Codex".
- The web dashboard shows the login you are using right now when it is not saved yet (Claude and Codex), instead of looking empty.

### Changed

- **`cswap gui` can change accounts by default**; `--read-only` gives a view only. `--write` is still accepted and does nothing. A read-only page had no buttons and nothing to do. A stopped server now says so on the page instead of "Failed to fetch".

- **Idle accounts are no longer refreshed in the background.** A lapsed idle token shows as "token expired" and is refreshed on demand (`cswap list`, `cswap switch`), or when auto-switch must leave the active account and no candidate has readable usage.
- The web dashboard polls without refreshing idle accounts' tokens, like the terminal dashboard (`list_accounts(refresh_idle=False)`).
- The "newer version on PyPI" notice is skipped for installs from a git URL or a folder, since PyPI's release is not theirs.
- The README leads with the one-line install and a screenshot, and links its own docs rather than upstream's.

### Fixed

- Codex usage: the newest reading is found even when a long session ends in one huge line (the scan used to read only the last 256 KB and silently fell back to an older session's number). A window that reset since the last reading shows as unknown in the web dashboard, not 0%, and the last-seen line says the numbers cover this machine only.
- A rotated refresh token survives a malformed refresh response; a non-numeric `expires_in` falls back to an hour.
- Auto-switch no longer overrides a manual switch made while it was deciding, and sequence.json writes take a lock.
- `cswap run` refuses to re-seed the profile of a live session; manual rotation skips accounts whose token is dead.
- `import --force` keeps a slot's disabled flag, threshold and priority and runs under the switch lock; `export` refuses a degraded credential read and adopts a session profile's newer token.
- On macOS a switch with a custom `CLAUDE_CONFIG_DIR` no longer writes the wrong Keychain item.
- Backward clock steps no longer freeze cooldowns, caches or usage ages; an absurd or non-finite `Retry-After` is capped at 7 days; a second 401 in a row backs off for at least an hour, and a 429 with no `Retry-After` for at least five minutes.
- Email validation matches the whole address; `purge` also removes retained `.prev` credential copies; the update check runs `uv` by absolute path.

### Known gaps

- Auto-switch for Codex is not built; `cswap codex switch` is manual.
- Plans of different sizes are compared by raw percent, not by headroom.
- A very large macOS credential is passed to `security` as an argument, visible to the same user's processes for a moment.
- The keepalive has not been run against a real account, and whether providers accept scheduled requests is not something cswap can know.
- Windows is untested for everything on this list.
