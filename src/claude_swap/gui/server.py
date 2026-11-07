"""`cswap gui`: a local web dashboard served from the standard library.

A loopback-only HTTP server that serves one static page and a small JSON API
over what the TUI and menu bar already call. Loopback is reachable by every
local user and by any web page open in the user's browser, so each request
passes independent checks before it touches the switcher: a Host allowlist
(DNS rebinding), an Origin check and JSON-only POSTs (CSRF), and a per-run
bearer token. Writes are off unless the server is started with ``write=True``.
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
from claude_swap.json_output import SCHEMA_VERSION, error_envelope
from claude_swap.settings import load_settings
from claude_swap.tui.data import run_action

POLL_SECONDS = 3
MAX_BODY = 4096
# Codex usage comes from scanning local rollout files; once per poll is wasteful.
CODEX_REFRESH_S = 30

# Slot numbers only. An email or alias would let `switch_to` reach its
# interactive disambiguation prompt, and the page never needs to send one.
_SLOT = re.compile(r"[0-9]{1,3}")

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

    def __init__(self, switcher, *, write: bool = False, port: int = 0, codex=None):
        # 127.0.0.1 only -- never 0.0.0.0 or ::.
        super().__init__(("127.0.0.1", port), _Handler)
        self.switcher = switcher
        self.write = write
        self._codex = codex  # None: built on first use, so Claude-only users pay nothing
        self._codex_rows: tuple[float, dict] | None = None
        self.token = secrets.token_urlsafe(16)  # per run, never persisted
        self.write_lock = threading.Lock()
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

    def codex_payload(self) -> dict:
        """Codex accounts, cached briefly. A Codex problem (keyring mode, a torn
        roster) is reported beside an empty list so it cannot blank the Claude half."""
        now = time.monotonic()
        cached = self._codex_rows
        if cached is not None and now - cached[0] < CODEX_REFRESH_S:
            return cached[1]
        try:
            # Reading must not write: persist only when changes are allowed.
            payload = {"accounts": self.codex().rows(persist=self.write)}
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
                },
            })
        elif path == "/api/accounts":
            self._json(200, self.server.switcher.list_accounts(json_output=True))
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
        }.get(urlsplit(self.path).path)
        if action is None:
            self._fail(404, "NotFound", "no such endpoint")
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

    def _finish(self, call, extra: dict) -> None:
        result = run_action(call)
        message = result.first_line
        if not result.ok:
            self._fail(400, "ClaudeSwitchError", message.removeprefix("Error: "))
        self._json(200, {
            "schemaVersion": SCHEMA_VERSION, "ok": True, "message": message, **extra,
        })
