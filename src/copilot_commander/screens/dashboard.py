from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, cast

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Input

from copilot_commander.bindings import DASHBOARD_BINDINGS, DASHBOARD_HINTS
from copilot_commander.controllers import (
    AgentIntentView,
    DashboardAgentListItemView,
    DashboardFilterState,
    DashboardSort,
    DashboardSortField,
    DashboardState,
)
from copilot_commander.screens.base import ShellScreen
from copilot_commander.screens.confirm_dialog import ConfirmScreen
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

    def compose_body(self) -> ComposeResult:
        with Vertical(id="dashboard-root"):
            yield StatusBar(id="dashboard-status-bar")
            yield FilterBar(id="dashboard-filter-row")
            with Horizontal(id="dashboard-main"):
                yield AgentListPanel(widget_id="dashboard-agents", classes="panel")
                with Vertical(id="dashboard-sidebar"):
                    yield AgentDetailPanel(id="dashboard-detail", classes="panel")
                    yield LogPreviewPanel(id="dashboard-log", classes="panel")
                    yield AlertPanel(id="dashboard-alerts", classes="panel")

    def on_mount(self) -> None:
        self.refresh_data()
        self.call_after_refresh(self.query_one(AgentListPanel).focus_list)

    def on_show(self) -> None:
        self.refresh_data()

    def refresh_data(self) -> None:
        sync_report = self.commander_app.last_sync_report
        self._state = self.runtime.dashboard.build_state(
            filters=self._filters,
            sort=self._sort,
            selected_agent_id=self._selected_agent_id,
            preview_line_limit=min(self.runtime.config.general.log_preview_lines, 12),
        )
        self._selected_agent_id = self._state.selected_agent_id
        if self._selected_agent_id is not None:
            self.commander_app.remember_agent_selection(self._selected_agent_id)
        self.query_one(StatusBar).set_state(self._state.health, self._state.metrics)
        filter_bar = self.query_one(FilterBar)
        filter_bar.set_query(self._filters.text_query)
        self.query_one(AgentListPanel).set_agents(
            self._state.agents,
            selected_agent_id=self._state.selected_agent_id,
        )
        self.query_one(AgentDetailPanel).set_agent(self._state.selected_agent)
        self.query_one(LogPreviewPanel).set_logs(self._state.selected_agent)
        self.query_one(AlertPanel).set_alerts(self._state.alerts)
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

    def _on_interrupt_confirmed(self, confirmed: bool) -> None:
        if not confirmed:
            self.set_status("interrupt cancelled")
            return
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

    def _on_stop_all_confirmed(self, confirmed: bool) -> None:
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
