"""The "cswap-dark" Textual theme and shared color constants.

agent-swap's own look, the same as the web control panel: graphite neutrals and
one violet accent that belongs to neither product. Bars are neutral until a limit
is close (amber, then red), and Claude Code / Codex are told apart by a small
coloured dot (CLAUDE_DOT, CODEX_DOT), not by repainting the whole interface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from textual.theme import Theme

# Core palette (single source of truth — widgets import these for rich
# renderables, the Theme below maps them onto Textual's design tokens).
ACCENT = "#8b7cf6"  # agent-swap violet
FOREGROUND = "#ececee"
MUTED = "#8d929b"  # secondary text
BACKGROUND = "#0e0f11"
SURFACE = "#16171a"
PANEL = "#1c1e22"

# Usage severity ramp (desaturated for dark backgrounds).
SEV_OK = "#c9cbd1"  # neutral: plenty of headroom, nothing to look at
SEV_WARN = "#f2b14c"  # amber: climbing (>= 70%)
SEV_CRIT = "#f26b6b"  # red: near the limit (>= 90%)
TRACK = "#2a2d33"  # unfilled bar track

# Which product a row belongs to: a dot, in the product's own colour.
CLAUDE_DOT = "#d97757"
CODEX_DOT = "#e7e7ea"
CLAUDE_DOT_LIGHT = "#c15f3c"
CODEX_DOT_LIGHT = "#2b2d31"

# Severity band edges. WARN mirrors where a user starts caring; CRIT mirrors
# the auto-switch default threshold so bar color and switch behavior agree.
WARN_PCT = 70.0
CRIT_PCT = 90.0


@dataclass(frozen=True)
class Palette:
    """Resolved colors for Rich renderables, keyed to a Textual theme.

    Rich renderables bake color into styles at render time, so they can't read
    Textual's ``$variables`` the way the .tcss layer does. A Palette carries the
    active theme's colors so the same render code paints correctly in either
    theme. Resolve from the Theme object (never App.theme_variables, which lags
    the deferred CSS refresh)."""

    accent: str
    foreground: str
    muted: str
    sev_ok: str
    sev_warn: str
    sev_crit: str
    track: str

    DARK: ClassVar["Palette"]

    def severity(self, pct: float | None) -> str:
        if pct is None:
            return self.muted
        if pct >= CRIT_PCT:
            return self.sev_crit
        if pct >= WARN_PCT:
            return self.sev_warn
        return self.sev_ok

    @classmethod
    def from_theme(cls, theme: Theme) -> "Palette":
        return cls(
            accent=theme.primary,
            foreground=theme.foreground,
            muted=theme.secondary,
            sev_ok=theme.success,
            sev_warn=theme.warning,
            sev_crit=theme.error,
            track=theme.variables.get("track", TRACK),
        )


CSWAP_DARK = Theme(
    name="cswap-dark",
    primary=ACCENT,
    secondary=MUTED,
    accent=ACCENT,
    foreground=FOREGROUND,
    background=BACKGROUND,
    surface=SURFACE,
    panel=PANEL,
    success=SEV_OK,
    warning=SEV_WARN,
    error=SEV_CRIT,
    dark=True,
    variables={
        # Footer keys pick up the accent instead of the default blue.
        "footer-key-foreground": ACCENT,
        "block-cursor-background": PANEL,
        "block-cursor-foreground": FOREGROUND,
        "block-cursor-text-style": "none",
        "track": TRACK,
    },
)

# Light companion palette (same intent, tuned for a warm near-white base).
ACCENT_LIGHT = "#5b4bd6"  # violet, deep enough for AA on panel (worst-case row bg)
FOREGROUND_LIGHT = "#17181b"
MUTED_LIGHT = "#61656d"
BACKGROUND_LIGHT = "#f6f6f7"
SURFACE_LIGHT = "#ffffff"
PANEL_LIGHT = "#e9e9ec"  # most-elevated = darkest (inverted from dark)
SEV_OK_LIGHT = "#3b3e45"  # neutral
SEV_WARN_LIGHT = "#8a5700"  # deep amber, AA on panel
SEV_CRIT_LIGHT = "#b42f2f"  # deep red, AA on panel
TRACK_LIGHT = "#d9dade"

CSWAP_LIGHT = Theme(
    name="cswap-light",
    primary=ACCENT_LIGHT,
    secondary=MUTED_LIGHT,
    accent=ACCENT_LIGHT,
    foreground=FOREGROUND_LIGHT,
    background=BACKGROUND_LIGHT,
    surface=SURFACE_LIGHT,
    panel=PANEL_LIGHT,
    success=SEV_OK_LIGHT,
    warning=SEV_WARN_LIGHT,
    error=SEV_CRIT_LIGHT,
    dark=False,
    variables={
        "footer-key-foreground": ACCENT_LIGHT,
        "block-cursor-background": PANEL_LIGHT,
        "block-cursor-foreground": FOREGROUND_LIGHT,
        "block-cursor-text-style": "none",
        "track": TRACK_LIGHT,
    },
)

Palette.DARK = Palette(
    accent=ACCENT, foreground=FOREGROUND, muted=MUTED,
    sev_ok=SEV_OK, sev_warn=SEV_WARN, sev_crit=SEV_CRIT, track=TRACK,
)
