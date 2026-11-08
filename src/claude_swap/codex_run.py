"""`aswap codex run`: Codex that follows an account switch without you restarting it.

A running Codex pins its account when it starts and ignores a changed ``auth.json``,
and nothing outside the process can change that. So this runs Codex as a child in the
same terminal and, when the account is switched (from the dashboard, ``aswap codex
switch`` or auto-switch), stops it and starts ``codex resume <session id>`` on the new
login: same terminal, same conversation, no typing. The answer that was streaming at that
moment is interrupted; everything before it is kept.

shortcut: POSIX only (it needs the terminal to be restored after Codex is stopped);
on restart only ``resume <id>`` is passed, not the options of the first start.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from claude_swap.codex import Codex, codex_home
from claude_swap.exceptions import ConfigError

POLL_S = 2.0
STOP_GRACE_S = 8.0
# Leave alternate screen, show the cursor, reset attributes, mouse and bracketed paste.
_TERMINAL_RESET = "\x1b[?1049l\x1b[?25h\x1b[0m\x1b[?1000l\x1b[?2004l"


def supervised_dir(cx: Codex) -> Path:
    return cx.root / "supervised"


def supervised_count(cx: Codex) -> int:
    """How many `aswap codex run` sessions are alive (dead registrations are removed)."""
    alive = 0
    if not hasattr(cx, "root") or not supervised_dir(cx).is_dir():
        return 0
    for f in supervised_dir(cx).glob("*.json"):
        try:
            pid = int(f.stem)
            os.kill(pid, 0)
        except PermissionError:
            alive += 1
        except (ValueError, OSError):
            with contextlib.suppress(OSError):
                f.unlink()
        else:
            alive += 1
    return alive


class Supervisor:
    def __init__(self, cx: Codex, args: list[str], *, poll: float = POLL_S, grace: float = STOP_GRACE_S,
                 binary: str | None = None, out=None):
        self.cx, self.args, self.poll, self.grace, self._out = cx, list(args), poll, grace, out
        self.binary = binary or shutil.which("codex")
        self.cwd = os.getcwd()
        self._term = None
        self._stopping = False
        self._child: subprocess.Popen | None = None

    # -- the account-changed marker ------------------------------------------------------

    def _stamp(self):
        try:
            st = (self.cx.root / "switched.json").stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    # -- the session to resume ---------------------------------------------------------------

    def _session_id(self, since: float) -> str | None:
        """The newest session this terminal's Codex wrote since it started, by working directory."""
        files = sorted((codex_home() / "sessions").rglob("rollout-*.jsonl"),
                       key=lambda f: f.stat().st_mtime, reverse=True)[:12]
        for f in files:
            try:
                if f.stat().st_mtime < since - 2:
                    break
                with f.open("rb") as fh:
                    meta = json.loads(fh.readline())["payload"]
                if os.path.realpath(meta.get("cwd", "")) == os.path.realpath(self.cwd) and meta.get("id"):
                    return str(meta["id"])
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return None

    # -- the terminal -------------------------------------------------------------------------------

    def _save_terminal(self) -> None:
        try:
            import termios
            self._term = termios.tcgetattr(sys.stdin.fileno()) if sys.stdin.isatty() else None
        except (ImportError, OSError, ValueError):
            self._term = None

    def _restore_terminal(self) -> None:
        if self._term is None:
            return
        with contextlib.suppress(Exception):
            import termios
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._term)
            sys.stdout.write(_TERMINAL_RESET)
            sys.stdout.flush()

    def _stop(self, proc: subprocess.Popen) -> None:
        proc.terminate()
        try:
            proc.wait(self.grace)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        self._restore_terminal()

    # -- the loop --------------------------------------------------------------------------------------

    def _register(self) -> Path:
        d = supervised_dir(self.cx)
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{os.getpid()}.json"
        f.write_text(json.dumps({"cwd": self.cwd, "started": time.time()}), encoding="utf-8")
        return f

    def run(self) -> int:
        if self.binary is None:
            raise ConfigError("Codex (`codex`) was not found on PATH")
        if sys.platform == "win32":
            raise ConfigError("`codex run` is not available on Windows yet")
        registration = self._register()
        self._save_terminal()
        # A handler (not SIG_IGN): ignoring SIGINT would be inherited by Codex across exec.
        old_int = signal.signal(signal.SIGINT, lambda *_: None)
        old_term = signal.signal(signal.SIGTERM, self._forward_term)
        seen = self._stamp()
        args = list(self.args)
        try:
            while True:
                launched = time.time()
                self._child = proc = subprocess.Popen([self.binary, *args])
                restart = False
                while True:
                    try:
                        proc.wait(self.poll)
                        break
                    except subprocess.TimeoutExpired:
                        pass
                    stamp = self._stamp()
                    if stamp != seen:
                        seen, restart = stamp, True
                        self._stop(proc)
                        break
                if not restart or self._stopping:
                    return proc.returncode if proc.returncode is not None else 0
                sid = self._session_id(launched)
                args = ["resume", sid] if sid else []
                print("\nagent-swap: account switched; reopening Codex on the new login"
                      + (" and resuming this conversation" if sid else "") + "…", file=self._out or sys.stderr, flush=True)
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)
            with contextlib.suppress(OSError):
                registration.unlink()

    def _forward_term(self, *_):
        self._stopping = True
        if self._child is not None and self._child.poll() is None:
            self._child.terminate()
