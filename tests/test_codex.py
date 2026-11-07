"""`cswap codex`: account switching for the OpenAI Codex CLI.

Every test points CODEX_HOME at a tmp_path; nothing here touches a real
~/.codex, makes a network call or runs `codex`.
"""

from __future__ import annotations

import base64
import json
import re
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from claude_swap import cli, codex
from claude_swap.credentials import CredentialStore
from claude_swap.exceptions import ConfigError, LockError, SwitchError
from claude_swap.locking import FileLock
from claude_swap.models import Platform


def _b64url(obj) -> str:
    raw = json.dumps(obj).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def fake_jwt(claims: dict) -> str:
    return f"{_b64url({'alg': 'none'})}.{_b64url(claims)}.sig"


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000000Z")


T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)


def make_auth(
    email="a@example.com",
    user="user-A",
    account="acct-A",
    plan="plus",
    refresh="rt-A-1",
    last_refresh=T0,
    exp=None,
    extra=None,
) -> str:
    """The exact shape Codex writes: pretty JSON, no trailing newline."""
    exp = exp if exp is not None else datetime.now(timezone.utc) + timedelta(days=9)
    auth_claims = {
        "chatgpt_account_id": account,
        "chatgpt_user_id": user,
        "chatgpt_plan_type": plan,
    }
    doc = {
        "OPENAI_API_KEY": None,
        "auth_mode": "chatgpt",
        "last_refresh": _ts(last_refresh),
        "tokens": {
            "access_token": fake_jwt(
                {"exp": int(exp.timestamp()), "https://api.openai.com/auth": auth_claims}
            ),
            "id_token": fake_jwt(
                {"email": email, "https://api.openai.com/auth": auth_claims}
            ),
            "refresh_token": refresh,
            "account_id": account,
            "login_owner_id": f"owner-{account}",
            "oauth_provenance": {"issuer": "https://auth.example", "client_id": "cid"},
        },
    }
    doc.update(extra or {})
    return json.dumps(doc, indent=2)


def make_apikey_auth(key="sk-test-0123456789abcdef") -> str:
    return json.dumps({"OPENAI_API_KEY": key, "auth_mode": "apikey", "tokens": None}, indent=2)


A = dict(email="a@example.com", user="user-A", account="acct-A", refresh="rt-A-1")
B = dict(email="b@example.com", user="user-B", account="acct-B", refresh="rt-B-1")
C = dict(email="c@example.com", user="user-C", account="acct-C", refresh="rt-C-1")


class Env:
    def __init__(self, home: Path):
        self.home = home
        self.live = home / "auth.json"

    def login(self, text: str) -> bytes:
        self.live.write_text(text, encoding="utf-8", newline="")
        self.live.chmod(0o600)
        return text.encode()

    def live_bytes(self) -> bytes:
        return self.live.read_bytes()

    def rotate(self, spec: dict, new_refresh: str, when: datetime) -> bytes:
        """Act as Codex: an atomic rewrite with a rotated refresh token."""
        text = make_auth(**{**spec, "refresh": new_refresh, "last_refresh": when})
        tmp = self.home / "auth.json.rot"
        tmp.write_text(text, encoding="utf-8", newline="")
        tmp.replace(self.live)
        return text.encode()

    def sw(self) -> codex.Codex:
        return codex.Codex()


@pytest.fixture(autouse=True)
def _linux_file_backend(monkeypatch):
    """The .enc backend: files are inspectable. Keychain routing has its own test."""
    monkeypatch.setattr(Platform, "detect", classmethod(lambda cls: Platform.LINUX))
    monkeypatch.setattr(codex, "LOCK_TIMEOUT", 0.3)
    monkeypatch.setattr(codex, "codex_running", lambda: False)


@pytest.fixture
def env(tmp_path, monkeypatch) -> Env:
    home = tmp_path / "codex_home"
    home.mkdir(mode=0o700)
    (home / "auth.lock").write_bytes(b"")
    monkeypatch.setenv("CODEX_HOME", str(home))
    return Env(home)


