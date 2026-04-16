from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Literal, cast

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.timer import Timer
from textual.widgets import Input

from copilot_commander.bindings import DASHBOARD_BINDINGS, DASHBOARD_HINTS
from copilot_commander.controllers import (
    AgentIntentView,
    DashboardAgentListItemView,
    DashboardFilterState,
    DashboardSort,
    DashboardSortField,
    DashboardState,
    DashboardSubAgentTreeView,
)
from copilot_commander.screens.base import ShellScreen
from copilot_commander.screens.confirm_dialog import ConfirmScreen
from copilot_commander.screens.message_input import MessageResult, SendMessageScreen
from copilot_commander.services.attention_service import AttentionNotification
from copilot_commander.widgets.dashboard import (
    AgentDetailPanel,
    AgentListPanel,
    AlertPanel,
    FilterBar,
    LogPreviewPanel,
    StatusBar,
)

if TYPE_CHECKING:
    from copilot_commander.app import CommanderApp, CommanderRuntime


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

    def __init__(self, runtime: CommanderRuntime) -> None:
        super().__init__(runtime)
        self._filters = DashboardFilterState(include_completed=False)
        self._sort = DashboardSort()
        self._selected_agent_id: str | None = None
        self._state: DashboardState | None = None
        self._detail_timer: Timer | None = None

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
                yield AgentListPanel(widget_id="dashboard-agents", classes="divider-right")
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
        self.refresh_data()

    def refresh_data(self) -> None:
        sync_report = self.commander_app.last_sync_report
        # Prefer pre-built state from worker thread (no main-thread queries).
        pre_built = self.commander_app.last_dashboard_state
        if pre_built is not None:
            self._state = pre_built
            self.commander_app.last_dashboard_state = None
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
                if self._state is None:
                    self.set_status("Discovering agents…")
                return
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
            self.commander_app.remember_agent_selection(effective_selected)
        self.query_one(StatusBar).set_state(self._state.health, self._state.metrics)
        filter_bar = self.query_one(FilterBar)
        filter_bar.set_query(self._filters.text_query)
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
        parts.append(f"sort:{self._sort.field}")
        self.set_status(" · ".join(parts))

    @property
    def commander_app(self) -> CommanderApp:
        return cast("CommanderApp", self.app)

    def on_agent_list_panel_agent_selected(
        self,
        message: AgentListPanel.AgentSelected,
    ) -> None:
        if message.agent_id == self._selected_agent_id:
            return
        self._selected_agent_id = message.agent_id
        self.commander_app.remember_agent_selection(self._selected_agent_id)
        # Debounce: cancel any pending detail load and schedule a new one.
        # This prevents stacking 200ms DB calls while the user holds arrow keys.
        if self._detail_timer is not None:
            self._detail_timer.stop()
        self._detail_timer = self.set_timer(0.05, self._update_selected_detail)

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
        tree = self.runtime.dashboard.load_subagents(agent_id)
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
        agent = self._find_selected_agent()
        name = (agent.repo_name or agent.name) if agent else "agent"
        self.app.push_screen(
            ConfirmScreen(
                message=f"Interrupt {name}? This sends Ctrl-C.",
                title="Interrupt Agent",
            ),
            callback=self._on_interrupt_confirmed,
        )

    def _on_interrupt_confirmed(self, confirmed: bool | None) -> None:
        if not confirmed:
            self.set_status("interrupt cancelled")
            return
        self._execute_agent_intent("interrupt", self.runtime.agents.interrupt_intent)

    def action_open_pane(self) -> None:
        self._execute_agent_intent("focus console", self.runtime.agents.open_pane_intent)

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
        self.commander_app.remember_session_selection(session_id)
        self.commander_app.switch_mode("replay")

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

    def _update_selected_detail(self) -> None:
        """Lightweight: rebuild only the detail panels for the newly selected agent."""
        item = self._find_selected_agent()
        if item is None:
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
        self.query_one(AgentDetailPanel).set_agent(selected_view)
        self.query_one(LogPreviewPanel).set_logs(selected_view)

    def _preview_line_limit(self) -> int:
        return min(self.runtime.config.general.log_preview_lines, 24)

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
