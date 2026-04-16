from __future__ import annotations

import re
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
    PURPLE,
    SELECTED_ROW_BG,
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    TONE_CRITICAL_FG,
    TONE_HEALTHY_FG,
    TONE_WARNING_FG,
    YELLOW,
)
from copilot_commander.widgets.common import (
    format_short_timestamp,
    format_timestamp,
    status_glyph_parts,
)

# ── ANSI escape stripper ────────────────────────────────────────────
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


_SEVERITY_STYLES: dict[str, str] = {
    "info": f"bold {SEVERITY_INFO}",
    "warning": f"bold {SEVERITY_WARNING}",
    "error": f"bold {SEVERITY_ERROR}",
}

_HEALTH_TONE_STYLES: dict[str, tuple[str, str]] = {
    "healthy": ("▲ healthy", TONE_HEALTHY_FG),
    "warning": ("▬ review", TONE_WARNING_FG),
    "critical": ("▼ critical", TONE_CRITICAL_FG),
}

_LOG_SOURCE_STYLES: dict[str, str] = {
    "stdout": FG1,
    "stderr": f"bold {SEVERITY_ERROR}",
    "tmux": FG2,
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


def _section_header(text: Text, title: str) -> None:
    """Render a clean section header with box-drawing decoration."""
    text.append(" ── ", style=FG4)
    text.append(title.upper(), style=f"bold {FG3}")
    text.append(" ──────────────────────────────────────\n", style=FG4)


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
    """Compact 1-line dashboard summary — btop-inspired health strip."""

    def set_state(
        self,
        health: DashboardHealthSummary,
        metrics: Sequence[DashboardMetricView],
    ) -> None:
        metric_lookup = {metric.key: metric.value for metric in metrics}
        tone_label, tone_color = _HEALTH_TONE_STYLES[health.tone]
        line = Text()
        line.append(" ", style=FG4)
        line.append(tone_label, style=f"bold {tone_color}")
        counters: list[tuple[str, int | str, str]] = [
            ("agents", health.total_agents, FG1),
            ("active", health.active_agents, GREEN),
        ]
        if health.attention_agents:
            counters.append(("attn", health.attention_agents, ORANGE))
        if health.waiting_input_agents:
            counters.append(("wait", health.waiting_input_agents, ORANGE))
        if health.error_agents:
            counters.append(("err", health.error_agents, SEVERITY_ERROR))
        if health.blocked_agents:
            counters.append(("block", health.blocked_agents, SEVERITY_ERROR))
        for label, value, style in counters:
            line.append("  ", style=FG4)
            line.append(f"{label}:", style=FG4)
            line.append(str(value), style=f"bold {style}")
        if "tokens" in metric_lookup:
            line.append("  ", style=FG4)
            line.append("tok:", style=FG4)
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
    """Compact agent list — clean selection, status glyphs, activity hint."""

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
            empty = Text()
            empty.append("\n  no agents found\n", style=f"bold {FG3}")
            empty.append("  press ", style=FG4)
            empty.append("r", style=f"bold {BLUE}")
            empty.append(" to scan", style=FG4)
            self.update(empty)
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
            header_style=f"bold {FG4}",
            border_style=FG4,
            pad_edge=False,
            show_edge=False,
            show_header=True,
            padding=(0, 1, 0, 0),
        )
        table.add_column("", width=2, no_wrap=True)
        table.add_column("agent", min_width=10, no_wrap=True, ratio=3)
        table.add_column("status", width=10, no_wrap=True)
        table.add_column("idle", width=5, no_wrap=True, justify="right")
        table.add_column("branch", min_width=8, no_wrap=True, ratio=2, overflow="ellipsis")
        for index, agent in enumerate(self._agents):
            is_selected = index == self._selected_index
            row_style = _row_style(agent, selected=is_selected)
            display_name = _display_name(agent, self._agents)
            status_text, status_style = _status_display(agent)
            # Selection indicator: ▎ bar on left
            indicator = Text("▎ ", style=f"bold {BLUE}") if is_selected else Text("  ")
            br_style = PURPLE if is_selected else FG4
            table.add_row(
                indicator,
                Text(display_name, style=f"bold {FG}" if is_selected else FG2),
                Text(status_text, style=status_style),
                Text(_format_idle(agent.idle_seconds), style=FG4),
                Text(agent.branch or "─", style=br_style, overflow="ellipsis"),
                style=row_style,
            )
        return table


