"""The claude-swap Textual application.

Owns the snapshot poll loop and every mutating action (switch/add/remove),
so the dashboard, the auto view, and the command palette all drive the same
code paths. Blocking switcher work always runs in thread workers — the UI
loop never touches file locks, keychain subprocesses, or the network.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import replace
from functools import partial

from textual.app import App
from textual.binding import Binding
from textual.reactive import reactive
from textual.worker import WorkerState

from claude_swap import printer
from claude_swap.models import AccountsSnapshot
from claude_swap.snapshot_source import account_identity
from claude_swap.settings import load_settings, load_ui_settings, set_setting
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.tui import codex_view
from claude_swap.tui.autoview import AutoScreen
from claude_swap.tui.dashboard import DashboardScreen, WatchScreen
from claude_swap.tui.data import ActionResult, SnapshotSource, format_duration, run_action
from claude_swap.tui.modals import AddTokenModal, ConfirmModal, OutputModal, TokenForm
from claude_swap.tui.theme import CSWAP_DARK, CSWAP_LIGHT


class CswapApp(App):
    """claude-swap interactive dashboard."""

    TITLE = "agent-swap"
    CSS_PATH = "cswap.tcss"
    # No command palette: actions live in the dashboard's nested menu, in
    # their own context — not in a global searchable list.
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [Binding("ctrl+t", "toggle_theme", "Theme")]

    POLL_INTERVAL_S = 3.0  # matches the old watch view's recapture cadence
    # Snapshot age stays hidden while polling is healthy (age never exceeds
    # ~POLL_INTERVAL_S); past this it surfaces as a staleness alarm. 60s also
    # keeps format_duration in whole minutes, so the note never ticks per
    # second.
    SNAPSHOT_AGE_NOTE_S = 60.0

    snapshot: reactive[AccountsSnapshot | None] = reactive(None)
    refresh_status: reactive[str] = reactive("")
    busy: reactive[bool] = reactive(False)
    # Codex accounts: their own poll, slower than Claude's (see codex_view.fetch_overview).
    codex: reactive[dict | None] = reactive(None)

    def __init__(
        self,
        switcher: ClaudeAccountSwitcher,
        *,
        start: str = "dashboard",
        detected: str | None = None,
    ) -> None:
        super().__init__()
        self.switcher = switcher
        self._start = start  # "dashboard" | "watch" (`aswap watch`)
        self._detected = detected  # terminal background sensed pre-driver, or None
        self.source = SnapshotSource(switcher)
        self._store_only = False
        self._full_next = False
        self._normal_refreshing = False
        self._store_refreshing = False
        self._normal_started_at: float | None = None
        self._refresh_generation = 0
        self._applied_generation = 0
        self._last_refresh_error = ""
        self._codex_refreshing = False
        # The auto-switch lines, drawn as ticks on the status strip's bars
        # everywhere: the threshold on the 5h row, the weekly line (when
        # set) on the 7d and per-model rows. Missing/invalid settings fall
        # back to the default.
        try:
            loaded = load_settings(switcher.backup_dir)
            self.threshold_pct: float | None = loaded.threshold
            self.weekly_threshold_pct: float | None = loaded.weekly_threshold
        except Exception:
            self.threshold_pct = None
            self.weekly_threshold_pct = None
        try:
            self._theme_name = load_ui_settings(switcher.backup_dir).theme
        except Exception:
            self._theme_name = "auto"

    def on_mount(self) -> None:
        self.register_theme(CSWAP_DARK)
        self.register_theme(CSWAP_LIGHT)
        resolved = self._resolved_theme()
        # We own the theme; $TEXTUAL_THEME is intentionally not honoured.
        self.theme = f"cswap-{resolved}"
        printer.set_theme(resolved)
        self.push_screen(DashboardScreen())
        if self._start == "watch":
            # Stacked over the dashboard so Esc lands there, not on exit.
            self.push_screen(WatchScreen())
        self.set_interval(self.POLL_INTERVAL_S, self._tick)
        self.set_interval(1.0, self._update_refresh_status)
        self.set_interval(codex_view.POLL_S, self.request_codex_refresh)
        self._tick()
        self.request_codex_refresh()

    # -- snapshot poll loop ---------------------------------------------------

    def _tick(self) -> None:
        """Start one eligible refresh lane.

        Normal mode prefers the fetch-enabled lane. When that lane is blocked,
        the poll tick may still observe another process's store update through
        one store-only lane. Auto mode already has an engine fetching, so it
        launches only store-only snapshots.
        """
        if self._store_only:
            self._start_store_refresh()
        elif not self._normal_refreshing:
            full, self._full_next = self._full_next, False
            self._start_normal_refresh(full=full)
        else:
            self._start_store_refresh()

    def _start_normal_refresh(self, *, full: bool) -> None:
        if self._normal_refreshing:
            return
        self._normal_refreshing = True
        self._normal_started_at = time.time()
        generation = self._next_refresh_generation()
        self._update_refresh_status()
        self.run_worker(
            partial(self._refresh_blocking, generation, "normal", full, False),
            thread=True,
            group="refresh-normal",
            exit_on_error=False,
            name="snapshot-refresh",
        )

    def _start_store_refresh(self) -> None:
        if self._store_refreshing:
            return
        self._store_refreshing = True
        generation = self._next_refresh_generation()
        self._update_refresh_status()
        self.run_worker(
            partial(self._refresh_blocking, generation, "store", False, True),
            thread=True,
            group="refresh-store",
            exit_on_error=False,
            name="snapshot-store-refresh",
        )

    def _next_refresh_generation(self) -> int:
        self._refresh_generation += 1
        return self._refresh_generation

    def _refresh_blocking(
        self, generation: int, lane: str, full: bool, store_only: bool
    ) -> None:
        snap = self.source.take(full=full, store_only=store_only)
        self.call_from_thread(self._apply_snapshot, generation, lane, snap)
        if self._store_only:
            self.source.refresh_plans()

    def _apply_snapshot(
        self, generation: int, lane: str, snap: AccountsSnapshot
    ) -> None:
        if lane == "normal":
            self._normal_refreshing = False
            self._normal_started_at = None
        else:
            self._store_refreshing = False
        self._last_refresh_error = ""
        if generation >= self._applied_generation:
            self._applied_generation = generation
            self.snapshot = snap
        elif self.snapshot is not None:
            # A later-started store repaint owns account metadata, but the
            # older worker may have completed a genuinely newer provider fetch.
            # SnapshotSource has already rejected per-account regressions, so
            # merge its canonical usage rows without restoring stale metadata.
            current = self.snapshot
            incoming = {acc.number: acc for acc in snap.accounts}
            accounts = tuple(
                replace(acc, usage=other.usage)
                if (
                    (other := incoming.get(acc.number)) is not None
                    and account_identity(acc) == account_identity(other)
                )
                else acc
                for acc in current.accounts
            )
            self.snapshot = replace(
                current,
                accounts=accounts,
                taken_at=max(current.taken_at, snap.taken_at),
            )
        self._update_refresh_status()

    def _update_refresh_status(self) -> None:
        parts: list[str] = []
        now = time.time()
        if self.snapshot is not None:
            age = max(0.0, now - self.snapshot.taken_at)
            if age >= self.SNAPSHOT_AGE_NOTE_S:
                parts.append(f"snapshot {format_duration(age)} ago")
        if self._normal_refreshing and self._normal_started_at is not None:
            elapsed = now - self._normal_started_at
            if elapsed >= self.POLL_INTERVAL_S:
                parts.append(f"refreshing {format_duration(elapsed)}")
        self.refresh_status = " · ".join(parts)

    def request_refresh(self, *, full: bool = False) -> None:
        if full:
            self._full_next = True
        self._tick()

    def set_store_only(self, value: bool) -> None:
        """Auto screen: the engine fetches, the poller only reads the store."""
        self._store_only = value
        self.request_refresh()

    # -- Codex: polled apart from the Claude snapshot ------------------------------

    def request_codex_refresh(self) -> None:
        if self._codex_refreshing:
            return
        self._codex_refreshing = True
        self.run_worker(self._codex_blocking, thread=True, group="refresh-codex",
                        exit_on_error=False, name="codex-refresh")

    def _codex_blocking(self) -> None:
        overview = codex_view.fetch_overview()
        self.call_from_thread(self._apply_codex, overview)

    def _apply_codex(self, overview: dict) -> None:
        self._codex_refreshing = False
        self.codex = overview

    def on_worker_state_changed(self, event) -> None:
        if event.state is not WorkerState.ERROR:
            return
        if event.worker.group == "refresh-codex":
            self._codex_refreshing = False
            return
        if event.worker.group in {"refresh-normal", "refresh-store"}:
            if event.worker.group == "refresh-normal":
                self._normal_refreshing = False
                self._normal_started_at = None
            else:
                self._store_refreshing = False
            self._update_refresh_status()
            msg = str(event.worker.error)
            if msg != self._last_refresh_error:
                self._last_refresh_error = msg
                lane = "store refresh" if event.worker.group == "refresh-store" else "refresh"
                self.notify(
                    f"{lane.capitalize()} failed: {msg}", severity="warning", timeout=6
                )
        elif event.worker.group == "action":
            self.busy = False
            self.notify(f"Action failed: {event.worker.error}", severity="error")
        elif event.worker.group == "engine":
            self.notify(
                f"Auto-switch engine stopped: {event.worker.error}",
                severity="error",
            )

    # -- mutating actions (single-flight, captured, off-thread) ---------------

    def _start_action(self, label: str, fn, *, show_output: bool = False) -> None:
        if self.busy:
            self.notify("Another action is still running", severity="warning")
            return
        self.busy = True
        self.run_worker(
            partial(self._action_blocking, label, fn, show_output),
            thread=True,
            group="action",
            exit_on_error=False,
            name=label,
        )

    def _action_blocking(self, label: str, fn, show_output: bool) -> None:
        result = run_action(fn)
        self.call_from_thread(self._action_done, label, result, show_output)

    def _action_done(
        self, label: str, result: ActionResult, show_output: bool
    ) -> None:
        self.busy = False
        self.request_refresh()
        self.request_codex_refresh()
        if not result.ok:
            self.push_screen(OutputModal(f"{label} — failed", result.output))
            return
        payload = result.payload or {}
        if "switched" in payload:
            if payload.get("switched"):
                to = payload.get("to") or {}
                target = to.get("email") or f"account {to.get('number')}"
                self.notify(f"Switched to {target}", title="Switch")
                if payload.get("note"):
                    self.notify(str(payload["note"]), title="Codex", timeout=8)
            else:
                reason = str(payload.get("reason") or "no switch performed")
                self.notify(reason, title="No switch", severity="warning")
            return
        if show_output and result.output.strip():
            self.push_screen(OutputModal(label, result.output))
        elif result.first_line:
            self.notify(result.first_line)

    # -- account operations ----------------------------------------------------

    def do_switch(self, number: str) -> None:
        self._start_action(
            f"Switch to account {number}",
            partial(self.switcher.switch_to, number, json_output=True),
        )

    def action_switch_best(self) -> None:
        self._start_action(
            "Switch (best)",
            partial(self.switcher.switch, strategy="best", json_output=True),
        )

    def do_toggle_disabled(self, number: str) -> None:
        """Hold the account out of auto-rotation, or return it — reads its
        current state from the live snapshot to pick the direction."""
        snap = self.snapshot
        acc = next(
            (a for a in (snap.accounts if snap else ()) if a.number == number), None
        )
        if acc is None:
            return
        target = not acc.disabled
        verb = "Disable" if target else "Enable"
        self._start_action(
            f"{verb} account {number}",
            partial(self.switcher.set_account_disabled, number, target),
        )

    def confirm_remove(self, number: str, email: str) -> None:
        self.push_screen(
            ConfirmModal(
                f"Remove account {number} ({email})?\n\n"
                "Its stored credentials and config backup are deleted.",
                title="Remove account",
                yes_label="Remove",
            ),
            partial(self._on_remove_confirm, number),
        )

    def _on_remove_confirm(self, number: str, confirmed: bool | None) -> None:
        if confirmed:
            self._start_action(
                f"Remove account {number}",
                partial(self.switcher.remove_account, number, assume_yes=True),
            )

    def action_add_current(self) -> None:
        self.push_screen(
            ConfirmModal(
                "Back up the current Claude Code login as a managed account?\n\n"
                "If this account is already managed, its stored credentials "
                "are refreshed in place.",
                title="Add account",
                yes_label="Add",
            ),
            self._on_add_confirm,
        )

    def _on_add_confirm(self, confirmed: bool | None) -> None:
        if confirmed:
            self._start_action(
                "Add current login",
                partial(self.switcher.add_account),
                show_output=True,
            )

    def action_add_token(self) -> None:
        self.push_screen(AddTokenModal(), self._on_token_form)

    def _on_token_form(self, form: TokenForm | None) -> None:
        if form is None:
            return
        run = partial(
            self._start_action,
            "Add account from token",
            partial(
                self.switcher.add_account_from_token,
                token=form.token,
                email=form.email,
                slot=form.slot,
                assume_yes=True,
            ),
            show_output=True,
        )
        occupant = self._slot_occupant(form.slot)
        if occupant is not None:
            self.push_screen(
                ConfirmModal(
                    f"Slot {form.slot} is occupied by {occupant}. Overwrite?",
                    title="Overwrite slot",
                    yes_label="Overwrite",
                ),
                lambda confirmed: run() if confirmed else None,
            )
        else:
            run()

    def _slot_occupant(self, slot: int | None) -> str | None:
        if slot is None or self.snapshot is None:
            return None
        for acc in self.snapshot.accounts:
            if acc.number == str(slot):
                return acc.email
        return None

    # -- Codex operations (same single-flight runner as Claude's) ---------------------

    def do_codex_switch(self, slot: str) -> None:
        self._start_action(f"Switch Codex to slot {slot}", partial(_codex_switch, slot, None))

    def codex_rotate(self, strategy: str | None) -> None:
        self._start_action(f"Codex: {strategy or 'rotate'}", partial(_codex_switch, None, strategy))

    def codex_save(self) -> None:
        self._start_action("Save Codex login", _codex_save)

    def codex_check(self) -> None:
        self._start_action("Check every Codex login", _codex_check, show_output=True)

    def codex_toggle_disabled(self, slot: str) -> None:
        rows = (self.codex or {}).get("accounts", [])
        row = next((r for r in rows if str(r["slot"]) == slot), None)
        if row is not None:
            self._start_action("Codex: " + ("enable" if row["disabled"] else "disable") + f" slot {slot}",
                               partial(_codex_disable, slot, not row["disabled"]))

    def confirm_codex_remove(self, slot: str, email: str) -> None:
        self.push_screen(
            ConfirmModal(f"Remove Codex slot {slot} ({email})?\n\n"
                         "Its stored copy is deleted; Codex itself is not touched.",
                         title="Remove Codex account", yes_label="Remove"),
            lambda ok: self._start_action(f"Remove Codex slot {slot}", partial(_codex_remove, slot)) if ok else None,
        )

    def codex_sign_in(self) -> None:
        """Leave the dashboard for `codex login` in this terminal, save the result, come back.

        The login in use is saved first unless OpenAI already revoked it (that copy is dead).
        """
        if self.busy:
            self.notify("Another action is still running", severity="warning")
            return
        binary = shutil.which("codex")
        if binary is None:
            self.notify("Codex (`codex`) was not found on PATH", severity="error")
            return
        from claude_swap.codex import Codex

        cx = Codex()
        unsaved = (self.codex or {}).get("unsaved")
        try:
            if cx.status().get("state") == "unmanaged" and not (
                    unsaved and unsaved.get("problem") in ("revoked", "unauthorized")):
                cx.add()
        except Exception as e:  # nothing is replaced if the first save fails
            self.notify(f"Could not save the Codex login in use first: {e}", severity="error")
            return
        with self.suspend():
            print("\nagent-swap: signing in to another Codex account. Finish in the browser.\n", flush=True)
            try:
                code = subprocess.run([binary, "login"]).returncode
            except KeyboardInterrupt:
                code = 130
            if code == 0:
                try:
                    slot, created = cx.add()
                    print(f"\nSaved as Codex slot {slot}.", flush=True)
                except Exception as e:
                    print(f"\nSigned in, but saving failed: {e}", flush=True)
            else:
                print("\nThe login did not complete; nothing was saved.", flush=True)
            try:
                input("Press Enter to return to agent-swap… ")
            except (EOFError, KeyboardInterrupt):
                pass
        self.request_codex_refresh()

    # -- navigation -------------------------------------------------------------

    def action_refresh_full(self) -> None:
        self.request_refresh(full=True)
        self.notify("Refreshing usage…", timeout=2)

    def action_open_auto(self) -> None:
        if isinstance(self.screen, AutoScreen):
            return
        self.push_screen(AutoScreen())

    def action_open_watch(self) -> None:
        if isinstance(self.screen, WatchScreen):
            return
        self.push_screen(WatchScreen())

    # -- theme --------------------------------------------------------------

    def _resolved_theme(self) -> str:
        """Concrete 'dark'/'light' for the current setting; auto → detected → dark."""
        if self._theme_name == "auto":
            return self._detected or "dark"
        return self._theme_name

    def apply_theme(self, name: str) -> None:
        """Switch the live theme: TUI + captured printer output + persistence.

        ``name`` is the setting; ``auto`` resolves against the pre-driver
        detection (never re-probes mid-session)."""
        self._theme_name = name
        resolved = self._resolved_theme()
        self.theme = f"cswap-{resolved}"
        printer.set_theme(resolved)
        try:
            set_setting(self.switcher.backup_dir, "ui.theme", name)
        except Exception as exc:  # persistence is best-effort; never crash the UI
            self.notify(f"Could not save theme: {exc}", severity="warning")

    def action_toggle_theme(self) -> None:
        order = ("dark", "light", "auto")
        nxt = order[(order.index(self._theme_name) + 1) % len(order)]
        self.apply_theme(nxt)
        self.notify(f"Theme: {nxt}")


# -- Codex actions, run off the UI thread by `_start_action` -----------------------------


def _codex_note() -> str | None:
    """What a switch means for a Codex that is already running."""
    from claude_swap.codex import Codex, codex_running
    from claude_swap.codex_run import supervised_count

    if not codex_running():
        return None
    followers = supervised_count(Codex())
    if followers:
        return f"{followers} Codex session(s) started with `aswap codex run` are reopening on the new login."
    return "A Codex already running keeps its account until restarted (start it with `aswap codex run`)."


def _codex_switch(target: str | None, strategy: str | None) -> dict:
    from claude_swap.codex import Codex

    cx = Codex()
    slot, changed = cx.switch(target, strategy=strategy)
    acct = cx._load()["accounts"].get(str(slot), {})
    label = acct.get("alias") or acct.get("email") or f"slot {slot}"
    if not changed:
        return {"switched": False, "reason": f"Codex is already on {label}"}
    return {"switched": True, "to": {"number": slot, "email": f"Codex {label}"}, "note": _codex_note()}


def _codex_save() -> None:
    from claude_swap.codex import Codex

    cx = Codex()
    cx.refuse_if_revoked()
    slot, created = cx.add()
    print(f"{'Added' if created else 'Refreshed'} Codex slot {slot}")


def _codex_check() -> None:
    from claude_swap.codex import Codex

    names = {"ok": "login ok", "revoked": "login revoked: sign in again", "unauthorized": "login rejected: sign in again",
             "failed": "could not check", "expiring": "token about to refresh (checked on its next use)", "apikey": "API key"}
    results = Codex().check_all()
    if not results:
        print("No Codex accounts yet.")
    for r in results:
        usage, parts = r["usage"] or {}, []
        for key in ("primary", "secondary"):
            w = usage.get(key)
            if w:
                parts.append(f"{codex_view.window_label(w['window_minutes'])} {w['used_percent']:.0f}%")
        print(f"{r['slot']:>2}  {r['label']}: {names.get(r['status'], r['status'])}" + (f"  ({' · '.join(parts)})" if parts else ""))


def _codex_disable(slot: str, disabled: bool) -> None:
    from claude_swap.codex import Codex

    Codex().set_disabled(slot, disabled)
    print(f"{'Disabled' if disabled else 'Enabled'} Codex slot {slot}")


def _codex_remove(slot: str) -> None:
    from claude_swap.codex import Codex

    Codex().remove(slot, yes=True)
    print(f"Removed Codex slot {slot}")
