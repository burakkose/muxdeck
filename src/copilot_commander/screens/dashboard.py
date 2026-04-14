from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, cast

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Input, Static

from copilot_commander.bindings import DASHBOARD_BINDINGS, DASHBOARD_HINTS
from copilot_commander.controllers import (
    AgentIntentView,
    DashboardFilterState,
    DashboardSort,
    DashboardState,
)
from copilot_commander.screens.base import ShellScreen
from copilot_commander.widgets.dashboard import (
    AgentDetailPanel,
    AgentListPanel,
    AlertPanel,
    FilterBar,
    HealthBanner,
    LogPreviewPanel,
    MetricStrip,
)

if TYPE_CHECKING:
    from copilot_commander.app import CommanderApp, CommanderRuntime


_SORT_ORDER = ("last_seen", "name", "status", "cost", "idle_seconds", "started_at")


class DashboardScreen(ShellScreen):
    SCREEN_TITLE = "DASHBOARD"
    BINDINGS = DASHBOARD_BINDINGS
    FOOTER_HINTS = DASHBOARD_HINTS

    def __init__(self, runtime: CommanderRuntime) -> None:
        super().__init__(runtime)
        self._filters = DashboardFilterState()
        self._sort = DashboardSort()
        self._selected_agent_id: str | None = None
        self._state: DashboardState | None = None

    def compose_body(self) -> ComposeResult:
        with Vertical(id="dashboard-root"):
            with Vertical(id="dashboard-top", classes="band"):
                yield HealthBanner(id="dashboard-health")
                yield MetricStrip(id="dashboard-metrics")
                yield FilterBar(id="dashboard-filters")
            with Horizontal(id="dashboard-main", classes="band"):
                with Vertical(id="dashboard-agents-panel", classes="panel"):
                    yield Static("AGENTS", classes="panel-title")
                    yield AgentListPanel(widget_id="dashboard-agents")
                with Vertical(id="dashboard-sidebar"):
                    with Vertical(id="dashboard-detail-panel", classes="panel"):
                        yield Static("DETAIL", classes="panel-title")
                        yield AgentDetailPanel(id="dashboard-detail")
                    with Vertical(id="dashboard-log-panel", classes="panel"):
                        yield Static("LOG PREVIEW", classes="panel-title")
                        yield LogPreviewPanel(id="dashboard-log")
                    with Vertical(id="dashboard-alerts-panel", classes="panel"):
                        yield Static("ALERTS", classes="panel-title")
                        yield AlertPanel(id="dashboard-alerts")

    def on_mount(self) -> None:
        self.refresh_data()
        self.call_after_refresh(self.query_one(AgentListPanel).focus_list)

    def on_show(self) -> None:
        self.refresh_data()

    def refresh_data(self) -> None:
        self._state = self.runtime.dashboard.build_state(
            filters=self._filters,
            sort=self._sort,
            selected_agent_id=self._selected_agent_id,
            preview_line_limit=min(self.runtime.config.general.log_preview_lines, 12),
        )
        self._selected_agent_id = self._state.selected_agent_id
        if self._selected_agent_id is not None:
            self.commander_app.remember_agent_selection(self._selected_agent_id)
        self.query_one(HealthBanner).set_health(self._state.health)
        self.query_one(MetricStrip).set_metrics(self._state.metrics)
        filter_bar = self.query_one(FilterBar)
        filter_bar.set_query(self._filters.text_query)
        filter_bar.set_summary(
            query=self._filters.normalized_query(),
            attention_only=self._filters.attention_only,
            include_completed=self._filters.include_completed,
            sort_label=self._sort.field,
        )
        self.query_one(AgentListPanel).set_agents(
            self._state.agents,
            selected_agent_id=self._state.selected_agent_id,
        )
        self.query_one(AgentDetailPanel).set_agent(self._state.selected_agent)
        self.query_one(LogPreviewPanel).set_logs(self._state.selected_agent)
        self.query_one(AlertPanel).set_alerts(self._state.alerts)
        self.set_status(
            f"{len(self._state.agents)} visible agents | {self._state.health.message}"
        )

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
        self.refresh_data()

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

    def action_focus_filter(self) -> None:
        self.query_one(FilterBar).focus_input()
        self.set_status("filter agents")

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
        self._set_agent_intent_status("interrupt", self.runtime.agents.interrupt_intent)

    def action_open_pane(self) -> None:
        self._set_agent_intent_status("open_pane", self.runtime.agents.open_pane_intent)

    def action_open_worktree(self) -> None:
        self._set_agent_intent_status("open_worktree", self.runtime.agents.open_worktree_intent)

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
