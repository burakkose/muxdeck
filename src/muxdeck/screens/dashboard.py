from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.timer import Timer
from textual.widgets import Input

from muxdeck.adapters.pane_stream import PaneStreamAdapter
from muxdeck.bindings import DASHBOARD_BINDINGS, DASHBOARD_HINTS
from muxdeck.controllers import (
    AgentIntentView,
    DashboardAgentListItemView,
    DashboardFilterState,
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
        self._loading: bool = False
        # Textual fires ``on_mount`` followed immediately by ``on_show``
        # on first activation. Without this guard the screen does
        # ``build_state`` twice back-to-back on every cold open and any
        # dashboard refresh that is in flight piles up.
        self._skip_next_show_refresh: bool = True

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
                    yield LogPreviewPanel(id="dashboard-log", classes="section")
                    yield AlertPanel(id="dashboard-alerts", classes="section")

    def on_mount(self) -> None:
        self.refresh_data()
        self.call_after_refresh(self.query_one(AgentListPanel).focus_list)

    def on_show(self) -> None:
        if self._skip_next_show_refresh:
            self._skip_next_show_refresh = False
            return
        self.refresh_data()

    def refresh_data(self) -> None:
        sync_report = self.muxdeck_app.last_sync_report
        # Prefer pre-built state from worker thread (no main-thread queries).
        pre_built = self.muxdeck_app.last_dashboard_state
        loading_widgets = (
            self.query_one(AgentListPanel),
            self.query_one(AgentDetailPanel),
            self.query_one(LogPreviewPanel),
            self.query_one(AlertPanel),
        )
        first_load = self._state is None
        if first_load and not self._loading and pre_built is None:
            self._loading = True
            self.set_status("loading dashboard…")
            self.begin_loading(*loading_widgets)
        if pre_built is not None:
            self._state = pre_built
            self.muxdeck_app.last_dashboard_state = None
        else:
            # Build state directly from controller (ensures filters/sort are applied).
            try:
                self._state = self.runtime.dashboard.build_state(
                    filters=self._filters,
                    sort=self._sort,
                    selected_agent_id=self._selected_agent_id,
                    preview_line_limit=self._preview_line_limit(),
                )
            except Exception:
                if first_load:
                    self.set_status("loading dashboard…")
                return
        self._loading = False
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
            self.query_one(LogPreviewPanel).set_logs(self._state.selected_agent)
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
        # Debounce: cancel any pending detail load and schedule a new one.
        # This prevents stacking 200ms DB calls while the user holds arrow keys.
        if self._detail_timer is not None:
            self._detail_timer.stop()
        self._detail_timer = self.set_timer(0.05, self._update_selected_detail)

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
        self.refresh_data()

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
        dashboard = self.runtime.sync_dashboard or self.runtime.dashboard
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
        self.query_one(LogPreviewPanel).set_logs(selected_view)

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

    def _preview_line_limit(self) -> int:
        return min(max(self.runtime.config.general.log_preview_lines, 12), 24)

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
