"""Sessions browser screen — discover, inspect, and resume Copilot CLI sessions."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.timer import Timer
from textual.widgets import Input
from textual.worker import Worker, WorkerState

from muxdeck.adapters.pane_stream import PaneStreamAdapter
from muxdeck.bindings import SESSIONS_BINDINGS, SESSIONS_HINTS
from muxdeck.domain.enums import AgentStatus
from muxdeck.screens.base import ShellScreen
from muxdeck.screens.compose_mirror import ComposeWithMirrorScreen
from muxdeck.screens.confirm_dialog import ConfirmScreen
from muxdeck.screens.session_maintenance import SessionMaintenanceScreen
from muxdeck.widgets.sessions import (
    SessionActionBar,
    SessionDetailPanel,
    SessionListPanel,
    SessionSelected,
    SessionSummaryBar,
)

if TYPE_CHECKING:
    from muxdeck.app import MuxdeckApp, MuxdeckRuntime
    from muxdeck.controllers.sessions_controller import (
        BulkDeleteResult,
        SessionDetailView,
        SessionsState,
    )


_WORKER_NAME = "sessions_load"
_BULK_DELETE_WORKER_NAME = "sessions_bulk_delete"
_SINGLE_DELETE_WORKER_NAME = "sessions_delete"

# Terminal agent statuses are excluded from the SESSIONS screen's
# "live" set. The dashboard already filters on the same pair (see
# ``controllers/dashboard_controller.py``); using one shared
# definition keeps the two views consistent. Without this filter a
# session whose Copilot CLI process exited (and was correctly marked
# DEAD/COMPLETED by the synchronizer after dead_grace_period_sec)
# would stay pinned to the green "active" status in the SESSIONS list
# forever, because the agent record carrying the original
# copilot_session_id is never deleted from SQLite.
_TERMINAL_AGENT_STATUSES: frozenset[AgentStatus] = frozenset(
    {AgentStatus.DEAD, AgentStatus.COMPLETED}
)

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


def _derive_seed_name(
    *,
    repo_root: str | None,
    cwd: str,
    origin: str,
) -> str:
    """Pick the agent ``name`` for a freshly seeded resumed-session row.

    Mirrors ``monitoring_service._derive_agent_name`` (repo basename
    over cwd basename) but also handles Windows-style paths because
    the seed flows carry ``C:\\Users\\...`` strings verbatim for
    ``windows`` origin sessions. ``PurePosixPath("C:\\foo\\bar").name``
    returns ``"C:\\foo\\bar"`` (no slashes), which would leak the
    whole path into the dashboard, so split on the appropriate
    separator. Falls back to ``"copilot"`` only when nothing else
    resolves — that should not happen in practice because the caller
    already validates ``cwd`` is non-empty.
    """
    candidates = (repo_root, cwd)
    use_backslash = origin == "windows"
    for candidate in candidates:
        if not candidate:
            continue
        # Strip any trailing separator so basename works for
        # ``C:\foo\`` and ``/foo/`` alike.
        if use_backslash:
            stripped = candidate.rstrip("\\/")
            tail = stripped.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        else:
            stripped = candidate.rstrip("/")
            tail = stripped.rsplit("/", 1)[-1]
        if tail and tail not in {".", ".."}:
            return tail
    return "copilot"


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
        # Action-result message that should persist past the next
        # refresh worker. Without this, ``_apply_state`` would
        # overwrite "✓ deleted 3" with the default "X sessions · Y
        # active" line as soon as the worker finishes.
        self._pending_status_after_refresh: str | None = None
        # In-flight single-delete bookkeeping. Caches the target label
        # so the worker's completion handler can still reference the
        # original selection even after focus has moved on.
        self._pending_delete_target: tuple[str, str] | None = None
        # Activation-refresh throttle. Switching tabs back to this
        # screen would otherwise trigger a full discover() rescan
        # (slow on WSL / Windows-mounted roots) every time, even when
        # the data is seconds-fresh. Keep the previous result if the
        # last successful refresh was less than ``_ACTIVATION_REFRESH_TTL``
        # seconds ago so quick j/k-style tab hopping is fluid; periodic
        # syncs and explicit user actions bypass this gate.
        self._last_refresh_completed_at: float = 0.0

    _ACTIVATION_REFRESH_TTL_SEC: float = 3.0

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

    def restore_default_focus(self) -> None:
        self.query_one(SessionListPanel).focus_list()

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
        # Skip if we just refreshed -- prevents the visible spinner
        # flash when the operator flips between tabs every few hundred
        # ms. Periodic sync still drives fresh data via refresh_data().
        elapsed = time.monotonic() - self._last_refresh_completed_at
        if elapsed < self._ACTIVATION_REFRESH_TTL_SEC:
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
        # Capture the resolver locally so the worker thread doesn't
        # touch the runtime later (and so the explicit None-check is
        # next to the call site below).
        session_resolver = self.runtime.session_resolver
        # Capture the session store too: the incremental-load fast
        # pass below scans only the local root so the screen paints
        # Linux sessions in <100 ms even on a cold start, while the
        # full discover() (which walks the slow Windows-mounted root
        # too) continues in this same worker and lands a second paint
        # with everything folded in. We only do this on first load --
        # subsequent refreshes already have a populated screen, so
        # the visible flicker of two repaints isn't worth it.
        session_store = getattr(self.runtime, "copilot_session_store", None)
        do_partial_paint = first_load and session_store is not None
        screen = self

        def _load() -> _LoadedSessionsState | None:
            # Live agent ids correlate running tmux panes with session
            # files on disk. Use the dedicated thread-safe SQLite store
            # when available because this function runs in a worker thread.
            #
            # Skip terminal-state agents (DEAD/COMPLETED) so a session
            # whose Copilot CLI process has exited stops claiming
            # "active" status. The synchronizer marks an agent
            # terminal once the backing tmux pane has been gone for
            # ``dead_grace_period_sec`` (default 10 s); by the next
            # sync cycle the SESSIONS screen sees the terminal status
            # here and demotes the row to "completed"/"unclosed"
            # based on whether ``session.shutdown`` was emitted.
            live_ids: set[str] = set()
            live_targets: dict[str, _LiveSessionTarget] = {}
            agents = live_store.list_agents()
            for agent in agents:
                if getattr(agent, "status", None) in _TERMINAL_AGENT_STATUSES:
                    continue
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
            # Agent records only exist for sessions whose Copilot CLI
            # process is hosted inside a WSL tmux pane that muxdeck
            # tracks. Sessions launched from pwsh.exe / cmd.exe on the
            # Windows side never get an agent row, so the agent-derived
            # set above misses them entirely and they show as
            # "completed"/"unclosed" even while actively running. Fold
            # in any session id that has a live ``inuse.*.lock`` file
            # under any configured root — for the primary Linux root
            # the resolver still validates pids via ``/proc``, for the
            # Windows mount it falls back to a lock-mtime freshness
            # check.
            if session_resolver is not None:
                live_ids.update(session_resolver.live_session_ids())
            frozen_live_ids = frozenset(live_ids)

            if do_partial_paint and session_store is not None:
                local_only = session_store.scan_local_only()
                partial_state = sessions_ctrl.build_state(
                    live_session_ids=frozen_live_ids,
                    selected_session_id=selected_id,
                    filter_text=filter_text,
                    show_completed=show_completed,
                    sessions=local_only,
                )
                partial_loaded = _LoadedSessionsState(
                    state=partial_state,
                    live_session_ids=frozen_live_ids,
                    live_targets=live_targets,
                )
                screen.app.call_from_thread(screen._apply_partial_load, partial_loaded)

            state = sessions_ctrl.build_state(
                live_session_ids=frozen_live_ids,
                selected_session_id=selected_id,
                filter_text=filter_text,
                show_completed=show_completed,
            )
            return _LoadedSessionsState(
                state=state,
                live_session_ids=frozen_live_ids,
                live_targets=live_targets,
            )

        self._loading = True
        self.run_worker(_load, thread=True, exclusive=True, name=_WORKER_NAME)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        super().on_worker_state_changed(event)
        name = event.worker.name
        if name == _BULK_DELETE_WORKER_NAME:
            if event.state == WorkerState.ERROR:
                err = event.worker.error
                self.set_status(f"✗ bulk delete failed: {err}" if err else "✗ bulk delete failed")
                return
            if event.state == WorkerState.CANCELLED:
                self.set_status("bulk delete cancelled")
                return
            if event.state != WorkerState.SUCCESS:
                return
            result = cast("BulkDeleteResult | None", event.worker.result)
            self._on_bulk_delete_complete(result)
            return
        if name == _SINGLE_DELETE_WORKER_NAME:
            if event.state == WorkerState.ERROR:
                err = event.worker.error
                self._pending_delete_target = None
                self.set_status(f"✗ delete failed: {err}" if err else "✗ delete failed")
                return
            if event.state == WorkerState.CANCELLED:
                self._pending_delete_target = None
                self.set_status("delete cancelled")
                return
            if event.state != WorkerState.SUCCESS:
                return
            payload = cast(
                "tuple[str, str, BaseException | None] | None",
                event.worker.result,
            )
            self._on_single_delete_complete(payload)
            return
        if name != _WORKER_NAME:
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
        self._last_refresh_completed_at = time.monotonic()
        self._schedule_pending_refresh()

    def _schedule_pending_refresh(self) -> None:
        if not self._refresh_pending:
            return
        self._refresh_pending = False
        if self.is_mounted:
            self.call_after_refresh(self.refresh_data)

    def _apply_partial_load(self, loaded: _LoadedSessionsState) -> None:
        """Paint a partial first-load result without finishing the load.

        The ``_load`` worker calls this via ``call_from_thread`` after
        scanning only the local root so the screen renders Linux
        sessions in the first ~50 ms even when the full discover()
        has to walk the slow Windows-mounted root afterwards.

        Called on the UI thread. ``self._loading`` stays ``True``
        because the worker is still running -- the SUCCESS handler
        will finalize and end_loading once the full state lands. We
        end the loading mask here so the partial paint is visible;
        the SUCCESS handler's ``end_loading`` call is idempotent.
        """
        if not self.is_mounted:
            return
        try:
            list_panel = self.query_one(SessionListPanel)
            detail_panel = self.query_one(SessionDetailPanel)
        except NoMatches:
            return
        self.end_loading(list_panel, detail_panel)
        self._live_session_ids = loaded.live_session_ids
        self._live_targets = loaded.live_targets
        self._apply_state(loaded.state)

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
        # Action callbacks (delete, bulk maintenance) frequently set
        # an outcome message right before triggering a refresh that
        # would otherwise overwrite it with the default summary line.
        # Honor the pending message if one is queued, then clear it.
        if self._pending_status_after_refresh is not None:
            self.set_status(self._pending_status_after_refresh)
            self._pending_status_after_refresh = None
            return
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
            # Seed the agent record with attribution derived from the
            # session metadata, not the pane's WSL cwd. For ``windows``
            # origin sessions copilot.exe runs inside pwsh.exe on the
            # Windows side, so monitoring would derive ``cwd=muxdeck``
            # / ``repo_root=muxdeck`` from the WSL pane. Pinning here
            # makes the dashboard show the real Windows repo/branch on
            # the very next refresh; for local sessions the pin still
            # surfaces ``copilot_session_id`` immediately so the
            # SESSIONS row flips to "active" without waiting for the
            # resolver to catch the new ``inuse.<pid>.lock``.
            self._seed_resumed_agent(detail, result)
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

    def _seed_resumed_agent(
        self,
        detail: SessionDetailView,
        result: object,
    ) -> None:
        """Pre-populate an agent record so the dashboard shows the right repo.

        Called immediately after ``actions.resume_session`` succeeds.
        Skips silently if the runtime cannot persist (no ``agents``
        controller wired, missing tmux metadata in the result, etc.):
        worst case the next monitoring sync writes a less precise row
        — same behavior as before the seed existed.
        """
        agents = getattr(self.runtime, "agents", None)
        seed = getattr(agents, "seed_resumed_session", None)
        if not callable(seed):
            return
        pane_meta = getattr(result, "pane_meta", None)
        if pane_meta is None:
            return
        cwd = self._seed_cwd_for(detail)
        if cwd is None:
            return
        repo_root = self._seed_repo_root_for(detail)
        branch = detail.branch if detail.branch and detail.branch != "—" else None
        worktree_path = repo_root or cwd
        name = _derive_seed_name(repo_root=repo_root, cwd=cwd, origin=detail.origin)
        task_title = detail.summary if detail.summary else None
        try:
            seed(
                copilot_session_id=detail.session_id,
                tmux_pane_id=pane_meta.pane_id,
                tmux_session_name=pane_meta.session_name or "",
                tmux_window_id=pane_meta.window_id or "",
                tmux_window_name=pane_meta.window_name,
                pane_tty=pane_meta.pane_tty,
                pane_pid=pane_meta.pane_pid,
                cwd=cwd,
                repo_root=repo_root,
                worktree_path=worktree_path,
                branch=branch,
                name=name,
                task_title=task_title,
            )
        except Exception:
            # If persistence fails for any reason, the user still gets
            # the resumed session — just without the pinned metadata.
            # The next monitoring sync will create the row from pane
            # snapshots (which may be wrong for Windows origin, but
            # that matches pre-seed behavior).
            return

    @staticmethod
    def _seed_cwd_for(detail: SessionDetailView) -> str | None:
        """Pick the cwd to pin on the seeded agent record.

        For ``windows`` origin, ``detail.windows_cwd`` is the verbatim
        ``C:\\...`` path the Copilot CLI actually ran in. For ``local``
        origin, ``detail.cwd`` already collapses ``windows_cwd or
        str(raw.cwd)`` so it is the right WSL path. Returns ``None``
        when neither source resolves to a usable string — without a
        cwd the Agent dataclass would refuse to validate.
        """
        if detail.origin == "windows" and detail.windows_cwd:
            return detail.windows_cwd
        if detail.cwd and detail.cwd != "—":
            return detail.cwd
        return None

    @staticmethod
    def _seed_repo_root_for(detail: SessionDetailView) -> str | None:
        if detail.git_root and detail.git_root != "—":
            return detail.git_root
        return None

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

    # ── delete / maintenance ─────────────────────────────────────────

    def action_delete_session(self) -> None:
        """Delete the selected session after a confirm prompt."""
        detail = self._selected_detail
        selected = self._selected_session_id
        if detail is None or selected is None:
            self.set_status("no session selected")
            return
        if self.runtime.sessions_ctrl is None:
            self.set_status("✗ session controller unavailable")
            return
        if selected in self._live_session_ids:
            self.set_status("✗ session is live — close its pane before deleting")
            return
        label = self._delete_label_for(detail)
        message = (
            f"Delete session {label}?\n\n"
            "This permanently removes the on-disk state for this session. "
            "Replay history and any in-progress conversation will be lost."
        )

        def _on_close(confirmed: bool | None) -> None:
            if confirmed:
                self._delete_session_now(selected, label)

        self.app.push_screen(
            ConfirmScreen(message, title="Delete session"),
            _on_close,
        )

    @staticmethod
    def _delete_label_for(detail: SessionDetailView) -> str:
        summary = detail.summary if detail.summary and detail.summary != "—" else None
        if summary:
            return f"{summary} ({detail.session_id[:8]})"
        return detail.session_id

    def _delete_session_now(self, session_id: str, label: str) -> None:
        sessions_ctrl = self.runtime.sessions_ctrl
        if sessions_ctrl is None:
            self.set_status("✗ session controller unavailable")
            return
        # Cheap pre-flight so the worker thread doesn't even start for
        # a live session -- this also lets us surface the live-session
        # PermissionError synchronously with the existing UX wording.
        if session_id in self._live_session_ids:
            self.set_status("✗ session is live; stop the agent before deleting")
            return
        # Cache the target so the worker completion handler can surface
        # a meaningful success/failure message even if the operator
        # has since moved the selection.
        self._pending_delete_target = (session_id, label)
        self.set_status(f"deleting {label}…")

        store = sessions_ctrl  # captured for the worker closure

        def _delete() -> tuple[str, str, BaseException | None]:
            try:
                store.delete_session(
                    session_id,
                    live_session_ids=self._live_session_ids,
                )
            except BaseException as exc:
                return session_id, label, exc
            return session_id, label, None

        self.run_worker(
            _delete,
            thread=True,
            exclusive=False,
            name=_SINGLE_DELETE_WORKER_NAME,
        )

    def _on_single_delete_complete(
        self,
        payload: tuple[str, str, BaseException | None] | None,
    ) -> None:
        self._pending_delete_target = None
        if payload is None:
            self.set_status("✗ delete failed: worker returned no result")
            return
        session_id, label, exc = payload
        if isinstance(exc, PermissionError):
            self.set_status(f"✗ {exc}")
            return
        if isinstance(exc, OSError):
            self.set_status(f"✗ delete failed: {exc}")
            return
        if exc is not None:
            self.set_status(f"✗ delete failed: {exc}")
            return
        if self._selected_session_id == session_id:
            self._selected_session_id = None
            self._selected_detail = None
            self._rendered_detail_session_id = None
            self.muxdeck_app.selected_session_id = None
        message = f"✓ deleted {label}"
        self.set_status(message)
        self._pending_status_after_refresh = message
        self.refresh_data()

    def action_session_maintenance(self) -> None:
        """Open the bulk-delete cohort picker."""
        sessions_ctrl = self.runtime.sessions_ctrl
        if sessions_ctrl is None:
            self.set_status("✗ session controller unavailable")
            return
        view = sessions_ctrl.maintenance_cohorts(
            live_session_ids=self._live_session_ids,
        )
        if view.total_eligible == 0:
            self.set_status("nothing to clean up")
            return

        def _on_close(days: int | None) -> None:
            if days is None:
                return
            self._confirm_bulk_delete(days)

        self.app.push_screen(SessionMaintenanceScreen(view), _on_close)

    def _confirm_bulk_delete(self, days: int) -> None:
        sessions_ctrl = self.runtime.sessions_ctrl
        if sessions_ctrl is None:
            return
        # Re-snapshot cohorts so the displayed count matches what's about
        # to be removed even if the screen was open for a while; the
        # `live_session_ids` may also have grown in the meantime.
        view = sessions_ctrl.maintenance_cohorts(
            live_session_ids=self._live_session_ids,
        )
        cohort = next(
            (c for c in view.cohorts if c.older_than_days == days),
            None,
        )
        if cohort is None or cohort.count == 0:
            self.set_status("no sessions in that cohort")
            return
        message = (
            f"Delete {cohort.count} session(s) {cohort.label.lower()}?\n\n"
            "This permanently removes their on-disk state. Live sessions are "
            "always preserved."
        )

        def _on_close(confirmed: bool | None) -> None:
            if confirmed:
                self._run_bulk_delete(days)

        self.app.push_screen(
            ConfirmScreen(message, title="Bulk delete sessions"),
            _on_close,
        )

    def _run_bulk_delete(self, days: int) -> None:
        sessions_ctrl = self.runtime.sessions_ctrl
        if sessions_ctrl is None:
            return
        live_ids = self._live_session_ids
        # No pre-count here -- maintenance_cohorts() is a slow discover()
        # walk that the confirmation flow already paid for, so we trust
        # the worker's progress_callback to surface the total as soon
        # as the first id has been attempted.
        self.set_status("deleting sessions…")
        controller = sessions_ctrl
        screen = self

        def _on_progress(deleted: int, failed: int, total_attempted: int) -> None:
            done = deleted + failed
            text = f"deleting {done}/{total_attempted} sessions…"
            screen.app.call_from_thread(screen.set_status, text)

        def _delete() -> BulkDeleteResult:
            return controller.bulk_delete_older_than(
                days,
                live_session_ids=live_ids,
                progress_callback=_on_progress,
            )

        self.run_worker(
            _delete,
            thread=True,
            exclusive=False,
            name=_BULK_DELETE_WORKER_NAME,
        )

    def _on_bulk_delete_complete(self, result: BulkDeleteResult | None) -> None:
        if result is None:
            self.set_status("✗ bulk delete failed: worker returned no result")
            return
        deleted = len(result.deleted_ids)
        failed = len(result.failures)
        skipped = len(result.skipped_live)
        parts = [f"✓ deleted {deleted}"]
        if failed:
            parts.append(f"✗ {failed} failed")
        if skipped:
            parts.append(f"skipped {skipped} live")
        message = " · ".join(parts)
        self.set_status(message)
        self._pending_status_after_refresh = message
        if (
            self._selected_session_id is not None
            and self._selected_session_id in result.deleted_ids
        ):
            self._selected_session_id = None
            self._selected_detail = None
            self._rendered_detail_session_id = None
            self.muxdeck_app.selected_session_id = None
        self.refresh_data()

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
