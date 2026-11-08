"""The dashboard as a control panel: rotate, remove, alias, thresholds, tokens,
auto-switch and browser logins.

Every value comes from a web page, so each route is pinned on what it accepts and
what it refuses. The switcher, Codex, engine and login are stubs: none of these
tests touches a real account or starts a real process.
"""

from __future__ import annotations

import json
import shutil
import threading

import pytest

from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.gui.server import GuiServer
from claude_swap.switcher import ClaudeAccountSwitcher
from tests.test_gui import StubCodex, StubSwitcher, call, post


class FullStub(StubSwitcher):
    def switch(self, strategy=None, json_output=False, **kw):
        self.calls.append(("switch", strategy))
        return {"switched": True, "from": {"number": 1}, "to": {"number": 2}}

    def remove_account(self, number, assume_yes=False):
        self.calls.append(("remove", number, assume_yes))
        print(f"Removed Account-{number}.")

    def set_alias(self, number, alias):
        self.calls.append(("alias", number, alias))

    def unset_alias(self, number):
        self.calls.append(("unalias", number))

    def set_account_threshold(self, number, value):
        if number == "10":
            raise ClaudeSwitchError("threshold must be between 50 and 99.9")
        self.calls.append(("threshold", number, value))

    def add_account_from_token(self, token, email=None, slot=None, assume_yes=False):
        self.calls.append(("token", email, assume_yes))
        print(f"Added {token} as account 3")  # a careless message: the server must not pass it on

    def _validate_email(self, email):
        return ClaudeAccountSwitcher._validate_email(self, email)  # the real pattern


class StubAuto:
    def __init__(self):
        self.calls, self.running = [], False

    def start(self, dry_run=False):
        self.calls.append(("start", dry_run))
        self.running = True

    def stop(self):
        self.calls.append(("stop",))
        self.running = False

    def restart(self):
        self.calls.append(("restart",))

    def snapshot(self):
        return {"running": self.running, "dryRun": False, "events": [{"ts": "10:00:00", "kind": "poll", "text": "ok"}]}


class StubJob:
    def __init__(self):
        self.started, self.busy, self.state = [], False, None

    def running(self):
        return self.busy

    def start(self, kind, argv, save):
        if self.busy:
            return False
        self.started.append((kind, argv, save))
        return True

    def snapshot(self):
        return self.state


@pytest.fixture
def parts(tmp_path):
    stub = FullStub(tmp_path)
    stub.live = None  # the live login is saved, unless a test says otherwise
    return stub, StubCodex(), StubAuto(), StubJob()


@pytest.fixture
def rw(parts):
    stub, codex, auto, job = parts
    server = GuiServer(stub, write=True, codex=codex, auto=auto, job=job)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def ro(parts):
    stub, codex, auto, job = parts
    server = GuiServer(stub, write=False, codex=codex, auto=auto, job=job)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


class TestRotate:
    @pytest.mark.parametrize("body,want", [({}, None), ({"strategy": "next"}, None), ({"strategy": "best"}, "best"),
                                           ({"strategy": "next-available"}, "next-available")])
    def test_strategies(self, rw, parts, body, want):
        assert post(rw, "/api/rotate", body)[0] == 200
        assert parts[0].calls[-1] == ("switch", want)

    @pytest.mark.parametrize("body", [{"strategy": "rm -rf"}, {"strategy": 3}, {"other": 1}])
    def test_anything_else_is_refused(self, rw, parts, body):
        assert post(rw, "/api/rotate", body)[0] == 400
        assert parts[0].calls == []

    def test_read_only_refuses(self, ro, parts):
        assert post(ro, "/api/rotate", {})[0] == 403
        assert parts[0].calls == []