def two_accounts(env: Env, **kw):
    """Slots 1 (A) and 2 (B) added; live is B."""
    sw = env.sw()
    env.login(make_auth(**A, **kw))
    sw.add()
    env.login(make_auth(**B, **kw))
    sw.add()
    return sw


# --- 1. round trip ---------------------------------------------------------


def test_round_trip_is_byte_exact_and_private(env):
    a = env.login(make_auth(**A, extra={"future_key": {"x": [1, 2]}}))
    sw = env.sw()
    assert sw.add() == (1, True)
    b = env.login(make_auth(**B))
    assert sw.add() == (2, True)

    assert sw.switch("1") == (1, True)
    assert env.live_bytes() == a
    assert sw.switch("2") == (2, True)
    assert env.live_bytes() == b
    assert sw.switch("a@example.com") == (1, True)
    assert env.live_bytes() == a
    if sys.platform != "win32":  # NTFS has no POSIX mode bits
        assert env.live.stat().st_mode & 0o777 == 0o600
    assert sw._read_blob(1) == a and sw._read_blob(2) == b
    assert json.loads(sw.seq.read_text())["activeAccountNumber"] == 1


def test_switch_to_the_active_slot_changes_nothing(env):
    sw = two_accounts(env)
    before = env.live_bytes()
    assert sw.switch("2") == (2, False)
    assert env.live_bytes() == before


# --- 2. capture-back ---------------------------------------------------------


def test_capture_back_keeps_the_rotated_refresh_token(env):
    sw = two_accounts(env)
    sw.switch("1")
    rotated = env.rotate(A, "rt-A-2", T0 + timedelta(days=1))

    sw.switch("2")
    assert sw._read_blob(1) == rotated
    prev = sw.credentials_dir / ".creds-codex-1-a@example.com.enc.prev"
    assert b"rt-A-1" in base64.b64decode(base64.b64decode(prev.read_text()))

    sw.switch("1")
    assert b"rt-A-2" in env.live_bytes() and b"rt-A-1" not in env.live_bytes()


# --- 3. stale backup is never written over a newer one -----------------------


def test_stale_live_does_not_overwrite_a_newer_backup(env):
    sw = two_accounts(env)
    sw.switch("1")  # live: A as of T0
    newer = make_auth(**{**A, "refresh": "rt-A-9"}, last_refresh=T0 + timedelta(days=3))
    sw._write_blob(1, newer.encode())

    sw.switch("2")
    assert sw._read_blob(1) == newer.encode()


# --- 4. unmanaged live login -------------------------------------------------


def test_unmanaged_live_login_is_refused_and_nothing_changes(env):
    sw = two_accounts(env)
    stranger = env.login(make_auth(**C))
    roster = sw.seq.read_bytes()

    with pytest.raises(SwitchError, match="cswap codex add"):
        sw.switch("1")
    assert env.live_bytes() == stranger
    assert sw.seq.read_bytes() == roster
    assert sw._read_blob(2) is not None


def test_unparsable_live_file_is_refused_too(env):
    sw = two_accounts(env)
    junk = env.login("{not json")
    with pytest.raises(SwitchError):
        sw.switch("1")
    assert env.live_bytes() == junk


# --- 5. compare-and-save ----------------------------------------------------


def _rotate_after_capture(monkeypatch, sw, env, times):
    real = sw._capture
    state = {"n": 0}

    def capture(*a, **k):
        real(*a, **k)
        if state["n"] < times:
            state["n"] += 1
            state["last"] = env.rotate(A, f"rt-A-{state['n'] + 1}", T0 + timedelta(days=state["n"]))

    monkeypatch.setattr(sw, "_capture", capture)
    return state


def test_a_single_unlocked_writer_is_recaptured(env, monkeypatch):
    sw = two_accounts(env)
    sw.switch("1")
    state = _rotate_after_capture(monkeypatch, sw, env, times=1)

    sw.switch("2")
    assert sw._read_blob(1) == state["last"]  # the rotation was captured, not lost
    assert b"rt-B-1" in env.live_bytes()


def test_a_second_change_aborts_with_live_untouched(env, monkeypatch):
    sw = two_accounts(env)
    sw.switch("1")
    state = _rotate_after_capture(monkeypatch, sw, env, times=2)

    with pytest.raises(SwitchError, match="changed"):
        sw.switch("2")
    assert env.live_bytes() == state["last"]


