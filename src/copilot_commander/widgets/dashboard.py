from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

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
from copilot_commander.services.operator_status_service import (
    OperatorStatus,
    OperatorStatusKind,
    describe_operator_status,
)
from copilot_commander.theme import (
    AQUA,
    ATTENTION_ROW_BG,
    BLUE,
    FG,
    FG1,
    FG2,
    FG3,
    FG4,
    GREEN,
    ORANGE,
    SELECTED_ROW_BG,
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    TONE_CRITICAL_FG,
    TONE_HEALTHY_FG,
    TONE_WARNING_FG,
    YELLOW,
)
from copilot_commander.widgets.common import format_short_timestamp, format_timestamp, status_glyph

_SEVERITY_STYLES: dict[str, str] = {
    "info": f"bold {SEVERITY_INFO}",
    "warning": f"bold {SEVERITY_WARNING}",
    "error": f"bold {SEVERITY_ERROR}",
}

_HEALTH_TONE_STYLES: dict[str, tuple[str, str]] = {
    "healthy": ("healthy", TONE_HEALTHY_FG),
    "warning": ("review", TONE_WARNING_FG),
    "critical": ("critical", TONE_CRITICAL_FG),
}

_LOG_SOURCE_STYLES: dict[str, str] = {
    "stdout": FG1,
    "stderr": f"bold {SEVERITY_ERROR}",
}


def _event_color(event: str) -> str:
    """Pick a color based on event emoji prefix."""
    if event.startswith(("📖", "✏️", "🔍")):
        return AQUA
    if event.startswith("⚡"):
        return GREEN
    if event.startswith(("💭", "🔧")):
        return YELLOW
    if event.startswith("⚠"):
        return ORANGE
    return FG


def _append_section_title(text: Text, title: str) -> None:
    text.append(f" {title}\n", style=f"bold {BLUE}")


_SHORT_STATUS: dict[AgentStatus, str] = {
    AgentStatus.RUNNING: "run",
    AgentStatus.IDLE: "idle",
    AgentStatus.WAITING_INPUT: "input",
    AgentStatus.BLOCKED: "block",
    AgentStatus.ERROR: "error",
    AgentStatus.DEAD: "dead",
    AgentStatus.COMPLETED: "done",
    AgentStatus.DISCOVERED: "disc",
    AgentStatus.STARTING: "start",
    AgentStatus.UNKNOWN: "?",
}


def _short_status(status: AgentStatus) -> str:
    return _SHORT_STATUS.get(status, "?")


def _status_display(agent: DashboardAgentListItemView) -> tuple[str, str]:
    operator_status = _resolved_operator_status(agent)
    style_lookup = {
        OperatorStatusKind.WORKING: FG3,
        OperatorStatusKind.WAITING_INPUT: f"bold {ORANGE}",
        OperatorStatusKind.BLOCKED: f"bold {SEVERITY_ERROR}",
        OperatorStatusKind.REVIEW_READY: f"bold {ORANGE}",
        OperatorStatusKind.FAILED: f"bold {SEVERITY_ERROR}",
        OperatorStatusKind.STALE: f"bold {YELLOW}",
        OperatorStatusKind.COMPLETED: FG4,
    }
    return (operator_status.label, style_lookup[operator_status.kind])


def _resolved_operator_status(agent: DashboardAgentListItemView) -> OperatorStatus:
    if agent.operator_status is not None:
        return agent.operator_status
    return describe_operator_status(
        agent_status=agent.status,
        needs_attention=agent.needs_attention,
        attention_reason=agent.attention_reason,
        idle_seconds=agent.idle_seconds,
        is_potentially_stuck=agent.is_potentially_stuck,
        task_title=agent.task_title,
        current_activity=agent.current_activity,
    )


