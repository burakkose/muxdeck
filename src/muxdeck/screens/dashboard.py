from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.timer import Timer
from textual.widgets import Input
from textual.worker import Worker, WorkerState

from muxdeck.adapters.pane_stream import PaneStreamAdapter
from muxdeck.bindings import DASHBOARD_BINDINGS, DASHBOARD_HINTS
from muxdeck.controllers import (
    AgentIntentView,
    DashboardAgentListItemView,
    DashboardFilterState,
    DashboardLogLineView,
    DashboardSelectedAgentView,
    DashboardSort,
    DashboardSortField,
    DashboardState,
    DashboardSubAgentTreeView,
)
from muxdeck.screens.base import ShellScreen
from muxdeck.screens.compose_mirror import ComposeWithMirrorScreen
from muxdeck.screens.confirm_dialog import ConfirmScreen
from muxdeck.screens.message_input import MessageResult, SendMessageScreen
from muxdeck.screens.window_input import (
    MoveWindowResult,
    MoveWindowScreen,
    RenameWindowResult,
    RenameWindowScreen,
)
from muxdeck.services.attention_service import AttentionNotification
from muxdeck.services.runtime_service import RuntimeSyncReport
from muxdeck.widgets.dashboard import (
    AgentDetailPanel,
    AgentListPanel,
    AlertPanel,
    FilterBar,
    LogPreviewPanel,
    StatusBar,
)

if TYPE_CHECKING:
    from muxdeck.app import MuxdeckApp, MuxdeckRuntime


_SORT_ORDER: tuple[DashboardSortField, ...] = (
    "last_seen",
    "name",
    "status",
    "cost",
    "idle_seconds",
    "started_at",
)

_DASHBOARD_WORKER = "dashboard_load"
_DASHBOARD_DETAIL_WORKER = "dashboard_detail"
_DASHBOARD_LIVE_TAIL_WORKER = "dashboard_live_tail"
_DASHBOARD_LIVE_TAIL_INTERVAL_SEC = 1.0
_DASHBOARD_LIVE_TAIL_CAPTURE_LINES = 200

_NOTIFY_SEVERITY: dict[str, Literal["information", "warning", "error"]] = {
    "info": "information",
    "warning": "warning",
    "error": "error",
}


