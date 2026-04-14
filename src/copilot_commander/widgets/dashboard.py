from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Input, ListItem, ListView, Static

from copilot_commander.controllers import (
    DashboardAgentListItemView,
    DashboardAlertView,
    DashboardHealthSummary,
    DashboardMetricView,
    DashboardSelectedAgentView,
)
from copilot_commander.widgets.common import format_short_timestamp, format_timestamp, join_lines


class MetricStrip(Static):
    def set_metrics(self, metrics: Sequence[DashboardMetricView]) -> None:
        text = "   ".join(f"{metric.label.upper()} {metric.value}" for metric in metrics)
        self.update(text or "NO METRICS")


class HealthBanner(Static):
    def set_health(self, health: DashboardHealthSummary) -> None:
        self.remove_class("tone-healthy", "tone-warning", "tone-critical")
        self.add_class(f"tone-{health.tone}")
        self.update(
            " | ".join(
                (
                    f"HEALTH {health.message.upper()}",
                    f"agents {health.total_agents}",
                    f"active {health.active_agents}",
                    f"attention {health.attention_agents}",
                    f"waiting {health.waiting_input_agents}",
                    f"blocked {health.blocked_agents}",
                    f"errors {health.error_agents}",
                )
            )
        )


class FilterBar(Vertical):
    def compose(self) -> ComposeResult:
        yield Static(id="dashboard-filter-summary")
        yield Input(placeholder="filter agents (/)", id="dashboard-filter-input")

    def set_summary(
        self,
        *,
        query: str | None,
        attention_only: bool,
        include_completed: bool,
        sort_label: str,
    ) -> None:
        parts = [f"sort {sort_label}"]
        if attention_only:
            parts.append("attention only")
        if not include_completed:
            parts.append("hide completed")
        if query:
            parts.append(f"query {query!r}")
        self.query_one("#dashboard-filter-summary", Static).update(" | ".join(parts).upper())

    def set_query(self, value: str | None) -> None:
        self.query_one("#dashboard-filter-input", Input).value = value or ""

    def focus_input(self) -> None:
        self.query_one(Input).focus()


class AgentListPanel(Vertical):
    class AgentSelected(Message):
        def __init__(self, agent_id: str) -> None:
            super().__init__()
            self.agent_id = agent_id

    def __init__(self, *, widget_id: str | None = None) -> None:
        super().__init__(id=widget_id)
        self._agent_ids: list[str] = []

    def compose(self) -> ComposeResult:
        yield ListView(id="dashboard-agent-list")

    def set_agents(
        self,
        agents: Sequence[DashboardAgentListItemView],
        *,
        selected_agent_id: str | None,
    ) -> None:
        list_view = self.query_one(ListView)
        list_view.clear()
        self._agent_ids = []
        selected_index = 0
        for index, agent in enumerate(agents):
            prefix = "!" if agent.needs_attention else " "
            branch = agent.branch or "-"
            task = agent.task_title or "-"
            last_seen = format_short_timestamp(agent.last_seen_at)
            row = (
                f"{prefix} {agent.name:<18.18} {agent.status.value:<14.14} "
                f"{last_seen:<9} {branch} :: {task}"
            )
            list_view.append(ListItem(Static(row, markup=False)))
            self._agent_ids.append(agent.agent_id)
            if agent.agent_id == selected_agent_id:
                selected_index = index
        if self._agent_ids:
            list_view.index = selected_index
        self._post_selection(list_view.index)

    def move_cursor(self, delta: int) -> None:
        if not self._agent_ids:
            return
        list_view = self.query_one(ListView)
        current = 0 if list_view.index is None else cast(int, list_view.index)
        list_view.index = max(0, min(len(self._agent_ids) - 1, current + delta))
        list_view.focus()
        self._post_selection(list_view.index)

    def focus_list(self) -> None:
        self.query_one(ListView).focus()

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        del event
        self._post_selection(self.query_one(ListView).index)

    def _post_selection(self, index: int | None) -> None:
        if index is None or index >= len(self._agent_ids):
            return
        self.post_message(self.AgentSelected(self._agent_ids[index]))


class AgentDetailPanel(Static):
    def set_agent(self, agent: DashboardSelectedAgentView | None) -> None:
        if agent is None:
            self.update("No agent selected.")
            return
        item = agent.item
        lines = (
            f"NAME      {item.name}",
            f"STATUS    {item.status.value}",
            f"TASK      {item.task_title or '-'}",
            f"BRANCH    {item.branch or '-'}",
            f"WORKTREE  {item.worktree_path or '-'}",
            f"REPO      {agent.repo_root or '-'}",
            f"SESSION   {agent.open_session_id or item.latest_session_id or '-'}",
            f"SESSIONS  {agent.session_count}",
            f"LAST EVT  {agent.latest_event_kind or '-'}",
            f"SEVERITY  {agent.latest_event_severity or '-'}",
            f"LAST SEEN {format_timestamp(item.last_seen_at)}",
            f"STARTED   {format_timestamp(item.started_at)}",
            f"IDLE      {item.idle_seconds}s",
            f"TOKENS    {item.token_total if item.token_total is not None else '-'}",
            f"COST USD  {item.estimated_cost_usd or '-'}",
            f"ATTN      {item.attention_reason or '-'}",
        )
        self.update(join_lines(lines))


class LogPreviewPanel(Static):
    def set_logs(self, agent: DashboardSelectedAgentView | None) -> None:
        if agent is None or not agent.log_preview:
            self.update("No recent log lines.")
            return
        lines = [
            f"{format_short_timestamp(line.captured_at)} {line.source:<10.10} {line.content}"
            for line in agent.log_preview
        ]
        self.update(join_lines(lines))


class AlertPanel(Static):
    def set_alerts(self, alerts: Sequence[DashboardAlertView]) -> None:
        if not alerts:
            self.update("No active alerts.")
            return
        lines = [
            (
                f"{format_short_timestamp(alert.occurred_at)} "
                f"{alert.severity.upper():<7} {alert.agent_name}: {alert.message}"
            )
            for alert in alerts
        ]
        self.update(join_lines(lines))


__all__ = [
    "AgentDetailPanel",
    "AgentListPanel",
    "AlertPanel",
    "FilterBar",
    "HealthBanner",
    "LogPreviewPanel",
    "MetricStrip",
]
