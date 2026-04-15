from __future__ import annotations

from collections.abc import Sequence

from rich import box
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Input, Static

from copilot_commander.controllers import (
    DashboardAgentListItemView,
    DashboardAlertView,
    DashboardHealthSummary,
    DashboardMetricView,
    DashboardSelectedAgentView,
)
from copilot_commander.domain.enums import AgentStatus
from copilot_commander.widgets.common import format_short_timestamp, format_timestamp, join_lines

_SOFT_TEXT = "rgb(219,226,239)"
_MUTED_TEXT = "rgb(153,169,191)"
_SURFACE_TEXT = "rgb(19,24,32)"
_SEVERITY_STYLES = {
    "info": "bold rgb(167,206,255)",
    "warning": "bold rgb(255,209,128)",
    "error": "bold rgb(255,166,166)",
}
_STATUS_STYLES = {
    AgentStatus.RUNNING: "bold rgb(146,227,169)",
    AgentStatus.IDLE: "bold rgb(255,216,128)",
    AgentStatus.WAITING_INPUT: "bold rgb(255,209,128)",
    AgentStatus.BLOCKED: "bold rgb(255,183,77)",
    AgentStatus.ERROR: "bold rgb(255,138,128)",
    AgentStatus.DEAD: "bold rgb(255,138,128)",
    AgentStatus.COMPLETED: "bold rgb(144,164,174)",
    AgentStatus.DISCOVERED: "bold rgb(167,206,255)",
    AgentStatus.STARTING: "bold rgb(167,206,255)",
    AgentStatus.UNKNOWN: "bold rgb(189,189,189)",
}
_HEALTH_TONE_STYLES = {
    "healthy": ("rgb(14,20,27) on rgb(157,230,178)", "bold rgb(157,230,178)"),
    "warning": ("rgb(14,20,27) on rgb(255,213,128)", "bold rgb(255,213,128)"),
    "critical": ("rgb(14,20,27) on rgb(255,171,171)", "bold rgb(255,171,171)"),
}


class MetricStrip(Static):
    def set_metrics(self, metrics: Sequence[DashboardMetricView]) -> None:
        if not metrics:
            self.update(Text("NO METRICS", style=f"bold {_MUTED_TEXT}"))
            return
        chips = Text()
        for index, metric in enumerate(metrics):
            if index:
                chips.append("  ")
            chips.append(
                f" {metric.label.upper()} ",
                style="bold rgb(19,24,32) on rgb(167,206,255)",
            )
            chips.append(f" {metric.value} ", style=f"bold {_SOFT_TEXT}")
        self.update(chips)


class HealthBanner(Static):
    def set_health(self, health: DashboardHealthSummary) -> None:
        self.remove_class("tone-healthy", "tone-warning", "tone-critical")
        self.add_class(f"tone-{health.tone}")
        badge_style, value_style = _HEALTH_TONE_STYLES[health.tone]
        summary = Text()
        summary.append(" HEALTH ", style=badge_style)
        summary.append(f" {health.message.upper()} ", style=value_style)
        for label, value in (
            ("agents", health.total_agents),
            ("active", health.active_agents),
            ("attention", health.attention_agents),
            ("waiting", health.waiting_input_agents),
            ("blocked", health.blocked_agents),
            ("errors", health.error_agents),
        ):
            summary.append("  ")
            summary.append(f"{label} ", style=f"bold {_MUTED_TEXT}")
            summary.append(str(value), style=f"bold {_SOFT_TEXT}")
        self.update(summary)


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


