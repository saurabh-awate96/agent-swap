"""Multi-account switcher for Claude Code and Codex."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("agent-swap")
except PackageNotFoundError:  # an environment installed before the rename
    __version__ = version("claude-swap")

from claude_swap.switcher import ClaudeAccountSwitcher

__all__ = ["ClaudeAccountSwitcher", "__version__"]