# --- 6. locks ---------------------------------------------------------------


def test_held_auth_lock_times_out_and_writes_nothing(env):
    sw = two_accounts(env)
    sw.switch("1")
    before, roster = env.live_bytes(), sw.seq.read_bytes()
    holder = FileLock(env.home / "auth.lock")
    assert holder.acquire()
    try:
        with pytest.raises(LockError):
            sw.switch("2")
    finally:
        holder.release()
    assert env.live_bytes() == before and sw.seq.read_bytes() == roster


def test_lock_order_is_cswap_then_codex(env, monkeypatch):
    sw = two_accounts(env)
    events = []

    class Recording:
        def __init__(self, path, timeout=10.0):
            self.path = path

        def __enter__(self):
            events.append(("enter", self.path))
            return self

        def __exit__(self, *a):
            events.append(("exit", self.path))

    monkeypatch.setattr(codex, "FileLock", Recording)
    sw.switch("1")
    mine, theirs = sw.root / ".lock", env.home / "auth.lock"
    assert events == [
        ("enter", mine), ("enter", theirs), ("exit", theirs), ("exit", mine),
    ]


# --- 7. atomicity -------------------------------------------------------------


def test_failed_rename_leaves_live_unchanged_and_no_temp(env, monkeypatch):
    sw = two_accounts(env)
    before = env.live_bytes()

    def boom(*a, **k):
        raise OSError("rename refused")

    monkeypatch.setattr("claude_swap.fsutil.replace_with_retry", boom)
    with pytest.raises(SwitchError):
        sw.switch("1")
    assert env.live_bytes() == before
    assert sorted(p.name for p in env.home.iterdir()) == ["auth.json", "auth.lock"]


# --- 8. post-verify and roster failure ----------------------------------------


def test_post_verify_mismatch_restores_live(env, monkeypatch):
    sw = two_accounts(env)
    before = env.live_bytes()
    real = codex.atomic_write_bytes
    calls = []

    def torn(path, data, **kw):
        calls.append(1)
        real(path, data + b" " if len(calls) == 1 else data, **kw)

    monkeypatch.setattr(codex, "atomic_write_bytes", torn)
    with pytest.raises(SwitchError, match="verif"):
        sw.switch("1")
    assert env.live_bytes() == before


def test_roster_write_failure_restores_live(env, monkeypatch):
    sw = two_accounts(env)
    before = env.live_bytes()

    def boom(data):
        raise OSError("disk full")

    monkeypatch.setattr(sw, "_save", boom)
    with pytest.raises(SwitchError, match="account list"):
        sw.switch("1")
    assert env.live_bytes() == before


# --- 9. storage mode -----------------------------------------------------------


@pytest.mark.parametrize("verb", ["add", "list", "switch", "remove", "status"])
@pytest.mark.parametrize("mode", ["keyring", "auto", "ephemeral"])
def test_non_file_storage_refuses_every_verb(env, verb, mode):
    live = env.login(make_auth(**A))
    (env.home / "config.toml").write_text(f'cli_auth_credentials_store = "{mode}"\n')
    sw = env.sw()
    with pytest.raises(ConfigError, match="file"):
        {
            "add": sw.add,
            "list": sw.rows,
            "switch": lambda: sw.switch("1"),
            "remove": lambda: sw.remove("1"),
            "status": sw.status,
        }[verb]()
    assert env.live_bytes() == live
    assert not sw.root.exists()


def test_explicit_file_storage_is_fine(env):
    env.login(make_auth(**A))
    (env.home / "config.toml").write_text('cli_auth_credentials_store = "file"\n')
    assert env.sw().add() == (1, True)


# --- 10. key collision with Claude slots ---------------------------------------


