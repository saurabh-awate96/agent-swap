"""Long-running work the dashboard starts: the auto-switch engine and browser logins.

Both live in the dashboard's own process and stop with it. Neither takes a value
from the page that could reach a shell: the engine reads its settings from
settings.json, and a login runs one fixed command (``claude auth login`` or
``codex login``) whose only variable part is an email address already checked
against the switcher's own pattern.
"""

from __future__ import annotations

import collections
import subprocess
import threading
import time
from collections.abc import Callable

LOGIN_TIMEOUT_S = 300


class AutoRunner:
    """Runs the auto-switch engine (the same one `cswap auto` runs) in a thread.

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


class LoginJob:
    """One browser login at a time, then the new login is saved.

    ``start`` runs ``argv`` (never through a shell, no input, output discarded),
    and when it exits 0 calls ``save()`` for the message to show. The state is
    what the page polls: ``{"kind", "state": running|done|error, "message"}``.
    """

    def __init__(self):
        self._state: dict | None = None
        self._lock = threading.Lock()

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
                           "message": "Finish signing in in the browser window that opened."}
        threading.Thread(target=self._run, args=(argv, save, timeout), daemon=True, name="agent-swap-login").start()
        return True

    def _finish(self, state: str, message: str) -> None:
        self._state = {**(self._state or {}), "state": state, "message": message}

    def _run(self, argv: list[str], save: Callable[[], str], timeout: float) -> None:
        try:
            done = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, timeout=timeout)
        except subprocess.TimeoutExpired:
            self._finish("error", f"No sign-in within {int(timeout // 60)} minutes; nothing was saved.")
            return
        except OSError as e:
            self._finish("error", f"Could not start the login: {e.strerror or 'failed'}.")
            return
        if done.returncode != 0:
            self._finish("error", f"The login did not complete (exit {done.returncode}); nothing was saved.")
            return
        try:
            self._finish("done", save())
        except Exception as e:  # the save itself refused or failed: say why
            self._finish("error", f"Signed in, but saving failed: {e}")
