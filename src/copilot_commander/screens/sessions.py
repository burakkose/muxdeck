"""Sessions browser screen — discover, inspect, and resume Copilot CLI sessions."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Input

from copilot_commander.bindings import SESSIONS_BINDINGS, SESSIONS_HINTS
from copilot_commander.screens.base import ShellScreen
from copilot_commander.widgets.sessions import (
    SessionDetailPanel,
    SessionListPanel,
    SessionSelected,
    SessionSummaryBar,
)

if TYPE_CHECKING:
    from copilot_commander.app import CommanderRuntime
    from copilot_commander.controllers.sessions_controller import SessionsState


class SessionsScreen(ShellScreen):
    SCREEN_TITLE = "SESSIONS"
    BINDINGS = SESSIONS_BINDINGS
    FOOTER_HINTS = SESSIONS_HINTS

    def __init__(self, runtime: CommanderRuntime) -> None:
        super().__init__(runtime)
        self._selected_session_id: str | None = None
        self._state: SessionsState | None = None
        self._show_completed: bool = True
        self._filter_text: str = ""
        self._filter_debounce_timer: object | None = None
        self._detail_timer: object | None = None

    def compose_body(self) -> ComposeResult:
        with Vertical(id="sessions-root"):
            yield SessionSummaryBar(widget_id="sessions-summary", classes="muted")
            yield Input(
                placeholder="/ filter sessions",
                id="sessions-filter-input",
            )
            with Horizontal(id="sessions-main"):
                yield SessionListPanel(
                    widget_id="sessions-list",
                    classes="panel",
                )
                yield SessionDetailPanel(
                    widget_id="sessions-detail",
                    classes="panel",
                )

    def on_mount(self) -> None:
        self.refresh_data()
        self.call_after_refresh(self.query_one(SessionListPanel).focus_list)

    def on_show(self) -> None:
        self.refresh_data()

    def refresh_data(self) -> None:
        if self.runtime.sessions_ctrl is None:
            return
        # Collect live agent session IDs for correlation
        live_ids: frozenset[str] = frozenset()
        try:
            agents = self.runtime.store.list_agents()
            live_ids = frozenset(a.copilot_session_id for a in agents if a.copilot_session_id)
        except Exception:
            pass

        self._state = self.runtime.sessions_ctrl.build_state(
            live_session_ids=live_ids,
            selected_session_id=self._selected_session_id,
            filter_text=self._filter_text,
            show_completed=self._show_completed,
        )

        list_panel = self.query_one(SessionListPanel)
        list_panel.set_sessions(
            self._state.sessions,
            selected_session_id=self._selected_session_id,
            notify=False,
        )

        detail_panel = self.query_one(SessionDetailPanel)
        detail_panel.set_detail(self._state.selected)

        summary = self.query_one(SessionSummaryBar)
        summary.set_counts(
            self._state.total_count,
            self._state.active_count,
            self._state.unclosed_count,
            self._state.completed_count,
        )

    def on_session_selected(self, event: SessionSelected) -> None:
        if event.session_id == self._selected_session_id:
            return
        self._selected_session_id = event.session_id
        if self._detail_timer is not None:
            self._detail_timer.stop()  # type: ignore[union-attr]
        self._detail_timer = self.set_timer(0.05, self._update_selected_detail)

    def _update_selected_detail(self) -> None:
        """Lightweight update — only refresh detail panel for cursor movement."""
        if self.runtime.sessions_ctrl is None or self._state is None:
            return
        detail = self.runtime.sessions_ctrl.get_session_detail(self._selected_session_id)
        self.query_one(SessionDetailPanel).set_detail(detail)

    # ── actions ──────────────────────────────────────────────────────

    def action_focus_filter(self) -> None:
        """Focus the filter input."""
        self.query_one("#sessions-filter-input", Input).focus()

    def action_escape_filter(self) -> None:
        """Return focus to the session list (ESC from filter)."""
        self.query_one(SessionListPanel).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "sessions-filter-input":
            self._filter_text = event.value
            if self._filter_debounce_timer is not None:
                self._filter_debounce_timer.stop()  # type: ignore[union-attr]
            self._filter_debounce_timer = self.set_timer(0.3, self.refresh_data)

    def action_cursor_down(self) -> None:
        sid = self.query_one(SessionListPanel).move_cursor(1)
        if sid:
            self._selected_session_id = sid

    def action_cursor_up(self) -> None:
        sid = self.query_one(SessionListPanel).move_cursor(-1)
        if sid:
            self._selected_session_id = sid

    def action_toggle_completed(self) -> None:
        self._show_completed = not self._show_completed
        label = "showing" if self._show_completed else "hiding"
        self.set_status(f"{label} completed sessions")
        self.refresh_data()

    def action_resume_session(self) -> None:
        """Resume the selected session in a new tmux window."""
        if self._selected_session_id is None:
            self.set_status("no session selected")
            return
        if self.runtime.actions is None:
            self.set_status("✗ action service unavailable")
            return
        if self.runtime.sessions_ctrl is None:
            self.set_status("✗ session store unavailable")
            return

        session = self.runtime.sessions_ctrl._store.get_session(self._selected_session_id)
        if session is None:
            self.set_status(f"✗ session {self._selected_session_id[:8]}… not found")
            return

        result = self.runtime.actions.resume_session(
            session.session_id,
            cwd=session.cwd or session.git_root,
            window_name=f"copilot-{(session.summary or session.session_id)[:20]}",
            origin=session.origin,
            windows_cwd=session.windows_cwd,
        )
        if result.success:
            self.set_status(f"✓ {result.message}")
        else:
            self.set_status(f"✗ {result.message}")

    def action_copy_session_id(self) -> None:
        """Show session ID and resume command in status for easy copy."""
        if self._selected_session_id is None:
            self.set_status("no session selected")
            return
        self.set_status(f"copilot --resume={self._selected_session_id}")

    def action_focus_pane(self) -> None:
        """Focus the tmux pane of an active session."""
        if self._selected_session_id is None:
            self.set_status("no session selected")
            return
        if self._state is None:
            return
        # Find if this session is active with a live pane
        try:
            agents = self.runtime.store.list_agents()
            for agent in agents:
                if (
                    agent.copilot_session_id == self._selected_session_id
                    and self.runtime.actions
                    and agent.tmux_pane_id
                ):
                    result = self.runtime.actions.focus_pane(
                        agent.tmux_pane_id,
                        window_id=agent.tmux_window_id or None,
                        session_name=agent.tmux_session_name or None,
                    )
                    msg = f"✓ {result.message}" if result.success else f"✗ {result.message}"
                    self.set_status(msg)
                    return
        except Exception:
            pass
        self.set_status("session has no active pane — use r to resume")


__all__ = ["SessionsScreen"]