def test_codex_and_claude_slots_with_one_email_do_not_cross(env, monkeypatch, block_real_keychain):
    monkeypatch.setattr(Platform, "detect", classmethod(lambda cls: Platform.MACOS))
    claude_host = SimpleNamespace(
        platform=Platform.MACOS,
        credentials_dir=env.home.parent / "claude_creds",
        sequence_file=env.home.parent / "claude_seq.json",
        _logger=__import__("logging").getLogger("t"),
    )
    claude = CredentialStore(claude_host)
    claude._write_account_credentials("1", "a@example.com", "CLAUDE-BLOB")

    blob = env.login(make_auth(**A))
    sw = env.sw()
    sw.add()

    assert claude._read_account_credentials("1", "a@example.com") == "CLAUDE-BLOB"
    assert sw._read_blob(1) == blob
    names = {account for (_svc, account) in block_real_keychain.data}
    assert {"account-1-a@example.com", "account-codex-1-a@example.com"} <= names


# --- 11. API-key accounts -------------------------------------------------------


def test_api_key_account_is_stored_listed_and_never_auto_picked(env):
    sw = env.sw()
    env.login(make_auth(**A))
    sw.add()
    key = env.login(make_apikey_auth())
    assert sw.add() == (2, True)
    env.login(make_auth(**B))
    sw.add()

    rows = {r["slot"]: r for r in sw.rows()}
    assert rows[2]["plan"] == "api" and rows[2]["email"] == "" and rows[2]["usage"] is None

    assert sw.switch(None) == (1, True)  # rotation from B skips the API key
    assert sw.switch(None) == (3, True)
    assert sw.switch("2") == (2, True)  # explicit is allowed
    assert env.live_bytes() == key
    assert "sk-test" not in sw.seq.read_text()


def test_rotation_with_no_other_account_says_so(env):
    sw = env.sw()
    env.login(make_auth(**A))
    sw.add()
    with pytest.raises(SwitchError, match="no other"):
        sw.switch(None)


# --- add / remove / status --------------------------------------------------------


def test_re_add_updates_the_slot_and_keeps_alias(env):
    sw = env.sw()
    env.login(make_auth(**A))
    sw.add(alias="work")
    rotated = env.rotate(A, "rt-A-2", T0 + timedelta(days=1))
    assert sw.add() == (1, False)
    assert sw._read_blob(1) == rotated
    assert sw.switch("work") == (1, False)


def test_add_without_a_login_is_an_error_and_writes_nothing(env):
    sw = env.sw()
    with pytest.raises(SwitchError, match="codex login"):
        sw.add()
    assert not sw.root.exists()


def test_add_refuses_a_file_with_no_credentials(env):
    env.login(json.dumps({"OPENAI_API_KEY": None, "tokens": None}))
    with pytest.raises(SwitchError):
        env.sw().add()


def test_remove_deletes_blob_prev_and_roster_entry_but_not_live(env):
    sw = two_accounts(env)
    sw.switch("1")
    env.rotate(A, "rt-A-2", T0 + timedelta(days=1))
    sw.switch("2")  # creates slot 1's .prev
    live = env.live_bytes()
    assert sw.remove("1") == 1
    assert sw._read_blob(1, "a@example.com") is None
    assert not list(sw.credentials_dir.glob(".creds-codex-1-*"))
    assert "1" not in json.loads(sw.seq.read_text())["accounts"]
    assert env.live_bytes() == live


def test_remove_of_the_active_slot_needs_yes(env):
    sw = two_accounts(env)
    with pytest.raises(SwitchError, match="-y"):
        sw.remove("2")
    assert sw.remove("2", yes=True) == 2
    assert json.loads(sw.seq.read_text())["activeAccountNumber"] is None


def test_status_reports_slot_or_unmanaged_without_token_values(env):
    sw = two_accounts(env)
    s = sw.status()
    assert s["state"] == "managed" and s["slot"] == 2 and s["email"] == "b@example.com"
    assert s["accessExpiresInSeconds"] > 8 * 86400
    env.login(make_auth(**C))
    assert sw.status()["state"] == "unmanaged"
    (env.home / "auth.json").unlink()
    assert sw.status()["state"] == "absent"


# --- phase 2: --slot / -y prompts, running-codex guard ------------------------------


