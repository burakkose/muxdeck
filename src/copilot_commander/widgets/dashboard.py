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
    AQUA,
    ATTENTION_ROW_BG,
    BLUE,
    BORDER,
    FG,
    FG1,
    FG3,
    FG4,
    GREEN,
    ORANGE,
    SELECTED_ROW_BG,
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    YELLOW,
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


def _event_color(event: str) -> str:
    """Pick a Gruvbox color based on event emoji prefix."""
    if event.startswith(("📖", "✏️", "🔍")):
        return AQUA
    if event.startswith("⚡"):
        return GREEN
    if event.startswith(("💭", "🔧")):
        return YELLOW
    if event.startswith("⚠"):
        return ORANGE
    return FG


_SHORT_STATUS: dict[AgentStatus, str] = {
    AgentStatus.RUNNING: "run",
    AgentStatus.IDLE: "idle",
    AgentStatus.WAITING_INPUT: "wait",
    AgentStatus.BLOCKED: "blk",
    AgentStatus.ERROR: "err",
    AgentStatus.DEAD: "dead",
    AgentStatus.COMPLETED: "done",
    AgentStatus.DISCOVERED: "disc",
    AgentStatus.STARTING: "init",
    AgentStatus.UNKNOWN: "?",
}


def _short_status(status: AgentStatus) -> str:
    return _SHORT_STATUS.get(status, "?")


def _display_name(
    agent: DashboardAgentListItemView,
    all_agents: tuple[DashboardAgentListItemView, ...],
) -> str:
    """Pick a unique, human-readable display name for the agent list.

    Prefer the process name (e.g. Planner, Reviewer). If multiple agents
    share the same name, disambiguate with the repo or worktree name.
    """
    name = agent.name
    duplicates = sum(1 for a in all_agents if a.name == name)
    if duplicates <= 1:
        return name
    suffix = agent.worktree_name or agent.repo_name
    if suffix and suffix != name:
        return f"{name}/{suffix}"
    return name


class StatusBar(Static):
    """Compact 1-line bar: subtle inline metrics, no colored background."""

    def set_state(
        self,
        health: DashboardHealthSummary,
        metrics: Sequence[DashboardMetricView],
    ) -> None:
        line = Text()
        for i, metric in enumerate(metrics):
            if i:
                line.append(" · ", style=FG4)
            line.append(f"{metric.label.lower()} ", style=FG4)
            line.append(str(metric.value), style=f"bold {FG}")
        if health.attention_agents:
            line.append(" · ", style=FG4)
            line.append(
                f"{health.attention_agents} need attention",
                style=f"bold {ORANGE}",
            )
        self.update(line)


class FilterBar(Vertical):
    def compose(self) -> ComposeResult:
        yield Input(placeholder="/ filter", id="dashboard-filter-input")

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
            show_header=False,
        )
        table.add_column("", width=1, no_wrap=True)
        table.add_column("name", min_width=6, no_wrap=True, ratio=2)
        table.add_column("st", width=4, no_wrap=True)
        table.add_column("activity", width=10, no_wrap=True)
        table.add_column("branch", min_width=4, no_wrap=True, ratio=2, overflow="ellipsis")
        for index, agent in enumerate(self._agents):
            is_selected = index == self._selected_index
            row_style = _row_style(agent, selected=is_selected)
            display_name = _display_name(agent, self._agents)
            status_text = _short_status(agent.status)
            status_style = FG3
            if agent.is_potentially_stuck:
                status_text = "⚠stk"
                status_style = YELLOW
            table.add_row(
                status_glyph(agent.status, selected=is_selected),
                Text(display_name, style=f"bold {FG}" if is_selected else FG),
                Text(status_text, style=status_style),
                Text(agent.sparkline, style=AQUA),
                Text(agent.branch or "-", style=FG1, overflow="ellipsis"),
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
        fields: list[tuple[str, str, str]] = [
            ("name", item.name, f"bold {FG}"),
            ("task", item.task_title or "", FG),
            ("status", item.status.value, FG1),
            ("branch", item.branch or "", FG1),
            ("pane", item.pane_id, f"bold {BLUE}"),
            ("repo", item.repo_name or "", FG1),
            ("idle", _format_idle(item.idle_seconds), FG1),
            ("tokens", str(item.token_total) if item.token_total is not None else "", FG1),
            ("cost", _format_cost(item.estimated_cost_usd), FG1),
            ("session", agent.open_session_id or item.latest_session_id or "", FG4),
            ("seen", format_timestamp(item.last_seen_at), FG4),
        ]
        if item.needs_attention and item.attention_reason:
            fields.append(("attn", item.attention_reason, f"bold {ORANGE}"))
        lines: list[Text] = []
        for label, value, style in fields:
            if not value or value == "-":
                continue
            line = Text()
            line.append(f"{label:<7} ", style=FG4)
            line.append(value, style=style)
            lines.append(line)
        # Recent events section
        if agent.recent_events:
            lines.append(Text())  # blank separator
            header = Text()
            header.append("─── recent events ", style=FG4)
            lines.append(header)
            for event in agent.recent_events:
                eline = Text()
                color = _event_color(event)
                eline.append(f"  {event}", style=color)
                lines.append(eline)
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
            self.update(Text("—", style=FG4))
            return
        src_map = {"stdout": "out", "stderr": "err"}
        lines: list[str] = []
        for line in agent.log_preview:
            ts = format_short_timestamp(line.captured_at)
            src = src_map.get(line.source, line.source[:3])
            lines.append(f"{ts} {src:<3} {line.content}")
        self.update(join_lines(lines))


class AlertPanel(Static):
    """Alert list with border-title."""

    def on_mount(self) -> None:
        self.border_title = "Alerts"

    def set_alerts(self, alerts: Sequence[DashboardAlertView]) -> None:
        if not alerts:
            self.update(Text("—", style=FG4))
            return
        lines: list[Text] = []
        for alert in alerts:
            line = Text()
            line.append(f"{format_short_timestamp(alert.occurred_at)} ", style=FG4)
            short_sev = alert.severity[:4]
            line.append(f"{short_sev:<4}", style=_SEVERITY_STYLES[alert.severity])
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
    try:
        return f"${float(value):.2f}"
    except (ValueError, TypeError):
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
