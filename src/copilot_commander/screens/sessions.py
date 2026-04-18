"""Sessions browser screen — discover, inspect, and resume Copilot CLI sessions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.timer import Timer
from textual.widgets import Input
from textual.worker import Worker, WorkerState

from copilot_commander.bindings import SESSIONS_BINDINGS, SESSIONS_HINTS
from copilot_commander.screens.base import ShellScreen
from copilot_commander.screens.compose_mirror import ComposeWithMirrorScreen
from copilot_commander.widgets.sessions import (
    SessionActionBar,
    SessionDetailPanel,
    SessionListPanel,
    SessionSelected,
    SessionSummaryBar,
)

if TYPE_CHECKING:
    from copilot_commander.app import CommanderApp, CommanderRuntime
    from copilot_commander.controllers.sessions_controller import SessionDetailView, SessionsState


_WORKER_NAME = "sessions_load"


@dataclass(frozen=True, slots=True)
class _LiveSessionTarget:
    pane_id: str
    window_id: str | None
    session_name: str | None


@dataclass(frozen=True, slots=True)
class _LoadedSessionsState:
    state: SessionsState
    live_session_ids: frozenset[str]
    live_targets: dict[str, _LiveSessionTarget]


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
        self._filter_debounce_timer: Timer | None = None
        self._detail_timer: Timer | None = None
        self._loading: bool = False
        self._selected_detail: SessionDetailView | None = None
        self._live_session_ids: frozenset[str] = frozenset()
        self._live_targets: dict[str, _LiveSessionTarget] = {}
        self._skip_next_show_refresh: bool = True

    @property
    def commander_app(self) -> CommanderApp:
        return cast("CommanderApp", self.app)

    def compose_body(self) -> ComposeResult:
        with Vertical(id="sessions-root"):
            yield SessionSummaryBar(widget_id="sessions-summary", classes="muted")
            yield Input(
                placeholder="/ filter sessions",
                id="sessions-filter-input",
            )
            yield SessionActionBar(widget_id="sessions-actions", classes="muted")
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
        self._refresh_on_activate()

    def on_screen_resume(self) -> None:
        self._refresh_on_activate()

    def _refresh_on_activate(self) -> None:
        # Textual mode switches resume cached screens, but the first
        # activation can also emit a show event around mount.
        if self._skip_next_show_refresh:
            self._skip_next_show_refresh = False
            return
        if self._loading:
            return
        self.refresh_data()

    def refresh_data(self) -> None:
        """Kick off a background worker; UI thread is never blocked.

        The session store scan can take seconds on WSL (it walks the
        Windows-side ``/mnt/c`` session directory). Running it inline
        would freeze the event loop on every tab switch, so we dispatch
        to a worker thread and paint when it returns.
        """
        if self.runtime.sessions_ctrl is None:
            return

        # Keep showing the previous state until the worker finishes so
        # the screen never flashes blank on refresh. On first load
        # (_state is None) paint a loading indicator on the list +
        # detail panels so the user sees progress instead of an empty
        # shell.
        first_load = self._state is None
        if first_load and not self._loading:
            self.set_status("loading sessions…")
            self.query_one(SessionSummaryBar).show_loading(
                filter_text=self._filter_text,
                show_completed=self._show_completed,
            )
            self.query_one(SessionActionBar).show_loading(
                filter_text=self._filter_text,
                show_completed=self._show_completed,
            )
            self.begin_loading(
                self.query_one(SessionListPanel),
                self.query_one(SessionDetailPanel),
            )

        selected_id = self._selected_session_id
        filter_text = self._filter_text
        show_completed = self._show_completed
        sessions_ctrl = self.runtime.sessions_ctrl
        live_store = self.runtime.sync_store or self.runtime.store

        def _load() -> _LoadedSessionsState | None:
            # Live agent ids correlate running tmux panes with session
            # files on disk. Use the dedicated thread-safe SQLite store
            # when available because this function runs in a worker thread.
            live_ids: set[str] = set()
            live_targets: dict[str, _LiveSessionTarget] = {}
            agents = live_store.list_agents()
            for agent in agents:
                session_id = agent.copilot_session_id
                if not session_id:
                    continue
                live_ids.add(session_id)
                if not agent.tmux_pane_id:
                    continue
                live_targets[session_id] = _LiveSessionTarget(
                    pane_id=agent.tmux_pane_id,
                    window_id=agent.tmux_window_id or None,
                    session_name=agent.tmux_session_name or None,
                )
            state = sessions_ctrl.build_state(
                live_session_ids=frozenset(live_ids),
                selected_session_id=selected_id,
                filter_text=filter_text,
                show_completed=show_completed,
            )
            return _LoadedSessionsState(
                state=state,
                live_session_ids=frozenset(live_ids),
                live_targets=live_targets,
            )

        self._loading = True
        self.run_worker(_load, thread=True, exclusive=True, name=_WORKER_NAME)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name != _WORKER_NAME:
            return
        if event.state == WorkerState.ERROR:
            self._loading = False
            self.end_loading(
                self.query_one(SessionListPanel),
                self.query_one(SessionDetailPanel),
            )
            self.set_status("session load failed")
            return
        if event.state != WorkerState.SUCCESS:
            return
        self._loading = False
        self.end_loading(
            self.query_one(SessionListPanel),
            self.query_one(SessionDetailPanel),
        )
        loaded = cast(_LoadedSessionsState | None, event.worker.result)
        if loaded is None:
            return
        self._live_session_ids = loaded.live_session_ids
        self._live_targets = loaded.live_targets
        self._apply_state(loaded.state)

    def _apply_state(self, state: SessionsState) -> None:
        self._state = state
        if state.selected_session_id is not None:
            self._selected_session_id = state.selected_session_id
            self.commander_app.remember_session_selection(state.selected_session_id)
        self._selected_detail = state.selected

        list_panel = self.query_one(SessionListPanel)
        list_panel.set_sessions(
            state.sessions,
            selected_session_id=state.selected_session_id,
            notify=False,
        )

        detail_panel = self.query_one(SessionDetailPanel)
        detail_panel.set_detail(self._selected_detail)
        self.query_one(SessionActionBar).set_state(
            self._selected_detail,
            has_live_pane=self._selected_session_id in self._live_targets,
            filter_text=self._filter_text,
            show_completed=self._show_completed,
        )

        summary = self.query_one(SessionSummaryBar)
        summary.set_counts(
            state.total_count,
            state.active_count,
            state.unclosed_count,
            state.completed_count,
        )
        self.set_status(f"{state.total_count} sessions · {state.active_count} active")

    def on_session_selected(self, event: SessionSelected) -> None:
        if event.session_id == self._selected_session_id:
            return
        self._selected_session_id = event.session_id
        self.commander_app.remember_session_selection(event.session_id)
        if self._detail_timer is not None:
            self._detail_timer.stop()
        self._detail_timer = self.set_timer(0.05, self._update_selected_detail)

    def _update_selected_detail(self) -> None:
        """Lightweight update — only refresh detail panel for cursor movement."""
        if self.runtime.sessions_ctrl is None or self._state is None:
            return
        detail = self.runtime.sessions_ctrl.get_session_detail(
            self._selected_session_id,
            live_session_ids=self._live_session_ids,
        )
        self._selected_detail = detail
        self.query_one(SessionDetailPanel).set_detail(detail)
        self.query_one(SessionActionBar).set_state(
            detail,
            has_live_pane=self._selected_session_id in self._live_targets,
            filter_text=self._filter_text,
            show_completed=self._show_completed,
        )

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
                self._filter_debounce_timer.stop()
            self._filter_debounce_timer = self.set_timer(0.3, self.refresh_data)

    def action_cursor_down(self) -> None:
        self.query_one(SessionListPanel).move_cursor(1)

    def action_cursor_up(self) -> None:
        self.query_one(SessionListPanel).move_cursor(-1)

    def action_open_replay(self) -> None:
        if self._selected_session_id is None:
            self.set_status("no session selected")
            return
        self.commander_app.remember_session_selection(self._selected_session_id)
        self.commander_app.selected_agent_id = None
        self.commander_app.switch_mode("replay")

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
        detail = self._selected_detail
        if detail is None:
            self.set_status("no session selected")
            return
        start_directory: Path | None = None
        if detail.origin != "windows":
            preferred = detail.cwd if detail.cwd != "—" else detail.git_root
            if preferred != "—":
                start_directory = Path(preferred)

        result = self.runtime.actions.resume_session(
            detail.session_id,
            cwd=start_directory,
            window_name=f"copilot-{(detail.summary or detail.session_id)[:20]}",
            origin=detail.origin,
            windows_cwd=detail.windows_cwd,
        )
        if result.success:
            self.set_status(f"✓ {result.message}")
        else:
            self.set_status(f"✗ {result.message}")

    def action_copy_session_id(self) -> None:
        """Show session ID and resume command in status for easy copy."""
        if self._selected_detail is None:
            self.set_status("no session selected")
            return
        self.set_status(self._selected_detail.resume_command)

    def action_focus_pane(self) -> None:
        """Focus the tmux pane of an active session."""
        target = self._selected_live_target()
        if target is None:
            self.set_status("session has no active pane — press l for live once it reconnects")
            return
        if self.runtime.actions is None:
            self.set_status("✗ action service unavailable")
            return
        result = self.runtime.actions.focus_pane(
            target.pane_id,
            window_id=target.window_id,
            session_name=target.session_name,
        )
        msg = f"✓ {result.message}" if result.success else f"✗ {result.message}"
        self.set_status(msg)

    def action_open_live(self) -> None:
        target = self._selected_live_target()
        if self._selected_detail is None:
            self.set_status("no session selected")
            return
        if target is None:
            self.set_status("session has no live pane to mirror")
            return
        if self.runtime.pane_stream is None:
            self.set_status("✗ pane streaming unavailable")
            return
        display_name = (
            self._selected_detail.summary
            if self._selected_detail.repository == "—"
            else self._selected_detail.repository
        )
        self.app.push_screen(
            ComposeWithMirrorScreen(
                self.runtime,
                pane_id=target.pane_id,
                display_name=display_name,
            )
        )

    def _selected_live_target(self) -> _LiveSessionTarget | None:
        if self._selected_session_id is None:
            return None
        return self._live_targets.get(self._selected_session_id)


__all__ = ["SessionsScreen"]