def test_add_into_an_occupied_slot_asks_first(env, monkeypatch):
    sw = env.sw()
    env.login(make_auth(**A))
    sw.add()
    env.login(make_auth(**B))
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    with pytest.raises(SwitchError, match="slot 1"):
        sw.add(slot=1)
    assert sw._read_blob(1) is not None and b"rt-A-1" in sw._read_blob(1)


def test_add_overwrite_with_yes_drops_the_old_key(env):
    sw = env.sw()
    env.login(make_auth(**A))
    sw.add()
    b = env.login(make_auth(**B))
    assert sw.add(slot=1, yes=True) == (1, True)
    assert sw._read_blob(1) == b
    assert not list(sw.credentials_dir.glob(".creds-codex-1-a@*"))


def test_non_interactive_overwrite_without_yes_is_refused(env, monkeypatch):
    sw = env.sw()
    env.login(make_auth(**A))
    sw.add()
    env.login(make_auth(**B))

    def eof(*_):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    with pytest.raises(SwitchError):
        sw.add(slot=1)


def _soon():
    return datetime.now(timezone.utc) + timedelta(minutes=3)


def test_guard_refuses_near_expiry_token_while_codex_runs(env, monkeypatch):
    sw = two_accounts(env, exp=_soon())
    before = env.live_bytes()
    monkeypatch.setattr(codex, "codex_running", lambda: True)
    with pytest.raises(SwitchError, match="--force"):
        sw.switch("1")
    assert env.live_bytes() == before
    assert sw.switch("1", force=True) == (1, True)


def test_guard_is_quiet_when_codex_is_not_running_or_token_is_fresh(env, monkeypatch):
    sw = two_accounts(env, exp=_soon())
    assert sw.switch("1") == (1, True)  # codex_running -> False
    monkeypatch.setattr(codex, "codex_running", lambda: True)
    env.login(make_auth(**A))  # exp 9 days out
    assert sw.switch("2") == (2, True)


# --- 12. output hygiene -------------------------------------------------------------


def test_no_secret_reaches_stdout_stderr_or_logs(env, monkeypatch, capsys, caplog, tmp_path):
    caplog.set_level(0, logger="claude-swap")
    secrets: list[str] = []

    def run(*argv):
        monkeypatch.setattr(sys, "argv", ["cswap", "codex", *argv, "--debug"])
        try:
            cli.main()
        except SystemExit:
            pass

    for spec in (A, B):
        text = env.login(make_auth(**spec)).decode()
        doc = json.loads(text)["tokens"]
        secrets += [doc["access_token"], doc["id_token"], doc["refresh_token"], doc["login_owner_id"]]
        run("add")
    env.login(make_apikey_auth())
    secrets.append("sk-test-0123456789abcdef")
    run("add")
    run("switch", "1")  # from the API key: captured, then A written
    run("switch", "2")
    run("list")
    run("status")
    run("list", "--json")
    run("status", "--json")
    run("remove", "1", "-y")
    run("remove", "3", "-y")

    out, err = capsys.readouterr()
    log_text = "\n".join(r.getMessage() for r in caplog.records)
    for log in Path(tmp_path).rglob("claude-swap.log*"):
        log_text += log.read_text()
    haystack = out + err + log_text
    assert "added" in haystack.lower() or "Added" in haystack  # the cycle really ran
    for s in secrets:
        assert s not in haystack
    assert not re.search(r"eyJ[\w-]*\.[\w-]+\.", haystack)
    assert "refresh_token" not in haystack
    assert "rt-A" not in haystack and "rt-B" not in haystack


def test_cli_prints_the_restart_caveat_on_switch(env, monkeypatch, capsys):
    two_accounts(env)
    monkeypatch.setattr(sys, "argv", ["cswap", "codex", "switch", "1"])
    cli.main()
    assert "keep their account until restarted" in capsys.readouterr().out


