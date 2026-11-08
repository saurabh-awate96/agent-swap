"""`cswap gui`: the loopback dashboard server and its safety checks.

The server is the only network-facing surface cswap has, so most of these
tests are about what it refuses: a request from the wrong Host (DNS
rebinding), the wrong Origin or a non-JSON body (CSRF), a missing token, or
any change while read-only. A refused request must never reach the switcher.
"""

from __future__ import annotations

import http.client
import json
import re
import threading
from pathlib import Path

import pytest

from claude_swap.exceptions import AccountNotFoundError, ConfigError, SwitchError
from claude_swap.gui.server import GuiServer


class StubSwitcher:
    """The four calls the server makes, recorded; no real accounts."""

    def __init__(self, backup_dir: Path):
        self.backup_dir = backup_dir
        self.calls: list[tuple] = []
        self.fail_with: Exception | None = None
        self.block: threading.Event | None = None

    def list_accounts(self, json_output=False, refresh_idle=True):
        self.calls.append(("list",))
        self.list_refresh_idle = refresh_idle
        return {
            "schemaVersion": 1,
            "activeAccountNumber": 1,
            "accounts": [{
                "number": 1, "email": "a@example.com", "active": True,
                "usageStatus": "ok",
                "usage": {"fiveHour": {"pct": 10.0, "resetsAt": None}},
            }],
        }

    live = ("me@example.com", "")  # (email, org uuid) of the login Claude Code is using
    saved = False

    def _get_current_identity_triple(self):
        return (self.live[0], self.live[1], "acct-uuid") if self.live else None

    def _account_exists(self, email, organization_uuid):
        return self.saved

    def switch_to(self, identifier, json_output=False, force=False):
        self.calls.append(("switch_to", identifier))
        if self.block is not None:
            self.block.wait(5)
        if self.fail_with is not None:
            raise self.fail_with
        return {"switched": True, "from": {"number": 1}, "to": {"number": int(identifier)}}

    def set_account_disabled(self, identifier, disabled):
        self.calls.append(("disable", identifier, disabled))
        print(f"Disabled Account-{identifier}.")

    def add_account(self):
        self.calls.append(("add",))
        print("Added Account-2.")


