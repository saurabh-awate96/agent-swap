"""`cswap gui`: a local web dashboard served from the standard library.

A loopback-only HTTP server that serves one static page and a small JSON API
over what the TUI and menu bar already call. Loopback is reachable by every
local user and by any web page open in the user's browser, so each request
passes independent checks before it touches the switcher: a Host allowlist
(DNS rebinding), an Origin check and JSON-only POSTs (CSRF), and a per-run
bearer token. Writes can be switched off with ``write=False`` (`--read-only`).
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import secrets
import shutil
import threading
import time
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import urlsplit

from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.gui.runner import AutoRunner, LoginJob
from claude_swap.json_output import SCHEMA_VERSION, error_envelope
from claude_swap.settings import load_settings, set_setting, unset_setting
from claude_swap.tui.data import run_action

POLL_SECONDS = 3
MAX_BODY = 4096
# Codex usage comes from scanning local rollout files; once per poll is wasteful.
CODEX_REFRESH_S = 30

# Slot numbers only. An email or alias would let `switch_to` reach its
# interactive disambiguation prompt, and the page never needs to send one.
_SLOT = re.compile(r"[0-9]{1,3}")

# The only settings the page may change: the auto-switch controls it shows.
_CONFIG_KEYS = {
    "autoswitch.threshold", "autoswitch.weeklyThreshold",
    "autoswitch.strategy", "autoswitch.keepalive",
}
_STRATEGIES = {"next": None, "best": "best", "next-available": "next-available"}
# Writes that stay possible while a browser login is waiting for the user.
_DURING_LOGIN = {"/api/auto", "/api/config"}

_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
}

_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

log = logging.getLogger(__name__)


class _Reject(Exception):
    """Stop handling and answer with this status and error body."""

    def __init__(self, status: int, kind: str, message: str):
        super().__init__(message)
        self.status, self.kind, self.message = status, kind, message


class GuiServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, switcher, *, write: bool = False, port: int = 0, codex=None, auto=None, job=None):
        # 127.0.0.1 only -- never 0.0.0.0 or ::.
        super().__init__(("127.0.0.1", port), _Handler)
        self.switcher = switcher
        self.write = write
        self._codex = codex  # None: built on first use, so Claude-only users pay nothing
        self._codex_rows: tuple[float, dict] | None = None
        self.token = secrets.token_urlsafe(16)  # per run, never persisted
        self.write_lock = threading.Lock()
        self.auto = auto or AutoRunner(switcher)  # the engine only starts when asked
        self.job = job or LoginJob()
        self.hosts = {
            f"127.0.0.1:{self.server_port}",
            f"localhost:{self.server_port}",
        }
        self.origins = {f"http://{h}" for h in self.hosts}
        # Read once: no request value ever becomes a filesystem path.
        pkg = resources.files("claude_swap.gui") / "static"
        self.assets = {
            path: ((pkg / name).read_bytes(), ctype)
            for path, (name, ctype) in _ASSETS.items()
        }


    def codex(self):
        if self._codex is None:
            from claude_swap.codex import Codex

            self._codex = Codex()
        return self._codex

    def unsaved_claude(self) -> dict | None:
        """The login Claude Code is using now, when cswap has not saved it.

        Only the address: it comes from ``.claude.json`` (no keychain read, no
        network), so the page can say "this one is not saved yet" instead of
        looking empty.
        """
        try:
            live = self.switcher._get_current_identity_triple()
            if live is None or self.switcher._account_exists(live[0], live[1]):
                return None
        except (ClaudeSwitchError, OSError):
            return None
        return {"email": live[0], "isOrganization": bool(live[1])}

    def codex_live_unsaved(self) -> bool:
        try:
            return self.codex().status()["state"] == "unmanaged"
        except ClaudeSwitchError:
            return False

    def codex_payload(self) -> dict:
        """Codex accounts, cached briefly. A Codex problem (keyring mode, a torn
        roster) is reported beside an empty list so it cannot blank the Claude half."""
        now = time.monotonic()
        cached = self._codex_rows
        if cached is not None and now - cached[0] < CODEX_REFRESH_S:
            return cached[1]
        try:
            # Reading must not write: persist only when changes are allowed.
            cx = self.codex()
            payload = {"accounts": cx.rows(persist=self.write, probe=True)}
            live = cx.status()
            payload["unsaved"] = (
                {k: live[k] for k in ("email", "plan", "authMode")}
                if live["state"] == "unmanaged" else None
            )
        except ClaudeSwitchError as e:
            payload = {"accounts": [], "error": str(e)}
        payload["schemaVersion"] = SCHEMA_VERSION
        # So the page can offer "add current login" before the first account exists.
        payload["installed"] = shutil.which("codex") is not None
        self._codex_rows = (now, payload)
        return payload


class _Handler(BaseHTTPRequestHandler):
    server: GuiServer
    protocol_version = "HTTP/1.0"
    timeout = 10  # a stalled client cannot pin a thread

    def log_message(self, *args) -> None:  # request lines would hit stderr
        pass

    def do_GET(self) -> None:
        self._serve(self._get)

    def do_POST(self) -> None:
        self._serve(self._post)

    def _not_allowed(self) -> None:
        # No CORS answer of any kind: a cross-site preflight must fail.
        self._serve(lambda: self._fail(405, "MethodNotAllowed", "GET and POST only"))

    do_OPTIONS = do_PUT = do_DELETE = do_PATCH = do_HEAD = _not_allowed

    # -- plumbing -----------------------------------------------------------

    def _fail(self, status: int, kind: str, message: str):
        raise _Reject(status, kind, message)

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for name, value in _SECURITY_HEADERS.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json")

    def _serve(self, route) -> None:
        try:
            self._check_origin()
            route()
        except _Reject as r:
            self._json(r.status, {
                "schemaVersion": SCHEMA_VERSION,
                "error": {"type": r.kind, "message": r.message},
            })
        except ClaudeSwitchError as e:
            self._json(400, error_envelope(e))
        except Exception:
            log.exception("gui request failed")
            self._json(500, {
                "schemaVersion": SCHEMA_VERSION,
                "error": {"type": "InternalError", "message": "see the cswap log"},
            })

    def _check_origin(self) -> None:
        """Host, Origin and Sec-Fetch-Site: applied to every request."""
        if self.headers.get("Host") not in self.server.hosts:
            raise _Reject(403, "BadHost", "unexpected Host header")
        origin = self.headers.get("Origin")
        if origin is None and self.command == "POST":
            raise _Reject(403, "BadOrigin", "Origin required")
        if origin is not None and origin not in self.server.origins:
            raise _Reject(403, "BadOrigin", "unexpected Origin")
        if self.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
            raise _Reject(403, "BadOrigin", "cross-site request")

    def _check_token(self) -> None:
        got = self.headers.get("Authorization", "")
        want = f"Bearer {self.server.token}"
        # Bytes: compare_digest raises on non-ASCII str.
        if not hmac.compare_digest(got.encode(), want.encode()):
            raise _Reject(401, "Unauthorized", "missing or wrong token")

    # -- routes -------------------------------------------------------------

    def _get(self) -> None:
        path = urlsplit(self.path).path
        asset = self.server.assets.get(path)
        if asset is not None:
            self._send(200, *asset)
            return
        self._check_token()
        if path == "/api/settings":
            s = load_settings(self.server.switcher.backup_dir)
            self._json(200, {
                "schemaVersion": SCHEMA_VERSION,
                "writable": self.server.write,
                "pollSeconds": POLL_SECONDS,
                "autoswitch": {
                    "threshold": s.threshold,
                    "weeklyThreshold": s.weekly_threshold,
                    "intervalSeconds": s.interval_seconds,
                    "cooldownSeconds": s.cooldown_seconds,
                    "strategy": s.strategy,
                    "model": s.model,
                    "keepalive": s.keepalive,
                },
            })
        elif path == "/api/accounts":
            # The page polls on a timer: unattended, so it never refreshes an idle
            # account's token (same rule as the terminal dashboard).
            payload = self.server.switcher.list_accounts(json_output=True, refresh_idle=False)
            payload["unsaved"] = self.server.unsaved_claude()
            payload["job"] = self.server.job.snapshot()
            self._json(200, payload)
        elif path == "/api/auto":
            s = load_settings(self.server.switcher.backup_dir)
            self._json(200, {
                "schemaVersion": SCHEMA_VERSION, **self.server.auto.snapshot(),
                "settings": {
                    "threshold": s.threshold, "weeklyThreshold": s.weekly_threshold,
                    "strategy": s.strategy, "keepalive": s.keepalive,
                    "intervalSeconds": s.interval_seconds,
                },
            })
        elif path == "/api/codex":
            self._json(200, self.server.codex_payload())
        else:
            self._fail(404, "NotFound", "no such page")

    def _post(self) -> None:
        self._check_token()
        if not self.server.write:
            self._fail(403, "ReadOnly", "start `cswap gui --write` to allow changes")
        action = {
            "/api/switch": self._switch,
            "/api/disable": self._disable,
            "/api/add-current": self._add_current,
            "/api/codex/switch": self._codex_switch,
            "/api/codex/add-current": self._codex_add_current,
            "/api/codex/rotate": self._codex_rotate,
            "/api/codex/remove": self._codex_remove,
            "/api/rotate": self._rotate,
            "/api/remove": self._remove,
            "/api/alias": self._alias,
            "/api/threshold": self._threshold,
            "/api/add-token": self._add_token,
            "/api/login": self._login,
            "/api/auto": self._auto,
            "/api/config": self._config,
        }.get(urlsplit(self.path).path)
        if action is None:
            self._fail(404, "NotFound", "no such endpoint")
        if self.server.job.running() and urlsplit(self.path).path not in _DURING_LOGIN:
            self._fail(409, "Busy", "a browser login is in progress; finish or wait for it first")
        body = self._read_body()
        # One write at a time: `run_action` redirects stdout/stdin process-wide.
        if not self.server.write_lock.acquire(blocking=False):
            self._fail(409, "Busy", "another change is still running")
        try:
            action(body)
        finally:
            self.server.write_lock.release()

    def _read_body(self) -> dict:
        ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self._fail(415, "UnsupportedMediaType", "Content-Type must be application/json")
        if "Transfer-Encoding" in self.headers:
            self._fail(411, "LengthRequired", "chunked bodies are not accepted")
        length = self.headers.get("Content-Length")
        if length is None or not length.isdigit():
            self._fail(411, "LengthRequired", "Content-Length required")
        if int(length) > MAX_BODY:
            self._fail(413, "TooLarge", "request body too large")
        try:
            body = json.loads(self.rfile.read(int(length)))
        except ValueError:
            body = None
        if not isinstance(body, dict):
            self._fail(400, "BadRequest", "body must be a JSON object")
        return body

    def _number(self, body: dict, allowed: set[str]) -> str:
        if set(body) - allowed:
            self._fail(400, "BadRequest", "unexpected field")
        number = body.get("number")
        if not isinstance(number, str) or not _SLOT.fullmatch(number):
            self._fail(400, "BadRequest", "number must be a slot number like \"2\"")
        return number

    def _switch(self, body: dict) -> None:
        number = self._number(body, {"number"})
        result = self.server.switcher.switch_to(number, json_output=True) or {}
        result.setdefault("schemaVersion", SCHEMA_VERSION)
        self._json(200, result)

    def _disable(self, body: dict) -> None:
        number = self._number(body, {"number", "disabled"})
        disabled = body.get("disabled")
        if not isinstance(disabled, bool):
            self._fail(400, "BadRequest", "disabled must be true or false")
        self._finish(
            partial(self.server.switcher.set_account_disabled, number, disabled),
            {"number": number, "disabled": disabled},
        )

    def _add_current(self, body: dict) -> None:
        if body:
            self._fail(400, "BadRequest", "unexpected field")
        self._finish(self.server.switcher.add_account, {})

    def _codex_switch(self, body: dict) -> None:
        number = self._number(body, {"number"})
        slot, changed = self.server.codex().switch(number)
        self.server._codex_rows = None
        self._json(200, {
            "schemaVersion": SCHEMA_VERSION, "switched": changed, "to": {"number": slot},
            "message": "Running Codex sessions keep their account until restarted.",
        })

    def _codex_add_current(self, body: dict) -> None:
        if body:
            self._fail(400, "BadRequest", "unexpected field")
        slot, created = self.server.codex().add()
        self.server._codex_rows = None
        self._json(200, {
            "schemaVersion": SCHEMA_VERSION, "ok": True, "number": slot,
            "message": f"{'Added' if created else 'Refreshed'} Codex slot {slot}",
        })

    def _finish(self, call, extra: dict, default: str = "Done.", hide: str | None = None) -> None:
        result = run_action(call)
        message = result.first_line
        if hide:  # a secret the action might echo back in a message
            message = message.replace(hide, "…")
        if not result.ok:
            self._fail(400, "ClaudeSwitchError", message.removeprefix("Error: "))
        self._json(200, {
            "schemaVersion": SCHEMA_VERSION, "ok": True, "message": message or default, **extra,
        })

    # -- more actions, all validated here: the page only ever sends plain values ----

    def _strategy(self, body: dict) -> str | None:
        if set(body) - {"strategy"}:
            self._fail(400, "BadRequest", "unexpected field")
        name = body.get("strategy", "next")
        if name not in _STRATEGIES:
            self._fail(400, "BadRequest", "strategy must be next, best or next-available")
        return _STRATEGIES[name]

    def _rotate(self, body: dict) -> None:
        result = self.server.switcher.switch(strategy=self._strategy(body), json_output=True) or {}
        result.setdefault("schemaVersion", SCHEMA_VERSION)
        self._json(200, result)

    def _remove(self, body: dict) -> None:
        number = self._number(body, {"number"})
        self._finish(partial(self.server.switcher.remove_account, number, assume_yes=True),
                     {"number": number}, "Removed.")

    def _alias(self, body: dict) -> None:
        number = self._number(body, {"number", "alias"})
        alias = body.get("alias")
        if not isinstance(alias, str) or len(alias) > 64:
            self._fail(400, "BadRequest", "alias must be text of at most 64 characters")
        alias = alias.strip()
        sw = self.server.switcher
        call = partial(sw.set_alias, number, alias) if alias else partial(sw.unset_alias, number)
        self._finish(call, {"number": number, "alias": alias}, "Alias saved." if alias else "Alias removed.")

    def _threshold(self, body: dict) -> None:
        number = self._number(body, {"number", "threshold"})
        value = body.get("threshold")
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            self._fail(400, "BadRequest", "threshold must be a number, or null to clear it")
        self._finish(partial(self.server.switcher.set_account_threshold, number, value),
                     {"number": number, "threshold": value},
                     "Threshold saved." if value is not None else "Threshold cleared.")

    def _add_token(self, body: dict) -> None:
        if set(body) - {"token", "email"}:
            self._fail(400, "BadRequest", "unexpected field")
        token, email = body.get("token"), body.get("email") or None
        if not isinstance(token, str) or not 8 <= len(token) <= 2048:
            self._fail(400, "BadRequest", "paste the whole token or API key")
        if email is not None and (not isinstance(email, str) or len(email) > 254):
            self._fail(400, "BadRequest", "email must be text")
        self._finish(
            partial(self.server.switcher.add_account_from_token, token=token, email=email, assume_yes=True),
            {}, "Account added.", hide=token,
        )

    def _login(self, body: dict) -> None:
        server, kind = self.server, body.get("kind")
        if kind == "claude":
            if set(body) - {"kind", "email"}:
                self._fail(400, "BadRequest", "unexpected field")
            email = body.get("email")
            if not isinstance(email, str) or len(email) > 254 or not server.switcher._validate_email(email):
                self._fail(400, "BadRequest", "enter a valid email address")
            if server.unsaved_claude() is not None:
                self._fail(409, "Unsaved", "Save the login you are using now first: signing in replaces it.")
            binary = shutil.which("claude")
            if binary is None:
                self._fail(400, "NotInstalled", "Claude Code (`claude`) was not found on PATH")
            argv = [binary, "auth", "login", "--email", email]

            def save() -> str:
                with server.write_lock:
                    result = run_action(server.switcher.add_account)
                if not result.ok:
                    raise ClaudeSwitchError(result.first_line.removeprefix("Error: "))
                return result.first_line or "Account saved."
        elif kind == "codex":
            if set(body) - {"kind"}:
                self._fail(400, "BadRequest", "unexpected field")
            if server.codex_live_unsaved():
                self._fail(409, "Unsaved", "Save the Codex login you are using now first: signing in replaces it.")
            binary = shutil.which("codex")
            if binary is None:
                self._fail(400, "NotInstalled", "Codex (`codex`) was not found on PATH")
            argv = [binary, "login"]

            def save() -> str:
                with server.write_lock:
                    slot, created = server.codex().add()
                    server._codex_rows = None
                return f"{'Added' if created else 'Refreshed'} Codex slot {slot}"
        else:
            self._fail(400, "BadRequest", "kind must be claude or codex")
        if not server.job.start(kind, argv, save):
            self._fail(409, "Busy", "a browser login is already in progress")
        self._json(200, {"schemaVersion": SCHEMA_VERSION, "ok": True,
                         "message": "Finish signing in in the browser window that opened."})

    def _auto(self, body: dict) -> None:
        if set(body) - {"action", "dryRun"}:
            self._fail(400, "BadRequest", "unexpected field")
        dry, action = body.get("dryRun", False), body.get("action")
        if not isinstance(dry, bool) or action not in ("start", "stop"):
            self._fail(400, "BadRequest", "action must be start or stop")
        auto = self.server.auto
        auto.start(dry_run=dry) if action == "start" else auto.stop()
        message = "Auto-switch stopped." if action == "stop" else (
            "Auto-switch started in dry-run: it only reports." if dry else "Auto-switch started.")
        self._json(200, {"schemaVersion": SCHEMA_VERSION, "ok": True, "message": message, **auto.snapshot()})

    def _config(self, body: dict) -> None:
        if set(body) - {"key", "value"}:
            self._fail(400, "BadRequest", "unexpected field")
        key, value = body.get("key"), body.get("value")
        if key not in _CONFIG_KEYS:
            self._fail(400, "BadRequest", "that setting cannot be changed from here")
        root = self.server.switcher.backup_dir
        if value is None:
            unset_setting(root, key)
        else:
            if isinstance(value, bool):
                raw = "true" if value else "false"
            elif isinstance(value, (int, float)) or (isinstance(value, str) and len(value) <= 40):
                raw = str(value)
            else:
                self._fail(400, "BadRequest", "value must be a number, true/false or a short text")
            try:
                set_setting(root, key, raw)
            except ValueError as e:
                self._fail(400, "BadRequest", str(e))
        self.server.auto.restart()  # an engine in progress picks the new value up
        self._json(200, {"schemaVersion": SCHEMA_VERSION, "ok": True, "message": "Saved."})

    def _codex_rotate(self, body: dict) -> None:
        strategy = self._strategy(body)
        if strategy == "next-available":
            self._fail(400, "BadRequest", "Codex rotation is next or best")
        slot, changed = self.server.codex().switch(None, strategy=strategy)
        self.server._codex_rows = None
        self._json(200, {
            "schemaVersion": SCHEMA_VERSION, "switched": changed, "to": {"number": slot},
            "message": "Running Codex sessions keep their account until restarted.",
        })

    def _codex_remove(self, body: dict) -> None:
        number = self._number(body, {"number"})
        slot = self.server.codex().remove(number, yes=True)
        self.server._codex_rows = None
        self._json(200, {"schemaVersion": SCHEMA_VERSION, "ok": True, "message": f"Removed Codex slot {slot}"})