def _display_name(
    agent: DashboardAgentListItemView,
    all_agents: tuple[DashboardAgentListItemView, ...],
) -> str:
    """Pick a unique, human-readable display name for the agent list."""
    name = agent.name
    duplicates = tuple(a for a in all_agents if a.name == name)
    if len(duplicates) <= 1:
        return name
    for values, suffix in (
        (tuple(item.worktree_name for item in duplicates), agent.worktree_name),
        (tuple(item.repo_name for item in duplicates), agent.repo_name),
        (tuple(item.window_name for item in duplicates), agent.window_name),
        (tuple(item.pane_id for item in duplicates), agent.pane_id),
    ):
        if suffix is None or suffix == name:
            continue
        if values.count(suffix) == 1:
            return f"{name}/{suffix}"
    return name


def _humanize_event_kind(kind: str | None) -> str:
    if kind is None:
        return "-"
    return kind.replace("_", " ")


def _format_idle(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60}m"


def _format_duration(started_at: datetime) -> str:
    """Format agent uptime from start to now."""
    now = datetime.now(UTC)
    delta = now - started_at
    total_secs = int(delta.total_seconds())
    if total_secs < 0:
        return "-"
    if total_secs < 60:
        return f"{total_secs}s"
    if total_secs < 3600:
        return f"{total_secs // 60}m {total_secs % 60}s"
    hours = total_secs // 3600
    minutes = (total_secs % 3600) // 60
    return f"{hours}h {minutes}m"


def _format_cost(value: str | None) -> str:
    if value is None:
        return "-"
    try:
        return f"${float(value):.2f}"
    except (ValueError, TypeError):
        return f"${value}"


def _format_session(agent: DashboardSelectedAgentView) -> str:
    session = agent.open_session_id or agent.item.latest_session_id
    if session is None:
        return "-"
    if agent.session_count <= 1:
        return session
    return f"{session} ({agent.session_count} total)"


def _row_style(agent: DashboardAgentListItemView, *, selected: bool) -> str:
    if selected:
        return f"on {SELECTED_ROW_BG}"
    if agent.status in {AgentStatus.COMPLETED, AgentStatus.DEAD}:
        return "dim"
    if agent.needs_attention:
        return f"on {ATTENTION_ROW_BG}"
    return ""


class StatusBar(Static):
    """Compact 1-line dashboard summary with explicit health counts."""

    def set_state(
        self,
        health: DashboardHealthSummary,
        metrics: Sequence[DashboardMetricView],
    ) -> None:
        metric_lookup = {metric.key: metric.value for metric in metrics}
        tone_label, tone_color = _HEALTH_TONE_STYLES[health.tone]
        line = Text()
        line.append(" health ", style=FG4)
        line.append(tone_label, style=f"bold {tone_color}")
        line.append(f" {health.message}", style=FG2)
        summary_items = (
            ("agents", metric_lookup.get("agents", health.total_agents), FG1),
            ("active", metric_lookup.get("active", health.active_agents), FG1),
            ("attention", health.attention_agents, ORANGE),
            ("waiting", health.waiting_input_agents, ORANGE),
            ("blocked", health.blocked_agents, SEVERITY_ERROR),
            ("errors", health.error_agents, SEVERITY_ERROR),
        )
        for label, value, style in summary_items:
            if label not in {"agents", "active"} and value == 0:
                continue
            line.append("  │  ", style=FG4)
            line.append(f"{label} ", style=FG3)
            line.append(str(value), style=f"bold {style}")
        if "tokens" in metric_lookup:
            line.append("  │  ", style=FG4)
            line.append("tokens ", style=FG3)
            line.append(str(metric_lookup["tokens"]), style=f"bold {AQUA}")
        self.update(line)


class FilterBar(Vertical):
    def compose(self) -> ComposeResult:
        yield Input(placeholder="/ filter", id="dashboard-filter-input")

    def set_query(self, value: str | None) -> None:
        self.query_one(Input).value = value or ""

    def focus_input(self) -> None:
        self.query_one(Input).focus()


