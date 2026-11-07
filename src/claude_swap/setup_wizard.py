"""`cswap setup`: one guided screen that gets a fresh install fully working.

A status board (what is installed, how many accounts are saved) above a menu.
Each menu entry runs the same code the matching command runs; nothing here
touches credentials itself. Arrow keys + Enter, a number, or `q`. Where raw
keys are not available (pipe, Windows, no tty) it falls back to typing the
number, so it also works over `curl | sh` once the installer hands it a tty.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys

from claude_swap import printer
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.printer import accent, bolded, dimmed, muted, warning

_CLEAR = "\033[H\033[2J"
_UP, _DOWN, _ENTER, _QUIT = "\x1b[A", "\x1b[B", "\r", "q"


def _version(binary: str) -> str | None:
    """`<binary> --version` first line, or None when it is not installed."""
    path = shutil.which(binary)
    if path is None:
        return None
    try:
        out = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=5
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "found"
    return out.splitlines()[0] if out else "found"


def _read_key() -> str:
    """One keypress (arrow keys come back as their escape sequence)."""
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        ch = os.read(fd, 1).decode("utf-8", "ignore")
        if ch == "\x1b":  # arrow keys arrive as ESC [ A/B within a few ms
            ch += os.read(fd, 2).decode("utf-8", "ignore")
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _raw_keys_ok() -> bool:
    if sys.platform == "win32" or not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    try:
        import termios  # noqa: F401
    except ImportError:
        return False
    return True


def _menu(items: list[tuple[str, str]], read_key=None) -> str:
    """Show `items` ([(key, label)]) and return the chosen key.

    `q` is always an exit and returns "q". Re-draws in place on each arrow key.
    """
    keys = [k for k, _ in items]
    if read_key is None and not _raw_keys_ok():
        while True:
            try:
                typed = input("Choose [" + "/".join(keys) + "/q]: ").strip().lower()
            except EOFError:
                return "q"
            if typed in keys or typed == "q":
                return typed
            warning(f"'{typed}' is not one of the choices.")

    read = read_key or _read_key
    at = 0

    def draw(first: bool) -> None:
        if not first:
            sys.stdout.write(f"\033[{len(items)}A")
        for i, (key, label) in enumerate(items):
            marker = accent("❯") if i == at else " "
            text = bolded(label) if i == at else label
            sys.stdout.write(f"\033[2K  {marker} {accent(key)}  {text}\n")
        sys.stdout.flush()

    draw(True)
    while True:
        k = read()
        if k in (_UP, "k"):
            at = (at - 1) % len(items)
        elif k in (_DOWN, "j"):
            at = (at + 1) % len(items)
        elif k in (_ENTER, "\n"):
            return keys[at]
        elif k in (_QUIT, "\x03", "\x04"):
            return "q"
        elif k in keys:
            return k
        else:
            continue
        draw(False)


def _counts(switcher) -> tuple[int, str | None, int]:
    """(Claude accounts saved, active Claude email, Codex accounts saved)."""
    claude, active = 0, None
    with contextlib.suppress(Exception):
        data = switcher._get_sequence_data() or {}
        accounts = data.get("accounts") or {}
        claude = len(accounts)
        current = accounts.get(str(data.get("activeAccountNumber")))
        active = current.get("email") if current else None
    codex_n = 0
    with contextlib.suppress(Exception):
        from claude_swap.codex import Codex

        codex_n = len(Codex()._load()["accounts"])
    return claude, active, codex_n


def _board(switcher, claude_v: str | None, codex_v: str | None) -> None:
    claude_n, active, codex_n = _counts(switcher)

    def line(ok: bool | None, name: str, detail: str) -> str:
        mark = accent("✓") if ok else (muted("○") if ok is None else printer.yellowed("!"))
        return f"  {mark} {bolded(name.ljust(14))} {detail}"

    print(line(bool(claude_v), "Claude Code", claude_v or "not found — install Claude Code first"))
    print(line(bool(codex_v) or None, "Codex CLI", codex_v or dimmed("not installed (optional)")))
    print()
    saved = f"{claude_n} saved" + (f" · active {active}" if active else "")
    print(line(claude_n > 0 or None, "Claude logins", saved if claude_n else dimmed("none yet")))
    if codex_v or codex_n:
        print(line(codex_n > 0 or None, "Codex logins", f"{codex_n} saved" if codex_n else dimmed("none yet")))
    print()


def _auto_switch_step(backup_root) -> None:
    from claude_swap.settings import load_settings, set_setting

    current = load_settings(backup_root).threshold
    print("  Auto-switch hops to your next account when the active one reaches a usage %.")
    print(dimmed(f"  Now: {current}%. Earlier = safer, later = squeezes each account more."))
    pick = input(f"  Switch at what % ? [Enter keeps {current}] ").strip()
    if not pick:
        return
    try:
        set_setting(backup_root, "autoswitch.threshold", pick)
    except Exception as e:  # settings validates the range and wording
        warning(str(e))
        return
    print(f"  {accent('Saved.')} Start it any time with: {bolded('cswap auto')}")


def _dashboard_step(switcher) -> None:
    from claude_swap import cli

    allow = input("  Let the web page switch accounts too? [y/N] ").strip().lower() in ("y", "yes")
    print(dimmed("  Ctrl+C stops the dashboard and brings you back here."))
    argv = ["--write"] if allow else []
    with contextlib.suppress(KeyboardInterrupt, SystemExit):
        cli._gui_command(argv)


def _tui_step(switcher) -> None:
    from claude_swap.tui import run as tui_run

    tui_run(switcher)


def run(switcher, *, read_key=None) -> int:
    """The wizard loop. Returns a process exit code."""
    from claude_swap import cli, paths
    from claude_swap.codex import Codex

    interactive = read_key is not None or _raw_keys_ok()
    claude_v, codex_v = _version("claude"), _version("codex")
    notice = ""
    while True:
        if interactive and printer.colors_enabled():
            sys.stdout.write(_CLEAR)
        print()
        print(f"  {printer.bold_accent('agent-swap')} {muted('· setup')}")
        print(dimmed("  Every Claude and Codex account you own, one keystroke apart."))
        print()
        _board(switcher, claude_v, codex_v)
        if notice:
            print(f"  {notice}\n")
            notice = ""
        items = [
            ("1", "Save the account I'm logged into right now"),
            ("2", "Add more Claude accounts (guided, one login each)"),
        ]
        if codex_v:
            items.append(("3", "Save my Codex login"))
        items += [
            ("4", "Auto-switch: choose when to hop to the next account"),
            ("5", "Open the web dashboard"),
            ("6", "Open the terminal dashboard"),
            ("q", "Finish"),
        ]
        print(dimmed("  ↑/↓ and Enter, or press a number"))
        pick = _menu(items, read_key)
        print()
        try:
            if pick == "q":
                break
            if pick == "1":
                switcher.add_account()
                notice = accent("Saved your current Claude login.")
            elif pick == "2":
                cli._onboard_accounts(switcher)
            elif pick == "3":
                slot, created = Codex().add()
                notice = accent(f"{'Added' if created else 'Refreshed'} Codex slot {slot}.")
            elif pick == "4":
                _auto_switch_step(paths.get_backup_root())
            elif pick == "5":
                _dashboard_step(switcher)
            elif pick == "6":
                _tui_step(switcher)
        except ClaudeSwitchError as e:
            notice = printer.yellowed(f"{e}")
        except (KeyboardInterrupt, EOFError):
            notice = dimmed("Cancelled.")
        if pick in ("2", "4"):
            with contextlib.suppress(EOFError, KeyboardInterrupt):
                input(dimmed("  Press Enter to continue "))

    print(f"  {bolded('You are set.')} The commands you will use:")
    for cmd, what in (
        ("cswap", "terminal dashboard"),
        ("cswap gui", "web dashboard"),
        ("cswap switch", "rotate to the next account"),
        ("cswap auto", "switch for you before a limit hits"),
        ("cswap codex switch", "same, for Codex"),
    ):
        print(f"    {accent(cmd.ljust(20))}{dimmed(what)}")
    print()
    return 0
