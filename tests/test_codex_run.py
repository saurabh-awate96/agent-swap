"""`aswap codex run`: Codex follows an account switch by restarting onto it and resuming."""

from __future__ import annotations

import json
import os
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

from claude_swap import codex_run
from claude_swap.codex import Codex
from claude_swap.exceptions import ConfigError
from tests.test_codex import A, B, env, make_auth  # noqa: F401  (the env fixture)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the supervisor is POSIX only")

FAKE = """#!{python}
import json, os, pathlib, sys, time
pathlib.Path(os.environ["FAKE_LOG"]).open("a").write(json.dumps(sys.argv[1:]) + "\\n")
mode = os.environ.get("FAKE_MODE", "session")
if sys.argv[1:2] == ["resume"] or mode == "quit":
    sys.exit(7)
if mode == "session":
    d = pathlib.Path(os.environ["CODEX_HOME"]) / "sessions" / "2026" / "11" / "08"
    d.mkdir(parents=True, exist_ok=True)
    (d / "rollout-x.jsonl").write_text(json.dumps({{"payload": {{"id": "sess-123", "cwd": os.getcwd()}}}}) + "\\n")
time.sleep(60)
"""


@pytest.fixture
def fake(env, tmp_path, monkeypatch):  # noqa: F811
    log = tmp_path / "starts.log"
    script = tmp_path / "bin" / "codex"
    script.parent.mkdir()
    script.write_text(FAKE.format(python=sys.executable))
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("PATH", f"{script.parent}{os.pathsep}{os.environ['PATH']}")
    env.log = log
    return env


def starts(env):
    return [json.loads(line) for line in env.log.read_text().splitlines()]


def sup(env, capsys=None, **kw):
    return codex_run.Supervisor(Codex(), [], poll=0.1, grace=3, **kw)


def test_a_switch_restarts_codex_and_resumes_the_same_conversation(fake, capsys):
    cx = Codex()
    threading.Timer(1.0, cx.announce_switch, [2]).start()
    code = sup(fake).run()
    assert code == 7                                   # the resumed fake exits with 7
    assert starts(fake) == [[], ["resume", "sess-123"]]
    assert "account switched" in capsys.readouterr().err
    assert codex_run.supervised_count(cx) == 0         # it unregistered on the way out


def test_without_a_session_it_restarts_plain(fake, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "nosession")
    threading.Timer(1.0, Codex().announce_switch, [2]).start()
    sup(fake).run()
    assert starts(fake) == [[], []]


def test_quitting_codex_ends_the_supervisor_with_its_exit_code(fake, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "quit")
    assert sup(fake).run() == 7
    assert starts(fake) == [[]]                        # no restart without a switch


def test_a_switch_from_before_it_started_does_not_restart_it(fake, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "quit")
    Codex().announce_switch(1)
    assert sup(fake).run() == 7
    assert starts(fake) == [[]]


def test_it_registers_while_alive_and_dead_registrations_are_pruned(env):
    cx = Codex()
    d = codex_run.supervised_dir(cx)
    d.mkdir(parents=True)
    (d / f"{os.getpid()}.json").write_text("{}")
    (d / "999999999.json").write_text("{}")           # no such process
    (d / "junk.json").write_text("{}")
    assert codex_run.supervised_count(cx) == 1
    assert sorted(f.name for f in d.iterdir()) == [f"{os.getpid()}.json"]


def test_no_codex_installed_is_a_clear_error(env, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises(ConfigError):
        codex_run.Supervisor(Codex(), [], binary=None).run()


def test_a_real_switch_writes_the_marker_supervisors_watch(env):
    sw = env.sw()
    env.login(make_auth(**A))
    sw.add()
    env.login(make_auth(**B))
    sw.add()
    assert not (sw.root / "switched.json").exists()
    sw.switch("1")
    assert json.loads((sw.root / "switched.json").read_text())["slot"] == 1
    stamp = (sw.root / "switched.json").stat().st_mtime_ns
    sw.switch("1")                                    # already live: nothing changed, nothing announced
    assert (sw.root / "switched.json").stat().st_mtime_ns == stamp