class AgentListPanel(Static, can_focus=True):
    """Compact agent table with clear state, idle time, and branch columns."""

    class AgentSelected(Message):
        def __init__(self, agent_id: str) -> None:
            super().__init__()
            self.agent_id = agent_id

    def __init__(self, *, widget_id: str | None = None, classes: str | None = None) -> None:
        super().__init__(id=widget_id, classes=classes)
        self._agents: tuple[DashboardAgentListItemView, ...] = ()
        self._selected_index = 0

    def on_mount(self) -> None:
        pass

    def set_agents(
        self,
        agents: Sequence[DashboardAgentListItemView],
        *,
        selected_agent_id: str | None,
    ) -> None:
        self._agents = tuple(agents)
        if not self._agents:
            self._selected_index = 0
            self.update(Text(" no agents found · press r to scan", style=FG4))
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
            box=None,
            header_style=f"bold {FG3}",
            border_style=FG4,
            pad_edge=False,
            show_edge=False,
            show_header=True,
            padding=(0, 1, 0, 0),
        )
        table.add_column("", width=1, no_wrap=True)
        table.add_column("name", min_width=8, no_wrap=True, ratio=2)
        table.add_column("state", width=8, no_wrap=True)
        table.add_column("idle", width=6, no_wrap=True)
        table.add_column("branch", min_width=6, no_wrap=True, ratio=2, overflow="ellipsis")
        for index, agent in enumerate(self._agents):
            is_selected = index == self._selected_index
            row_style = _row_style(agent, selected=is_selected)
            display_name = _display_name(agent, self._agents)
            status_text, status_style = _status_display(agent)
            table.add_row(
                status_glyph(agent.status, selected=is_selected),
                Text(display_name, style=f"bold {FG}" if is_selected else FG),
                Text(status_text, style=status_style),
                Text(_format_idle(agent.idle_seconds), style=FG2),
                Text(agent.branch or "-", style=FG1, overflow="ellipsis"),
                style=row_style,
            )
        return table


class AgentDetailPanel(Static):
    """Selected-agent focus section."""

    def set_agent(self, agent: DashboardSelectedAgentView | None) -> None:
        result = Text()
        _append_section_title(result, "focus")
        if agent is None:
            result.append(" no agent selected", style=FG4)
            self.update(result)
            return
        item = agent.item
        operator_status = _resolved_operator_status(item)
        _, status_style = _status_display(item)
        result.append(f" {item.name}", style=f"bold {FG}")
        result.append("  ")
        result.append(operator_status.headline, style=status_style)
        result.append("\n")
        if item.task_title:
            result.append(f" {item.task_title}\n", style=FG2)
        fields: list[tuple[str, str, str]] = [
            ("reason", operator_status.reason, status_style),
            ("activity", item.current_activity or "", FG1),
            ("branch", item.branch or "", FG1),
            ("repo", item.repo_name or "", FG2),
            ("worktree", item.worktree_name or "", FG2),
            ("window", item.window_name or "", FG2),
            ("pane", item.pane_id, BLUE),
            ("session", _format_session(agent), FG4),
            ("copilot", agent.copilot_session_id or "", FG4),
            ("duration", _format_duration(item.started_at), FG2),
            ("idle", _format_idle(item.idle_seconds), FG2),
            ("pulse", item.sparkline if item.sparkline.strip() else "-", AQUA),
            ("seen", format_timestamp(item.last_seen_at), FG4),
            ("event", _humanize_event_kind(agent.latest_event_kind), FG4),
            ("level", agent.latest_event_severity or "", FG4),
            ("cost", _format_cost(item.estimated_cost_usd), FG2),
            ("tokens", str(item.token_total) if item.token_total is not None else "", FG2),
        ]
        for label, value, style in fields:
            if not value or value == "-":
                continue
            result.append(f" {label:<9}", style=FG4)
            result.append(f"{value}\n", style=style)
        self.update(result)


