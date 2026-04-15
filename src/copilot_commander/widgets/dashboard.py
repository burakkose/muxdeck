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
    TONE_CRITICAL_BG,
    TONE_CRITICAL_FG,
    TONE_HEALTHY_BG,
    TONE_HEALTHY_FG,
    TONE_WARNING_BG,
    TONE_WARNING_FG,
)
from copilot_commander.widgets.common import (
    format_short_timestamp,
    format_timestamp,
    join_lines,
    status_glyph,
)

_SEVERITY_STYLES: dict[str, str] = {
    "info": f"bold {SEVERITY_INFO}",
    "warning": f"bold {SEVERITY_WARNING}",
    "error": f"bold {SEVERITY_ERROR}",
}
_HEALTH_TONE_STYLES: dict[str, tuple[str, str]] = {
    "healthy": (f"{BADGE_FG} on {TONE_HEALTHY_BG}", f"bold {TONE_HEALTHY_FG}"),
    "warning": (f"{BADGE_FG} on {TONE_WARNING_BG}", f"bold {TONE_WARNING_FG}"),
    "critical": (f"{BADGE_FG} on {TONE_CRITICAL_BG}", f"bold {TONE_CRITICAL_FG}"),
}


class StatusBar(Static):
    """Compact 1-line bar showing health + key metrics."""

    def set_state(
        self,
        health: DashboardHealthSummary,
        metrics: Sequence[DashboardMetricView],
    ) -> None:
        self.remove_class("tone-healthy", "tone-warning", "tone-critical")
        self.add_class(f"tone-{health.tone}")
        badge_style, _value_style = _HEALTH_TONE_STYLES[health.tone]
        line = Text()
        line.append(f" {health.message.upper()} ", style=badge_style)
        for metric in metrics:
            line.append("  ")
            line.append(f"{metric.label.lower()} ", style=FG4)
            line.append(str(metric.value), style=f"bold {FG}")
        self.update(line)


class FilterBar(Vertical):
    def compose(self) -> ComposeResult:
        yield Input(placeholder="/ filter agents", id="dashboard-filter-input")

    def set_query(self, value: str | None) -> None:
        self.query_one(Input).value = value or ""

    def focus_input(self) -> None:
        self.query_one(Input).focus()


class AgentListPanel(Static, can_focus=True):
    """Compact agent table with border-title. Widget IS the panel."""

    class AgentSelected(Message):
        def __init__(self, agent_id: str) -> None:
            super().__init__()
            self.agent_id = agent_id

    def __init__(self, *, widget_id: str | None = None, classes: str | None = None) -> None:
        super().__init__(id=widget_id, classes=classes)
        self._agents: tuple[DashboardAgentListItemView, ...] = ()
        self._selected_index = 0

    def on_mount(self) -> None:
        self.border_title = "Agents"

    def set_agents(
        self,
        agents: Sequence[DashboardAgentListItemView],
        *,
        selected_agent_id: str | None,
    ) -> None:
        self._agents = tuple(agents)
        self.border_title = f"Agents ({len(self._agents)})"
        if not self._agents:
            self._selected_index = 0
            self.update(Text("No agents discovered — press r to scan", style=FG4))
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
            box=box.SIMPLE,
            header_style=f"bold {FG3}",
            border_style=BORDER,
            pad_edge=False,
            show_edge=False,
        )
        table.add_column("", width=1, no_wrap=True)
        table.add_column("name", min_width=8, max_width=18, overflow="ellipsis")
        table.add_column("status", min_width=6, max_width=10, overflow="ellipsis")
        table.add_column("branch", min_width=6, max_width=14, overflow="ellipsis")
        table.add_column("pane", width=4, no_wrap=True)
        table.add_column("idle", width=5, justify="right")
        table.add_column("info", min_width=8, overflow="ellipsis")
        for index, agent in enumerate(self._agents):
            is_selected = index == self._selected_index
            row_style = _row_style(agent, selected=is_selected)
            display_name = agent.repo_name or agent.worktree_name or agent.name
            info = agent.attention_reason or agent.task_title or ""
            status_label = agent.status.value.replace("_", " ")
            table.add_row(
                status_glyph(agent.status, selected=is_selected),
                Text(display_name, style=f"bold {FG}" if is_selected else FG),
                Text(status_label, style=FG3),
                Text(agent.branch or "-", style=FG1),
                Text(agent.pane_id, style=f"bold {BLUE}"),
                Text(_format_idle(agent.idle_seconds), style=FG4),
                Text(info, style=f"{ORANGE}" if agent.needs_attention else FG4),
                style=row_style,
            )
        return table


class AgentDetailPanel(Static):
    """Selected agent detail with border-title."""

    def on_mount(self) -> None:
        self.border_title = "Detail"

    def set_agent(self, agent: DashboardSelectedAgentView | None) -> None:
        if agent is None:
            self.update(Text("No agent selected", style=FG4))
            return
        item = agent.item
        lines: list[Text] = []
        for label, value in (
            ("name", item.name),
            ("status", item.status.value),
            ("repo", item.repo_name or "-"),
            ("branch", item.branch or "-"),
            ("pane", item.pane_id),
            ("task", item.task_title or "-"),
            ("session", agent.open_session_id or item.latest_session_id or "-"),
            ("sessions", str(agent.session_count)),
            ("seen", format_timestamp(item.last_seen_at)),
            ("started", format_timestamp(item.started_at)),
            ("idle", _format_idle(item.idle_seconds)),
            ("tokens", str(item.token_total) if item.token_total is not None else "-"),
            ("cost", _format_cost(item.estimated_cost_usd)),
            ("attn", item.attention_reason or "-"),
        ):
            line = Text()
            line.append(f"{label:<9}", style=FG4)
            line.append(str(value), style=FG)
            lines.append(line)
        result = Text()
        for i, line in enumerate(lines):
            if i:
                result.append("\n")
            result.append_text(line)
        self.update(result)


class LogPreviewPanel(Static):
    """Log tail with border-title."""

    def on_mount(self) -> None:
        self.border_title = "Log"

    def set_logs(self, agent: DashboardSelectedAgentView | None) -> None:
        if agent is None or not agent.log_preview:
            self.update(Text("No recent logs", style=FG4))
            return
        lines = [
            f"{format_short_timestamp(line.captured_at)} {line.source:<8.8} {line.content}"
            for line in agent.log_preview
        ]
        self.update(join_lines(lines))


class AlertPanel(Static):
    """Alert list with border-title."""

    def on_mount(self) -> None:
        self.border_title = "Alerts"

    def set_alerts(self, alerts: Sequence[DashboardAlertView]) -> None:
        if not alerts:
            self.update(Text("No active alerts", style=FG4))
            return
        lines: list[Text] = []
        for alert in alerts:
            line = Text()
            line.append(f"{format_short_timestamp(alert.occurred_at)} ", style=FG4)
            line.append(f"{alert.severity.upper():<5}", style=_SEVERITY_STYLES[alert.severity])
            line.append(f" {alert.agent_name}: ", style=f"bold {FG}")
            line.append(alert.message, style=FG1)
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


def _row_style(agent: DashboardAgentListItemView, *, selected: bool) -> str:
    if selected:
        return f"on {SELECTED_ROW_BG}"
    if agent.status in {AgentStatus.COMPLETED, AgentStatus.DEAD}:
        return "dim"
    if agent.needs_attention:
        return f"on {ATTENTION_ROW_BG}"
    return ""


__all__ = [
    "AgentDetailPanel",
    "AgentListPanel",
    "AlertPanel",
    "FilterBar",
    "LogPreviewPanel",
    "StatusBar",
]
