"""OpenAI Codex CLI account switching (``aswap codex ...``).

Switching is local files only: no OpenAI request, no token refresh, and never
``codex login`` / ``codex logout`` (logout revokes the login server-side). The one
network use is the optional live-usage read below, made by Codex itself.
``$CODEX_HOME/auth.json`` is replaced only once its current bytes are held in
a slot, and a slot's blob is the exact bytes of that file, never re-serialised.
No token value is ever logged, printed or put in an exception message.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from claude_swap import paths, printer, spawn
from claude_swap.credentials import CredentialStore
from claude_swap.exceptions import (
    AccountNotFoundError,
    ConfigError,
    CredentialError,
    SwitchError,
)
from claude_swap.fsutil import atomic_write_bytes, atomic_write_text
from claude_swap.locking import FileLock
from claude_swap.logging_config import setup_logging
from claude_swap.models import Platform
from claude_swap.settings import load_usage_settings

LOCK_TIMEOUT = 10.0
# Codex refreshes (rotating the single-use refresh token) when the access token
# is this close to expiry. A swap mid-refresh strands the rotated token.
NEAR_EXPIRY_S = 600
_AUTH_CLAIM = "https://api.openai.com/auth"
_PROFILE_CLAIM = "https://api.openai.com/profile"


def codex_home() -> Path:
    env = os.environ.get("CODEX_HOME")
    return Path(os.path.expanduser(env)) if env else Path.home() / ".codex"


def codex_running() -> bool:
    """Whether a ``codex`` process exists. shortcut: pgrep -x matches the native
    binary only, and is skipped on Windows; upgrade if a wrapper name shows up."""
    if sys.platform == "win32":
        return False
    try:
        return spawn.run(["pgrep", "-x", "codex"], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@dataclass(frozen=True)
class Auth:
    identity: tuple[str, str]  # (chatgpt_user_id, chatgpt_account_id)
    email: str
    plan: str  # "api" for an API key
    mode: str  # "chatgpt" | "apikey"
    last_refresh: datetime | None
    access_exp: datetime | None


def _claims(token) -> dict:
    # Decoded without verifying the signature: for matching and display only.
    try:
        seg = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))
    except (AttributeError, IndexError, ValueError, TypeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def _when(value, *, epoch: bool = False) -> datetime | None:
    try:
        if epoch:
            return datetime.fromtimestamp(float(value), timezone.utc)
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError, OverflowError, OSError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_auth(raw: bytes) -> Auth:
    try:
        doc = json.loads(raw)
    except ValueError:
        doc = None
    if not isinstance(doc, dict):
        raise SwitchError("Codex auth.json is not a JSON object")
    tokens = doc.get("tokens") if isinstance(doc.get("tokens"), dict) else {}
    key = doc.get("OPENAI_API_KEY")
    has_key = isinstance(key, str) and bool(key)
    has_login = isinstance(tokens.get("refresh_token"), str) and bool(tokens["refresh_token"])
    if has_key and (doc.get("auth_mode") == "apikey" or not has_login):
        digest = hashlib.sha256(key.encode()).hexdigest()[:12]
        return Auth(("", f"apikey:{digest}"), "", "api", "apikey", None, None)
    if not has_login:
        raise SwitchError("Codex auth.json holds no ChatGPT login or API key")
    ident, access = _claims(tokens.get("id_token")), _claims(tokens.get("access_token"))
    info = ident.get(_AUTH_CLAIM) if isinstance(ident.get(_AUTH_CLAIM), dict) else {}
    ainfo = access.get(_AUTH_CLAIM) if isinstance(access.get(_AUTH_CLAIM), dict) else {}
    account = str(tokens.get("account_id") or info.get("chatgpt_account_id") or "")
    if not account:
        raise SwitchError("Codex auth.json has no account id")
    profile = ident.get(_PROFILE_CLAIM) if isinstance(ident.get(_PROFILE_CLAIM), dict) else {}
    return Auth(
        identity=(str(info.get("chatgpt_user_id") or ainfo.get("chatgpt_user_id") or ""), account),
        email=str(ident.get("email") or profile.get("email") or ""),
        plan=str(info.get("chatgpt_plan_type") or ""),
        mode="chatgpt",
        last_refresh=_when(doc.get("last_refresh")),
        access_exp=_when(access.get("exp"), epoch=True),
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# -- usage, from Codex's own rollout files (no network) --------------------------

_ROLLOUT_FILES = 50
_TAIL_BYTES = 256 * 1024  # one backwards read
_MAX_SCAN = 32 * 1024 * 1024  # how far from the end of a file to look


def _window(w) -> dict | None:
    try:
        return {k: float(w[k]) for k in ("used_percent", "window_minutes", "resets_at")}
    except (KeyError, TypeError, ValueError):
        return None


def _snapshot_in(line: bytes) -> dict | None:
    """The usable ``token_count`` rate-limit reading in one rollout line, if any."""
    if b'"token_count"' not in line:
        return None
    try:
        event = json.loads(line)
        limits = event["payload"]["rate_limits"]
        if limits["limit_id"] != "codex":
            return None
        return {
            "primary": _window(limits.get("primary")),
            "secondary": _window(limits.get("secondary")),
            "seenAt": event["timestamp"],
        }
    except (ValueError, KeyError, TypeError):
        return None  # a torn last line, a null rate_limits, another event shape


def _last_snapshot(fh, size: int) -> dict | None:
    """The newest usable ``token_count`` rate-limit event in the file.

    Reads backwards in 256 KB blocks, joining a line that spans blocks, so one
    oversized line (a big tool output) at the end of a long session cannot hide
    the reading before it. A file that stayed that quiet for more than
    ``_MAX_SCAN`` bytes yields nothing.

    shortcut: the budget is 32 MB from the end; raise _MAX_SCAN if a session's
    quiet tail ever outgrows it.
    """
    floor = max(0, size - _MAX_SCAN)
    pos, carry = size, b""
    while pos > floor:
        start = max(floor, pos - _TAIL_BYTES)
        fh.seek(start)
        lines = (fh.read(pos - start) + carry).split(b"\n")
        carry = lines.pop(0) if start > 0 else b""  # starts mid-line: finished by the next block back
        for line in reversed(lines):
            snap = _snapshot_in(line)
            if snap:
                return snap
        pos = start
    return None


# -- the live account's current numbers, from the real client -------------------
#
# Rollouts only record what Codex wrote on THIS machine, so an account idle for a
# while shows a stale reading and, once its window has reset, none. For the live
# account only, `codex app-server` (OpenAI's own client, run with your own login,
# so nothing here impersonates it) answers `account/rateLimits/read` with the
# current figures. It is asked only when the local reading is stale, at most
# once per PROBE_STALE_S, and never switches or rewrites a login by itself.

PROBE_STALE_S = 300
PROBE_BUDGET_S = 15.0
_PROBE: dict = {}  # {"identity", "at" (monotonic), "snap", "problem"}: last attempt, shared by every Codex()


def _problem_of(result: dict | None) -> str | None:
    """What OpenAI said was wrong with the login, from an error reply (None when fine or silent)."""
    err = (result or {}).get("error")
    if not isinstance(err, str):
        return None
    low = err.lower()
    if "token_revoked" in low or "invalidated" in low:
        return "revoked"
    return "unauthorized" if "401" in low or "unauthorized" in low else "failed"


def _read_rate_limits(budget: float | None = None, home: Path | None = None) -> dict | None:
    """The ``result`` of ``account/rateLimits/read`` from ``codex app-server``, or None.

    None on any failure (no codex, a crash, no answer in time); ``{"error": ...}`` when
    OpenAI refused the login. ``home`` runs the client against another ``CODEX_HOME``.
    """
    binary = shutil.which("codex")
    if binary is None:
        return None
    from claude_swap import __version__

    deadline = time.monotonic() + (PROBE_BUDGET_S if budget is None else budget)
    try:
        proc = subprocess.Popen(
            [binary, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
            env=None if home is None else {**os.environ, "CODEX_HOME": str(home)},
        )
    except OSError:
        return None
    lines: queue.Queue = queue.Queue()

    def pump() -> None:
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)  # the client closed its output

    threading.Thread(target=pump, daemon=True).start()

    def send(obj: dict) -> None:
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def reply(want: int) -> dict | None:
        while True:
            try:
                line = lines.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                return None
            if line is None:
                return None
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg, dict) and msg.get("id") == want and ("result" in msg or "error" in msg):
                return msg

    try:
        send({"id": 1, "method": "initialize",
              "params": {"clientInfo": {"name": "agent-swap", "version": __version__}}})
        if reply(1) is None:
            return None
        send({"method": "initialized"})
        send({"id": 2, "method": "account/rateLimits/read"})
        msg = reply(2) or {}
        if isinstance(msg.get("result"), dict):
            return msg["result"]
        err = msg.get("error")  # e.g. a revoked login: say so instead of going quiet
        return {"error": str(err.get("message", ""))[:400]} if isinstance(err, dict) else None
    except OSError:  # a broken pipe: the client went away
        return None
    finally:
        with contextlib.suppress(OSError):
            proc.stdin.close()
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _usage_from_rate_limits(result: dict | None) -> dict | None:
    """A rollout-shaped usage dict (plus ``live``) from an ``account/rateLimits/read`` result."""
    try:
        limits = (result.get("rateLimitsByLimitId") or {}).get("codex") or result["rateLimits"]
        if limits.get("limitId") not in (None, "codex"):
            return None
        windows = {
            name: _window({
                "used_percent": limits[name]["usedPercent"],
                "window_minutes": limits[name]["windowDurationMins"],
                "resets_at": limits[name]["resetsAt"],
            }) if limits.get(name) else None
            for name in ("primary", "secondary")
        }
    except (AttributeError, KeyError, TypeError):
        return None
    if not any(windows.values()):
        return None
    seen = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    return {**windows, "seenAt": seen, "live": True}


def _needs_probe(usage: dict | None, now: float) -> bool:
    """Whether a stored reading is too old, or has a window that reset since."""
    if not usage:
        return True
    seen = _when(usage["seenAt"])
    if seen is None or now - seen.timestamp() > PROBE_STALE_S:
        return True
    return any(w and w["resets_at"] <= now for w in (usage.get("primary"), usage.get("secondary")))


def _probe_snapshot(identity) -> dict | None:
    """The live reading, asked for at most once per PROBE_STALE_S (a failure counts too)."""
    now = time.monotonic()
    if _PROBE.get("identity") == identity and now - _PROBE["at"] < PROBE_STALE_S:
        return _PROBE["snap"]
    result = _read_rate_limits()
    snap = _usage_from_rate_limits(result)
    _PROBE.update(identity=identity, at=now, snap=snap, problem=_problem_of(result))
    return snap


def load_percent(usage: dict | None, now: float) -> float | None:
    """Worst of the two windows; one that has reset since the snapshot counts as 0."""
    if not usage:
        return None
    seen = [
        0.0 if w["resets_at"] <= now else w["used_percent"]
        for w in (usage.get("primary"), usage.get("secondary"))
        if w
    ]
    return max(seen) if seen else None


def describe_usage(usage: dict | None, now: float) -> str:
    if not usage:
        return "-"
    cells = [
        f"{label} reset" if w["resets_at"] <= now else f"{label} {w['used_percent']:.0f}%"
        for label, w in (("5h", usage.get("primary")), ("wk", usage.get("secondary")))
        if w
    ]
    seen = _when(usage["seenAt"])
    age = f" ({printer.format_age(int(seen.timestamp() * 1000))})" if seen else ""
    return " ".join(cells) + age


def _confirm(question: str) -> bool:
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


class Codex:
    def __init__(self, debug: bool = False):
        self.home = codex_home()
        self.live = self.home / "auth.json"
        backup_root = paths.get_backup_root()
        self.root = backup_root / "codex"
        self.seq = self.root / "sequence.json"
        self.credentials_dir = self.root / "credentials"
        self._log = setup_logging(backup_root, debug=debug)
        self._store = CredentialStore(
            SimpleNamespace(
                platform=Platform.detect(),
                credentials_dir=self.credentials_dir,
                sequence_file=self.seq,
                _logger=self._log,
            )
        )

    # -- storage -----------------------------------------------------------

    def _require_file_store(self) -> None:
        try:
            cfg = tomllib.loads((self.home / "config.toml").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            raise ConfigError("Cannot read Codex config.toml; refusing to touch auth.json") from None
        mode = cfg.get("cli_auth_credentials_store", "file")
        if mode != "file":
            raise ConfigError(
                f'Codex is set to cli_auth_credentials_store = "{mode}"; '
                'aswap codex supports "file" only'
            )

    def _load(self) -> dict:
        try:
            text = self.seq.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"activeAccountNumber": None, "sequence": [], "accounts": {}}
        try:
            data = json.loads(text)
            if isinstance(data["accounts"], dict) and isinstance(data["sequence"], list):
                return data
        except (ValueError, KeyError, TypeError):
            pass
        raise ConfigError(f"Codex account list {self.seq} is unreadable; nothing was changed")

    def _save(self, data: dict) -> None:
        data["lastUpdated"] = _now()
        self.root.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.seq, json.dumps(data, indent=2, allow_nan=False))

    def _read_live(self) -> bytes | None:
        try:
            return self.live.read_bytes() or None
        except FileNotFoundError:
            return None

    def _key(self, n: int, email: str | None) -> tuple[str, str]:
        # "codex-N", not "N": the Keychain username would otherwise collide
        # with a Claude slot of the same number and email.
        if email is None:
            email = self._load()["accounts"][str(n)]["email"]
        return f"codex-{n}", email

    def _read_blob(self, n: int, email: str | None = None) -> bytes | None:
        raw, unreadable = self._store._read_account_credentials_ex(*self._key(n, email))
        if unreadable:
            raise CredentialError(f"Codex slot {n} backup could not be read")
        if not raw:
            return None
        try:
            return base64.b64decode(raw, validate=True)
        except ValueError:
            raise CredentialError(f"Codex slot {n} backup is corrupt") from None

    def _write_blob(self, n: int, blob: bytes, email: str | None = None) -> None:
        # Base64 keeps the bytes exact through the Keychain (printable text only).
        self._store._write_account_credentials(*self._key(n, email), base64.b64encode(blob).decode())
        if self._read_blob(n, email) != blob:
            raise CredentialError(f"Codex slot {n} backup did not verify after writing")

    @contextlib.contextmanager
    def _locked(self, *, codex_lock: bool = True):
        # Always cswap's lock first. Codex never takes it, so there is no cycle.
        with FileLock(self.root / ".lock", LOCK_TIMEOUT):
            if not codex_lock:
                yield
                return
            with FileLock(self.home / "auth.lock", LOCK_TIMEOUT):
                yield

    # -- roster helpers ----------------------------------------------------

    @staticmethod
    def _match(data: dict, auth: Auth) -> int | None:
        for n, acct in data["accounts"].items():
            if (acct.get("userId", ""), acct["accountId"]) == auth.identity:
                return int(n)
        return None

    @staticmethod
    def _resolve(data: dict, target: str) -> int:
        if target.isdigit() and target in data["accounts"]:
            return int(target)
        for n, acct in data["accounts"].items():
            if target and target.lower() in (acct["email"].lower(), acct.get("alias", "").lower()):
                return int(n)
        raise AccountNotFoundError(f"No Codex account matches {target!r}")

    @staticmethod
    def _label(acct: dict) -> str:
        return acct.get("alias") or acct["email"] or acct["plan"]

    def _capture(self, n: int, live: bytes, auth: Auth) -> None:
        """Hold the live bytes in slot ``n`` (it may carry a rotated refresh token)."""
        held = self._read_blob(n)
        if held == live:
            return
        if held is not None:
            try:
                prior = parse_auth(held)
            except SwitchError:
                prior = None
            if prior and prior.last_refresh and auth.last_refresh and prior.last_refresh > auth.last_refresh:
                # Someone restored a newer backup; the live copy is the stale one.
                self._log.warning(f"Codex slot {n}: backup is newer than auth.json, kept")
                return
        self._write_blob(n, live)

    def _restore(self, live: bytes | None) -> None:
        if live is None:
            self.live.unlink(missing_ok=True)
        else:
            atomic_write_bytes(self.live, live, mode=0o600)

    # -- commands ----------------------------------------------------------

    def add(self, slot: int | None = None, alias: str | None = None, yes: bool = False):
        """Store the live login in a slot. Returns ``(slot, created)``."""
        self._require_file_store()
        missing = SwitchError("No Codex login found. Run `codex login` first, then `aswap codex add`.")
        if not self._read_live():
            raise missing
        self.root.mkdir(parents=True, exist_ok=True)
        with self._locked():
            live = self._read_live()
            if not live:
                raise missing
            auth = parse_auth(live)
            data = self._load()
            n = self._match(data, auth)
            created = n is None
            if created:
                n = slot or max((int(k) for k in data["accounts"]), default=0) + 1
                old = data["accounts"].get(str(n))
                if old:
                    if not (yes or _confirm(f"Codex slot {n} holds {self._label(old)}. Overwrite?")):
                        raise SwitchError(f"Codex slot {n} is in use; pick another --slot or pass -y to overwrite")
                    self._store.delete_account_credentials_strict(f"codex-{n}", old["email"])
                self._write_blob(n, live, auth.email)
                data["accounts"][str(n)] = {
                    "email": auth.email,
                    "accountId": auth.identity[1],
                    "userId": auth.identity[0],
                    "plan": auth.plan,
                    "authMode": auth.mode,
                    "added": _now(),
                }
                data["sequence"] = sorted({*data["sequence"], n})
            else:
                self._capture(n, live, auth)
            if alias:
                self._check_alias(data, n, alias)
                data["accounts"][str(n)]["alias"] = alias
            data["activeAccountNumber"] = n
            self._save(data)
        self._log.info(f"Codex slot {n} {'added' if created else 'refreshed'}")
        return n, created

    @staticmethod
    def _check_alias(data: dict, n: int, alias: str) -> None:
        taken = {a.get("alias") for k, a in data["accounts"].items() if k != str(n)}
        if not alias or len(alias) > 64 or alias.isdigit() or "@" in alias or alias in taken:
            raise SwitchError(f"Alias {alias!r} is not usable (empty, numeric, an email, or already taken)")

    def _edit(self, target: str, change) -> int:
        """Apply ``change(account_dict)`` to one slot under the roster lock. Returns the slot."""
        self._require_file_store()
        with self._locked(codex_lock=False):
            data = self._load()
            n = self._resolve(data, target)
            change(data, data["accounts"][str(n)], n)
            self._save(data)
        return n

    def set_alias(self, target: str, alias: str | None) -> int:
        """Name a slot (``None`` or blank removes the name)."""
        alias = (alias or "").strip()

        def change(data, acct, n):
            if alias:
                self._check_alias(data, n, alias)
                acct["alias"] = alias
            else:
                acct.pop("alias", None)
        return self._edit(target, change)

    def set_disabled(self, target: str, disabled: bool) -> int:
        """Hold a slot out of rotate / best / auto-switch (an explicit switch still works)."""
        def change(data, acct, n):
            if disabled:
                acct["disabled"] = True
            else:
                acct.pop("disabled", None)
        return self._edit(target, change)

    def set_threshold(self, target: str, pct: float | None) -> int:
        """A slot's own switch-away line for auto-switch (``None`` = the global one)."""
        if pct is not None:
            if isinstance(pct, bool) or not isinstance(pct, (int, float)) or not 50 <= pct <= 99.9:
                raise ConfigError("threshold must be between 50 and 99.9")

        def change(data, acct, n):
            if pct is None:
                acct.pop("threshold", None)
            else:
                acct["threshold"] = float(pct)
        return self._edit(target, change)

    def add_from_api_key(self, key: str, alias: str | None = None):
        """Log Codex in with an API key (the key goes to `codex login` on stdin, never argv), then store it.

        Replaces the live login, so an unsaved one is refused first.
        """
        if not key or len(key) > 2048 or "\n" in key.strip():
            raise ConfigError("paste a single API key")
        if self.status().get("state") == "unmanaged":
            raise SwitchError("The live Codex login is not saved yet; save it first (aswap codex add).")
        binary = shutil.which("codex")
        if binary is None:
            raise ConfigError("Codex (`codex`) was not found on PATH")
        try:
            done = subprocess.run([binary, "login", "--with-api-key"], input=key.strip() + "\n",
                                  text=True, capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            raise SwitchError("`codex login --with-api-key` did not finish") from None
        if done.returncode != 0:
            raise SwitchError(f"`codex login --with-api-key` failed (exit {done.returncode})")
        return self.add(alias=alias)

    def _check_stored(self, n: int, blob: bytes) -> tuple[str, dict | None, bytes | None]:
        """Ask the real client about a stored login without switching to it.

        It runs against a throwaway CODEX_HOME holding a copy of the slot's auth.json (0700 dir,
        0600 file, removed afterwards). If the client refreshed the token, the single-use refresh
        token in the stored copy is dead, so the new file is handed back to be saved.
        Returns (status, usage snapshot or None, new auth.json bytes or None).
        """
        import tempfile

        auth = parse_auth(blob)
        if auth.mode == "apikey":
            return "apikey", None, None
        if auth.access_exp and (auth.access_exp - datetime.now(timezone.utc)).total_seconds() < NEAR_EXPIRY_S:
            return "expiring", None, None  # the client would refresh it; do that on its next real use
        with tempfile.TemporaryDirectory(prefix="agent-swap-codex-") as tmp:
            os.chmod(tmp, 0o700)
            atomic_write_bytes(Path(tmp) / "auth.json", blob, mode=0o600)
            result = _read_rate_limits(home=Path(tmp))
            try:
                after = (Path(tmp) / "auth.json").read_bytes()
            except OSError:
                after = None
        newer = after if after and after != blob and parse_auth(after).identity == auth.identity else None
        problem = _problem_of(result)
        if problem:
            return problem, None, newer
        snap = _usage_from_rate_limits(result)
        return ("ok" if snap else "failed"), snap, newer

    def check_all(self, only: int | None = None) -> list[dict]:
        """Check every stored login with the real client and record the result per slot.

        The live account is asked as it is; the others through a throwaway home (see above).
        Returns ``[{"slot", "label", "status", "usage"}]`` where status is ok, revoked,
        unauthorized, failed, expiring (not asked, about to refresh on its own) or apikey.
        ``only`` checks just that slot.
        """
        self._require_file_store()
        _PROBE.clear()
        data = self._load()
        if only is not None and str(only) not in data["accounts"]:
            raise AccountNotFoundError(f"No Codex account in slot {only}")
        live_n, live_auth = self._live_slot(data)
        out, found = [], {}
        for key, acct in sorted(data["accounts"].items(), key=lambda kv: int(kv[0])):
            n = int(key)
            if only is not None and n != only:
                continue
            if acct["authMode"] == "apikey":
                out.append({"slot": n, "label": self._label(acct), "status": "apikey", "usage": None})
                continue
            if n == live_n:
                snap = _probe_snapshot(live_auth.identity)
                status = "ok" if snap else (_PROBE.get("problem") or "failed")
                found[n] = (status, snap, None)
            else:
                blob = self._read_blob(n)
                found[n] = self._check_stored(n, blob) if blob else ("failed", None, None)
            out.append({"slot": n, "label": self._label(acct), "status": found[n][0], "usage": found[n][1]})
        with self._locked(codex_lock=False):
            data = self._load()
            for n, (status, snap, newer) in found.items():
                acct = data["accounts"].get(str(n))
                if acct is None:
                    continue
                acct["health"] = {"status": status, "at": _now()}
                if snap:
                    acct["usage"] = snap
                if newer is not None:
                    self._write_blob(n, newer)
            self._save(data)
        return out

    def _refresh_usage(self, data: dict) -> bool:
        """Fold the newest rollout snapshot per slot into ``data``. True if it changed."""
        slots = {n: a for n, a in data["accounts"].items() if a["authMode"] != "apikey"}
        found: dict[str, dict] = {}
        files = [
            f
            for sub in ("sessions", "archived_sessions")
            for f in (self.home / sub).rglob("rollout-*.jsonl")
        ]
        files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        for f in files[:_ROLLOUT_FILES]:
            try:
                with f.open("rb") as fh:
                    meta = json.loads(fh.readline())["payload"]
                    account, user = meta["creator_account_id"], meta.get("creator_user_id")
                    owners = [n for n, a in slots.items() if a["accountId"] == account]
                    if len(owners) > 1:
                        owners = [n for n in owners if slots[n].get("userId") == user]
                    if len(owners) != 1:
                        continue  # unknown account, or a rollout from before the field existed
                    snap = _last_snapshot(fh, f.stat().st_size)
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if snap and snap["seenAt"] > found.get(owners[0], {"seenAt": ""})["seenAt"]:
                found[owners[0]] = snap
            if len(found) == len(slots):
                break
        changed = False
        for n, snap in found.items():
            held = data["accounts"][n].get("usage")
            if not held or snap["seenAt"] > held["seenAt"]:
                data["accounts"][n]["usage"] = snap
                changed = True
        return changed

    def _live_slot(self, data: dict) -> tuple[int | None, Auth | None]:
        live = self._read_live()
        try:
            auth = parse_auth(live) if live else None
        except SwitchError:
            return None, None
        if auth is None or auth.mode == "apikey":
            return None, None
        return self._match(data, auth), auth

    def _apply_probe(self, data: dict, persist: bool) -> tuple[int, str] | None:
        """Replace the live account's stale reading with the real client's, when it answers.

        Returns ``(slot, problem)`` when OpenAI refused the live login (revoked, unauthorized).
        """
        n, auth = self._live_slot(data)
        if n is None or not _needs_probe(data["accounts"][str(n)].get("usage"), time.time()):
            return None
        snap = _probe_snapshot(auth.identity)
        if snap is None or self._live_slot(data)[0] != n:
            problem = _PROBE.get("problem") if self._live_slot(data)[0] == n else None
            return (n, problem) if problem else None  # no answer, or the login changed while asking
        data["accounts"][str(n)]["usage"] = snap
        if persist:
            with self._locked(codex_lock=False):
                fresh = self._load()
                held = fresh["accounts"].get(str(n), {}).get("usage")
                if str(n) in fresh["accounts"] and (not held or snap["seenAt"] > held["seenAt"]):
                    fresh["accounts"][str(n)]["usage"] = snap
                    self._save(fresh)
        return None

    def rows(self, persist: bool = True, probe: bool = False) -> list[dict]:
        """One dict per stored account. ``persist=False`` writes nothing (the
        read-only dashboard): usage is folded in for this call only. ``probe``
        lets a stale live account be read through the real client (see above)."""
        self._require_file_store()
        data = self._load()
        problems: dict[int, str] = {}
        if data["accounts"]:
            with self._locked(codex_lock=False):
                data = self._load()
                if self._refresh_usage(data) and persist:
                    self._save(data)
            if probe and load_usage_settings(paths.get_backup_root()).codex_probe:
                found = self._apply_probe(data, persist)
                if found:
                    problems[found[0]] = found[1]
        active = data["activeAccountNumber"]
        return [
            {
                "slot": int(n),
                "alias": a.get("alias", ""),
                "email": a["email"],
                "plan": a["plan"],
                "authMode": a["authMode"],
                "active": int(n) == active,
                "disabled": bool(a.get("disabled")),
                "threshold": a.get("threshold"),
                "problem": problems.get(int(n)),
                "health": a.get("health"),
                "usage": a.get("usage"),
            }
            for n, a in sorted(data["accounts"].items(), key=lambda kv: int(kv[0]))
        ]

    def _choose(self, data: dict, target: str | None, strategy: str | None) -> int:
        if target:
            return self._resolve(data, target)
        now = time.time()
        usable = {
            int(n): a for n, a in data["accounts"].items()
            if a["authMode"] != "apikey" and not a.get("disabled")
        }
        if strategy == "best":
            load = {n: load_percent(a.get("usage"), now) for n, a in usable.items()}
            load = {n: p for n, p in load.items() if p is not None}
            if load:
                return min(load, key=lambda n: (load[n], n))
        active = data["activeAccountNumber"]
        order = [n for n in data["sequence"] if n in usable]
        others = [n for n in order if n != active]
        if strategy == "next-available":  # skip slots whose last reading is at the limit
            others = [n for n in others if (load_percent(usable[n].get("usage"), now) or 0) < 100]
        if not others:
            raise SwitchError("There is no other Codex account to rotate to")
        after = [n for n in others if active is not None and n > active]
        return (after or others)[0]

    def _guard(self, auth: Auth, force: bool) -> None:
        if force or auth.access_exp is None:
            return
        if (auth.access_exp - datetime.now(timezone.utc)).total_seconds() < NEAR_EXPIRY_S and codex_running():
            raise SwitchError(
                "Codex is running and its access token is about to refresh; swapping now "
                "could lose the rotated login. Finish or quit Codex, or pass --force."
            )

    def switch(self, target: str | None = None, strategy: str | None = None, force: bool = False):
        """Make ``target`` (default: next) the live login. Returns ``(slot, changed)``."""
        self._require_file_store()
        if not self._load()["accounts"]:
            raise SwitchError("No Codex accounts yet. Log in with Codex, then run `aswap codex add`.")
        self.root.mkdir(parents=True, exist_ok=True)
        with self._locked():
            data = self._load()
            self._refresh_usage(data)  # saved with the switch, so it outlives rollout rotation
            t = self._choose(data, target, strategy)
            tblob = self._read_blob(t)
            if tblob is None:
                raise CredentialError(f"Codex slot {t} has no stored login")
            parse_auth(tblob)
            for _attempt in range(2):
                live = self._read_live()
                if live:
                    auth = parse_auth(live)
                    out = self._match(data, auth)
                    if out is None:
                        raise SwitchError(
                            "The live Codex login is not managed by cswap, so switching would drop it. "
                            "Run `aswap codex add` first."
                        )
                    self._guard(auth, force)
                    self._capture(out, live, auth)
                    if out == t:
                        if data["activeAccountNumber"] != t:
                            data["activeAccountNumber"] = t
                            self._save(data)
                        return t, False
                # Compare-and-save: a writer that ignored auth.lock may have rotated it.
                if self._read_live() == live:
                    break
            else:
                raise SwitchError("Codex auth.json changed while switching; nothing was replaced. Try again.")
            try:
                atomic_write_bytes(self.live, tblob, mode=0o600)
            except OSError as e:
                raise SwitchError(f"Could not write Codex auth.json: {e.strerror or 'I/O error'}") from None
            if self._read_live() != tblob:
                self._restore(live)
                raise SwitchError("Codex auth.json did not verify after writing; the previous login was restored")
            data["activeAccountNumber"] = t
            data["accounts"][str(t)]["activatedAt"] = _now()
            try:
                self._save(data)
            except OSError as e:
                self._restore(live)
                raise SwitchError(f"Could not save the Codex account list: {e.strerror or 'I/O error'}") from None
        self._log.info(f"Codex switched to slot {t}")
        self.announce_switch(t)
        return t, True

    def announce_switch(self, slot: int) -> None:
        """Tell Codex sessions started with `aswap codex run` that the account changed.

        They watch this file and restart onto the new login (see codex_run). Best effort:
        the switch itself has already happened.
        """
        try:
            atomic_write_text(self.root / "switched.json", json.dumps({"slot": slot, "at": _now()}))
        except OSError:
            self._log.warning("could not write the Codex switch marker")

    def remove(self, target: str, yes: bool = False) -> int:
        self._require_file_store()
        with self._locked(codex_lock=False):
            data = self._load()
            n = self._resolve(data, target)
            if data["activeAccountNumber"] == n and not yes:
                raise SwitchError(
                    f"Codex slot {n} is the active login. The live auth.json is left alone "
                    "either way; pass -y to remove the slot."
                )
            self._store.delete_account_credentials_strict(f"codex-{n}", data["accounts"][str(n)]["email"])
            del data["accounts"][str(n)]
            data["sequence"] = [k for k in data["sequence"] if k != n]
            if data["activeAccountNumber"] == n:
                data["activeAccountNumber"] = None
            self._save(data)
        return n

    def status(self) -> dict:
        self._require_file_store()
        live = self._read_live()
        if not live:
            return {"state": "absent"}
        try:
            auth = parse_auth(live)
        except SwitchError:
            return {"state": "unreadable"}
        data = self._load()
        n = self._match(data, auth)
        exp = auth.access_exp
        return {
            "state": "managed" if n else "unmanaged",
            "slot": n,
            "email": auth.email,
            "plan": auth.plan,
            "authMode": auth.mode,
            "lastRefresh": auth.last_refresh.isoformat() if auth.last_refresh else None,
            "accessExpiresInSeconds": int((exp - datetime.now(timezone.utc)).total_seconds()) if exp else None,
            "usage": data["accounts"][str(n)].get("usage") if n else None,
        }