class FleetHealthPanel(Static):
    """Fleet-level counts, similar to a small btop summary box."""

    def set_state(
        self,
        health: DashboardHealthSummary,
        selected: DashboardSelectedAgentView | None,
    ) -> None:
        result = Text()
        _append_section_title(result, "fleet")
        tone_label, tone_color = _HEALTH_TONE_STYLES[health.tone]
        result.append(" health   ", style=FG4)
        result.append(f"{tone_label} {health.message}\n", style=f"bold {tone_color}")
        for label, value, style in (
            ("agents", f"{health.total_agents} total / {health.active_agents} active", FG1),
            ("attention", str(health.attention_agents), ORANGE),
            ("waiting", str(health.waiting_input_agents), ORANGE),
            ("blocked", str(health.blocked_agents), SEVERITY_ERROR),
            ("errors", str(health.error_agents), SEVERITY_ERROR),
        ):
            result.append(f" {label:<9}", style=FG4)
            if label == "agents":
                result.append(f"{value}\n", style=style)
            else:
                result.append(f"{value}\n", style=f"bold {style}")
        if selected is not None:
            status_label, status_style = _status_display(selected.item)
            result.append(" selected ", style=FG4)
            result.append(selected.item.name, style=f"bold {FG}")
            result.append("  ")
            result.append(status_label, style=status_style)
        self.update(result)


class ActivityPanel(Static):
    """Selected-agent activity and parsed recent markers."""

    def set_agent(self, agent: DashboardSelectedAgentView | None) -> None:
        result = Text()
        _append_section_title(result, "activity")
        if agent is None:
            result.append(" no activity available", style=FG4)
            self.update(result)
            return
        item = agent.item
        activity = item.current_activity or item.task_title or "-"
        pulse = item.sparkline if item.sparkline.strip() else "-"
        for label, value, style in (
            ("current", activity, FG1),
            ("pulse", pulse, AQUA),
            ("event", _humanize_event_kind(agent.latest_event_kind), FG2),
            ("output", format_timestamp(item.last_log_at), FG4),
        ):
            if not value or value == "-":
                continue
            result.append(f" {label:<8}", style=FG4)
            result.append(f"{value}\n", style=style)
        if not agent.recent_events:
            result.append(" recent   no parsed activity markers", style=FG4)
            self.update(result)
            return
        result.append(" recent\n", style=FG4)
        for event in agent.recent_events[-6:]:
            result.append(" ")
            result.append(event, style=_event_color(event))
            result.append("\n")
        self.update(result)


class LogPreviewPanel(Static):
    """Recent pane output, promoted to the primary detail panel."""

    def set_logs(self, agent: DashboardSelectedAgentView | None) -> None:
        result = Text()
        _append_section_title(result, "output")
        if agent is None or not agent.log_preview:
            result.append(" no recent output", style=FG4)
            self.update(result)
            return
        src_map = {"stdout": "out", "stderr": "err"}
        for index, line in enumerate(agent.log_preview):
            if index:
                result.append("\n")
            ts = format_short_timestamp(line.captured_at)
            src = src_map.get(line.source, line.source[:3])
            source_style = _LOG_SOURCE_STYLES.get(line.source, FG2)
            result.append(f" {ts} ", style=FG4)
            result.append(f"{src:<3}", style=source_style)
            result.append(" ")
            result.append(line.content, style=source_style)
        self.update(result)


class AlertPanel(Static):
    """Active attention items."""

    def set_alerts(self, alerts: Sequence[DashboardAlertView]) -> None:
        joined = Text()
        _append_section_title(joined, "attention")
        if not alerts:
            joined.append(" no active alerts", style=FG4)
            self.update(joined)
            return
        for index, alert in enumerate(alerts[:6]):
            if index:
                joined.append("\n")
            joined.append(f" {format_short_timestamp(alert.occurred_at)} ", style=FG4)
            short_sev = alert.severity[:4]
            joined.append(f"{short_sev:<4}", style=_SEVERITY_STYLES[alert.severity])
            joined.append(f" {alert.agent_name}: ", style=f"bold {FG1}")
            joined.append(alert.message, style=FG2)
        self.update(joined)


__all__ = [
    "ActivityPanel",
    "AgentDetailPanel",
    "AgentListPanel",
    "AlertPanel",
    "FilterBar",
    "FleetHealthPanel",
    "LogPreviewPanel",
    "StatusBar",
]