class AgentDetailPanel(Static):
    """Selected-agent focus section — grouped fields, compact header."""

    def set_agent(self, agent: DashboardSelectedAgentView | None) -> None:
        result = Text()
        _section_header(result, "agent detail")
        if agent is None:
            result.append("  no agent selected\n", style=FG4)
            self.update(result)
            return
        item = agent.item
        operator_status = _resolved_operator_status(item)
        _, status_style = _status_display(item)

        # ── header: name + status on one line ──
        glyph_char, glyph_color = status_glyph_parts(item.status)
        result.append(f"  {glyph_char} ", style=f"bold {glyph_color}")
        result.append(item.name, style=f"bold {FG}")
        result.append("  ")
        result.append(operator_status.headline, style=status_style)
        if item.task_title:
            result.append(f"  {item.task_title}", style=FG3)
        result.append("\n")

        # ── activity line (pulled from former ActivityPanel) ──
        activity = item.current_activity or operator_status.reason
        if activity:
            result.append("  ")
            result.append("» ", style=f"bold {AQUA}")
            result.append(activity, style=FG1)
            result.append("\n")

        result.append("\n")

        # ── identity fields ──
        _field_row(result, "branch", item.branch, PURPLE)
        _field_row(result, "repo", item.repo_name, FG2)
        _field_row(result, "worktree", item.worktree_name, FG2)

        # ── session fields ──
        session_display = _format_session(agent)
        if len(session_display) > 24:
            session_display = session_display[:20] + "…"
        _field_row(result, "session", session_display, FG4)
        copilot_id = agent.copilot_session_id or ""
        if copilot_id and len(copilot_id) > 16:
            copilot_id = copilot_id[:12] + "…"
        _field_row(result, "copilot", copilot_id, FG4)

        # ── timing fields ──
        _field_row(result, "uptime", _format_duration(item.started_at), FG2)
        _field_row(result, "idle", _format_idle(item.idle_seconds), FG2)

        # ── cost/tokens ──
        cost = _format_cost(item.estimated_cost_usd)
        if cost != "-":
            _field_row(result, "cost", cost, GREEN)
        if item.token_total is not None:
            _field_row(result, "tokens", str(item.token_total), AQUA)

        # ── sparkline ──
        pulse = item.sparkline if item.sparkline.strip() else ""
        if pulse:
            _field_row(result, "pulse", pulse, AQUA)

        # ── recent parsed events (deduplicated) ──
        if agent.recent_events:
            seen: set[str] = set()
            unique: list[str] = []
            for event in agent.recent_events:
                if event not in seen:
                    seen.add(event)
                    unique.append(event)
            result.append("\n")
            for event in unique[-3:]:
                result.append("  ")
                result.append(event, style=_event_color(event))
                result.append("\n")

        self.update(result)


def _field_row(text: Text, label: str, value: str | None, style: str) -> None:
    """Render a single labeled field row. Skip if value is empty."""
    if not value or value == "-":
        return
    text.append(f"  {label:<10}", style=FG4)
    text.append(f"{value}\n", style=style)