class StubCodex:
    """The Codex calls the server makes, recorded; never reads a real ~/.codex."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.error: Exception | None = None

    def rows(self, persist=True, probe=False):
        self.calls.append(("rows", persist))
        self.probe = probe
        if self.error is not None:
            raise self.error
        return [{
            "slot": 1, "alias": "main", "email": "c@example.com", "plan": "plus",
            "authMode": "chatgpt", "active": True,
            "usage": {"primary": {"used_percent": 45.0, "window_minutes": 300, "resets_at": 1},
                      "secondary": None, "seenAt": "2026-11-07T12:00:00Z"},
        }]

    state = "managed"

    def status(self):
        return {"state": self.state, "slot": 1 if self.state == "managed" else None,
                "email": "live@example.com", "plan": "plus", "authMode": "chatgpt"}

    def switch(self, target=None, strategy=None, force=False):
        self.calls.append(("switch", target))
        self.last_strategy = strategy
        return (int(target) if target else 2), True

    def set_disabled(self, target, disabled):
        self.calls.append(("disable", target, disabled))
        return int(target)

    def set_alias(self, target, alias):
        self.calls.append(("alias", target, alias))
        return int(target)

    def set_threshold(self, target, pct):
        self.calls.append(("threshold", target, pct))
        return int(target)

    def add_from_api_key(self, key, alias=None):
        self.calls.append(("apikey",))
        return 3, True

    def remove(self, target, yes=False):
        self.calls.append(("remove", target, yes))
        return int(target)

    def add(self, slot=None, alias=None, yes=False):
        self.calls.append(("add",))
        return 2, True


@pytest.fixture
def stub(tmp_path):
    return StubSwitcher(tmp_path)


@pytest.fixture
def codex():
    return StubCodex()


def _start(stub, write, codex=None):
    server = GuiServer(stub, write=write, codex=codex or StubCodex())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def ro(stub, codex):
    server = _start(stub, write=False, codex=codex)
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def rw(stub, codex):
    server = _start(stub, write=True, codex=codex)
    yield server
    server.shutdown()
    server.server_close()


def call(server, method, path, *, headers=None, body=None, host="default", token=True):
    """One request. ``host`` overrides the Host header; ``token`` adds the bearer."""
    port = server.server_port
    h = {}
    if host == "default":
        h["Host"] = f"127.0.0.1:{port}"
    elif host is not None:
        h["Host"] = host
    if token:
        h["Authorization"] = f"Bearer {server.token}"
    h.update(headers or {})
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    # http.client adds its own Host when none is given; skip_host stops that so
    # a test can send a forged or missing one.
    conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    for name, value in h.items():
        conn.putheader(name, value)
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        if "Content-Length" not in h:
            conn.putheader("Content-Length", str(len(data)))
    conn.endheaders(data)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = None
    return resp.status, parsed, resp


def post(server, path, body, **kw):
    headers = {
        "Origin": f"http://127.0.0.1:{server.server_port}",
        "Content-Type": "application/json",
    }
    headers.update(kw.pop("headers", {}))
    return call(server, "POST", path, headers=headers, body=body, **kw)


class TestStartup:
    def test_binds_loopback_only_and_is_read_only_by_default(self, ro):
        assert ro.server_address[0] == "127.0.0.1"
        assert ro.server_port != 0
        assert ro.write is False

    def test_each_run_gets_a_fresh_long_token(self, stub):
        a, b = GuiServer(stub), GuiServer(stub)
        try:
            assert a.token != b.token
            assert len(a.token) >= 22
        finally:
            a.server_close()
            b.server_close()


class TestHost:
    @pytest.mark.parametrize("path", ["/", "/api/accounts"])
    def test_a_forged_host_is_refused(self, ro, stub, path):
        status, _, _ = call(ro, "GET", path, host="evil.example")
        assert status == 403
        assert stub.calls == []

    def test_the_wrong_port_is_refused(self, ro):
        assert call(ro, "GET", "/", host="127.0.0.1:1")[0] == 403

    def test_a_missing_host_is_refused(self, ro):
        assert call(ro, "GET", "/", host=None)[0] == 403

    @pytest.mark.parametrize("name", ["127.0.0.1", "localhost"])
    def test_loopback_names_are_accepted(self, ro, name):
        status, _, _ = call(ro, "GET", "/api/settings", host=f"{name}:{ro.server_port}")
        assert status == 200


class TestToken:
    @pytest.mark.parametrize("headers", [
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": "Bearer x"},          # different length
        {"Authorization": "Bearer café"},  # non-ASCII must not raise
    ])
    def test_a_missing_or_wrong_token_is_401(self, ro, stub, headers):
        status, _, _ = call(ro, "GET", "/api/accounts", headers=headers, token=False)
        assert status == 401
        assert stub.calls == []

    def test_the_bare_token_without_bearer_is_refused(self, ro):
        status, _, _ = call(
            ro, "GET", "/api/accounts", token=False,
            headers={"Authorization": ro.token},
        )
        assert status == 401

    def test_a_token_in_the_query_string_is_ignored(self, ro):
        status, _, _ = call(ro, "GET", f"/api/accounts?token={ro.token}", token=False)
        assert status == 401

    @pytest.mark.parametrize("path", ["/", "/app.js", "/app.css"])
    def test_the_static_page_needs_no_token(self, ro, path):
        assert call(ro, "GET", path, token=False)[0] == 200


class TestOrigin:
    @pytest.mark.parametrize("origin", [
        "http://evil.example", "http://127.0.0.1:1", "null",
    ])
    def test_a_foreign_origin_is_refused_on_get_and_post(self, rw, stub, origin):
        assert call(rw, "GET", "/api/accounts", headers={"Origin": origin})[0] == 403
        assert post(rw, "/api/switch", {"number": "2"}, headers={"Origin": origin})[0] == 403
        assert stub.calls == []

    def test_a_post_without_origin_is_refused(self, rw, stub):
        status, _, _ = call(
            rw, "POST", "/api/switch",
            headers={"Content-Type": "application/json"}, body={"number": "2"},
        )
        assert status == 403
        assert stub.calls == []

    def test_a_get_without_origin_is_fine(self, ro):
        assert call(ro, "GET", "/api/accounts")[0] == 200

    def test_cross_site_fetch_metadata_is_refused(self, ro):
        assert call(ro, "GET", "/api/accounts", headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
        assert call(ro, "GET", "/api/accounts", headers={"Sec-Fetch-Site": "same-origin"})[0] == 200


class TestMethodsAndHeaders:
    @pytest.mark.parametrize("method", ["OPTIONS", "PUT", "DELETE", "PATCH"])
    def test_other_methods_get_405_and_no_cors_headers(self, ro, method):
        status, _, resp = call(ro, method, "/api/switch")
        assert status == 405
        assert not [h for h, _ in resp.getheaders() if h.lower().startswith("access-control-")]

    @pytest.mark.parametrize("path", ["/", "/app.js", "/api/accounts", "/nope"])
    def test_every_response_carries_the_security_headers(self, ro, path):
        _, _, resp = call(ro, "GET", path)
        assert "default-src 'none'" in resp.getheader("Content-Security-Policy")
        assert resp.getheader("X-Content-Type-Options") == "nosniff"
        assert resp.getheader("Cache-Control") == "no-store"
        assert resp.getheader("X-Frame-Options") == "DENY"
        assert resp.getheader("Referrer-Policy") == "no-referrer"

    @pytest.mark.parametrize("path", [
        "/../../etc/passwd", "/%2e%2e/x", "/static/../app.js", "/index.html",
    ])
    def test_only_the_three_assets_are_served(self, ro, path):
        assert call(ro, "GET", path, token=False)[0] in (401, 404)


class TestReadOnly:
    def test_a_fully_valid_post_is_refused_and_never_reaches_the_switcher(self, ro, stub):
        status, body, _ = post(ro, "/api/switch", {"number": "2"})
        assert status == 403
        assert body["error"]["type"] == "ReadOnly"
        assert stub.calls == []

    def test_settings_report_whether_changes_are_allowed(self, ro, rw):
        assert call(ro, "GET", "/api/settings")[1]["writable"] is False
        assert call(rw, "GET", "/api/settings")[1]["writable"] is True


class TestWrites:
    def test_switch(self, rw, stub):
        status, body, _ = post(rw, "/api/switch", {"number": "2"})
        assert status == 200
        assert body["switched"] is True
        assert stub.calls == [("switch_to", "2")]

    def test_disable_and_add(self, rw, stub):
        assert post(rw, "/api/disable", {"number": "2", "disabled": True})[0] == 200
        assert post(rw, "/api/add-current", {})[0] == 200
        assert stub.calls == [("disable", "2", True), ("add",)]

    @pytest.mark.parametrize("body", [
        {"number": "a@b.com"}, {"number": "dev"}, {"number": "-1"},
        {"number": "1234"}, {"number": 2}, {}, {"number": "2", "force": True},
        {"number": "2", "provider": "codex"},
    ])
    def test_only_plain_slot_numbers_are_accepted(self, rw, stub, body):
        assert post(rw, "/api/switch", body)[0] == 400
        assert stub.calls == []

    def test_disabled_must_be_a_real_boolean(self, rw, stub):
        assert post(rw, "/api/disable", {"number": "2", "disabled": "true"})[0] == 400
        assert stub.calls == []

    @pytest.mark.parametrize("body", [b"not json", b"[1]", b'"x"'])
    def test_the_body_must_be_a_json_object(self, rw, stub, body):
        assert post(rw, "/api/switch", body)[0] == 400
        assert stub.calls == []

    @pytest.mark.parametrize("ctype", [
        "text/plain", "application/x-www-form-urlencoded", "multipart/form-data",
    ])
    def test_a_non_json_content_type_is_415(self, rw, stub, ctype):
        status, _, _ = post(rw, "/api/switch", {"number": "2"}, headers={"Content-Type": ctype})
        assert status == 415
        assert stub.calls == []

    def test_json_with_a_charset_is_fine(self, rw):
        status, _, _ = post(
            rw, "/api/switch", {"number": "2"},
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        assert status == 200

    def test_an_oversized_body_is_413_and_a_missing_length_is_411(self, rw, stub):
        assert post(rw, "/api/switch", b"x" * 5000)[0] == 413
        status, _, _ = call(
            rw, "POST", "/api/switch",
            headers={
                "Origin": f"http://127.0.0.1:{rw.server_port}",
                "Content-Type": "application/json",
            },
        )
        assert status == 411
        assert stub.calls == []

    def test_a_switcher_error_is_a_400_with_its_type(self, rw, stub):
        stub.fail_with = AccountNotFoundError("No account 9")
        status, body, _ = post(rw, "/api/switch", {"number": "9"})
        assert status == 400
        assert body["error"] == {"type": "AccountNotFoundError", "message": "No account 9"}

    def test_an_unexpected_error_leaks_no_detail(self, rw, stub):
        stub.fail_with = RuntimeError("/Users/me/secret/path blew up")
        status, body, resp = post(rw, "/api/switch", {"number": "2"})
        assert status == 500
        assert "secret" not in json.dumps(body)

    def test_a_second_change_while_one_runs_gets_409(self, rw, stub):
        stub.block = threading.Event()
        first = threading.Thread(target=post, args=(rw, "/api/switch", {"number": "2"}))
        first.start()
        for _ in range(100):
            if stub.calls:
                break
            threading.Event().wait(0.05)
        try:
            assert post(rw, "/api/switch", {"number": "3"})[0] == 409
        finally:
            stub.block.set()
            first.join()
        assert post(rw, "/api/switch", {"number": "3"})[0] == 200


class TestPage:
    def test_the_payload_is_passed_through_unchanged(self, ro):
        status, body, _ = call(ro, "GET", "/api/accounts")
        assert status == 200
        assert body["accounts"][0]["email"] == "a@example.com"

    def test_no_markup_injection_or_external_resources_in_the_shipped_page(self):
        root = Path(__file__).resolve().parents[1] / "src" / "claude_swap" / "gui" / "static"
        bad = re.compile(
            r"innerHTML|outerHTML|insertAdjacentHTML|eval\(|new Function|document\.write"
            r"|\son[a-z]+\s*=|https?://"
        )
        for name in ("index.html", "app.js", "app.css"):
            text = (root / name).read_text(encoding="utf-8")
            assert not bad.search(text), name
        assert "<script>" not in (root / "index.html").read_text(encoding="utf-8")  # no inline script
        assert "<style" not in (root / "index.html").read_text(encoding="utf-8")


class TestCodex:
    def test_a_live_claude_login_that_is_not_saved_is_reported(self, ro, stub):
        _, body, _ = call(ro, "GET", "/api/accounts")
        assert body["unsaved"] == {"email": "me@example.com", "isOrganization": False}

    def test_a_saved_or_absent_live_login_reports_nothing(self, ro, stub):
        stub.saved = True
        assert call(ro, "GET", "/api/accounts")[1]["unsaved"] is None
        stub.saved, stub.live = False, None
        assert call(ro, "GET", "/api/accounts")[1]["unsaved"] is None

    def test_a_live_codex_login_that_is_not_saved_is_reported(self, ro, codex):
        codex.state = "unmanaged"
        _, body, _ = call(ro, "GET", "/api/codex")
        assert body["unsaved"] == {"email": "live@example.com", "plan": "plus", "authMode": "chatgpt"}

    def test_a_managed_codex_login_reports_nothing_unsaved(self, ro, codex):
        _, body, _ = call(ro, "GET", "/api/codex")
        assert body["unsaved"] is None

    def test_polling_never_refreshes_an_idle_accounts_token(self, ro, stub):
        """The page polls on a timer: unattended, like the terminal dashboard."""
        status, _, _ = call(ro, "GET", "/api/accounts")
        assert status == 200
        assert stub.list_refresh_idle is False

    def test_accounts_are_served_beside_claudes(self, ro):
        status, body, _ = call(ro, "GET", "/api/codex")
        assert status == 200
        assert body["accounts"][0]["email"] == "c@example.com"
        assert body["schemaVersion"] == 1

    def test_the_page_asks_for_the_live_reading(self, ro, codex):
        call(ro, "GET", "/api/codex")
        assert codex.probe is True

    def test_reading_never_writes_while_read_only(self, ro, codex):
        call(ro, "GET", "/api/codex")
        assert codex.calls == [("rows", False)]

    def test_reading_may_persist_usage_once_changes_are_allowed(self, rw, codex):
        call(rw, "GET", "/api/codex")
        assert codex.calls == [("rows", True)]

    def test_the_scan_is_cached_between_polls(self, ro, codex):
        for _ in range(3):
            call(ro, "GET", "/api/codex")
        assert codex.calls == [("rows", False)]

    def test_a_codex_problem_does_not_blank_the_page(self, ro, codex):
        codex.error = ConfigError("Codex is set to keyring")
        status, body, _ = call(ro, "GET", "/api/codex")
        assert status == 200
        assert body["accounts"] == []
        assert "keyring" in body["error"]
        assert call(ro, "GET", "/api/accounts")[0] == 200  # Claude half unaffected

    def test_it_needs_the_same_token_and_host(self, ro, codex):
        assert call(ro, "GET", "/api/codex", token=False)[0] == 401
        assert call(ro, "GET", "/api/codex", host="evil.example")[0] == 403
        assert codex.calls == []

    def test_switch_and_add_are_refused_while_read_only(self, ro, codex):
        assert post(ro, "/api/codex/switch", {"number": "2"})[0] == 403
        assert post(ro, "/api/codex/add-current", {})[0] == 403
        assert codex.calls == []

    def test_switch_and_add_when_changes_are_allowed(self, rw, codex):
        status, body, _ = post(rw, "/api/codex/switch", {"number": "2"})
        assert status == 200 and body["switched"] is True
        assert "restarted" in body["message"]
        assert post(rw, "/api/codex/add-current", {})[0] == 200
        assert [c for c in codex.calls if c[0] != "rows"] == [("switch", "2"), ("add",)]

    @pytest.mark.parametrize("body", [{"number": "a@b.com"}, {"number": 2}, {}, {"number": "2", "force": True}])
    def test_only_plain_slot_numbers_are_accepted(self, rw, codex, body):
        assert post(rw, "/api/codex/switch", body)[0] == 400
        assert codex.calls == []

    def test_a_codex_error_is_a_400_with_its_type(self, rw, codex):
        codex.switch = lambda *a, **k: (_ for _ in ()).throw(SwitchError("not managed"))
        status, body, _ = post(rw, "/api/codex/switch", {"number": "2"})
        assert status == 400
        assert body["error"] == {"type": "SwitchError", "message": "not managed"}
