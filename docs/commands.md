# Command reference

Every command, with the flags people use most. `aswap --help` and `aswap <command> --help` list all of them. Codex commands are under `aswap codex` and are covered in [codex.md](codex.md); the dashboards are in [gui.md](gui.md) and [tui.md](tui.md).

Accounts are named by **NUM** (the slot number), **EMAIL** or **ALIAS** wherever a command takes a target.

The original flag spellings (`aswap --switch`, `aswap --list`, ...) keep working.

## Setup

### aswap setup

```bash
aswap setup
```

A guided first-run screen. It checks Claude Code and Codex, shows how many logins are saved, and walks you through saving the login you are on, adding more Claude accounts, saving a Codex login, choosing the auto-switch line and opening a dashboard. Arrow keys and Enter, or press a number. It only calls the commands listed here, so it is safe to re-run.

### aswap onboard

```bash
aswap onboard
```

Adds several of your own Claude accounts in one sitting. For each one it asks for the email, runs `claude auth login --email …` (you sign in in the browser) and saves the login. It never logs anything out.

## Accounts

### Add accounts

```bash
aswap add                       # save the login Claude Code is using now
aswap add --slot 3              # into a specific slot (asks before overwriting)
aswap add --alias dev           # and give it a short name
aswap add-token sk-ant-oat01-…  # a setup token, without a browser login
aswap add-token sk-ant-api03-…  # a Console API key
aswap add-token - --slot 3      # read the token from stdin
aswap add-token --email user@example.com
```

`add-token` detects the token type. `--email` is optional; without it the account is labelled `setup-token-{slot}@token.local` (or `api-key-{slot}@token.local`). No Anthropic API call is made to add a token.

Setup tokens show `usage unavailable` unless `usage.headerProbe` is on (it sends a tiny real request per check). API-key accounts have no subscription quota, so they show no usage and are never rotated onto unless `--include-api-key-accounts` is given to `aswap auto`.

Third-party providers configured with `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` are not managed. Those variables stay in effect after a switch, so unset them.

## Switch and inspect

```bash
aswap switch                    # rotate to the next account
aswap switch 2                  # jump to account 2 (NUM, EMAIL or ALIAS)
aswap switch --strategy best    # the account with the most quota left
aswap switch --strategy next-available   # rotate, skipping accounts at their limit
aswap switch 2 --force          # activate it without backing up the login in use first
aswap list                      # accounts with plan, 5h and 7d usage and reset times
aswap list --token-status       # plus source-labelled OAuth token diagnostics
aswap status                    # the account in use
```

Rotating (`aswap switch` with no target, and `--strategy`) skips disabled accounts. `--strategy` takes `best` (most quota left) or `next-available`; `--model NAMES` also counts per-model weekly limits.

A switch takes Claude Code's own credential locks, so it never collides with a token refresh. On macOS Claude Code reads the new login after its Keychain cache (about 30 seconds) expires; see [the README](../README.md#do-i-need-to-restart-after-switching).

### Account settings

```bash
aswap disable 2                 # hold out of rotation (keeps its login)
aswap enable 2                  # return it to rotation
aswap alias 2 dev               # short name, usable wherever NUM|EMAIL is
aswap alias 2 --unset
aswap alias                     # list all aliases
aswap threshold 2 85            # switch away from account 2 at 85%
aswap threshold 2 --unset       # back to the global line
aswap threshold                 # show the global line and every override
aswap priority 2 100            # tier for priority-consume-first (0-1000, default 0)
aswap priority 2 --unset
aswap priority                  # every account's tier
```

Disabled accounts are skipped by `switch` (rotate and `best`), `auto`, and the `best` and `next-available` strategies. They stay a valid explicit target for `aswap switch 2`.

### Reorder and remove

```bash
aswap move 2 1                  # assign account 2 to slot 1 (moves to an empty slot, swaps if taken)
aswap swap 2 3                  # exchange two accounts' slot numbers
aswap remove 2                  # remove an account (alias: rm)
aswap unclaimed                 # stashed credential entries and why they were stashed
aswap unclaimed --purge ID      # delete one (recover it with /login and aswap add)
```

## Auto-switch

```bash
aswap auto                              # foreground loop, checks every 60 seconds
aswap auto --threshold 80               # switch earlier
aswap auto --weekly-threshold 97        # its own line for the 7-day and per-model windows
aswap auto --model Fable                # also switch when that model's weekly limit is hit
aswap auto --strategy weekly-first      # best, weekly-first, consume-first or priority-consume-first
aswap auto --interval 120               # seconds between checks (15 to 3600)
aswap auto --cooldown 600               # seconds between proactive switches
aswap auto --once                       # one check, maybe switch, then exit
aswap auto --dry-run                    # report what it would do, never switch
aswap auto --once --json                # one JSON event per line, for scripts
```