class FleetHealthPanel(Static):
    """Fleet-level counts — used by operations screen."""

    def set_state(
        self,
        health: DashboardHealthSummary,
        selected: DashboardSelectedAgentView | None,
    ) -> None:
        result = Text()
        _section_header(result, "fleet")
        tone_label, tone_color = _HEALTH_TONE_STYLES[health.tone]
        result.append("  health   ", style=FG4)
        result.append(f"{tone_label}\n", style=f"bold {tone_color}")
        for label, value, style in (
            ("agents", f"{health.total_agents} total / {health.active_agents} active", FG1),
            ("attention", str(health.attention_agents), ORANGE),
            ("waiting", str(health.waiting_input_agents), ORANGE),
            ("blocked", str(health.blocked_agents), SEVERITY_ERROR),
            ("errors", str(health.error_agents), SEVERITY_ERROR),
        ):
            result.append(f"  {label:<9}", style=FG4)
            result.append(f"{value}\n", style=f"bold {style}" if label != "agents" else style)
        if selected is not None:
            status_label, status_style = _status_display(selected.item)
            result.append("  selected ", style=FG4)
            result.append(selected.item.name, style=f"bold {FG}")
            result.append("  ")
            result.append(status_label, style=status_style)
        self.update(result)


class ActivityPanel(Static):
    """Selected-agent activity — used by operations screen."""

    def set_agent(self, agent: DashboardSelectedAgentView | None) -> None:
        result = Text()
        _section_header(result, "activity")
        if agent is None:
            result.append("  no activity available", style=FG4)
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
            result.append(f"  {label:<8}", style=FG4)
            result.append(f"{value}\n", style=style)
        if not agent.recent_events:
            result.append("  recent   no parsed activity markers", style=FG4)
            self.update(result)
            return
        result.append("  recent\n", style=FG4)
        for event in agent.recent_events[-6:]:
            result.append("  ")
            result.append(event, style=_event_color(event))
            result.append("\n")
        self.update(result)


class LogPreviewPanel(Static):
    """Recent pane output — ANSI-stripped, timestamp-grouped, syntax-highlighted."""

    def set_logs(self, agent: DashboardSelectedAgentView | None) -> None:
        result = Text()
        _section_header(result, "output")
        if agent is None or not agent.log_preview:
            result.append("  no recent output\n", style=FG4)
            self.update(result)
            return
        last_ts = ""
        src_map = {"stdout": "out", "stderr": "err", "tmux": "tmx"}
        for line in agent.log_preview:
            ts = format_short_timestamp(line.captured_at)
            src = src_map.get(line.source, line.source[:3])
            source_style = _LOG_SOURCE_STYLES.get(line.source, FG2)
            content = _strip_ansi(line.content)
            # Timestamp grouping: show only when time changes
            if ts != last_ts:
                result.append(f"  {ts} ", style=FG4)
                last_ts = ts
            else:
                result.append("           ", style=FG4)
            result.append(f"{src:<3} ", style=source_style)
            # Light syntax highlighting for log content
            result.append(_highlight_log_line(content, source_style))
            result.append("\n")
        self.update(result)


def _highlight_log_line(content: str, default_style: str) -> Text:
    """Apply light syntax highlighting to log line content."""
    text = Text()
    lower = content.lower()
    if any(kw in lower for kw in ("error", "fail", "traceback", "exception")):
        text.append(content, style=f"bold {SEVERITY_ERROR}")
    elif any(kw in lower for kw in ("warning", "warn", "deprecat")):
        text.append(content, style=YELLOW)
    elif content.startswith(("●", "✓", "✗", "│", "└", "├")):
        # Tool calls / tree output
        text.append(content, style=AQUA)
    elif content.startswith(("$", ">", "λ")):
        # Command prompts
        text.append(content, style=f"bold {GREEN}")
    else:
        text.append(content, style=default_style)
    return text


class AlertPanel(Static):
    """Active attention items — compact severity badges."""

    def set_alerts(self, alerts: Sequence[DashboardAlertView]) -> None:
        joined = Text()
        _section_header(joined, "alerts")
        if not alerts:
            joined.append("  no active alerts\n", style=FG4)
            self.update(joined)
            return
        for alert in alerts[:5]:
            short_sev = alert.severity[:4].upper()
            joined.append("  ")
            joined.append(f"{short_sev:<4}", style=_SEVERITY_STYLES.get(alert.severity, FG3))
            joined.append(f" {alert.agent_name}", style=f"bold {FG1}")
            joined.append(f"  {alert.message}\n", style=FG3)
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
