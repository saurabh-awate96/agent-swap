"""Long-running work the dashboard starts: the auto-switch engine and browser logins.

Both live in the dashboard's own process and stop with it. Neither takes a value
from the page that could reach a shell: the engine reads its settings from
settings.json, and a login runs one fixed command (``claude auth login`` or
``codex login``) whose only variable part is an email address already checked
against the switcher's own pattern.
"""

from __future__ import annotations

import collections
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable

LOGIN_TIMEOUT_S = 300
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_URL = re.compile(r"https://[^\s\"'<>)]+")
_CODE = re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4,6}\b")  # a device-login code, like ABCD-1234


_LAUNCHERS = ("open", "xdg-open", "sensible-browser", "x-www-browser", "gnome-open", "wslview")


def no_browser_env(shim_dir: str) -> dict[str, str]:
    """An environment in which a login cannot open a browser: it prints its link instead, and the link is
    copied for the user to paste where they want. Browser launchers found on PATH do nothing.

    shortcut: POSIX only, and a client that calls the OS directly (Codex on macOS) ignores it; Codex signs in by device code.
    """
    for name in _LAUNCHERS:
        path = os.path.join(shim_dir, name)
        with open(path, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(path, 0o755)
    return {**os.environ, "PATH": shim_dir + os.pathsep + os.environ.get("PATH", ""), "BROWSER": "true"}


def copy_to_clipboard(text: str) -> bool:
    """Put ``text`` on the system clipboard with the platform's own tool. False if none worked.

    Done by the dashboard's process, not the page: a browser only lets a page write the
    clipboard right after a click, and the sign-in link arrives later, from a poll.
    """
    for argv in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"],
                 ["xsel", "--clipboard", "--input"], ["clip"]):
        if shutil.which(argv[0]) is None:
            continue
        try:
            subprocess.run(argv, input=text, text=True, timeout=5, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False


class AutoRunner:
    """Runs the auto-switch engine (the same one `aswap auto` runs) in a thread.

    shortcut: engines are single-use, so changing a setting restarts it; the
    event log is the last EVENTS lines kept in memory only.
    """

    EVENTS = 40

    def __init__(self, switcher):
        self.switcher = switcher
        self.dry_run = False
        self.events: collections.deque = collections.deque(maxlen=self.EVENTS)
        self._engine = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _on_event(self, event) -> None:
        self.events.append({"ts": time.strftime("%H:%M:%S"), "kind": event.kind, "text": event.human()})

    def start(self, dry_run: bool = False) -> None:
        from claude_swap.autoswitch import AutoSwitchEngine
        from claude_swap.settings import load_settings

        with self._lock:
            if self.running():
                return
            settings = load_settings(self.switcher.backup_dir)
            engine = AutoSwitchEngine(self.switcher, settings, self._on_event, dry_run=dry_run)
            self._engine, self.dry_run = engine, dry_run
            self._thread = threading.Thread(target=engine.run_loop, daemon=True, name="agent-swap-auto")
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            engine, thread = self._engine, self._thread
            if engine is not None:
                engine.stop()
        if thread is not None:
            thread.join(timeout=10)  # let a tick in progress finish before anything restarts

    def restart(self) -> None:
        """Pick up changed settings. A no-op when the engine is not running."""
        if self.running():
            dry = self.dry_run
            self.stop()
            self.start(dry_run=dry)

    def snapshot(self) -> dict:
        return {"running": self.running(), "dryRun": self.dry_run and self.running(),
                "events": list(self.events)}


class CodexAutoRunner:
    """The Codex counterpart of AutoRunner: leave a Codex account near its line for a better one.

    Same shape (start / stop / restart / snapshot, a bounded event log). Each tick reads
    the usage (through ``Codex.rows(probe=True)``), and when the live slot is at its line
    it switches to the least-used enabled slot that is under its own. A running Codex
    keeps its account until restarted, so every switch says so.

    shortcut: a fixed cooldown between switches and in-memory events; the Claude engine's
    hysteresis and quarantine are not copied.
    """

    EVENTS = 40
    COOLDOWN_S = 300

    def __init__(self, codex, backup_dir, clock: Callable[[], float] = time.time):
        self.codex, self.backup_dir, self.clock = codex, backup_dir, clock
        self.dry_run = False
        self.events: collections.deque = collections.deque(maxlen=self.EVENTS)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_switch = 0.0
        self._last_text = ""
        self._lock = threading.Lock()

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _event(self, kind: str, text: str) -> None:
        if kind == "no-switch" and text == self._last_text:
            return  # the same quiet answer every tick would drown the log
        self._last_text = text
        self.events.append({"ts": time.strftime("%H:%M:%S"), "kind": kind, "text": text})

    def start(self, dry_run: bool = False) -> None:
        with self._lock:
            if self.running():
                return
            self._stop = threading.Event()
            self.dry_run = dry_run
            self._thread = threading.Thread(target=self._loop, args=(self._stop,), daemon=True, name="agent-swap-codex-auto")
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=10)

    def restart(self) -> None:
        if self.running():
            dry = self.dry_run
            self.stop()
            self.start(dry_run=dry)

    def snapshot(self) -> dict:
        return {"running": self.running(), "dryRun": self.dry_run and self.running(), "events": list(self.events)}

    def _loop(self, stop: threading.Event) -> None:
        from claude_swap.settings import load_settings

        while not stop.is_set():
            settings = load_settings(self.backup_dir)
            try:
                self.tick(settings.threshold)
            except Exception as e:  # a failing tick never ends the loop
                self._event("error", f"{type(e).__name__}: {e}")
            stop.wait(max(15.0, settings.interval_seconds))

    def tick(self, global_line: float) -> None:
        from claude_swap.codex import load_percent
        from claude_swap.exceptions import ClaudeSwitchError

        cx, now = self.codex(), self.clock()
        rows = cx.rows(persist=True, probe=True)
        live = next((r for r in rows if r["active"]), None)
        if live is None:
            self._event("no-switch", "no live Codex login that aswap manages")
            return
        load = load_percent(live["usage"], now)
        line = live["threshold"] or global_line
        name = live["alias"] or live["email"]
        if load is None or load < line:
            shown = "no reading yet" if load is None else f"{load:.0f}% < {line:.0f}%"
            self._event("no-switch", f"staying on {name}: {shown}")
            return
        loads = {r["slot"]: load_percent(r["usage"], now) for r in rows
                 if not r["active"] and not r["disabled"] and r["authMode"] != "apikey"}
        under = {n: (l or 0.0) for n, l in loads.items()
                 if (l or 0.0) < (next(r["threshold"] for r in rows if r["slot"] == n) or global_line)}
        if not under:
            self._event("all-exhausted", f"{name} is at {load:.0f}% and no other Codex account is under its line")
            return
        if now - self._last_switch < self.COOLDOWN_S:
            self._event("no-switch", f"{name} is at {load:.0f}%, but a switch happened under five minutes ago")
            return
        target = min(under, key=lambda n: (under[n], n))
        if self.dry_run:
            self._event("no-switch", f"dry run: would switch {name} ({load:.0f}%) to slot {target} ({under[target]:.0f}%)")
            return
        try:
            slot, _ = cx.switch(str(target))
        except ClaudeSwitchError as e:
            self._event("error", f"could not switch: {e}")
            return
        self._last_switch = now
        self._event("switch", f"switched {name} ({load:.0f}%) to slot {slot}; running Codex sessions keep their account until restarted")