`--once` exits `0` when it switched, `1` on error, `2` when there was nothing to do and `3` when it wanted to switch but had no viable target. The rules behind each flag are in [auto-switch.md](auto-switch.md). The engine also runs inside the web control panel for Claude and Codex.

## aswap run

Session mode: Claude Code as one account in this terminal.

```bash
aswap run 2                     # Claude Code as account 2, in this terminal only
aswap run user@example.com      # by email
aswap run 2 -- --resume         # everything after -- goes to claude
aswap run 2 --no-share-history  # this account keeps its own chat history for this launch
aswap run 2 --no-share          # do not share ~/.claude settings, skills or CLAUDE.md
aswap run 2 --require-session   # refuse instead of running the default login
aswap run                       # no account: use the directory's mapping, else the default login
```

Other terminals and the VS Code extension stay on your default account, so two accounts can work in parallel.

- **Chat history** is shared by default: every session reads and writes the history in `~/.claude`. History an account kept on its own is merged in on its next launch, and nothing already saved is lost; a file that differs on both sides is kept in `history-conflicts/` in the backup directory. `aswap config set run.shareHistory false` gives each account its own history.
- **Windows** shares conversations through a directory junction, which needs no special rights. `history.jsonl` is shared only when Windows allows symlinks (Developer Mode or an elevated terminal).
- **MCP servers.** User-scope MCP servers (`claude mcp add -s user`) are copied from your default profile on every launch, so manage them there. MCP OAuth logins are not copied; HTTP servers may ask you to authenticate once per profile through `/mcp`.
- **Your default account.** Running the account that is already your default login launches plain `claude` on it, because a second copy of that credential would go stale. `--require-session` refuses instead.
- **Tokens.** A session refreshes its own copy of the token. When it exits, the rotated credential is saved back into the account before any switch, swap, move or usage check uses it. While a session runs, `aswap switch` will not move the default login onto its account if the stored copy is behind.
- **Usage.** While a session runs, its account's usage is read with the session's own credential, which aswap never refreshes.

### Map a directory to an account

```bash
aswap map 2 ~/work/client-app   # map a directory to account 2
aswap map user@example.com      # map the current directory
aswap map                       # list mappings
aswap unmap ~/work/client-app   # remove one (default: the current directory)
```

With a mapping, a bare `aswap run` in that directory (or any subfolder) launches that account in session mode. Unmapped directories launch plain `claude` on the default login. Mappings are per machine, are not part of `aswap export`, and are removed with their account.

## Backup and move between machines

```bash
aswap export backup.cswap                # every account, plaintext JSON
aswap export backup.cswap --account 2    # one account
aswap export backup.cswap --full         # also ~/.claude.json and the full credential (same-PC backups)
aswap import backup.cswap                # skips accounts that already exist
aswap import backup.cswap --force        # overwrite existing accounts
aswap export - | gpg -c > backup.gpg     # encrypt by piping through your own tool
```

The export is plaintext. By default it carries only each account's own login; machine-shared MCP and plugin OAuth tokens and the device token stay on the source machine. If an imported account is the one you are logged into, run `aswap switch N --force` to activate it.

A plain `aswap import` also replaces accounts whose refresh token has died, because a dead copy is worth nothing.

### Share usage readings between machines

Machines that hold the same logins can share one usage-endpoint rate limit. `import-usage` lets one machine poll and hand its readings to the others:

```bash
aswap list --json | ssh laptop aswap import-usage - --hold 600
```

`--hold SECONDS` stops the receiving machine fetching those accounts for that long. It never runs past the reading's earliest window reset, nor past an hour. Renew it with each hand-over, or lift it with `--hold 0`.

## Dashboards

```bash
aswap                 # the terminal dashboard, in an interactive terminal (same as aswap tui)
aswap tui             # the terminal dashboard
aswap watch           # the dashboard, opened on the live monitor
aswap gui             # the web control panel (see gui.md)
aswap gui --read-only --no-open --port 8765
aswap menubar         # the macOS menu bar app (see menubar.md)
```

## Settings

```bash
aswap config                                  # every setting and its effective value
aswap config get autoswitch.threshold
aswap config set autoswitch.threshold 80      # validated against the range
aswap config unset autoswitch.threshold
aswap config path
aswap config list --json                      # list and get take --json
```

`aswap config --help` lists every key with its range and default. Hand-editing `settings.json` also works; invalid values in it fall back to the defaults with a logged warning.

## Updates and removal

```bash
aswap upgrade          # uv and pipx installs on macOS and Linux upgrade themselves
aswap purge            # remove agent-swap's stored logins and data (see the README's Uninstall)
```

`aswap upgrade` on Windows prints the command to run instead. Installs from a git URL or a folder upgrade from that same source, and the PyPI "newer version" notice stays off for them.

## Version and help

```bash
aswap --version
aswap help
```
