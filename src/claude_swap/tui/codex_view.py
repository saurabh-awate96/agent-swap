"""Codex in the terminal dashboard: the same cards as Claude's, in Codex's own colour.

Both products share agent-swap's accent; a small coloured dot next to the product name
says which one a row belongs to. The data is `Codex.rows()` (usage from Codex's own session files, the live
account read from the real client) and is polled on its own slower clock by the app,
because it does not come from the Claude snapshot.
"""

from __future__ import annotations

import shutil
import time
from datetime import datetime

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, ListItem, ListView, Static

from claude_swap import printer
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.tui import data
from claude_swap.tui.theme import Palette
from claude_swap.tui.widgets import usage_bar

POLL_S = 30.0  # the live reading is itself cached for five minutes


def codex_palette(theme) -> Palette:
    """The app's palette (Codex is told apart by its dot, not by its own accent)."""
    return Palette.from_theme(theme)


def fetch_overview() -> dict:
    """Everything the Codex panel shows, in one blocking call (run it off the UI thread)."""
    from claude_swap.codex import Codex, codex_running
    from claude_swap.codex_run import supervised_count

    out = {"accounts": [], "unsaved": None, "installed": shutil.which("codex") is not None,
           "running": False, "supervised": 0, "error": None, "at": time.time()}
    try:
        cx = Codex()
        out["accounts"] = cx.rows(persist=False, probe=True)
        live = cx.status()
        if live.get("state") == "unmanaged":
            out["unsaved"] = {"email": live.get("email"), "plan": live.get("plan"),
                              "problem": cx.live_problem()}
        out["running"], out["supervised"] = codex_running(), supervised_count(cx)
    except ClaudeSwitchError as e:
        out["error"] = str(e)
    return out


def window_label(minutes) -> str:
    if not minutes:
        return "5h"
    if minutes == 300:
        return "5h"
    if minutes == 10080:
        return "7d"
    hours = minutes / 60
    return f"{round(hours)}h" if hours < 48 else f"{round(hours / 24)}d"


PROBLEM_TEXT = {
    "revoked": "OpenAI says this login was revoked: sign in again (Codex menu → Sign in)",
    "unauthorized": "OpenAI rejected this login: sign in again (Codex menu → Sign in)",
    "failed": "could not read this account's usage from Codex just now",
}


def _windows(row: dict, now: float) -> list[tuple[str, float | None, str]]:
    """(label, pct or None, suffix) per usage window the account reports."""
    usage = row.get("usage") or {}
    rows = []
    for key in ("primary", "secondary"):
        w = usage.get(key)
        if not w:
            continue
        label = window_label(w.get("window_minutes"))
        remaining = w["resets_at"] - now
        if remaining <= 0:
            rows.append((label, None, "reset since last seen"))
            continue
        clock = datetime.fromtimestamp(w["resets_at"]).strftime("%H:%M")
        rows.append((label, float(w["used_percent"]),
                     f"resets {data.format_duration(remaining)} · {clock}"))
    return rows


def _header(row: dict, palette: Palette, *, active_style: str, gap: str) -> Text:
    text = Text()
    text.append(f"{row['slot']:>2}  ", style=active_style)
    email = row.get("email") or "(API key)"
    if row.get("alias"):
        text.append(row["alias"], style=f"bold {palette.accent}")
        text.append(f" ({email})", style=palette.foreground)
    else:
        text.append(email, style=palette.foreground)
    text.append(f"  [{row['plan']}]", style=palette.muted)
    if row.get("active"):
        text.append(f"{gap}● active", style=f"bold {palette.accent}")
    if row.get("disabled"):
        text.append(f"{gap}(disabled)", style=palette.muted)
    if row.get("threshold") is not None:
        text.append(f"{gap}th {row['threshold']:g}%", style=palette.muted)
    return text


