"""The dashboard's long-running helpers: the browser-login job and the engine thread."""

from __future__ import annotations

import sys
import threading
import time
from types import SimpleNamespace

import pytest

from claude_swap.gui.runner import AutoRunner, LoginJob


def wait_for(job, state, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        snap = job.snapshot()
        if snap and snap["state"] == state:
            return snap
        time.sleep(0.02)
    raise AssertionError(f"job never reached {state}: {job.snapshot()}")


class TestLoginJob:
    def test_a_login_that_succeeds_is_saved_and_reported(self):
        job, saved = LoginJob(), []
        assert job.start("claude", [sys.executable, "-c", "pass"], lambda: saved.append(1) or "Added Account 2")
        snap = wait_for(job, "done")
        assert snap["message"] == "Added Account 2" and saved == [1]

    def test_a_login_that_fails_saves_nothing(self):
        job, saved = LoginJob(), []
        job.start("claude", [sys.executable, "-c", "raise SystemExit(3)"], lambda: saved.append(1) or "x")
        snap = wait_for(job, "error")
        assert "exit 3" in snap["message"] and "nothing was saved" in snap["message"]
        assert saved == []

    def test_a_login_nobody_finishes_times_out(self):
        job = LoginJob()
        job.start("claude", [sys.executable, "-c", "import time; time.sleep(30)"], lambda: "x", timeout=0.5)
        assert "No sign-in" in wait_for(job, "error")["message"]

    def test_a_missing_program_is_reported_not_raised(self):
        job = LoginJob()
        job.start("codex", ["/nonexistent/definitely-not-here"], lambda: "x")
        assert "Could not start" in wait_for(job, "error")["message"]

    def test_a_save_that_refuses_says_why(self):
        job = LoginJob()

        def refuse():
            raise RuntimeError("identity mismatch")

        job.start("claude", [sys.executable, "-c", "pass"], refuse)
        assert "identity mismatch" in wait_for(job, "error")["message"]

    def test_only_one_at_a_time(self):
        job, gate = LoginJob(), threading.Event()
        assert job.start("claude", [sys.executable, "-c", "import time; time.sleep(3)"], lambda: "x")
        assert job.running()
        assert job.start("codex", [sys.executable, "-c", "pass"], lambda: "y") is False
        gate.set()

    def test_the_command_is_never_run_through_a_shell(self):
        # A hostile-looking argument stays one literal argument.
        job = LoginJob()
        job.start("claude", [sys.executable, "-c", "import sys; sys.exit(0 if sys.argv[1] == 'a; echo pwned' else 9)", "a; echo pwned"],
                  lambda: "ok")
        assert wait_for(job, "done")["message"] == "ok"


class FakeEngine:
    """Stands in for AutoSwitchEngine: blocks in run_loop until stop()."""

    instances: list = []

    def __init__(self, switcher, settings, on_event, dry_run=False):
        self.on_event, self.dry_run, self._stop = on_event, dry_run, threading.Event()
        FakeEngine.instances.append(self)

    def run_loop(self):
        self.on_event(SimpleNamespace(kind="switch", human=lambda: "switched 1 -> 2"))
        self._stop.wait()
        return 0

    def stop(self):
        self._stop.set()


@pytest.fixture
def runner(tmp_path, monkeypatch):
    FakeEngine.instances = []
    monkeypatch.setattr("claude_swap.autoswitch.AutoSwitchEngine", FakeEngine)
    return AutoRunner(SimpleNamespace(backup_dir=tmp_path))


def wait_until(cond, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class TestAutoRunner:
    def test_start_runs_an_engine_and_collects_its_events(self, runner):
        runner.start()
        assert wait_until(lambda: runner.snapshot()["events"])
        snap = runner.snapshot()
        assert snap["running"] is True and snap["dryRun"] is False
        assert snap["events"][0]["kind"] == "switch" and snap["events"][0]["text"] == "switched 1 -> 2"
        runner.stop()

    def test_start_twice_runs_one_engine(self, runner):
        runner.start()
        runner.start()
        assert len(FakeEngine.instances) == 1
        runner.stop()

    def test_stop_ends_the_thread(self, runner):
        runner.start()
        runner.stop()
        assert runner.snapshot()["running"] is False

    def test_dry_run_is_passed_to_the_engine_and_reported(self, runner):
        runner.start(dry_run=True)
        assert FakeEngine.instances[0].dry_run is True and runner.snapshot()["dryRun"] is True
        runner.stop()
        assert runner.snapshot()["dryRun"] is False

    def test_restart_starts_a_fresh_engine_only_if_one_was_running(self, runner):
        runner.restart()
        assert FakeEngine.instances == []
        runner.start()
        runner.restart()
        assert len(FakeEngine.instances) == 2 and runner.snapshot()["running"] is True
        runner.stop()

    def test_the_event_log_is_bounded(self, runner):
        for i in range(100):
            runner._on_event(SimpleNamespace(kind="poll", human=lambda: "tick"))
        assert len(runner.snapshot()["events"]) == AutoRunner.EVENTS


class TestLoginOutput:
    def test_the_sign_in_url_and_device_code_are_kept_for_the_page(self):
        job = LoginJob()
        script = ("print('Open https://auth.example.com/device?x=1 in your browser.')\n"
                  "print('\\x1b[1mEnter code ABCD-12345\\x1b[0m')\n"
                  "print('visit http://insecure.example/ too')")
        job.start("codex", [sys.executable, "-c", script], lambda: "ok")
        snap = wait_for(job, "done")
        assert snap["url"] == "https://auth.example.com/device?x=1"   # https only, trailing text cut
        assert snap["code"] == "ABCD-12345"

    def test_a_login_that_prints_nothing_has_neither(self):
        job = LoginJob()
        job.start("claude", [sys.executable, "-c", "pass"], lambda: "ok")
        snap = wait_for(job, "done")
        assert "url" not in snap and "code" not in snap


class TestCodexAutoRunner:
    NOW = 1_000_000.0

    @staticmethod
    def window(pct, resets=2_000_000.0):
        return {"used_percent": pct, "window_minutes": 300, "resets_at": resets}

    def row(self, slot, pct, active=False, disabled=False, threshold=None, mode="chatgpt"):
        usage = None if pct is None else {"primary": self.window(pct), "secondary": None, "seenAt": "x"}
        return {"slot": slot, "alias": "", "email": f"s{slot}@example.com", "plan": "plus", "authMode": mode,
                "active": active, "disabled": disabled, "threshold": threshold, "usage": usage}

    def runner(self, rows, switch=None):
        from claude_swap.gui.runner import CodexAutoRunner

        calls = []

        class FakeCodex:
            def rows(self, persist=True, probe=False):
                return rows

            def switch(self, target=None, strategy=None, force=False):
                calls.append(target)
                if switch:
                    raise switch
                return int(target), True

        r = CodexAutoRunner(lambda: FakeCodex(), None, clock=lambda: self.NOW)
        r.calls = calls
        return r

    def text(self, r):
        return r.snapshot()["events"][-1]

    def test_it_stays_while_the_live_slot_is_under_its_line(self):
        r = self.runner([self.row(1, 40, active=True), self.row(2, 10)])
        r.tick(90)
        assert r.calls == [] and "staying" in self.text(r)["text"]

    def test_it_moves_to_the_least_used_slot_under_its_line(self):
        r = self.runner([self.row(1, 95, active=True), self.row(2, 60), self.row(3, 20), self.row(4, 99)])
        r.tick(90)
        assert r.calls == ["3"] and self.text(r)["kind"] == "switch"

    def test_a_slots_own_line_replaces_the_global_one(self):
        r = self.runner([self.row(1, 80, active=True, threshold=70), self.row(2, 75, threshold=60), self.row(3, 85)])
        r.tick(90)
        assert r.calls == ["3"]  # 2 is over its own 60 line; 3 is under the global 90

    def test_disabled_and_api_key_slots_are_never_targets(self):
        r = self.runner([self.row(1, 95, active=True), self.row(2, 1, disabled=True), self.row(3, 1, mode="apikey")])
        r.tick(90)
        assert r.calls == [] and self.text(r)["kind"] == "all-exhausted"

    def test_dry_run_only_reports(self):
        r = self.runner([self.row(1, 95, active=True), self.row(2, 10)])
        r.dry_run = True
        r.tick(90)
        assert r.calls == [] and "would switch" in self.text(r)["text"]

    def test_a_switch_refused_by_codex_becomes_an_event_not_a_crash(self):
        from claude_swap.exceptions import SwitchError

        r = self.runner([self.row(1, 95, active=True), self.row(2, 10)], switch=SwitchError("Codex is running"))
        r.tick(90)
        assert self.text(r)["kind"] == "error" and "Codex is running" in self.text(r)["text"]

    def test_the_cooldown_blocks_a_second_switch(self):
        r = self.runner([self.row(1, 95, active=True), self.row(2, 10)])
        r.tick(90)
        r.tick(90)
        assert r.calls == ["2"]

    def test_no_reading_means_stay_put(self):
        r = self.runner([self.row(1, None, active=True), self.row(2, 10)])
        r.tick(90)
        assert r.calls == []
