"""Codex in the terminal dashboard: the cards, the panel, the `c` menu and the switch screen.

The Codex side is replaced with a fixed overview and recorded actions: no real ~/.codex,
no real client, no account touched.
"""

from __future__ import annotations

import time

import pytest

from claude_swap.tui import codex_view
from claude_swap.tui.theme import Palette
from tests.test_tui import FakeSwitcher, make_account, make_app, settle

NOW = time.time()


def row(slot, *, active=False, pct=(30, 40), minutes=(300, 10080), live=True, problem=None,
        alias="", disabled=False, plan="plus", mode="chatgpt", health=None):
    usage = None
    if pct is not None:
        windows = {}
        for key, p, m in zip(("primary", "secondary"), pct, minutes):
            if p is not None:
                windows[key] = {"used_percent": float(p), "window_minutes": float(m), "resets_at": NOW + 3600}
        usage = {"primary": windows.get("primary"), "secondary": windows.get("secondary"),
                 "seenAt": "2026-11-08T08:00:00.000Z", "live": live}
    return {"slot": slot, "alias": alias, "email": f"u{slot}@example.com", "plan": plan, "authMode": mode,
            "active": active, "disabled": disabled, "threshold": None, "problem": problem,
            "health": health, "usage": usage}


def overview(*rows, unsaved=None, running=False, supervised=0):
    return {"accounts": list(rows), "unsaved": unsaved, "installed": True, "running": running,
            "supervised": supervised, "error": None, "at": NOW}


class TestCards:
    def test_a_card_shows_both_windows_and_where_the_numbers_came_from(self):
        text = codex_view.codex_card_text(row(1, active=True, alias="main"), 100, now=NOW).plain
        assert "main (u1@example.com)" in text and "● active" in text
        assert "5h" in text and "7d" in text and "30%" in text and "40%" in text
        assert "read from Codex" in text

    def test_a_revoked_login_says_what_to_do(self):
        text = codex_view.codex_card_text(row(1, active=True, problem="revoked"), 100, now=NOW).plain
        assert "revoked" in text and "sign in again" in text

    def test_a_plan_without_a_weekly_window_says_so(self):
        text = codex_view.codex_card_text(row(2, pct=(79, None), minutes=(43200, None), plan="go"), 100, now=NOW).plain
        assert "30d" in text and "no weekly limit" in text

    def test_a_reset_window_is_unknown_not_zero(self):
        r = row(1)
        r["usage"]["primary"]["resets_at"] = NOW - 60
        text = codex_view.codex_card_text(r, 100, now=NOW).plain
        assert "reset since last seen" in text and " 0%" not in text

    def test_the_mini_line_is_one_line_with_both_numbers(self):
        text = codex_view.codex_mini_text(row(2, pct=(80, 52)), NOW).plain
        assert "\n" not in text and "5h 80%" in text and "7d 52%" in text

    def test_codex_uses_the_app_accent_and_is_told_apart_by_its_dot(self):
        from claude_swap.tui.theme import CLAUDE_DOT, CODEX_DOT, CSWAP_DARK

        assert codex_view.codex_palette(CSWAP_DARK).accent == Palette.DARK.accent
        assert CODEX_DOT != CLAUDE_DOT


@pytest.fixture
def codex_app(tmp_path, monkeypatch):
    """The dashboard with a fixed Codex overview and recorded Codex actions."""
    state = {"overview": overview(row(1, active=True, alias="main"), row(2, pct=(80, 52)))}
    monkeypatch.setattr(codex_view, "fetch_overview", lambda: state["overview"])
    calls = []
    import claude_swap.tui.app as tui_app

    monkeypatch.setattr(tui_app, "_codex_switch", lambda target, strategy: calls.append(("switch", target, strategy))
                        or {"switched": True, "to": {"email": f"Codex slot {target}"}, "note": None})
    monkeypatch.setattr(tui_app, "_codex_save", lambda: calls.append(("save",)) or print("Added Codex slot 3"))
    monkeypatch.setattr(tui_app, "_codex_check", lambda: calls.append(("check",)) or print(" 1  main: login ok"))
    monkeypatch.setattr(tui_app, "_codex_disable", lambda slot, d: calls.append(("disable", slot, d)))
    app = make_app(FakeSwitcher([make_account(1, active=True)], tmp_path))
    app._codex_calls, app._codex_state = calls, state
    return app


async def wait_codex(pilot, app):
    for _ in range(40):
        await pilot.pause()
        if app.codex is not None:
            return
    raise AssertionError("the Codex overview never arrived")


@pytest.mark.asyncio
class TestDashboard:
    async def test_the_dashboard_shows_codex_under_claude(self, codex_app):
        async with codex_app.run_test(size=(110, 44)) as pilot:
            await settle(pilot)
            await wait_codex(pilot, codex_app)
            panel = codex_app.screen.query_one("#codex-panel")
            text = panel.render().plain
            assert panel.display and "● Codex" in text
            assert "main (u1@example.com)" in text and "u2@example.com" in text

    async def test_an_unsaved_revoked_login_is_flagged_not_offered_for_saving(self, codex_app):
        codex_app._codex_state["overview"] = overview(
            row(2), unsaved={"email": "x@example.com", "plan": "plus", "problem": "revoked"})
        async with codex_app.run_test(size=(110, 44)) as pilot:
            await settle(pilot)
            await wait_codex(pilot, codex_app)
            text = codex_app.screen.query_one("#codex-panel").render().plain
            assert "[not saved]" in text and "dead login" in text

    async def test_c_opens_the_codex_menu_and_its_actions_reach_codex(self, codex_app):
        from textual.widgets import ListView

        from claude_swap.tui.widgets import MenuItem

        async with codex_app.run_test(size=(110, 44)) as pilot:
            await settle(pilot)
            await wait_codex(pilot, codex_app)
            await pilot.press("c")
            await pilot.pause()
            menu = codex_app.screen.query_one("#menu", ListView)
            ids = [i.action_id for i in menu.query(MenuItem)]
            assert ids[:6] == ["codex-switch", "codex-rotate", "codex-best", "codex-signin", "codex-save", "codex-check"]
            await pilot.press("down", "down", "enter")          # best pick
            for _ in range(20):
                await pilot.pause()
            assert ("switch", None, "best") in codex_app._codex_calls

    async def test_the_codex_switch_screen_switches_the_highlighted_account(self, codex_app):
        from claude_swap.tui.codex_view import CodexSwitchScreen

        async with codex_app.run_test(size=(110, 44)) as pilot:
            await settle(pilot)
            await wait_codex(pilot, codex_app)
            codex_app.push_screen(CodexSwitchScreen())
            for _ in range(10):
                await pilot.pause()
            await pilot.press("down", "enter")
            for _ in range(20):
                await pilot.pause()
            assert ("switch", "2", None) in codex_app._codex_calls

    async def test_no_codex_at_all_hides_the_panel(self, codex_app):
        codex_app._codex_state["overview"] = {**overview(), "installed": False}
        async with codex_app.run_test(size=(110, 44)) as pilot:
            await settle(pilot)
            await wait_codex(pilot, codex_app)
            assert not codex_app.screen.query_one("#codex-panel").display
