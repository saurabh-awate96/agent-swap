"""The project was renamed claude-swap -> agent-swap.

What must keep working for people who used the old name: every old command,
the version lookup inside an old environment, and (checked elsewhere) the
data folders and Keychain service, which keep their names on purpose.
"""

from __future__ import annotations

import importlib
import importlib.metadata as md
import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _project() -> dict:
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)["project"]


def test_the_distribution_is_agent_swap():
    assert _project()["name"] == "agent-swap"


def test_every_old_command_still_exists_beside_the_new_ones():
    scripts = _project()["scripts"]
    assert set(scripts) == {"aswap", "agent-swap", "cswap", "claude-swap"}
    assert set(scripts.values()) == {"claude_swap.cli:main"}


def test_version_falls_back_to_the_old_distribution_name(monkeypatch):
    import claude_swap

    def fake_version(name):
        if name == "agent-swap":
            raise md.PackageNotFoundError(name)
        return "9.9.9"

    monkeypatch.setattr(md, "version", fake_version)
    try:
        assert importlib.reload(claude_swap).__version__ == "9.9.9"
    finally:
        monkeypatch.undo()
        importlib.reload(claude_swap)
