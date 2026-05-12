"""Sessions browser screen — discover, inspect, and resume Copilot CLI sessions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.timer import Timer
from textual.widgets import Input
from textual.worker import Worker, WorkerState

from muxdeck.adapters.pane_stream import PaneStreamAdapter
from muxdeck.bindings import SESSIONS_BINDINGS, SESSIONS_HINTS
from muxdeck.screens.base import ShellScreen
from muxdeck.screens.compose_mirror import ComposeWithMirrorScreen
from muxdeck.widgets.sessions import (
    SessionActionBar,
    SessionDetailPanel,
    SessionListPanel,
    SessionSelected,
    SessionSummaryBar,
)

if TYPE_CHECKING:
    from muxdeck.app import MuxdeckApp, MuxdeckRuntime
    from muxdeck.controllers.sessions_controller import SessionDetailView, SessionsState


_WORKER_NAME = "sessions_load"

# tmux's target syntax is ``<session>:<window>`` so a literal ``:`` in
# a window name confuses the address parser; control chars render as
# garbage in the status line; very long titles overflow the bar and
# truncate the more useful right-hand edge. The 40-char ceiling is
# wide enough for human session titles ("Refactor ReplicationSequence
# Abstraction") yet short enough to fit alongside the session
# indicator in tmux's default status format.
_WINDOW_NAME_FORBIDDEN = re.compile(r"[\x00-\x1f\t\n\r:]+")
_WINDOW_NAME_WHITESPACE = re.compile(r"\s+")
_WINDOW_NAME_MAX_LEN = 40


def _build_window_name(summary: str | None, session_id: str) -> str:
    """Sanitize a session summary into a tmux-safe window name.

    Falls back to ``copilot-<id8>`` when ``summary`` is missing or
    sanitizes to empty (e.g. a title made entirely of control chars
    or colons).
    """
    fallback = f"copilot-{session_id[:8]}"
    if not summary or summary == "—":
        return fallback
    cleaned = _WINDOW_NAME_FORBIDDEN.sub(" ", summary)
    cleaned = _WINDOW_NAME_WHITESPACE.sub(" ", cleaned).strip()
    if not cleaned:
        return fallback
    if len(cleaned) > _WINDOW_NAME_MAX_LEN:
        cleaned = cleaned[:_WINDOW_NAME_MAX_LEN].rstrip()
    return cleaned or fallback


@dataclass(frozen=True, slots=True)
class _LiveSessionTarget:
    pane_id: str
    window_id: str | None
    session_name: str | None
    pane_pid: int | None = None


@dataclass(frozen=True, slots=True)
class _LoadedSessionsState:
    state: SessionsState
    live_session_ids: frozenset[str]
    live_targets: dict[str, _LiveSessionTarget]


class SessionsScreen(ShellScreen):
    SCREEN_TITLE = "SESSIONS"
    BINDINGS = SESSIONS_BINDINGS
    FOOTER_HINTS = SESSIONS_HINTS

    def __init__(self, runtime: MuxdeckRuntime) -> None:
        super().__init__(runtime)
        self._selected_session_id: str | None = None
        self._state: SessionsState | None = None
        self._show_completed: bool = True
        self._filter_text: str = ""
        self._filter_debounce_timer: Timer | None = None
        self._detail_timer: Timer | None = None
        self._loading: bool = False
        self._refresh_pending: bool = False
        self._selected_detail: SessionDetailView | None = None
        # Tracks which session is currently rendered in the detail
        # panel + action bar so cursor moves that bounce back to the
        # same id (or refreshes that re-select the active row) skip the
        # detail repaint entirely.
        self._rendered_detail_session_id: str | None = None
        self._live_session_ids: frozenset[str] = frozenset()
        self._live_targets: dict[str, _LiveSessionTarget] = {}
        self._skip_next_show_refresh: bool = True

    @property
    def muxdeck_app(self) -> MuxdeckApp:
        return cast("MuxdeckApp", self.app)

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
                    classes="panel focusable",
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
        if self._loading:
            # Thread workers can't be force-cancelled once they're inside
            # a blocking filesystem scan, so coalesce refresh requests
            # instead of piling up parallel rescans.
            self._refresh_pending = True
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
                    pane_pid=getattr(agent, "pid", None),
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
        super().on_worker_state_changed(event)
        if event.worker.name != _WORKER_NAME:
            return
        if event.state == WorkerState.ERROR:
            self._loading = False
            self.end_loading(
                self.query_one(SessionListPanel),
                self.query_one(SessionDetailPanel),
            )
            self.set_status("session load failed")
            self._schedule_pending_refresh()
            return
        if event.state == WorkerState.CANCELLED:
            self._loading = False
            self.end_loading(
                self.query_one(SessionListPanel),
                self.query_one(SessionDetailPanel),
            )
            self._schedule_pending_refresh()
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
            self._schedule_pending_refresh()
            return
        self._live_session_ids = loaded.live_session_ids
        self._live_targets = loaded.live_targets
        self._apply_state(loaded.state)
        self._schedule_pending_refresh()

    def _schedule_pending_refresh(self) -> None:
        if not self._refresh_pending:
            return
        self._refresh_pending = False
        if self.is_mounted:
            self.call_after_refresh(self.refresh_data)

    def _apply_state(self, state: SessionsState) -> None:
        self._state = state
        if state.selected_session_id is not None:
            self._selected_session_id = state.selected_session_id
            self.muxdeck_app.remember_session_selection(state.selected_session_id)
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
        self._rendered_detail_session_id = (
            self._selected_detail.session_id if self._selected_detail else None
        )

        summary = self.query_one(SessionSummaryBar)
        summary.set_counts(
            state.total_count,
            state.active_count,
            state.unclosed_count,
            state.completed_count,
        )
        parts = [f"{state.total_count} sessions", f"{state.active_count} active"]
        query = self._filter_text.strip()
        if query:
            parts.append(f"filter:{query}")
        if not self._show_completed:
            parts.append("hide-done")
        self.set_status(" · ".join(parts))

    def on_session_selected(self, event: SessionSelected) -> None:
        if event.session_id == self._selected_session_id:
            return
        self._selected_session_id = event.session_id
        self.muxdeck_app.remember_session_selection(event.session_id)
        # Refresh ``_selected_detail`` synchronously so any user action
        # that races a cursor move (e.g. pressing ``y`` to copy details
        # right after ``j``) sees the up-to-date payload. The warm-cache
        # lookup behind ``get_session_detail`` is O(1) — sub-ms in
        # practice — so it does not contribute to perceived lag.
        self._load_selected_detail()
        if self._detail_timer is not None:
            self._detail_timer.stop()
        # Detail/action-bar repaint debounce. Each ``Static.update`` on
        # those panels triggers ``refresh(layout=True)``, which is the
        # actual source of cursor-movement lag at scale. Coalescing
        # repaints to the trailing edge of typematic key repeat keeps
        # held-down j/k feeling smooth while a deliberate single tap
        # still updates the panels in 120ms.
        self._detail_timer = self.set_timer(0.12, self._repaint_selected_detail)

    def _load_selected_detail(self) -> None:
        """Refresh ``_selected_detail`` from the controller (no paint)."""
        if self.runtime.sessions_ctrl is None:
            return
        self._selected_detail = self.runtime.sessions_ctrl.get_session_detail(
            self._selected_session_id,
            live_session_ids=self._live_session_ids,
        )

    def _repaint_selected_detail(self) -> None:
        """Paint the latest ``_selected_detail`` into the side panels.

        The data load is split into ``_load_selected_detail`` so this
        method can short-circuit when the requested session is already
        on screen, avoiding redundant ``Static.update`` / layout passes
        when the cursor bounces back to a previously-rendered row.
        """
        if self._state is None:
            return
        selected = self._selected_session_id
        if selected is not None and selected == self._rendered_detail_session_id:
            return
        detail = self._selected_detail
        self._rendered_detail_session_id = detail.session_id if detail is not None else None
        self.query_one(SessionDetailPanel).set_detail(detail)
        self.query_one(SessionActionBar).set_state(
            detail,
            has_live_pane=selected in self._live_targets,
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
        self.muxdeck_app.remember_session_selection(self._selected_session_id)
        self.muxdeck_app.selected_agent_id = None
        self.muxdeck_app.switch_mode("replay")

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
            window_name=_build_window_name(detail.summary, detail.session_id),
            origin=detail.origin,
            windows_cwd=detail.windows_cwd,
        )
        if result.success:
            # Resume just spawned a fresh ``copilot`` process. Two
            # caches are about to be wrong:
            #   * The InuseLockResolver lock-cache (15 s TTL) does
            #     not yet contain the new ``inuse.<pid>.lock`` so the
            #     next sync would persist the agent without a
            #     ``copilot_session_id`` and the SESSIONS row would
            #     keep its stale "completed/unclosed" status.
            #   * The CopilotSessionStore TTL cache (10 s) does not
            #     yet contain the freshly-touched workspace.yaml so
            #     a subsequent ``/name`` rename inside the resumed
            #     session would also be invisible until the TTL
            #     elapsed.
            # Drop both caches so the sync we trigger right after
            # paints the active state on the very next refresh tick.
            resolver = getattr(self.runtime, "session_resolver", None)
            if resolver is not None:
                resolver.invalidate_lock_cache()
            session_store = getattr(self.runtime, "copilot_session_store", None)
            if session_store is not None:
                session_store.invalidate()
            self.set_status(f"✓ {result.message} · waiting for sync…")
            # Manual refresh threads through ``MuxdeckApp._refresh_current_screen``
            # which kicks the synchronizer worker and on completion
            # calls ``_refresh_screen_widgets(force=True)``. That
            # invokes ``SessionsScreen.refresh_data`` which runs the
            # ``_load`` worker against ``live_store.list_agents()``;
            # by then the freshly persisted agent carries its
            # ``copilot_session_id`` so ``live_session_ids`` includes
            # this row and the status flips to "active". No periodic
            # screen-level timer is needed: the app sync loop is the
            # single source of truth for "the world might have
            # changed".
            kicker = getattr(self.muxdeck_app, "action_refresh_screen", None)
            if callable(kicker):
                kicker()
            else:
                # Test harnesses without the full MuxdeckApp shape
                # (e.g. ``_Harness`` in unit tests) won't have a sync
                # loop to drive; refresh the screen directly so the
                # next assertion sees the post-resume state.
                self.refresh_data()
        else:
            self.set_status(f"✗ {result.message}")

    def action_copy_details(self) -> None:
        selected_session_id = self.query_one(SessionListPanel).get_selected_id()
        if selected_session_id is None:
            self.set_status("no session selected")
            return
        self._selected_session_id = selected_session_id
        self.muxdeck_app.remember_session_selection(selected_session_id)
        self._load_selected_detail()
        if self._selected_detail is None:
            self.set_status("no session detail loaded")
            return
        # Flush any pending debounced repaint so the rendered detail
        # panel reflects the session we're about to copy.
        if self._detail_timer is not None:
            self._detail_timer.stop()
            self._detail_timer = None
        self._repaint_selected_detail()
        self.copy_rendered_text("session details", self.query_one(SessionDetailPanel))

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
        pane_id, stream_adapter = self._resolve_live_mirror_target(target)
        if stream_adapter is None:
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
                pane_id=pane_id,
                display_name=display_name,
                show_editor=False,
                stream_adapter=stream_adapter,
            )
        )

    def _selected_live_target(self) -> _LiveSessionTarget | None:
        if self._selected_session_id is None:
            return None
        return self._live_targets.get(self._selected_session_id)

    def _resolve_live_mirror_target(
        self,
        target: _LiveSessionTarget,
    ) -> tuple[str, PaneStreamAdapter | None]:
        stream_adapter = self.runtime.pane_stream
        resolver = self.runtime.session_resolver
        if resolver is None or target.pane_pid is None:
            return target.pane_id, stream_adapter
        resolved = resolver.resolve_target_for_pid(target.pane_pid)
        if resolved is None or resolved.pane_id is None:
            return target.pane_id, stream_adapter
        if resolved.socket_path is None:
            return resolved.pane_id, stream_adapter
        nested_stream = self._stream_adapter_for_socket(resolved.socket_path)
        if nested_stream is None:
            return target.pane_id, stream_adapter
        return resolved.pane_id, nested_stream

    def _stream_adapter_for_socket(self, socket_path: Path) -> PaneStreamAdapter | None:
        tmux = self.runtime.tmux
        if tmux is None:
            return None
        return PaneStreamAdapter(tmux=tmux.with_socket_path(socket_path))


__all__ = ["SessionsScreen"]
