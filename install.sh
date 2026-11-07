#!/bin/sh
# agent-swap one-line installer (macOS / Linux). Gives you the aswap command (cswap still works).
#
#   curl -LsSf https://raw.githubusercontent.com/saurabh-awate96/agent-swap/main/install.sh | sh
#
# What it does: installs agent-swap with uv (or pipx if that is what you have),
# puts it on your PATH, then opens `cswap setup` so you can save your logins.
# It never reads or changes a Claude/Codex login itself.
#
# Knobs (all optional):
#   CSWAP_SPEC      what to install: a path, a git+https URL or a PyPI name
#                   (default: this checkout if run from one, else CSWAP_REPO)
#   CSWAP_REPO      git URL to install from
#   CSWAP_NO_SETUP  set to skip the guided setup at the end

set -eu

REPO="${CSWAP_REPO:-https://github.com/saurabh-awate96/agent-swap.git}"

if [ -t 1 ]; then A="$(printf '\033[38;5;173m')"; D="$(printf '\033[2m')"; R="$(printf '\033[0m')"; else A=""; D=""; R=""; fi
say()  { printf '%s\n' "  $A●$R $*"; }
note() { printf '%s\n' "    $D$*$R"; }
die()  { printf '%s\n' "  ✗ $*" >&2; exit 1; }

# 1. Where the code comes from.
if [ -n "${CSWAP_SPEC:-}" ]; then
  SPEC="$CSWAP_SPEC"
elif [ -f "$0" ] && here="$(cd "$(dirname "$0")" && pwd)" && [ -f "$here/pyproject.toml" ] \
     && grep -q '^name = "agent-swap"' "$here/pyproject.toml"; then
  SPEC="$here"
else
  SPEC="git+$REPO"
fi

printf '\n  %sagent-swap%s %s· installer%s\n\n' "$A" "$R" "$D" "$R"

# 2. A tool installer: uv, else pipx, else fetch uv (astral's official script).
if command -v uv >/dev/null 2>&1; then
  TOOL=uv
elif command -v pipx >/dev/null 2>&1; then
  TOOL=pipx
else
  command -v curl >/dev/null 2>&1 || die "Need uv, pipx or curl to continue."
  say "Installing uv (the Python tool installer) from astral.sh"
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 || die "Could not install uv. Install it from https://docs.astral.sh/uv/ and re-run."
  PATH="$HOME/.local/bin:$PATH"; export PATH
  command -v uv >/dev/null 2>&1 || die "uv installed but is not on PATH; open a new terminal and re-run."
  TOOL=uv
fi

# 3. Install. The project used to be called claude-swap and both provide `cswap`,
#    so an old install is replaced (your saved logins live elsewhere and stay).
if [ "$TOOL" = uv ]; then
  if uv tool list 2>/dev/null | grep -q '^claude-swap '; then note "replacing the old claude-swap install"; uv tool uninstall claude-swap >/dev/null 2>&1 || true; fi
elif pipx list --short 2>/dev/null | grep -q '^claude-swap '; then
  note "replacing the old claude-swap install"; pipx uninstall claude-swap >/dev/null 2>&1 || true
fi

# A re-run is an upgrade. --reinstall matters for a local checkout:
#    uv caches that build and would otherwise reinstall the stale copy.
say "Installing agent-swap with $TOOL"
note "from $SPEC"
if [ "$TOOL" = uv ]; then
  uv tool install --force --reinstall --quiet --from "$SPEC" agent-swap || die "Install failed (see the message above)."
  BIN="$(uv tool dir --bin)"
else
  pipx install --force "$SPEC" >/dev/null || die "Install failed. pipx needs Python 3.12 or newer."
  BIN="$(pipx environment --value PIPX_BIN_DIR)"
fi
[ -x "$BIN/cswap" ] || die "Installed, but $BIN/cswap is missing."

# 4. PATH, so `cswap` works in new terminals.
case ":$PATH:" in
  *":$BIN:"*) ;;
  *)
    say "Adding $BIN to your shell PATH (edits your shell profile)"
    if [ "$TOOL" = uv ]; then uv tool update-shell >/dev/null 2>&1 || note "Add it yourself: export PATH=\"$BIN:\$PATH\""
    else pipx ensurepath >/dev/null 2>&1 || note "Add it yourself: export PATH=\"$BIN:\$PATH\""; fi
    note "Open a new terminal, or run: export PATH=\"$BIN:\$PATH\""
    ;;
esac
say "Installed: $("$BIN/aswap" --version 2>/dev/null || echo cswap)"

# 5. Guided setup. Under `curl | sh` stdin is the script, so read the keyboard
#    from the terminal; with no terminal at all (CI), just say what to run.
if [ -z "${CSWAP_NO_SETUP:-}" ] && ( : </dev/tty ) 2>/dev/null; then
  printf '\n'
  exec "$BIN/cswap" setup </dev/tty
fi
printf '\n  Next: %scswap setup%s\n\n' "$A" "$R"
