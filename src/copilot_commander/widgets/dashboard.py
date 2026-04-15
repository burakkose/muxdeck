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
from copilot_commander.theme import (
    ATTENTION_ROW_BG,
    BADGE_BG,
    BADGE_FG,
    BLUE,
    BORDER,
    FG,
    FG1,
    FG3,
    FG4,
    ORANGE,
    SELECTED_ROW_BG,
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    STATUS_BLOCKED,
    STATUS_COMPLETED,
    STATUS_DEAD,
    STATUS_DISCOVERED,
    STATUS_ERROR,
    STATUS_IDLE,
    STATUS_RUNNING,
    STATUS_STARTING,
    STATUS_UNKNOWN,
    STATUS_WAITING_INPUT,
    TONE_CRITICAL_BG,
    TONE_CRITICAL_FG,
    TONE_HEALTHY_BG,
    TONE_HEALTHY_FG,
    TONE_WARNING_BG,
    TONE_WARNING_FG,
)
from copilot_commander.widgets.common import format_short_timestamp, format_timestamp, join_lines

_SEVERITY_STYLES: dict[str, str] = {
    "info": f"bold {SEVERITY_INFO}",
    "warning": f"bold {SEVERITY_WARNING}",
    "error": f"bold {SEVERITY_ERROR}",
}
_STATUS_STYLES: dict[AgentStatus, str] = {
    AgentStatus.RUNNING: f"bold {STATUS_RUNNING}",
    AgentStatus.IDLE: f"bold {STATUS_IDLE}",
    AgentStatus.WAITING_INPUT: f"bold {STATUS_WAITING_INPUT}",
    AgentStatus.BLOCKED: f"bold {STATUS_BLOCKED}",
    AgentStatus.ERROR: f"bold {STATUS_ERROR}",
    AgentStatus.DEAD: f"bold {STATUS_DEAD}",
    AgentStatus.COMPLETED: f"bold {STATUS_COMPLETED}",
    AgentStatus.DISCOVERED: f"bold {STATUS_DISCOVERED}",
    AgentStatus.STARTING: f"bold {STATUS_STARTING}",
    AgentStatus.UNKNOWN: f"bold {STATUS_UNKNOWN}",
}
_HEALTH_TONE_STYLES: dict[str, tuple[str, str]] = {
    "healthy": (f"{BADGE_FG} on {TONE_HEALTHY_BG}", f"bold {TONE_HEALTHY_FG}"),
    "warning": (f"{BADGE_FG} on {TONE_WARNING_BG}", f"bold {TONE_WARNING_FG}"),
    "critical": (f"{BADGE_FG} on {TONE_CRITICAL_BG}", f"bold {TONE_CRITICAL_FG}"),
}


class MetricStrip(Static):
    def set_metrics(self, metrics: Sequence[DashboardMetricView]) -> None:
        if not metrics:
            self.update(Text("NO METRICS", style=f"bold {FG4}"))
            return
        chips = Text()
        for index, metric in enumerate(metrics):
            if index:
                chips.append("  ")
            chips.append(
                f" {metric.label.upper()} ",
                style=f"bold {BADGE_FG} on {BADGE_BG}",
            )
            chips.append(f" {metric.value} ", style=f"bold {FG}")
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
            summary.append(f"{label} ", style=f"bold {FG4}")
            summary.append(str(value), style=f"bold {FG}")
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
                    style=FG4,
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
            header_style=f"bold {FG3}",
            border_style=BORDER,
            row_styles=(FG, FG1),
            pad_edge=False,
        )
        table.add_column("", width=2, no_wrap=True)
        table.add_column("name", min_width=10, max_width=20, overflow="ellipsis")
        table.add_column("status", min_width=8, max_width=12, overflow="ellipsis")
        table.add_column("branch", min_width=8, max_width=16, overflow="ellipsis")
        table.add_column("pane", width=5, no_wrap=True)
        table.add_column("idle", width=7, justify="right")
        table.add_column("info", min_width=10, overflow="ellipsis")
        for index, agent in enumerate(self._agents):
            is_selected = index == self._selected_index
            row_style = _row_style(agent, selected=is_selected)
            display_name = agent.repo_name or agent.worktree_name or agent.name
            info = agent.attention_reason or agent.task_title or ""
            table.add_row(
                Text(
                    _marker_text(agent, selected=is_selected),
                    style=_marker_style(agent, is_selected),
                ),
                Text(display_name, style=f"bold {FG}" if is_selected else FG),
                _status_text(agent.status),
                Text(agent.branch or "-", style=FG),
                Text(agent.pane_id, style=f"bold {BLUE}"),
                Text(_format_idle(agent.idle_seconds), style=FG),
                Text(info, style=f"{ORANGE}" if agent.needs_attention else FG4),
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
            f"REPO      {item.repo_name or '-'}",
            f"BRANCH    {item.branch or '-'}",
            f"WORKTREE  {item.worktree_path or '-'}",
            f"PANE      {item.pane_id}",
            f"TASK      {item.task_title or '-'}",
            f"SESSION   {agent.open_session_id or item.latest_session_id or '-'}",
            f"SESSIONS  {agent.session_count}",
            f"LAST EVT  {agent.latest_event_kind or '-'}",
            f"SEVERITY  {agent.latest_event_severity or '-'}",
            f"LAST SEEN {format_timestamp(item.last_seen_at)}",
            f"STARTED   {format_timestamp(item.started_at)}",
            f"IDLE      {_format_idle(item.idle_seconds)}",
            f"TOKENS    {item.token_total if item.token_total is not None else '-'}",
            f"COST USD  {_format_cost(item.estimated_cost_usd)}",
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
            line.append(f"{format_short_timestamp(alert.occurred_at)} ", style=FG4)
            line.append(f"{alert.severity.upper():<7}", style=_SEVERITY_STYLES[alert.severity])
            line.append(f" {alert.agent_name}: ", style=f"bold {FG}")
            line.append(alert.message, style=FG)
            lines.append(line)
        joined = Text()
        for index, line in enumerate(lines):
            if index:
                joined.append("\n")
            joined.append_text(line)
        self.update(joined)


def _format_idle(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60}m"


def _format_cost(value: str | None) -> str:
    if value is None:
        return "-"
    return f"${value}"


def _marker_style(agent: DashboardAgentListItemView, selected: bool) -> str:
    if selected:
        return f"bold {BLUE}"
    if agent.needs_attention:
        return f"bold {ORANGE}"
    return FG4


def _marker_text(agent: DashboardAgentListItemView, *, selected: bool) -> str:
    if selected:
        return ">"
    if agent.needs_attention:
        return "!"
    return " "


def _row_style(agent: DashboardAgentListItemView, *, selected: bool) -> str:
    if selected:
        return f"on {SELECTED_ROW_BG}"
    if agent.status in {AgentStatus.COMPLETED, AgentStatus.DEAD}:
        return "dim"
    if agent.needs_attention:
        return f"on {ATTENTION_ROW_BG}"
    return ""


def _status_text(status: AgentStatus) -> Text:
    label = status.value.replace("_", " ")
    return Text(label, style=_STATUS_STYLES.get(status, f"bold {FG}"))


__all__ = [
    "AgentDetailPanel",
    "AgentListPanel",
    "AlertPanel",
    "FilterBar",
    "HealthBanner",
    "LogPreviewPanel",
    "MetricStrip",
]