class TestAccountEdits:
    def test_remove_confirms_nothing_itself_but_takes_a_slot_number_only(self, rw, parts):
        assert post(rw, "/api/remove", {"number": "2"})[0] == 200
        assert parts[0].calls == [("remove", "2", True)]
        assert post(rw, "/api/remove", {"number": "a@b.co"})[0] == 400
        assert post(rw, "/api/remove", {"number": "2", "extra": 1})[0] == 400
        assert len(parts[0].calls) == 1

    def test_alias_set_and_cleared(self, rw, parts):
        assert post(rw, "/api/alias", {"number": "1", "alias": "  work "})[0] == 200
        assert post(rw, "/api/alias", {"number": "1", "alias": "   "})[0] == 200
        assert parts[0].calls == [("alias", "1", "work"), ("unalias", "1")]

    @pytest.mark.parametrize("alias", [None, 5, "x" * 65])
    def test_a_bad_alias_is_refused(self, rw, parts, alias):
        assert post(rw, "/api/alias", {"number": "1", "alias": alias})[0] == 400
        assert parts[0].calls == []

    def test_threshold_set_cleared_and_validated(self, rw, parts):
        assert post(rw, "/api/threshold", {"number": "1", "threshold": 85})[0] == 200
        assert post(rw, "/api/threshold", {"number": "1", "threshold": None})[0] == 200
        assert parts[0].calls == [("threshold", "1", 85), ("threshold", "1", None)]
        for bad in ("85", True, [1]):
            assert post(rw, "/api/threshold", {"number": "1", "threshold": bad})[0] == 400
        status, body, _ = post(rw, "/api/threshold", {"number": "10", "threshold": 10})
        assert status == 400 and "between 50 and 99.9" in body["error"]["message"]

    def test_a_token_is_passed_on_and_never_echoed_back(self, rw, parts):
        token = "sk-ant-oat01-" + "a" * 40
        status, body, _ = post(rw, "/api/add-token", {"token": token, "email": "me@example.com"})
        assert status == 200
        assert parts[0].calls == [("token", "me@example.com", True)]
        assert token not in body["message"]

    @pytest.mark.parametrize("body", [{"token": "short"}, {"token": 5}, {"token": "x" * 3000},
                                      {"token": "x" * 20, "slot": 1}, {"token": "x" * 20, "email": 5}])
    def test_a_bad_token_request_is_refused(self, rw, parts, body):
        assert post(rw, "/api/add-token", body)[0] == 400
        assert parts[0].calls == []


class TestAutoSwitch:
    def test_start_and_stop(self, rw, parts):
        _, started, _ = post(rw, "/api/auto", {"action": "start"})
        assert started["message"] == "Auto-switch started."
        status, body, _ = post(rw, "/api/auto", {"action": "stop"})
        assert status == 200 and body["running"] is False and body["message"] == "Auto-switch stopped."
        assert parts[2].calls == [("start", False), ("stop",)]

    def test_dry_run_is_a_boolean(self, rw, parts):
        assert post(rw, "/api/auto", {"action": "start", "dryRun": True})[0] == 200
        assert post(rw, "/api/auto", {"action": "start", "dryRun": "yes"})[0] == 400
        assert post(rw, "/api/auto", {"action": "reboot"})[0] == 400
        assert parts[2].calls == [("start", True)]

    def test_status_and_settings_are_readable_even_read_only(self, ro):
        status, body, _ = call(ro, "GET", "/api/auto")
        assert status == 200
        assert body["events"][0]["kind"] == "poll"
        assert set(body["settings"]) == {"threshold", "weeklyThreshold", "strategy", "keepalive", "intervalSeconds"}

    def test_starting_is_refused_read_only(self, ro, parts):
        assert post(ro, "/api/auto", {"action": "start"})[0] == 403
        assert parts[2].calls == []


class TestConfig:
    def _saved(self, parts):
        path = parts[0].backup_dir / "settings.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def test_a_shown_setting_is_saved_and_the_engine_restarted(self, rw, parts):
        assert post(rw, "/api/config", {"key": "autoswitch.threshold", "value": 80})[0] == 200
        assert post(rw, "/api/config", {"key": "autoswitch.keepalive", "value": True})[0] == 200
        assert post(rw, "/api/config", {"key": "autoswitch.strategy", "value": "weekly-first"})[0] == 200
        saved = self._saved(parts)["autoswitch"]
        assert saved["threshold"] == 80 and saved["keepalive"] is True and saved["strategy"] == "weekly-first"
        assert parts[2].calls == [("restart",)] * 3

    def test_null_goes_back_to_the_default(self, rw, parts):
        post(rw, "/api/config", {"key": "autoswitch.weeklyThreshold", "value": 97})
        assert post(rw, "/api/config", {"key": "autoswitch.weeklyThreshold", "value": None})[0] == 200
        assert "weeklyThreshold" not in self._saved(parts).get("autoswitch", {})

    @pytest.mark.parametrize("body", [
        {"key": "updates.check", "value": False},      # real, but not one the page shows
        {"key": "../../etc/passwd", "value": 1},
        {"key": "autoswitch.threshold", "value": "banana"},
        {"key": "autoswitch.threshold", "value": [1]},
        {"key": "autoswitch.threshold", "value": "9" * 50},
        {"key": "autoswitch.threshold", "value": 80, "extra": 1},
    ])
    def test_everything_else_is_refused_and_writes_nothing(self, rw, parts, body):
        assert post(rw, "/api/config", body)[0] == 400
        assert self._saved(parts) == {}
        assert parts[2].calls == []