def test_cli_errors_exit_nonzero_without_a_traceback(env, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["cswap", "codex", "switch", "1"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 1
    assert "Error:" in capsys.readouterr().err


def test_holding_the_lock_from_a_thread_blocks_then_releases(env):
    sw = two_accounts(env)
    lock = FileLock(env.home / "auth.lock")
    assert lock.acquire()
    threading.Timer(0.1, lock.release).start()
    sw.switch("1")  # waits out the 0.1 s hold within the 0.3 s timeout
    assert b"rt-A-1" in env.live_bytes()


# --- phase 3: usage from local rollout files ----------------------------------------

NOW = datetime.now(timezone.utc)


def window(pct, minutes=300, resets_in=3600):
    return {"used_percent": pct, "window_minutes": minutes, "resets_at": int(NOW.timestamp()) + resets_in}


def limits(primary, secondary, limit_id="codex"):
    return {"limit_id": limit_id, "limit_name": None, "plan_type": "plus",
            "primary": primary, "secondary": secondary, "credits": None}


def rollout(env, name, creator=("user-A", "acct-A"), events=(), torn=False):
    """Synthetic rollout: a session_meta line, then token_count events."""
    meta = {"id": name, "base_instructions": "private words"}
    if creator:
        meta.update(creator_user_id=creator[0], creator_account_id=creator[1])
    lines = [{"timestamp": "2026-11-05T09:00:00.000Z", "type": "session_meta", "payload": meta}]
    for ts, rl in events:
        lines.append({"timestamp": ts, "type": "event_msg",
                      "payload": {"type": "token_count", "info": None, "rate_limits": rl}})
    path = env.home / "sessions" / "2026" / "11" / "05" / f"rollout-{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(x) for x in lines) + "\n"
    path.write_text(text + ('{"timestamp": "2026-11-05T12:00:00.000Z", "type": "event_ms' if torn else ""))
    return path


def usage_by_slot(sw):
    return {r["slot"]: r["usage"] for r in sw.rows()}


def test_usage_is_attributed_by_creator_account_and_latest_event_wins(env):
    sw = two_accounts(env)
    rollout(env, "a", ("user-A", "acct-A"), [
        ("2026-11-05T10:00:00.000Z", limits(window(10), window(20, 10080))),
        ("2026-11-05T11:00:00.000Z", limits(window(30), window(25, 10080))),
    ])
    rollout(env, "b", ("user-B", "acct-B"), [("2026-11-05T10:30:00.000Z", limits(window(70), None))])
    rollout(env, "c", ("user-C", "acct-C"), [("2026-11-05T10:30:00.000Z", limits(window(99), None))])
    u = usage_by_slot(sw)
    assert u[1]["primary"]["used_percent"] == 30 and u[1]["secondary"]["used_percent"] == 25
    assert u[1]["seenAt"] == "2026-11-05T11:00:00.000Z"
    assert u[2]["primary"]["used_percent"] == 70 and u[2]["secondary"] is None


def test_usage_ignores_null_premium_legacy_and_torn_lines(env):
    sw = two_accounts(env)
    rollout(env, "a", ("user-A", "acct-A"), [
        ("2026-11-05T10:00:00.000Z", limits(window(11), window(12, 10080))),
        ("2026-11-05T11:00:00.000Z", limits(window(88), None, limit_id="premium")),
        ("2026-11-05T11:30:00.000Z", None),
    ], torn=True)
    rollout(env, "old", None, [("2026-11-05T12:00:00.000Z", limits(window(77), None))])
    u = usage_by_slot(sw)
    assert u[1]["primary"]["used_percent"] == 11
    assert u[2] is None  # the unattributed legacy rollout is not guessed onto anyone


def _append_huge_line(path, kb=400):
    """A tool output bigger than the old 256 KB tail window, after the last reading."""
    big = json.dumps({"timestamp": "2026-11-05T12:30:00.000Z", "type": "response_item",
                      "payload": {"output": "x" * kb * 1024}})
    with path.open("a") as fh:
        fh.write(big + "\n")


def test_the_newest_reading_is_found_behind_a_huge_trailing_line(env):
    """Regression: a long session whose file ends in one oversized line yielded
    nothing, and the scan silently fell back to an OLDER session's lower number."""
    sw = two_accounts(env)
    rollout(env, "old", ("user-A", "acct-A"), [("2026-11-05T09:30:00.000Z", limits(window(90), window(33, 10080)))])
    new = rollout(env, "new", ("user-A", "acct-A"), [("2026-11-05T11:00:00.000Z", limits(window(8), window(35, 10080)))])
    _append_huge_line(new)
    import os, time
    os.utime(new, (time.time() + 10, time.time() + 10))  # "new" is the most recently written
    u = usage_by_slot(sw)[1]
    assert u["primary"]["used_percent"] == 8 and u["secondary"]["used_percent"] == 35
    assert u["seenAt"] == "2026-11-05T11:00:00.000Z"


def test_the_backwards_scan_gives_up_after_its_budget(env, monkeypatch):
    sw = two_accounts(env)
    path = rollout(env, "a", ("user-A", "acct-A"), [("2026-11-05T10:00:00.000Z", limits(window(30), None))])
    _append_huge_line(path)
    monkeypatch.setattr(codex, "_MAX_SCAN", 100 * 1024)
    assert usage_by_slot(sw)[1] is None  # the documented limit, not a crash


def test_usage_survives_rollout_rotation_and_stores_only_numbers(env):
    sw = two_accounts(env)
    path = rollout(env, "a", ("user-A", "acct-A"), [("2026-11-05T10:00:00.000Z", limits(window(42), None))])
    usage_by_slot(sw)
    path.unlink()
    assert usage_by_slot(sw)[1]["primary"]["used_percent"] == 42
    stored = json.loads(sw.seq.read_text())["accounts"]["1"]["usage"]
    assert set(stored) == {"primary", "secondary", "seenAt"}
    assert "private words" not in sw.seq.read_text()


def test_an_expired_window_counts_as_zero_and_reads_reset():
    now = NOW.timestamp()
    usage = {"primary": window(90, resets_in=-5), "secondary": window(40, 10080), "seenAt": "2026-11-05T10:00:00.000Z"}
    assert codex.load_percent(usage, now) == 40
    text = codex.describe_usage(usage, now)
    assert "5h reset" in text and "wk 40%" in text
    assert codex.describe_usage(None, now) == "-"


def test_cli_list_shows_usage(env, monkeypatch, capsys):
    two_accounts(env)
    rollout(env, "a", ("user-A", "acct-A"), [("2026-11-05T10:00:00.000Z", limits(window(30), window(25, 10080)))])
    monkeypatch.setattr(sys, "argv", ["cswap", "codex", "list"])
    cli.main()
    out = capsys.readouterr().out
    assert "5h 30%" in out and "wk 25%" in out and "a@example.com" in out


def seed(env, a=None, b=None, c=None):
    for creator, w in ((("user-A", "acct-A"), a), (("user-B", "acct-B"), b), (("user-C", "acct-C"), c)):
        if w is not None:
            rollout(env, creator[1], creator, [("2026-11-05T10:00:00.000Z", limits(*w))])


def three_accounts(env):
    sw = two_accounts(env)
    env.login(make_auth(**C))
    sw.add()
    return sw


def test_best_picks_the_lowest_load_and_counts_the_weekly_window(env):
    sw = three_accounts(env)
    seed(env, a=(window(5), window(100, 10080)), b=(window(50), window(10, 10080)), c=(window(20), None))
    sw.switch("2")
    assert sw.switch(None, strategy="best") == (3, True)  # A is capped weekly, C beats B


def test_best_treats_a_reset_window_as_empty(env):
    sw = three_accounts(env)
    seed(env, a=(window(95, resets_in=-60), None), b=(window(50), None), c=(window(60), None))
    assert sw.switch(None, strategy="best") == (1, True)


def test_best_skips_api_keys_and_unknown_slots(env):
    sw = env.sw()
    env.login(make_auth(**A))
    sw.add()
    env.login(make_apikey_auth())
    sw.add()
    env.login(make_auth(**B))
    sw.add()
    seed(env, a=(window(80), None))  # B has no snapshot, the key never counts
    assert sw.switch(None, strategy="best") == (1, True)


def test_best_without_any_snapshot_falls_back_to_rotation(env):
    sw = two_accounts(env)
    assert sw.switch(None, strategy="best") == (1, True)


def test_cli_rejects_a_target_with_a_strategy(env, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["cswap", "codex", "switch", "1", "--strategy", "best"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 2