class DashboardScreen(ShellScreen):
    SCREEN_TITLE = "DASHBOARD"
    BINDINGS = DASHBOARD_BINDINGS
    FOOTER_HINTS = DASHBOARD_HINTS

    def __init__(self, runtime: MuxdeckRuntime) -> None:
        super().__init__(runtime)
        self._filters = DashboardFilterState(include_completed=False)
        self._sort = DashboardSort()
        self._selected_agent_id: str | None = None
        self._state: DashboardState | None = None
        self._detail_timer: Timer | None = None
        self._filter_timer: Timer | None = None
        # Monotonic token bumped on every selection change AND every
        # ``_apply_state``. The detail worker captures the token at
        # kickoff and drops its result if the token has advanced —
        # that way a slow detail load can never overwrite a fresher
        # selection or a fresher ``_state``.
        self._detail_request_token: int = 0
        # Monotonic token bumped on every ``_apply_state``. The local
        # ``_DASHBOARD_WORKER`` captures it at kickoff and drops its
        # result if a fresher state has already been painted (e.g. the
        # app sync worker delivered a post-refresh build while the local
        # pre-refresh build was still running). Prevents the cold-start
        # "instant paint from store" path from clobbering the first
        # post-sync paint.
        self._state_apply_seq: int = 0
        self._loading: bool = False
        # Textual fires ``on_mount`` followed immediately by ``on_show``
        # on first activation. Without this guard the screen does
        # ``build_state`` twice back-to-back on every cold open and any
        # dashboard refresh that is in flight piles up.
        self._skip_next_show_refresh: bool = True
        # Live-tail state for the "Selected agent · output" panel.
        # Without this loop the panel only refreshes when the discovery
        # service writes a new ``log_chunks`` row (every >=2s, plus a
        # content-dedup that freezes the panel when nothing changed).
        # The tail capture runs in a worker thread so the subprocess
        # round-trip never blocks the UI event loop.
        self._live_tail_timer: Timer | None = None
        self._live_tail_token: int = 0
        self._live_tail_agent_id: str | None = None
        self._live_tail_pane_id: str | None = None
        self._live_tail_stream: PaneStreamAdapter | None = None
        self._live_tail_lines: dict[str, tuple[DashboardLogLineView, ...]] = {}
        self._live_tail_sequence: int = 0

    @property
    def current_filters(self) -> DashboardFilterState:
        return self._filters

    @property
    def current_sort(self) -> DashboardSort:
        return self._sort

    @property
    def current_selected_agent_id(self) -> str | None:
        return self._selected_agent_id

    def compose_body(self) -> ComposeResult:
        with Vertical(id="dashboard-root"):
            yield StatusBar(id="dashboard-status-bar")
            yield FilterBar(id="dashboard-filter-row")
            with Horizontal(id="dashboard-main", classes="frame"):
                yield AgentListPanel(
                    widget_id="dashboard-agents", classes="divider-right focusable"
                )
                with Vertical(id="dashboard-sidebar"):
                    yield AgentDetailPanel(
                        id="dashboard-detail",
                        classes="section",
                    )
                    # Attention sits ABOVE output so the operator sees
                    # "what needs my action right now" before they read
                    # any logs. Previously the alert panel was buried
                    # below the dominant output panel and operators
                    # reported missing stale-agent warnings entirely.
                    yield AlertPanel(id="dashboard-alerts", classes="section")
                    yield LogPreviewPanel(id="dashboard-log", classes="section")

    def on_mount(self) -> None:
        self.refresh_data()
        self.call_after_refresh(self.query_one(AgentListPanel).focus_list)

    def on_show(self) -> None:
        if self._skip_next_show_refresh:
            self._skip_next_show_refresh = False
            return
        self.refresh_data()
        if self._selected_agent_id is not None and self._live_tail_timer is None:
            self._start_live_tail(self._selected_agent_id)

    def on_screen_resume(self) -> None:
        # Textual switches between modes by hiding instead of unmounting,
        # so ``set_interval`` timers from the previous activation keep
        # firing in the background. Stop the tail in ``on_screen_suspend``
        # and restart it here to avoid wasting subprocess calls on a
        # dashboard the operator is no longer looking at.
        if self._selected_agent_id is not None and self._live_tail_timer is None:
            self._start_live_tail(self._selected_agent_id)

    def on_screen_suspend(self) -> None:
        self._stop_live_tail()

    def on_unmount(self) -> None:
        self._stop_live_tail()

    def refresh_data(self) -> None:
        sync_report = self.muxdeck_app.last_sync_report
        # Prefer pre-built state from the app sync worker (off main thread).
        pre_built = self.muxdeck_app.last_dashboard_state
        if pre_built is not None:
            self.muxdeck_app.last_dashboard_state = None
            self._apply_state(pre_built, sync_report)
            return

        loading_widgets = (
            self.query_one(AgentListPanel),
            self.query_one(AgentDetailPanel),
            self.query_one(LogPreviewPanel),
            self.query_one(AlertPanel),
        )
        first_load = self._state is None
        # Cold open: when the synchronizer is configured but hasn't
        # delivered a sync result yet, the SQLite store may still hold
        # last-session agents whose status (active/working/idle) no
        # longer reflects the running tmux fleet. The previous
        # behaviour kicked off a local build anyway and painted that
        # stale state for ~1 second before the sync result arrived,
        # which made the dashboard show wrong status on first paint.
        # Defer the local build so the loading overlay stays up until
        # the sync worker delivers ``last_dashboard_state`` via
        # ``_refresh_screen_widgets``. The synchronizer always runs
        # ``call_after_refresh`` from app.on_mount, so we know it's
        # already in flight by the time we get here.
        #
        # ``sync_attempted`` flips True after the first sync finishes
        # (success or failure). If the sync errored we still fall
        # back to the local build so the dashboard isn't stuck on
        # "syncing fleet…" forever.
        synchronizer_pending = (
            first_load
            and not self.muxdeck_app.sync_attempted
            and getattr(self.runtime, "synchronizer", None) is not None
        )
        if first_load:
            self.set_status("syncing fleet…" if synchronizer_pending else "loading dashboard…")
            self.begin_loading(*loading_widgets)
        if synchronizer_pending:
            return

        # Snapshot inputs so the worker doesn't read mutable UI state.
        # ``sync_dashboard`` uses the thread-safe SQLite connection;
        # the foreground ``runtime.dashboard`` is bound to the UI thread
        # and would raise from a worker.
        dashboard = getattr(self.runtime, "sync_dashboard", None) or self.runtime.dashboard
        filters = self._filters
        sort = self._sort
        selected_id = self._selected_agent_id
        preview_lines = self._preview_line_limit()
        # Capture the current state-apply sequence. If a fresher state
        # is painted (e.g. the app sync worker delivers post-refresh
        # data) before this worker finishes, ``on_worker_state_changed``
        # will see the seq has advanced and drop the stale result.
        kickoff_seq = self._state_apply_seq

        def _build() -> tuple[int, DashboardState] | None:
            try:
                state = dashboard.build_state(
                    filters=filters,
                    sort=sort,
                    selected_agent_id=selected_id,
                    preview_line_limit=preview_lines,
                )
                return (kickoff_seq, state)
            except Exception:
                return None

        self.run_worker(_build, thread=True, exclusive=True, name=_DASHBOARD_WORKER)

    def _apply_state(
        self,
        state: DashboardState,
        sync_report: RuntimeSyncReport | None,
    ) -> None:
        loading_widgets = (
            self.query_one(AgentListPanel),
            self.query_one(AgentDetailPanel),
            self.query_one(LogPreviewPanel),
            self.query_one(AlertPanel),
        )
        self._state = state
        self._loading = False
        # Bump both tokens so any in-flight workers (detail load, local
        # pre-sync state build) drop their results instead of
        # overwriting the freshly-applied state.
        self._detail_request_token += 1
        self._state_apply_seq += 1
        self.end_loading(*loading_widgets)
        # Preserve the live selection across async refreshes.
        #
        # The sync worker captures ``_selected_agent_id`` at kickoff and
        # bakes it into ``state.selected_agent_id``. If the user pressed
        # j/k while the sync was in flight, that value is already stale;
        # blindly assigning it back here causes the visible cursor to
        # snap "backward" onto the old row. Keep whatever the user just
        # navigated to and only adopt the state's selection when we
        # don't have one yet (e.g. first load, or the previous selection
        # no longer exists).
        agent_ids = {a.agent_id for a in self._state.agents}
        if self._selected_agent_id is None or self._selected_agent_id not in agent_ids:
            self._selected_agent_id = self._state.selected_agent_id
        effective_selected = self._selected_agent_id
        if effective_selected is not None:
            self.muxdeck_app.remember_agent_selection(effective_selected)
        selected_item = next(
            (agent for agent in self._state.agents if agent.agent_id == effective_selected),
            None,
        )
        self.query_one(StatusBar).set_state(self._state.health, self._state.metrics, selected_item)
        filter_bar = self.query_one(FilterBar)
        filter_bar.set_query(self._filters.text_query)
        filter_bar.set_state(
            filter_text=self._filters.text_query,
            visible_agents=len(self._state.agents),
            total_agents=self._state.health.total_agents,
            attention_only=self._filters.attention_only,
            include_completed=self._filters.include_completed,
            sort_label=self._sort.field,
        )
        self.query_one(AgentListPanel).set_agents(
            self._state.agents,
            selected_agent_id=effective_selected,
        )
        # If the live selection drifted from what the worker built, the
        # cached ``selected_agent`` view is for the wrong agent. Rebuild
        # the detail panels from the (fast) single-agent path so the
        # sidebar stays consistent with the highlighted row.
        if effective_selected is not None and self._state.selected_agent_id != effective_selected:
            self._update_selected_detail()
        else:
            panel = self.query_one(AgentListPanel)
            if panel.selected_subagent is not None:
                self.query_one(AgentDetailPanel).set_subagent(panel.selected_subagent)
            else:
                self.query_one(AgentDetailPanel).set_agent(self._state.selected_agent)
            self.query_one(LogPreviewPanel).set_logs(
                self._with_live_preview(self._state.selected_agent),
            )
        # Cold-start: when the dashboard first paints with a remembered
        # selection (or the worker picked one for us), no AgentSelected
        # message will ever fire — start the live tail here so the
        # output panel updates without waiting for the operator to move
        # the cursor.
        if effective_selected is not None and self._live_tail_agent_id != effective_selected:
            self._start_live_tail(effective_selected)
        elif effective_selected is None:
            self._stop_live_tail()
        self.query_one(AlertPanel).set_alerts(self._state.alerts)
        attention_controller = getattr(self.runtime, "attention", None)
        if attention_controller is not None:
            self._emit_notifications(attention_controller.observe_dashboard_state(self._state))
        if sync_report is None:
            self.set_status(f"{len(self._state.agents)} agents · {self._state.health.message}")
            return
        if sync_report.error is not None:
            self.set_status(sync_report.error)
            return
        parts = [f"{len(self._state.agents)} visible"]
        if self._filters.attention_only:
            parts.append("attn-only")
        if not self._filters.include_completed:
            parts.append("hide-done")
        query = (self._filters.text_query or "").strip()
        if query:
            parts.append(f"filter:{query}")
        parts.append(f"sort:{self._sort.field}")
        self.set_status(" · ".join(parts))

    @property
    def muxdeck_app(self) -> MuxdeckApp:
        return cast("MuxdeckApp", self.app)

    def on_agent_list_panel_agent_selected(
        self,
        message: AgentListPanel.AgentSelected,
    ) -> None:
        if message.agent_id == self._selected_agent_id:
            return
        self._selected_agent_id = message.agent_id
        self.muxdeck_app.remember_agent_selection(self._selected_agent_id)
        if self._state is not None:
            self.query_one(StatusBar).set_state(
                self._state.health,
                self._state.metrics,
                self._find_selected_agent(),
            )
        # Bumping the token here invalidates any in-flight detail load
        # for the previous selection so its result is dropped instead
        # of clobbering the freshly highlighted row.
        self._detail_request_token += 1
        # Debounce: cancel any pending detail load and schedule a new one.
        # Coalesces rapid j/k presses into a single worker dispatch
        # instead of stacking SQLite + JSONL loads per keystroke.
        if self._detail_timer is not None:
            self._detail_timer.stop()
        self._detail_timer = self.set_timer(0.1, self._schedule_selected_detail_worker)
        # Restart the live-tail loop on the freshly selected pane. The
        # capture itself runs in a worker thread, so this is cheap.
        self._start_live_tail(self._selected_agent_id)

    def on_agent_list_panel_sub_agent_highlighted(
        self,
        message: AgentListPanel.SubAgentHighlighted,
    ) -> None:
        # Fast path: rendering a sub-agent needs no DB work — the view
        # carries prompt/result/type already. We still want the parent
        # agent's detail in the cache for when the cursor moves back up,
        # so we don't cancel the debounced `_update_selected_detail`.
        detail_panel = self.query_one(AgentDetailPanel)
        if message.subagent is None:
            # Reset to the parent agent's detail (if we already have it).
            if self._state is not None and self._state.selected_agent is not None:
                detail_panel.set_agent(self._state.selected_agent)
            return
        detail_panel.set_subagent(message.subagent)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "dashboard-filter-input":
            return
        self._filters = DashboardFilterState(
            statuses=self._filters.statuses,
            attention_only=self._filters.attention_only,
            text_query=event.value,
            include_completed=self._filters.include_completed,
        )
        # Debounce: each keystroke would otherwise kick off a worker
        # that re-runs ``build_state`` (per-agent SQL queries + JSONL
        # reads). Coalesce typing bursts into a single rebuild after
        # the user pauses.
        if self._filter_timer is not None:
            self._filter_timer.stop()
        self._filter_timer = self.set_timer(0.2, self.refresh_data)

    def action_cursor_down(self) -> None:
        self.query_one(AgentListPanel).move_cursor(1)

    def action_cursor_up(self) -> None:
        self.query_one(AgentListPanel).move_cursor(-1)

    def action_toggle_expand(self) -> None:
        """Expand or collapse the selected agent's sub-agent tree.

        The widget tracks expansion state; the screen's only job is
        to react to an ``ExpandRequested`` message by loading the
        tree on a worker and feeding it back. That keeps the keystroke
        itself cheap (no DB or filesystem work on the UI thread).
        """
        self.query_one(AgentListPanel).toggle_expand()

    def on_agent_list_panel_expand_requested(
        self,
        message: AgentListPanel.ExpandRequested,
    ) -> None:
        agent_id = message.agent_id
        # Exclusive per agent id so rapid expand/collapse/expand of the
        # same row doesn't start overlapping loads.
        self.run_worker(
            lambda agent_id=agent_id: self._load_subagents_sync(agent_id),
            thread=True,
            exclusive=True,
            name=f"subagents:{agent_id}",
        )

    def _load_subagents_sync(self, agent_id: str) -> DashboardSubAgentTreeView:
        # This runs on a Textual worker thread, so we must use the
        # thread-safe sqlite connection (``sync_dashboard``). The
        # default ``runtime.dashboard`` is bound to a connection that
        # lives on the main thread and will raise
        # ``sqlite3.ProgrammingError`` when touched from here.
        dashboard = getattr(self.runtime, "sync_dashboard", None) or self.runtime.dashboard
        tree = dashboard.load_subagents(agent_id)
        # Hop back to the main thread to mutate the widget.
        self.app.call_from_thread(self._apply_subagents, agent_id, tree)
        return tree

    def _apply_subagents(self, agent_id: str, tree: DashboardSubAgentTreeView) -> None:
        try:
            panel = self.query_one(AgentListPanel)
        except Exception:
            return
        panel.set_subagents(agent_id, tree)

    def action_focus_filter(self) -> None:
        self.query_one(FilterBar).focus_input()
        self.set_status("filter agents")

    def action_escape_filter(self) -> None:
        """Return focus to the agent list (ESC from filter or anywhere)."""
        self.query_one(AgentListPanel).focus_list()

    def action_toggle_attention(self) -> None:
        self._filters = DashboardFilterState(
            statuses=self._filters.statuses,
            attention_only=not self._filters.attention_only,
            text_query=self._filters.text_query,
            include_completed=self._filters.include_completed,
        )
        self.refresh_data()

    def action_toggle_completed(self) -> None:
        self._filters = DashboardFilterState(
            statuses=self._filters.statuses,
            attention_only=self._filters.attention_only,
            text_query=self._filters.text_query,
            include_completed=not self._filters.include_completed,
        )
        self.refresh_data()

    def action_open_attention_inbox(self) -> None:
        if getattr(self.runtime, "attention", None) is None:
            self.set_status("attention inbox unavailable")
            return
        self.app.switch_mode("attention")

    def action_cycle_sort(self) -> None:
        next_index = (_SORT_ORDER.index(self._sort.field) + 1) % len(_SORT_ORDER)
        self._sort = DashboardSort(
            field=_SORT_ORDER[next_index],
            descending=self._sort.descending,
        )
        self.refresh_data()

    def action_mark_complete(self) -> None:
        if self._selected_agent_id is None:
            self.set_status("no agent selected")
            return
        result = self.runtime.agents.mark_complete(self._selected_agent_id)
        self.set_status(
            f"mark_complete {result.agent.name} "
            f"session {result.session_id or '-'} ended={result.session_ended}"
        )
        self.refresh_data()

    def action_interrupt_agent(self) -> None:
        if self._selected_agent_id is None:
            self.set_status("no agent selected")
            return
        name = self._selected_action_subject()
        self.app.push_screen(
            ConfirmScreen(
                message=f"Interrupt {name}? This sends Ctrl-C to the parent pane.",
                title="Interrupt Agent",
            ),
            callback=self._on_interrupt_confirmed,
        )

    def _on_interrupt_confirmed(self, confirmed: bool | None) -> None:
        if not confirmed:
            self.set_status("interrupt cancelled")
            return
        self._execute_agent_intent("interrupt", self.runtime.agents.interrupt_intent)

    def action_kill_agent(self) -> None:
        if self._selected_agent_id is None:
            self.set_status("no agent selected")
            return
        agent = self._find_selected_agent()
        if agent is None or not agent.pane_id:
            self.set_status("✗ agent has no pane")
            return
        self.app.push_screen(
            ConfirmScreen(
                message=(
                    f"Kill the tmux pane for {self._selected_action_subject()}? "
                    "This force-stops the agent and any active sub-agent."
                ),
                title="Kill Agent Pane",
            ),
            callback=self._on_kill_confirmed,
        )

    def _on_kill_confirmed(self, confirmed: bool | None) -> None:
        if not confirmed:
            self.set_status("kill cancelled")
            return
        self._execute_agent_intent("kill", self.runtime.agents.kill_pane_intent)

    def action_open_pane(self) -> None:
        self._execute_agent_intent("focus console", self.runtime.agents.open_pane_intent)

    def action_rename_window(self) -> None:
        if self._selected_agent_id is None:
            self.set_status("no agent selected")
            return
        agent = self._find_selected_agent()
        if agent is None:
            self.set_status("no agent detail loaded")
            return
        self.app.push_screen(
            RenameWindowScreen(
                self._selected_action_subject(),
                current_name=agent.window_name,
            ),
            callback=self._on_rename_window_result,
        )

    def _on_rename_window_result(self, result: RenameWindowResult | None) -> None:
        if result is None:
            self.set_status("rename cancelled")
            return
        self._execute_agent_intent(
            "rename window",
            lambda agent_id: self.runtime.agents.rename_window_intent(
                agent_id,
                new_name=result.name,
            ),
        )

    def action_move_to_window(self) -> None:
        if self._selected_agent_id is None:
            self.set_status("no agent selected")
            return
        if self.runtime.actions is None:
            self.set_status("✗ action service unavailable")
            return
        agent = self._find_selected_agent()
        if agent is None or not agent.pane_id:
            self.set_status("✗ agent has no pane")
            return
        self.app.push_screen(
            MoveWindowScreen(
                self._selected_action_subject(),
                current_window_name=agent.window_name,
                choices=self.runtime.actions.window_choices(
                    exclude_window_id=agent.window_id,
                ),
            ),
            callback=self._on_move_window_result,
        )

    def _on_move_window_result(self, result: MoveWindowResult | None) -> None:
        if result is None:
            self.set_status("move cancelled")
            return
        self._execute_agent_intent(
            "move to window",
            lambda agent_id: self.runtime.agents.move_to_window_intent(
                agent_id,
                target_window=result.target_window,
                new_window_name=result.new_window_name,
            ),
        )

    def action_open_worktree(self) -> None:
        self._set_agent_intent_status("open_worktree", self.runtime.agents.open_worktree_intent)

    def action_send_message(self) -> None:
        """Open modal to send a message to the selected agent's tmux pane."""
        if self._selected_agent_id is None:
            self.set_status("no agent selected")
            return
        if self.runtime.actions is None:
            self.set_status("✗ action service unavailable")
            return
        agent = self._find_selected_agent()
        if agent is None or not agent.pane_id:
            self.set_status("✗ agent has no pane")
            return
        name = agent.repo_name or agent.name
        self.app.push_screen(
            SendMessageScreen(name, agent.pane_id),
            callback=self._on_message_result,
        )

    def _on_message_result(self, result: MessageResult | None) -> None:
        if result is None:
            self.set_status("message cancelled")
            return
        if self.runtime.actions is None:
            return
        action_result = self.runtime.actions.send_message(result.pane_id, result.text)
        prefix = "✓" if action_result.success else "✗"
        self.set_status(f"{prefix} {action_result.message}")

    def action_view_logs(self) -> None:
        """Switch to replay screen with the selected agent's latest session."""
        if self._selected_agent_id is None:
            self.set_status("no agent selected")
            return
        if self._state is None or self._state.selected_agent is None:
            self.set_status("no agent detail loaded")
            return
        session_id = self._state.selected_agent.open_session_id
        if session_id is None:
            self.set_status("no active session for this agent")
            return
        self.muxdeck_app.remember_session_selection(session_id)
        self.muxdeck_app.switch_mode("replay")

    def action_copy_details(self) -> None:
        list_panel = self.query_one(AgentListPanel)
        selected_agent_id = list_panel.selected_agent_id
        if selected_agent_id is None:
            self.set_status("no agent selected")
            return
        if selected_agent_id != self._selected_agent_id:
            self._selected_agent_id = selected_agent_id
            self.muxdeck_app.remember_agent_selection(selected_agent_id)
        self._update_selected_detail()
        label = "sub-agent details" if list_panel.selected_subagent is not None else "agent details"
        self.copy_rendered_text(label, self.query_one(AgentDetailPanel))

    def action_view_pane(self) -> None:
        """Open a full-screen live mirror for the selected agent pane."""
        if self._selected_agent_id is None:
            self.set_status("no agent selected")
            return
        agent = self._find_selected_agent()
        if agent is None or not agent.pane_id:
            self.set_status("✗ agent has no pane")
            return
        pane_id, stream_adapter = self._resolve_live_mirror_target(agent)
        if stream_adapter is None:
            self.set_status("✗ pane streaming unavailable")
            return
        self.app.push_screen(
            ComposeWithMirrorScreen(
                self.runtime,
                pane_id=pane_id,
                display_name=agent.repo_name or agent.name,
                show_editor=False,
                stream_adapter=stream_adapter,
            )
        )

    def _execute_agent_intent(
        self,
        label: str,
        loader: Callable[[str], AgentIntentView],
    ) -> None:
        if self._selected_agent_id is None:
            self.set_status("no agent selected")
            return
        if self.runtime.actions is None:
            self.set_status("✗ action service unavailable")
            return
        try:
            intent = loader(self._selected_agent_id)
        except Exception as exc:  # pragma: no cover - defensive UI boundary
            self.set_status(f"{label} unavailable: {exc}")
            return
        result = self.runtime.actions.execute_intent(intent)
        prefix = "✓" if result.success else "✗"
        self.set_status(f"{prefix} {result.message}")

    def _set_agent_intent_status(
        self,
        label: str,
        loader: Callable[[str], AgentIntentView],
    ) -> None:
        if self._selected_agent_id is None:
            self.set_status("no agent selected")
            return
        try:
            intent = loader(self._selected_agent_id)
        except Exception as exc:  # pragma: no cover - defensive UI boundary
            self.set_status(f"{label} unavailable: {exc}")
            return
        metadata = " ".join(f"{key}={value}" for key, value in intent.metadata)
        suffix = f" {metadata}" if metadata else ""
        self.set_status(f"{intent.label.lower()} -> {intent.agent.name}{suffix}")

    def action_stop_all(self) -> None:
        """Emergency stop: confirm then interrupt ALL visible agents."""
        if self.runtime.actions is None:
            self.set_status("✗ action service unavailable")
            return
        if self._state is None or not self._state.agents:
            self.set_status("no agents to stop")
            return
        count = sum(1 for a in self._state.agents if a.pane_id)
        if count == 0:
            self.set_status("no active panes to stop")
            return
        self.app.push_screen(
            ConfirmScreen(
                message=f"Stop ALL {count} agents? This sends Ctrl-C to each.",
                title="Emergency Stop",
            ),
            callback=self._on_stop_all_confirmed,
        )

    def _on_stop_all_confirmed(self, confirmed: bool | None) -> None:
        if not confirmed:
            self.set_status("stop cancelled")
            return
        if self.runtime.actions is None or self._state is None:
            return
        pane_ids = [a.pane_id for a in self._state.agents if a.pane_id]
        results = self.runtime.actions.stop_all_agents(pane_ids)
        ok = sum(1 for r in results if r.success)
        fail = len(results) - ok
        if fail:
            self.set_status(f"⚠ stopped {ok}/{len(results)} agents ({fail} failed)")
        else:
            self.set_status(f"✓ stopped {ok} agents")

    def _find_selected_agent(self) -> DashboardAgentListItemView | None:
        """Find the selected agent from the current state."""
        if self._state is None or self._selected_agent_id is None:
            return None
        for agent in self._state.agents:
            if agent.agent_id == self._selected_agent_id:
                return agent
        return None

    def _selected_action_subject(self) -> str:
        subagent = self.query_one(AgentListPanel).selected_subagent
        if subagent is not None:
            return subagent.display_name
        agent = self._find_selected_agent()
        if agent is None:
            return "agent"
        return agent.repo_name or agent.name

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        super().on_worker_state_changed(event)
        if event.worker.name == _DASHBOARD_DETAIL_WORKER:
            if event.state != WorkerState.SUCCESS:
                return
            result = event.worker.result
            if not isinstance(result, tuple) or len(result) != 3:
                return
            token, agent_id, view = result
            self._apply_async_detail(token, agent_id, view)
            return
        if event.worker.name != _DASHBOARD_WORKER:
            return
        if event.state == WorkerState.ERROR:
            # On first-load failure keep the loading overlay so the user
            # sees the dashboard is still trying — a subsequent refresh
            # (periodic sync or manual ``r``) will retry. On a refresh
            # failure where we already have a state, surface the error
            # in the footer but keep the previous data on screen.
            if self._state is not None:
                self.set_status("dashboard refresh failed")
            return
        if event.state != WorkerState.SUCCESS:
            return
        result = event.worker.result
        if not isinstance(result, tuple) or len(result) != 2:
            return
        kickoff_seq, state = result
        if not isinstance(state, DashboardState):
            return
        # Drop the result if a fresher state was painted while this
        # worker was running — most commonly the app sync worker
        # delivering post-refresh data after the cold-start "paint
        # from store" build kicked off but before it returned.
        if kickoff_seq != self._state_apply_seq:
            return
        self._apply_state(state, self.muxdeck_app.last_sync_report)

    def _schedule_selected_detail_worker(self) -> None:
        """Kick off a worker to load detail for the currently selected agent.

        Runs the SQLite + JSONL work on a thread so the UI stays
        responsive while the user holds j/k. The captured token gates
        the result: if the user (or a fresh sync) advances state
        before the worker finishes, the result is dropped.
        """
        item = self._find_selected_agent()
        if item is None:
            self._update_selected_detail()
            return
        sync_dashboard = getattr(self.runtime, "sync_dashboard", None)
        if sync_dashboard is None:
            # Fallback: no thread-safe controller, use the synchronous
            # path (which lives on the UI thread but at least keeps
            # behavior correct).
            self._update_selected_detail()
            return
        token = self._detail_request_token
        agent_id = item.agent_id
        preview_lines = self._preview_line_limit()

        def _build() -> tuple[int, str, DashboardSelectedAgentView | None]:
            try:
                view = sync_dashboard.build_selected_agent_view(
                    item, preview_line_limit=preview_lines
                )
            except Exception:
                return token, agent_id, None
            return token, agent_id, view

        self.run_worker(
            _build,
            thread=True,
            exclusive=True,
            name=_DASHBOARD_DETAIL_WORKER,
        )

    def _apply_async_detail(
        self,
        token: int,
        agent_id: str,
        view: DashboardSelectedAgentView | None,
    ) -> None:
        # Drop stale worker results — the user moved on or a fresh
        # sync replaced ``_state`` while we were loading.
        if token != self._detail_request_token:
            return
        if agent_id != self._selected_agent_id:
            return
        if view is None:
            return
        if self._state is not None:
            self._state = DashboardState(
                generated_at=self._state.generated_at,
                metrics=self._state.metrics,
                filters=self._state.filters,
                sort=self._state.sort,
                health=self._state.health,
                alerts=self._state.alerts,
                agents=self._state.agents,
                selected_agent_id=agent_id,
                selected_agent=view,
            )
            self.query_one(StatusBar).set_state(
                self._state.health,
                self._state.metrics,
                view.item,
            )
        panel = self.query_one(AgentListPanel)
        if panel.selected_subagent is not None:
            self.query_one(AgentDetailPanel).set_subagent(panel.selected_subagent)
        else:
            self.query_one(AgentDetailPanel).set_agent(view)
        self.query_one(LogPreviewPanel).set_logs(self._with_live_preview(view))

    def _update_selected_detail(self) -> None:
        """Lightweight: rebuild only the detail panels for the newly selected agent."""
        item = self._find_selected_agent()
        if item is None:
            if self._state is not None:
                self.query_one(StatusBar).set_state(self._state.health, self._state.metrics, None)
            self.query_one(AgentDetailPanel).set_agent(None)
            self.query_one(LogPreviewPanel).set_logs(None)
            return
        try:
            selected_view = self.runtime.dashboard.build_selected_agent_view(
                item,
                preview_line_limit=self._preview_line_limit(),
            )
        except Exception:
            return
        if self._state is not None:
            self._state = DashboardState(
                generated_at=self._state.generated_at,
                metrics=self._state.metrics,
                filters=self._state.filters,
                sort=self._state.sort,
                health=self._state.health,
                alerts=self._state.alerts,
                agents=self._state.agents,
                selected_agent_id=item.agent_id,
                selected_agent=selected_view,
            )
            self.query_one(StatusBar).set_state(
                self._state.health,
                self._state.metrics,
                selected_view.item,
            )
        # Don't clobber a sub-agent detail view if the cursor is currently
        # parked on a sub-agent row under this parent.
        panel = self.query_one(AgentListPanel)
        if panel.selected_subagent is not None:
            self.query_one(AgentDetailPanel).set_subagent(panel.selected_subagent)
        else:
            self.query_one(AgentDetailPanel).set_agent(selected_view)
        self.query_one(LogPreviewPanel).set_logs(self._with_live_preview(selected_view))

    def _resolve_live_mirror_target(
        self,
        agent: DashboardAgentListItemView,
    ) -> tuple[str, PaneStreamAdapter | None]:
        stream_adapter = self.runtime.pane_stream
        if not agent.pane_id:
            return "", stream_adapter
        resolver = self.runtime.session_resolver
        if resolver is None:
            return agent.pane_id, stream_adapter
        agent_record = self.runtime.store.get_agent(agent.agent_id)
        if agent_record is None:
            return agent.pane_id, stream_adapter
        target = resolver.resolve_target_for_pid(getattr(agent_record, "pid", None))
        if target is None or target.pane_id is None:
            return agent.pane_id, stream_adapter
        if target.socket_path is None:
            return target.pane_id, stream_adapter
        nested_stream = self._stream_adapter_for_socket(target.socket_path)
        if nested_stream is None:
            return agent.pane_id, stream_adapter
        return target.pane_id, nested_stream

    def _stream_adapter_for_socket(self, socket_path: Path) -> PaneStreamAdapter | None:
        tmux = self.runtime.tmux
        if tmux is None:
            return None
        return PaneStreamAdapter(tmux=tmux.with_socket_path(socket_path))

    # --------------------------------------------------------------- live tail

    def _with_live_preview(
        self,
        view: DashboardSelectedAgentView | None,
    ) -> DashboardSelectedAgentView | None:
        """Substitute the cached live tail into the painted output panel.

        The discovery loop's ``log_chunks`` are written every >=2s and
        are deduped on content, so painting them straight from the store
        produces a panel that lags badly and freezes the moment a pane
        stops changing. Whenever the live-tail loop has captured fresh
        text for the highlighted agent, swap the controller-built
        ``log_preview`` for the live capture so the panel reads like
        ``tail -f``.
        """
        if view is None:
            return None
        cached = self._live_tail_lines.get(view.item.agent_id)
        if cached is None:
            return view
        return replace(view, log_preview=cached)

    def _start_live_tail(self, agent_id: str) -> None:
        self._stop_live_tail()
        agent = next(
            (a for a in (self._state.agents if self._state else ()) if a.agent_id == agent_id),
            None,
        )
        if agent is None or not agent.pane_id:
            return
        # Claim the agent on the UI thread so subsequent stale-result
        # checks have something to compare against, but defer the
        # expensive part (``_resolve_live_mirror_target`` walks /proc
        # and may stat the SQLite store + spin up nested socket
        # adapters) to a worker thread. Without this defer, every j/k
        # cursor move blocked the UI for the full resolver round-trip
        # — on slow filesystems (WSL /mnt/c) that's tens to hundreds
        # of milliseconds per keystroke and made the whole dashboard
        # feel laggy.
        self._live_tail_agent_id = agent_id
        self._live_tail_token += 1
        token = self._live_tail_token
        target_agent = agent

        def _resolve_and_capture() -> None:
            try:
                pane_id, stream_adapter = self._resolve_live_mirror_target(target_agent)
            except AttributeError:
                # Runtime missing optional infra (e.g. ``pane_stream``
                # / ``tmux`` / ``store``) — common in lighter test
                # harnesses and in production environments where tmux
                # integration is disabled.
                return
            if stream_adapter is None or not pane_id:
                return
            # Fold the first capture into the same worker call so the
            # operator sees recent output without waiting one full
            # tail interval after the resolver round-trip.
            try:
                captured_text = stream_adapter.capture_tail(
                    pane_id, lines=_DASHBOARD_LIVE_TAIL_CAPTURE_LINES
                )
            except Exception:
                captured_text = ""
            self.app.call_from_thread(
                self._install_live_tail,
                agent_id,
                token,
                pane_id,
                stream_adapter,
                captured_text,
            )

        self.run_worker(
            _resolve_and_capture,
            thread=True,
            exclusive=True,
            name=_DASHBOARD_LIVE_TAIL_WORKER,
        )

    def _install_live_tail(
        self,
        agent_id: str,
        token: int,
        pane_id: str,
        stream_adapter: PaneStreamAdapter,
        captured_text: str,
    ) -> None:
        # Drop stale resolver results — the operator already moved on
        # to a different agent or stopped the tail entirely. Both
        # ``_start_live_tail`` and ``_stop_live_tail`` bump the token
        # synchronously on the UI thread before this worker callback
        # can land, so the token check alone catches every stale path.
        if token != self._live_tail_token:
            return
        self._live_tail_pane_id = pane_id
        self._live_tail_stream = stream_adapter
        if captured_text:
            self._apply_live_tail(agent_id, token, captured_text)
        self._live_tail_timer = self.set_interval(
            _DASHBOARD_LIVE_TAIL_INTERVAL_SEC,
            self._tick_live_tail,
            name=_DASHBOARD_LIVE_TAIL_WORKER,
        )

    def _stop_live_tail(self) -> None:
        if self._live_tail_timer is not None:
            self._live_tail_timer.stop()
            self._live_tail_timer = None
        self._live_tail_agent_id = None
        self._live_tail_pane_id = None
        self._live_tail_stream = None
        # Bump the token so any worker still mid-capture drops its
        # result instead of writing it into the cache.
        self._live_tail_token += 1

    def _tick_live_tail(self) -> None:
        agent_id = self._live_tail_agent_id
        pane_id = self._live_tail_pane_id
        stream = self._live_tail_stream
        if agent_id is None or not pane_id or stream is None:
            return
        token = self._live_tail_token

        def _capture() -> None:
            self._capture_live_tail(stream, pane_id, agent_id, token)

        self.run_worker(
            _capture,
            thread=True,
            exclusive=True,
            name=_DASHBOARD_LIVE_TAIL_WORKER,
        )

    def _capture_live_tail(
        self,
        stream: PaneStreamAdapter,
        pane_id: str,
        agent_id: str,
        token: int,
    ) -> None:
        try:
            text = stream.capture_tail(pane_id, lines=_DASHBOARD_LIVE_TAIL_CAPTURE_LINES)
        except Exception:
            # Pane vanished, tmux unreachable, etc. The next tick will
            # retry; meanwhile, leave any cached lines visible rather
            # than blanking the panel on a single transient failure.
            return
        if token != self._live_tail_token:
            return
        self.app.call_from_thread(self._apply_live_tail, agent_id, token, text)

    def _apply_live_tail(self, agent_id: str, token: int, captured_text: str) -> None:
        if token != self._live_tail_token:
            return
        if agent_id != self._selected_agent_id:
            return
        self._live_tail_sequence += 1
        captured_at = datetime.now(UTC)
        line_limit = self._preview_line_limit()
        lines = self._build_live_preview_lines(
            captured_text,
            line_limit=line_limit,
            captured_at=captured_at,
            sequence_no=self._live_tail_sequence,
        )
        if not lines:
            # Pane is genuinely empty (or only whitespace). Drop the
            # cached entry so the panel falls back to the controller's
            # "no recent output" / "launching" placeholder rather than
            # showing the previous agent's stale tail.
            self._live_tail_lines.pop(agent_id, None)
        else:
            self._live_tail_lines[agent_id] = lines
        if self._state is not None and self._state.selected_agent is not None:
            self.query_one(LogPreviewPanel).set_logs(
                self._with_live_preview(self._state.selected_agent),
            )

    @staticmethod
    def _build_live_preview_lines(
        captured_text: str,
        *,
        line_limit: int,
        captured_at: datetime,
        sequence_no: int,
    ) -> tuple[DashboardLogLineView, ...]:
        if line_limit <= 0:
            return ()
        # Preserve interior blank lines so the panel reads like the
        # actual tmux pane (paragraph spacing, command/output gaps,
        # box-drawing tables). Trim only the trailing blank rows
        # tmux emits as pane-bottom padding when the prompt is high
        # in the pane, otherwise the panel would show empty rows at
        # the bottom while real content scrolls off the top.
        rows = [line.rstrip() for line in captured_text.splitlines()]
        while rows and not rows[-1]:
            rows.pop()
        if not rows:
            return ()
        tail = rows[-line_limit:]
        return tuple(
            DashboardLogLineView(
                captured_at=captured_at,
                source="tmux_capture",
                sequence_no=sequence_no,
                content=line,
            )
            for line in tail
        )

    def _preview_line_limit(self) -> int:
        # Send enough preview lines to fill a tall panel — the
        # ``LogPreviewPanel`` widget tails this list to the rows that
        # actually fit on screen, so over-fetching here is cheap and
        # keeps the panel honest on terminals taller than ~25 rows.
        return min(max(self.runtime.config.general.log_preview_lines, 12), 200)

    def _emit_notifications(self, notifications: tuple[AttentionNotification, ...]) -> None:
        if not notifications:
            return
        self.app.bell()
        head = notifications[0]
        message = head.message
        if len(notifications) > 1:
            message = f"{message} (+{len(notifications) - 1} more critical)"
        self.app.notify(
            message,
            title=head.title,
            severity=_NOTIFY_SEVERITY[head.severity],
        )