class TestLogin:
    @pytest.fixture(autouse=True)
    def _binaries(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: f"/opt/bin/{name}")

    def test_a_claude_login_runs_one_fixed_command(self, rw, parts):
        assert post(rw, "/api/login", {"kind": "claude", "email": "me@example.com"})[0] == 200
        kind, argv, _ = parts[3].started[0]
        assert kind == "claude"
        assert argv == ["/opt/bin/claude", "auth", "login", "--email", "me@example.com"]

    @pytest.mark.parametrize("email", [None, 5, "not an email", "a@b.co\n--flag", "x" * 300])
    def test_an_invalid_email_never_starts_a_login(self, rw, parts, email):
        assert post(rw, "/api/login", {"kind": "claude", "email": email})[0] == 400
        assert parts[3].started == []

    def test_an_unsaved_live_login_blocks_it_because_signing_in_replaces_it(self, rw, parts):
        parts[0].live = ("me@example.com", "")
        status, body, _ = post(rw, "/api/login", {"kind": "claude", "email": "me@example.com"})
        assert status == 409 and "Save the login" in body["error"]["message"]
        assert parts[3].started == []

    def test_saving_the_new_login_happens_under_the_write_lock(self, rw, parts):
        post(rw, "/api/login", {"kind": "claude", "email": "me@example.com"})
        save = parts[3].started[0][2]
        assert save().startswith("Added")  # the stub's add_account prints "Added Account-2."
        assert ("add",) in parts[0].calls
        assert rw.write_lock.acquire(blocking=False)  # released again afterwards
        rw.write_lock.release()

    def test_a_codex_login_and_its_save(self, rw, parts):
        assert post(rw, "/api/login", {"kind": "codex"})[0] == 200
        kind, argv, save = parts[3].started[0]
        assert (kind, argv) == ("codex", ["/opt/bin/codex", "login"])
        assert save() == "Added Codex slot 2"
        assert ("add",) in parts[1].calls

    def test_an_unsaved_codex_login_blocks_it(self, rw, parts):
        parts[1].state = "unmanaged"
        assert post(rw, "/api/login", {"kind": "codex"})[0] == 409
        assert parts[3].started == []

    def test_missing_binaries_and_unknown_kinds(self, rw, parts, monkeypatch):
        assert post(rw, "/api/login", {"kind": "shell", "email": "me@example.com"})[0] == 400
        assert post(rw, "/api/login", {"kind": "codex", "email": "x"})[0] == 400
        monkeypatch.setattr(shutil, "which", lambda name: None)
        assert post(rw, "/api/login", {"kind": "claude", "email": "me@example.com"})[0] == 400
        assert post(rw, "/api/login", {"kind": "codex"})[0] == 400
        assert parts[3].started == []

    def test_only_one_login_at_a_time(self, rw, parts):
        parts[3].busy = True
        assert post(rw, "/api/login", {"kind": "codex"})[0] == 409

    def test_other_changes_wait_while_a_login_is_open_but_auto_switch_does_not(self, rw, parts):
        parts[3].busy = True
        assert post(rw, "/api/switch", {"number": "2"})[0] == 409
        assert post(rw, "/api/rotate", {})[0] == 409
        assert parts[0].calls == []
        assert post(rw, "/api/auto", {"action": "stop"})[0] == 200

    def test_the_login_state_is_in_the_accounts_poll(self, rw, parts):
        parts[3].state = {"kind": "claude", "state": "running", "message": "Finish signing in"}
        _, body, _ = call(rw, "GET", "/api/accounts")
        assert body["job"]["state"] == "running"

    def test_read_only_cannot_log_in(self, ro, parts):
        assert post(ro, "/api/login", {"kind": "codex"})[0] == 403


class TestCodexControls:
    def test_rotate_and_best(self, rw, parts):
        assert post(rw, "/api/codex/rotate", {})[0] == 200
        assert post(rw, "/api/codex/rotate", {"strategy": "best"})[0] == 200
        assert post(rw, "/api/codex/rotate", {"strategy": "next-available"})[0] == 400
        assert [c for c in parts[1].calls if c[0] == "switch"] == [("switch", None), ("switch", None)]

    def test_remove_takes_a_slot_number(self, rw, parts):
        assert post(rw, "/api/codex/remove", {"number": "1"})[0] == 200
        assert post(rw, "/api/codex/remove", {"number": "x"})[0] == 400
        assert ("remove", "1", True) in parts[1].calls


class TestCommand:
    """`cswap gui`: the panel can change accounts unless asked for a view only."""

    @pytest.mark.parametrize("argv,write", [([], True), (["--write"], True), (["--read-only"], False)])
    def test_the_flag_decides_whether_it_can_change_accounts(self, argv, write, monkeypatch):
        from types import SimpleNamespace

        from claude_swap import cli

        seen = {}

        class FakeServer:
            server_port, token = 1, "t"

            def __init__(self, switcher, write, port):
                seen["write"] = write

            def serve_forever(self):
                raise KeyboardInterrupt

            def server_close(self):
                pass

        monkeypatch.setattr("claude_swap.gui.server.GuiServer", FakeServer)
        monkeypatch.setattr(cli, "ClaudeAccountSwitcher", lambda debug=False: SimpleNamespace())
        monkeypatch.setattr(cli, "_guard_root", lambda switcher: None)
        cli._gui_command(argv + ["--no-open"])
        assert seen["write"] is write
