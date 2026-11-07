"""`cswap onboard`: several of the user's own accounts in one guided sitting.

The command is the documented manual flow (log in with an account, then
`cswap add`) run in a loop through Claude Code's own `claude auth login`. These
tests pin that it launches exactly that command, saves only after a login
succeeded, and never logs out.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from claude_swap import cli
from claude_swap.switcher import ClaudeAccountSwitcher


@pytest.fixture
def env(temp_home):
    """Patch everything that touches the real machine; hand back the mocks."""
    with patch("os.geteuid", return_value=1000, create=True), \
            patch("shutil.which", return_value="/usr/local/bin/claude") as which, \
            patch.object(
                ClaudeAccountSwitcher, "_get_current_identity_triple",
                return_value=None,
            ) as identity, \
            patch.object(ClaudeAccountSwitcher, "add_account") as add, \
            patch.object(ClaudeAccountSwitcher, "list_accounts") as listing, \
            patch(
                "subprocess.run", return_value=SimpleNamespace(returncode=0)
            ) as run:
        yield SimpleNamespace(
            which=which, identity=identity, add=add, listing=listing, run=run
        )


def _answers(*lines):
    return patch("builtins.input", side_effect=list(lines))


def test_each_email_gets_a_login_then_a_save(env):
    with _answers("a@example.com", "b@example.com", ""):
        cli._onboard_command([])

    assert [c.args[0] for c in env.run.call_args_list] == [
        ["/usr/local/bin/claude", "auth", "login", "--email", "a@example.com"],
        ["/usr/local/bin/claude", "auth", "login", "--email", "b@example.com"],
    ]
    assert env.add.call_count == 2
    env.listing.assert_called_once()


def test_never_logs_out(env):
    with _answers("a@example.com", ""):
        cli._onboard_command([])

    # Logging out can revoke the token of the account being left.
    assert all("logout" not in c.args[0] for c in env.run.call_args_list)


def test_a_login_that_did_not_complete_saves_nothing(env, capsys):
    env.run.return_value = SimpleNamespace(returncode=1)

    with _answers("a@example.com", ""):
        cli._onboard_command([])

    env.add.assert_not_called()
    captured = capsys.readouterr()
    assert "nothing was saved" in captured.out + captured.err


def test_a_malformed_address_never_opens_a_browser_login(env):
    with _answers("not-an-email", ""):
        cli._onboard_command([])

    env.run.assert_not_called()
    env.add.assert_not_called()


def test_an_existing_login_is_offered_first(env):
    env.identity.return_value = ("me@example.com", "org", "uuid")

    with _answers("y", ""):
        cli._onboard_command([])

    env.add.assert_called_once_with()
    env.run.assert_not_called()


def test_declining_the_existing_login_skips_it(env):
    env.identity.return_value = ("me@example.com", "org", "uuid")

    with _answers("n", ""):
        cli._onboard_command([])

    env.add.assert_not_called()


def test_exits_cleanly_when_claude_is_not_installed(env):
    env.which.return_value = None

    with pytest.raises(SystemExit) as stop:
        cli._onboard_command([])

    assert stop.value.code == 1
    env.run.assert_not_called()


def test_ctrl_c_exits_without_a_traceback(env):
    with patch("builtins.input", side_effect=KeyboardInterrupt), \
            pytest.raises(SystemExit) as stop:
        cli._onboard_command([])

    assert stop.value.code == 130
