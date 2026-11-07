"""OpenAI Codex CLI account switching (``cswap codex ...``).

Local files only: no OpenAI request of any kind, no token refresh, and never
``codex login`` / ``codex logout`` (logout revokes the login server-side).
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
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from claude_swap import paths, spawn
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
                'cswap codex supports "file" only'
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
        missing = SwitchError("No Codex login found. Run `codex login` first, then `cswap codex add`.")
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
                taken = {a.get("alias") for k, a in data["accounts"].items() if k != str(n)}
                if alias.isdigit() or "@" in alias or alias in taken:
                    raise SwitchError(f"Alias {alias!r} is not usable (numeric, an email, or already taken)")
                data["accounts"][str(n)]["alias"] = alias
            data["activeAccountNumber"] = n
            self._save(data)
        self._log.info(f"Codex slot {n} {'added' if created else 'refreshed'}")
        return n, created

    def rows(self) -> list[dict]:
        self._require_file_store()
        data = self._load()
        active = data["activeAccountNumber"]
        return [
            {
                "slot": int(n),
                "alias": a.get("alias", ""),
                "email": a["email"],
                "plan": a["plan"],
                "authMode": a["authMode"],
                "active": int(n) == active,
                "usage": a.get("usage"),
            }
            for n, a in sorted(data["accounts"].items(), key=lambda kv: int(kv[0]))
        ]

    def _choose(self, data: dict, target: str | None, strategy: str | None) -> int:
        if target:
            return self._resolve(data, target)
        active = data["activeAccountNumber"]
        order = [n for n in data["sequence"] if data["accounts"][str(n)]["authMode"] != "apikey"]
        others = [n for n in order if n != active]
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
            raise SwitchError("No Codex accounts yet. Log in with Codex, then run `cswap codex add`.")
        self.root.mkdir(parents=True, exist_ok=True)
        with self._locked():
            data = self._load()
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
                            "Run `cswap codex add` first."
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
        return t, True

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