def codex_card_text(row: dict, width: int, *, threshold: float | None = None,
                    now: float | None = None, palette: Palette = Palette.DARK) -> Text:
    """The full card: header, then one bar per usage window, then what we know about the login."""
    now = now if now is not None else time.time()
    text = _header(row, palette, active_style=f"bold {palette.foreground}", gap="   ")
    usage, live = row.get("usage"), bool((row.get("usage") or {}).get("live"))
    line = row.get("threshold") if row.get("threshold") is not None else threshold

    if row.get("problem"):
        text.append("\n    ")
        text.append(f"⚠ {PROBLEM_TEXT.get(row['problem'], PROBLEM_TEXT['failed'])}", style=palette.sev_warn)
    if row.get("authMode") == "apikey":
        text.append("\n    ")
        text.append("· API key (no quota)", style=palette.muted)
        return text
    if not usage:
        if not row.get("problem"):
            text.append("\n    ")
            text.append("no usage seen yet (it appears after you use Codex)", style=palette.muted)
        return text

    windows = _windows(row, now)
    label_width = max((len(label) for label, _p, _s in windows), default=2)
    bar_width = max(12, min(30, width - 42 - label_width))
    for label, pct, suffix in windows:
        text.append("\n    ")
        text.append(usage_bar(f"{label:<{label_width}}", pct, suffix, bar_width,
                              stale=not live or bool(row.get("problem")),
                              threshold=line, palette=palette))
    if not usage.get("secondary"):
        primary = usage.get("primary") or {}
        long = (primary.get("window_minutes") or 0) >= 20000
        text.append("\n    ")
        text.append(
            f"· this plan has one {window_label(primary.get('window_minutes'))} limit and no weekly limit"
            if long else "· no weekly limit reported for this login",
            style=palette.muted,
        )
    health = row.get("health")
    if health and health.get("at"):
        checked = _age(health["at"])
        ok = health["status"] in ("ok", "apikey", "expiring")
        text.append("\n    ")
        text.append(f"{'└ login ok' if ok else '⚠ login ' + health['status']} · checked {checked}",
                    style=palette.muted if ok else palette.sev_warn)
    seen = _age(usage.get("seenAt"))
    text.append("\n    ")
    text.append(f"└ read from Codex {seen}" if live else f"└ last seen {seen} · this machine only",
                style=palette.muted)
    return text


def _age(stamp) -> str:
    try:
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return "a while ago"
    return printer.format_age(int(when.timestamp() * 1000)) or "just now"


def codex_mini_text(row: dict, now: float, *, palette: Palette = Palette.DARK) -> Text:
    """One line for an account that is not the live one."""
    text = _header(row, palette, active_style=f"bold {palette.muted}", gap="  ")
    text.no_wrap = True
    text.overflow = "ellipsis"
    text.append("   ")
    if row.get("problem"):
        text.append("⚠ login " + row["problem"], style=palette.sev_warn)
        return text
    if row.get("authMode") == "apikey":
        text.append("API key", style=palette.muted)
        return text
    parts = [(label, pct) for label, pct, _s in _windows(row, now)]
    if not parts:
        text.append("usage unknown", style=palette.muted)
    for i, (label, pct) in enumerate(parts):
        if i:
            text.append(" · ", style=palette.track)
        text.append(f"{label} ", style=palette.muted)
        text.append("?" if pct is None else f"{pct:.0f}%",
                    style=palette.muted if pct is None else palette.severity(pct))
    return text


