"""`aswap setup`: the guided first-run screen and the one-line installer.

The wizard only routes to code that already has its own tests (add_account,
the onboard loop, Codex.add, set_setting, the dashboards), so these tests pin
the routing, the keys, and that a failing step is reported instead of ending
the session.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_swap import cli, setup_wizard
from claude_swap.exceptions import ClaudeSwitchError

UP, DOWN, ENTER = "\x1b[A", "\x1b[B", "\r"
ROOT = Path(__file__).resolve().parent.parent


def keys(*seq):
    it = iter(seq)
    return lambda: next(it)


class TestMenu:
    ITEMS = [("1", "one"), ("2", "two"), ("3", "three"), ("q", "Finish")]

    def test_arrow_down_then_enter_picks_the_second(self):
        assert setup_wizard._menu(self.ITEMS, keys(DOWN, ENTER)) == "2"

    def test_up_from_the_top_wraps_to_the_last_entry(self):
        assert setup_wizard._menu(self.ITEMS, keys(UP, ENTER)) == "q"

    def test_a_number_picks_immediately(self):
        assert setup_wizard._menu(self.ITEMS, keys("3")) == "3"

    def test_q_and_ctrl_c_quit(self):
        assert setup_wizard._menu(self.ITEMS, keys("q")) == "q"
        assert setup_wizard._menu(self.ITEMS, keys("\x03")) == "q"

    def test_unknown_keys_are_ignored(self):
        assert setup_wizard._menu(self.ITEMS, keys("z", "9", "j", ENTER)) == "2"

    def test_without_raw_keys_it_asks_for_a_number(self, capsys):
        with patch.object(setup_wizard, "_raw_keys_ok", return_value=False), \
                patch("builtins.input", side_effect=["x", "2"]):
            assert setup_wizard._menu(self.ITEMS) == "2"
        assert "not one of the choices" in capsys.readouterr().out

    def test_end_of_input_quits_instead_of_looping(self):
        with patch.object(setup_wizard, "_raw_keys_ok", return_value=False), \
                patch("builtins.input", side_effect=EOFError):
            assert setup_wizard._menu(self.ITEMS) == "q"


@pytest.fixture
def wiz():
    """A switcher double, with Claude Code + Codex 'installed' and no accounts."""
    switcher = MagicMock()
    with patch.object(setup_wizard, "_version", side_effect=lambda b: f"{b} 1.0"), \
            patch.object(setup_wizard, "_counts", return_value=(0, None, 0)):
        yield switcher


def test_finish_prints_the_cheat_sheet(wiz, capsys):
    assert setup_wizard.run(wiz, read_key=keys("q")) == 0
    out = capsys.readouterr().out
    assert "aswap switch" in out and "aswap gui" in out


def test_status_board_shows_what_is_installed_and_saved(wiz, capsys):
    with patch.object(setup_wizard, "_counts", return_value=(2, "a@example.com", 1)):
        setup_wizard.run(wiz, read_key=keys("q"))
    out = capsys.readouterr().out
    assert "claude 1.0" in out and "codex 1.0" in out
    assert "2 saved" in out and "active a@example.com" in out and "1 saved" in out


def test_codex_entry_is_hidden_when_codex_is_not_installed(wiz, capsys):
    with patch.object(setup_wizard, "_version", side_effect=lambda b: "1.0" if b == "claude" else None):
        setup_wizard.run(wiz, read_key=keys("q"))
    assert "Save my Codex login" not in capsys.readouterr().out


def test_save_current_login_calls_add_account(wiz, capsys):
    setup_wizard.run(wiz, read_key=keys("1", "q"))
    wiz.add_account.assert_called_once_with()
    assert "Saved your current Claude login" in capsys.readouterr().out


def test_a_failing_step_is_reported_and_the_wizard_keeps_going(wiz, capsys):
    wiz.add_account.side_effect = ClaudeSwitchError("not logged in")
    assert setup_wizard.run(wiz, read_key=keys("1", "q")) == 0
    assert "not logged in" in capsys.readouterr().out


def test_ctrl_c_inside_a_step_returns_to_the_menu(wiz, capsys):
    wiz.add_account.side_effect = KeyboardInterrupt
    assert setup_wizard.run(wiz, read_key=keys("1", "q")) == 0
    assert "Cancelled" in capsys.readouterr().out


def test_more_accounts_runs_the_shared_onboard_loop(wiz):
    with patch.object(cli, "_onboard_accounts") as loop, \
            patch("builtins.input", return_value=""):
        setup_wizard.run(wiz, read_key=keys("2", "q"))
    loop.assert_called_once_with(wiz)


def test_codex_entry_saves_the_codex_login(wiz, capsys):
    with patch("claude_swap.codex.Codex") as codex:
        codex.return_value.add.return_value = (3, True)
        setup_wizard.run(wiz, read_key=keys("3", "q"))
    codex.return_value.add.assert_called_once_with()
    assert "Added Codex slot 3" in capsys.readouterr().out


def test_auto_switch_step_saves_the_threshold(wiz, tmp_path):
    with patch("claude_swap.paths.get_backup_root", return_value=tmp_path), \
            patch("builtins.input", side_effect=["85", ""]):
        setup_wizard.run(wiz, read_key=keys("4", "q"))
    saved = json.loads((tmp_path / "settings.json").read_text())
    assert saved["autoswitch"]["threshold"] == 85


def test_auto_switch_step_rejects_nonsense_without_writing(wiz, tmp_path, capsys):
    with patch("claude_swap.paths.get_backup_root", return_value=tmp_path), \
            patch("builtins.input", side_effect=["banana", ""]):
        setup_wizard.run(wiz, read_key=keys("4", "q"))
    assert not (tmp_path / "settings.json").exists()
    assert "banana" in capsys.readouterr().out  # the settings validator's reason, as a warning


def test_auto_switch_step_keeps_the_current_value_on_enter(wiz, tmp_path):
    with patch("claude_swap.paths.get_backup_root", return_value=tmp_path), \
            patch("builtins.input", side_effect=["", ""]):
        setup_wizard.run(wiz, read_key=keys("4", "q"))
    assert not (tmp_path / "settings.json").exists()


def test_the_web_control_panel_opens_ready_to_use(wiz):
    # No question first: a view-only page cannot do anything, so it starts able to change accounts.
    with patch.object(cli, "_gui_command") as gui:
        setup_wizard.run(wiz, read_key=keys("5", "q"))
    gui.assert_called_once_with([])


def test_setup_command_runs_the_wizard_and_exits_with_its_code():
    with patch("claude_swap.setup_wizard.run", return_value=0) as run, \
            patch.object(cli, "ClaudeAccountSwitcher"), \
            patch.object(cli, "_guard_root"), \
            pytest.raises(SystemExit) as exit_:
        cli._setup_command([])
    run.assert_called_once()
    assert exit_.value.code == 0


@pytest.mark.skipif(sys.platform == "win32", reason="install.sh is for macOS/Linux; NTFS has no exec bit")
class TestInstaller:
    def test_shell_installer_parses(self):
        script = ROOT / "install.sh"
        subprocess.run(["sh", "-n", str(script)], check=True)
        assert script.stat().st_mode & 0o111, "install.sh must be executable"

    def test_installer_hands_the_keyboard_to_setup_under_curl_sh(self):
        # `curl | sh` makes stdin the script; setup must read from the tty.
        text = (ROOT / "install.sh").read_text()
        assert 'aswap" setup </dev/tty' in text
        assert "CSWAP_NO_SETUP" in text