class AgentListPanel(Static, can_focus=True):
    class AgentSelected(Message):
        def __init__(self, agent_id: str) -> None:
            super().__init__()
            self.agent_id = agent_id

    def __init__(self, *, widget_id: str | None = None) -> None:
        super().__init__(id=widget_id)
        self._agents: tuple[DashboardAgentListItemView, ...] = ()
        self._selected_index = 0

    def set_agents(
        self,
        agents: Sequence[DashboardAgentListItemView],
        *,
        selected_agent_id: str | None,
    ) -> None:
        self._agents = tuple(agents)
        if not self._agents:
            self._selected_index = 0
            self.update(
                Text(
                    "No agents discovered yet. Press r to rescan tmux.",
                    style=_MUTED_TEXT,
                )
            )
            return
        selected_index = next(
            (
                index
                for index, agent in enumerate(self._agents)
                if agent.agent_id == selected_agent_id
            ),
            min(self._selected_index, len(self._agents) - 1),
        )
        self._selected_index = selected_index
        self._refresh_table()
        self._post_selection(self._selected_index)

    def move_cursor(self, delta: int) -> None:
        if not self._agents:
            return
        self._selected_index = max(0, min(len(self._agents) - 1, self._selected_index + delta))
        self.focus()
        self._refresh_table()
        self._post_selection(self._selected_index)

    def focus_list(self) -> None:
        self.focus()

    def _post_selection(self, index: int | None) -> None:
        if index is None or index >= len(self._agents):
            return
        self.post_message(self.AgentSelected(self._agents[index].agent_id))

    def _refresh_table(self) -> None:
        self.update(self._build_table())

    def _build_table(self) -> Table:
        table = Table(
            expand=True,
            box=box.SIMPLE_HEAVY,
            header_style="bold rgb(176,190,215)",
            border_style="rgb(69,90,116)",
            row_styles=("rgb(219,226,239)", "rgb(201,212,231)"),
            pad_edge=False,
        )
        table.add_column("", width=2, no_wrap=True)
        table.add_column("name", min_width=12, max_width=18, overflow="ellipsis")
        table.add_column("status", min_width=10, max_width=14, overflow="ellipsis")
        table.add_column("repo", min_width=10, max_width=14, overflow="ellipsis")
        table.add_column("branch", min_width=12, max_width=18, overflow="ellipsis")
        table.add_column("worktree", min_width=10, max_width=14, overflow="ellipsis")
        table.add_column("pane", width=6, no_wrap=True)
        table.add_column("task", min_width=14, max_width=20, overflow="ellipsis")
        table.add_column("idle", width=7, justify="right")
        table.add_column("session", width=10, no_wrap=True)
        table.add_column("tok", width=7, justify="right")
        table.add_column("cost", width=10, justify="right")
        for index, agent in enumerate(self._agents):
            is_selected = index == self._selected_index
            row_style = _row_style(agent, selected=is_selected)
            table.add_row(
                Text(
                    _marker_text(agent, selected=is_selected),
                    style=_marker_style(agent, is_selected),
                ),
                Text(agent.name, style=f"bold {_SOFT_TEXT}" if is_selected else _SOFT_TEXT),
                _status_text(agent.status),
                Text(agent.repo_name or "-", style=_SOFT_TEXT),
                Text(agent.branch or "-", style=_SOFT_TEXT),
                Text(agent.worktree_name or "-", style=_SOFT_TEXT),
                Text(agent.pane_id, style="bold rgb(167,206,255)"),
                Text(agent.task_title or "-", style=_SOFT_TEXT),
                Text(f"{agent.idle_seconds}s", style=_SOFT_TEXT),
                Text(_short_session(agent.latest_session_id), style=_MUTED_TEXT),
                Text(
                    str(agent.token_total) if agent.token_total is not None else "-",
                    style=_SOFT_TEXT,
                ),
                Text(_format_cost(agent.estimated_cost_usd), style=_SOFT_TEXT),
                style=row_style,
            )
        return table


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
            f"REPO      {agent.repo_root or '-'}",
            f"BRANCH    {item.branch or '-'}",
            f"WORKTREE  {item.worktree_path or '-'}",
            f"PANE      {item.pane_id}",
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
        lines: list[Text] = []
        for alert in alerts:
            line = Text()
            line.append(f"{format_short_timestamp(alert.occurred_at)} ", style=_MUTED_TEXT)
            line.append(f"{alert.severity.upper():<7}", style=_SEVERITY_STYLES[alert.severity])
            line.append(f" {alert.agent_name}: ", style=f"bold {_SOFT_TEXT}")
            line.append(alert.message, style=_SOFT_TEXT)
            lines.append(line)
        joined = Text()
        for index, line in enumerate(lines):
            if index:
                joined.append("\n")
            joined.append_text(line)
        self.update(joined)


def _format_cost(value: str | None) -> str:
    if value is None:
        return "-"
    return f"${value}"


def _marker_style(agent: DashboardAgentListItemView, selected: bool) -> str:
    if selected:
        return "bold rgb(167,206,255)"
    if agent.needs_attention:
        return "bold rgb(255,209,128)"
    return _MUTED_TEXT


def _marker_text(agent: DashboardAgentListItemView, *, selected: bool) -> str:
    if selected:
        return ">"
    if agent.needs_attention:
        return "!"
    return " "


def _row_style(agent: DashboardAgentListItemView, *, selected: bool) -> str:
    if selected:
        return "on rgb(34,43,56)"
    if agent.status in {AgentStatus.COMPLETED, AgentStatus.DEAD}:
        return "dim"
    if agent.needs_attention:
        return "on rgb(43,38,31)"
    return ""


def _short_session(session_id: str | None) -> str:
    if session_id is None:
        return "-"
    return session_id[:8]


def _status_text(status: AgentStatus) -> Text:
    label = status.value.replace("_", " ")
    return Text(label, style=_STATUS_STYLES.get(status, f"bold {_SOFT_TEXT}"))


__all__ = [
    "AgentDetailPanel",
    "AgentListPanel",
    "AlertPanel",
    "FilterBar",
    "HealthBanner",
    "LogPreviewPanel",
    "MetricStrip",
]