class LoginJob:
    """One browser login at a time, then the new login is saved.

    ``start`` runs ``argv`` (never through a shell, no input; its output is only read for a URL and a code),
    and when it exits 0 calls ``save()`` for the message to show. The state is
    what the page polls: ``{"kind", "state": running|done|error, "message"}``, plus the
    ``url`` and device ``code`` the client printed, if any (https only), and ``copied``:
    whether the link was put on the clipboard.
    """

    def __init__(self, copier: Callable[[str], bool] | None = None):
        self._state: dict | None = None
        self._lock = threading.Lock()
        self._copier = copier  # None: the system clipboard, looked up when used

    def running(self) -> bool:
        return bool(self._state and self._state["state"] == "running")

    def snapshot(self) -> dict | None:
        return dict(self._state) if self._state else None

    def start(self, kind: str, argv: list[str], save: Callable[[], str], timeout: float = LOGIN_TIMEOUT_S) -> bool:
        """False when a login is already running."""
        with self._lock:
            if self.running():
                return False
            self._state = {"kind": kind, "state": "running",
                           "message": "Link ready. Paste it into the browser you want to sign in with."}
        threading.Thread(target=self._run, args=(argv, save, timeout), daemon=True, name="agent-swap-login").start()
        return True

    def _finish(self, state: str, message: str) -> None:
        self._state = {**(self._state or {}), "state": state, "message": message}

    def _note(self, line: str) -> None:
        """Keep the sign-in URL and device code the client prints, for a browser that did not open."""
        line = _ANSI.sub("", line)
        state = self._state
        if state is None:
            return
        url = _URL.search(line)
        if url and "url" not in state:
            state["url"] = url.group(0).rstrip(".,")
            # So it can be pasted into the browser profile of the account you want.
            copier = self._copier or copy_to_clipboard
            try:
                state["copied"] = bool(copier(state["url"]))
            except Exception:
                state["copied"] = False
        code = _CODE.search(line)
        if code and "code" not in state:
            state["code"] = code.group(0)

    def _run(self, argv: list[str], save: Callable[[], str], timeout: float) -> None:
        if os.name != "posix":
            return self._run_with_env(argv, save, timeout, None)
        with tempfile.TemporaryDirectory(prefix="agent-swap-login-") as shim:
            self._run_with_env(argv, save, timeout, no_browser_env(shim))

    def _run_with_env(self, argv: list[str], save: Callable[[], str], timeout: float, env: dict | None) -> None:
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, errors="replace", env=env)
        except OSError as e:
            self._finish("error", f"Could not start the login: {e.strerror or 'failed'}.")
            return

        def pump() -> None:
            for line in proc.stdout:
                self._note(line)

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            self._finish("error", f"No sign-in within {int(timeout // 60)} minutes; nothing was saved.")
            return
        finally:
            reader.join(timeout=2)
        if proc.returncode != 0:
            self._finish("error", f"The login did not complete (exit {proc.returncode}); nothing was saved.")
            return
        try:
            self._finish("done", save())
        except Exception as e:  # the save itself refused or failed: say why
            self._finish("error", f"Signed in, but saving failed: {e}")