class CodexPanel(Static):
    """The Codex part of the dashboard: the live account full-size, the others as one line each."""

    def on_mount(self) -> None:
        self.watch(self.app, "codex", self._changed)
        self.watch(self.app, "theme", lambda _t: self.refresh(layout=True))

    def _changed(self, overview) -> None:
        shown = bool(overview) and bool(
            overview["accounts"] or overview["unsaved"] or overview["error"] or overview["installed"])
        self.display = shown
        self.refresh(layout=True)

    def render(self) -> Text:
        palette = codex_palette(self.app.current_theme)
        ov = self.app.codex
        if ov is None:
            return Text("codex: loading…", style=palette.muted)
        from claude_swap.tui.theme import CODEX_DOT, CODEX_DOT_LIGHT

        text = Text()
        text.append("● ", style=CODEX_DOT if getattr(self.app.current_theme, "dark", True) else CODEX_DOT_LIGHT)
        text.append("Codex", style=f"bold {palette.foreground}")
        if ov["supervised"]:
            text.append(f"   {ov['supervised']} session(s) follow a switch by themselves", style=palette.muted)
        elif ov["running"]:
            text.append("   a running Codex keeps its account until restarted: use `aswap codex run`",
                        style=palette.sev_warn)
        text.append("\n")
        if ov["error"]:
            text.append(f"\n{ov['error']}", style=palette.sev_warn)
            return text
        now, width = time.time(), (self.size.width or 80) - 2
        blocks: list[Text] = []
        un = ov["unsaved"]
        if un:
            block = Text()
            block.append(" ·  ", style=f"bold {palette.muted}")
            block.append(un["email"] or "(login)", style=palette.foreground)
            block.append("  [not saved]", style=palette.muted)
            block.append("\n    ")
            if un["problem"] in ("revoked", "unauthorized"):
                block.append(f"⚠ {PROBLEM_TEXT[un['problem']]}. Saving it would store a dead login.", style=palette.sev_warn)
            else:
                block.append("logged in now, not saved yet: Codex menu → Save current login", style=palette.muted)
            blocks.append(block)
        threshold = getattr(self.app, "threshold_pct", None)
        for row in ov["accounts"]:
            blocks.append(codex_card_text(row, width, threshold=threshold, now=now, palette=palette)
                          if row["active"] else codex_mini_text(row, now, palette=palette))
        if not blocks:
            text.append("\nNo Codex accounts yet. Use the Codex menu → Sign in to another account.", style=palette.muted)
            return text
        previous_multiline = False
        for i, block in enumerate(blocks):
            multiline = "\n" in block.plain
            if i:
                text.append("\n\n" if (multiline or previous_multiline) else "\n")
            text.append(block)
            previous_multiline = multiline
        return text


class CodexCard(Static):
    def __init__(self, row: dict) -> None:
        super().__init__()
        self._row = row

    def set_row(self, row: dict) -> None:
        self._row = row
        self.refresh(layout=True)

    def render(self) -> Text:
        return codex_card_text(self._row, self.size.width or 80,
                               threshold=getattr(self.app, "threshold_pct", None),
                               palette=codex_palette(self.app.current_theme))


class CodexItem(ListItem):
    def __init__(self, row: dict) -> None:
        super().__init__(CodexCard(row))
        self.slot = str(row["slot"])
        self.email = row.get("email") or ""

    def set_row(self, row: dict) -> None:
        self.query_one(CodexCard).set_row(row)


class CodexSwitchScreen(Screen):
    """Every Codex account full-size: arrows pick, Enter switches, like the Claude one."""

    BINDINGS = [
        Binding("enter", "select_highlighted", "Switch", priority=True),
        Binding("b", "best", "Best pick"),
        Binding("n", "next_available", "Next available"),
        Binding("escape,q,s", "back", "Back"),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._slots: list[str] = []

    def compose(self) -> ComposeResult:
        yield Static("switch Codex to which account?", id="list-title")
        yield ListView(id="accounts")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#accounts", ListView).focus()
        self.watch(self.app, "codex", self._on_overview)

    async def _on_overview(self, overview) -> None:
        if not overview:
            return
        listview = self.query_one("#accounts", ListView)
        rows = overview["accounts"]
        slots = [str(r["slot"]) for r in rows]
        if slots != self._slots:
            first = not self._slots
            previous = listview.index
            await listview.clear()
            await listview.extend(CodexItem(r) for r in rows)
            self._slots = slots
            active = next((i for i, r in enumerate(rows) if r["active"]), 0)
            listview.index = None if not rows else (active if first else min(previous or 0, len(rows) - 1))
        else:
            for item, row in zip(listview.query(CodexItem), rows):
                item.set_row(row)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, CodexItem):
            self.app.do_codex_switch(event.item.slot)
            self.app.pop_screen()

    def action_select_highlighted(self) -> None:
        self.query_one("#accounts", ListView).action_select_cursor()

    def action_best(self) -> None:
        self.app.codex_rotate("best")
        self.app.pop_screen()

    def action_next_available(self) -> None:
        self.app.codex_rotate("next-available")
        self.app.pop_screen()

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_cursor_down(self) -> None:
        self.query_one("#accounts", ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#accounts", ListView).action_cursor_up()
