from __future__ import annotations

import contextlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from rich.table import Table
from rich.text import Text
from textual._context import NoActiveAppError
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
    DashboardSubAgentTreeView,
    DashboardSubAgentView,
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


@dataclass(frozen=True, slots=True)
class _AgentRow:
    agent: DashboardAgentListItemView

    @property
    def parent_agent_id(self) -> str:
        return self.agent.agent_id


@dataclass(frozen=True, slots=True)
class _SubAgentHeaderRow:
    parent_agent_id: str
    count: int = 0
    loading: bool = False


@dataclass(frozen=True, slots=True)
class _SubAgentRow:
    parent_agent_id: str
    subagent: DashboardSubAgentView


_Row = _AgentRow | _SubAgentHeaderRow | _SubAgentRow


class AgentListPanel(Static, can_focus=True):
    """Compact agent list — clean selection, status glyphs, activity hint.

    Supports per-row expand/collapse for showing a parent agent's
    **active** sub-agent tree. Sub-agent rows are fully navigable:
    the cursor steps over them, and the right-hand pane still shows
    the owning parent agent's detail (sub-agents have no pane of their
    own).
    """

    class AgentSelected(Message):
        def __init__(self, agent_id: str) -> None:
            super().__init__()
            self.agent_id = agent_id

    class SubAgentHighlighted(Message):
        """Emitted when the cursor lands on (or leaves) a sub-agent row.

        ``subagent`` is ``None`` when the cursor is on a non-subagent
        row — the screen should show the regular agent detail in that
        case. When present, the screen should render the sub-agent
        detail (prompt, result, etc.) instead.
        """

        def __init__(self, subagent: DashboardSubAgentView | None) -> None:
            super().__init__()
            self.subagent = subagent

    class ExpandRequested(Message):
        """Emitted when the user expands a row whose sub-agent tree

        hasn't been loaded yet. The screen is responsible for kicking
        off a worker-thread load and feeding the result back via
        :meth:`set_subagents`.
        """

        def __init__(self, agent_id: str) -> None:
            super().__init__()
            self.agent_id = agent_id

    def __init__(self, *, widget_id: str | None = None, classes: str | None = None) -> None:
        super().__init__(id=widget_id, classes=classes)
        self._agents: tuple[DashboardAgentListItemView, ...] = ()
        # ``_selected_index`` indexes into ``_rows`` (all visible rows,
        # including sub-agent child rows), not into ``_agents``. The
        # parent agent id is derived via :attr:`selected_agent_id`.
        self._selected_index = 0
        self._expanded: set[str] = set()
        self._subagents: dict[str, DashboardSubAgentTreeView] = {}
        self._loading: set[str] = set()
        self._rows: tuple[_Row, ...] = ()

    def on_mount(self) -> None:
        pass

    def set_agents(
        self,
        agents: Sequence[DashboardAgentListItemView],
        *,
        selected_agent_id: str | None,
    ) -> None:
        self._agents = tuple(agents)
        live_ids = {agent.agent_id for agent in self._agents}
        self._expanded &= live_ids
        self._loading &= live_ids
        for stale in set(self._subagents) - live_ids:
            self._subagents.pop(stale, None)

        if not self._agents:
            self._selected_index = 0
            self._rows = ()
            empty = Text()
            empty.append("\n  no agents found\n", style=f"bold {FG3}")
            empty.append("  press ", style=FG4)
            empty.append("r", style=f"bold {BLUE}")
            empty.append(" to scan", style=FG4)
            self.update(empty)
            return
        # Capture whatever the cursor was on before the refresh. We
        # want to keep the user on the *exact* row they were looking
        # at — including sub-agent children — not just the parent.
        prior_parent = self._parent_agent_id_at(self._selected_index)
        prior_sub = self.selected_subagent
        prior_is_header = self._is_header_row(self._selected_index)
        self._rebuild_rows()
        # Explicit selection from the caller wins only when it changes
        # the parent focus; if the caller is just echoing back the
        # same parent we already had, don't clobber the in-parent
        # position the user has navigated to.
        if selected_agent_id is not None and selected_agent_id != prior_parent:
            self._selected_index = self._row_index_for_agent(selected_agent_id)
        else:
            target_parent = prior_parent or selected_agent_id
            sub_call_id = prior_sub.tool_call_id if prior_sub is not None else None
            self._selected_index = self._row_index_for_position(
                parent_agent_id=target_parent,
                sub_tool_call_id=sub_call_id,
                prefer_header=prior_is_header,
            )
        self._refresh_table()
        self._post_selection(self._selected_index)

    def move_cursor(self, delta: int) -> str | None:
        if not self._rows:
            return None
        target = max(0, min(len(self._rows) - 1, self._selected_index + delta))
        # Skip the "active sub-agents" header — it's a decorative label,
        # not a selectable row. Advance in the same direction the user
        # moved (falling back to the other direction if we're cornered
        # at a boundary) until we land on a real row.
        step = 1 if delta >= 0 else -1
        if self._is_header_row(target):
            probe = target + step
            while 0 <= probe < len(self._rows) and self._is_header_row(probe):
                probe += step
            if 0 <= probe < len(self._rows):
                target = probe
            else:
                # Cornered — try the opposite direction instead of
                # leaving the cursor parked on the header.
                back = target - step
                while 0 <= back < len(self._rows) and self._is_header_row(back):
                    back -= step
                if 0 <= back < len(self._rows):
                    target = back
        self._selected_index = target
        with contextlib.suppress(NoActiveAppError):
            self.focus()
        self._refresh_table()
        self._post_selection(self._selected_index)
        return self._parent_agent_id_at(self._selected_index)

    @property
    def selected_agent_id(self) -> str | None:
        """Return the id of the parent agent owning the highlighted row.

        Sub-agent rows still resolve to their parent agent so that the
        right-hand pane keeps showing useful context (sub-agents don't
        have their own tmux pane to talk about).
        """
        if not self._rows or self._selected_index >= len(self._rows):
            return None
        return self._parent_agent_id_at(self._selected_index)

    @property
    def selected_subagent(self) -> DashboardSubAgentView | None:
        """The sub-agent under the cursor, or ``None`` when on a parent row."""
        if not self._rows or self._selected_index >= len(self._rows):
            return None
        row = self._rows[self._selected_index]
        return row.subagent if isinstance(row, _SubAgentRow) else None

    def focus_list(self) -> None:
        self.focus()

    def toggle_expand(self) -> str | None:
        """Expand or collapse the parent agent of the selected row.

        Works whether the cursor is on the parent row itself or on one
        of its sub-agent children. Collapsing from within a sub-agent
        row snaps the cursor back to the parent so the user doesn't
        end up on an invisible index.
        """
        agent_id = self.selected_agent_id
        if agent_id is None:
            return None
        if agent_id in self._expanded:
            self._expanded.discard(agent_id)
            self._loading.discard(agent_id)
            self._rebuild_rows()
            self._selected_index = self._row_index_for_agent(agent_id)
            self._refresh_table()
            return agent_id
        self._expanded.add(agent_id)
        if agent_id not in self._subagents:
            self._loading.add(agent_id)
            self.post_message(self.ExpandRequested(agent_id))
        self._rebuild_rows()
        self._refresh_table()
        return agent_id

    def set_subagents(self, agent_id: str, tree: DashboardSubAgentTreeView) -> None:
        """Feed a loaded sub-agent tree back into the widget."""
        # Preserve the cursor position the same way :meth:`set_agents`
        # does — rebuilding rows invalidates indices, and we don't
        # want the user to lose their place when a tree refresh lands.
        prior_parent = self._parent_agent_id_at(self._selected_index)
        prior_sub = self.selected_subagent
        prior_is_header = self._is_header_row(self._selected_index)
        self._subagents[agent_id] = tree
        self._loading.discard(agent_id)
        self._rebuild_rows()
        if self._rows:
            self._selected_index = self._row_index_for_position(
                parent_agent_id=prior_parent,
                sub_tool_call_id=prior_sub.tool_call_id if prior_sub is not None else None,
                prefer_header=prior_is_header,
            )
        self._refresh_table()

    def _post_selection(self, index: int | None) -> None:
        if index is None or index >= len(self._rows):
            return
        agent_id = self._parent_agent_id_at(index)
        if agent_id is not None:
            self.post_message(self.AgentSelected(agent_id))
        self.post_message(self.SubAgentHighlighted(self.selected_subagent))

    def _parent_agent_id_at(self, index: int) -> str | None:
        if not self._rows or index >= len(self._rows):
            return None
        row = self._rows[index]
        return row.parent_agent_id

    def _row_index_for_agent(self, agent_id: str | None) -> int:
        if agent_id is None:
            return 0
        for idx, row in enumerate(self._rows):
            if isinstance(row, _AgentRow) and row.agent.agent_id == agent_id:
                return idx
        return 0

    def _row_index_for_position(
        self,
        *,
        parent_agent_id: str | None,
        sub_tool_call_id: str | None,
        prefer_header: bool,
    ) -> int:
        """Restore the cursor to the row that was focused before a refresh.

        Prefers the specific sub-agent (by tool_call_id), falls back to
        the sub-agent header, then to the parent row itself, so a
        periodic refresh never drags the user back up to the parent
        row while they're browsing children.
        """
        if parent_agent_id is None:
            return 0
        parent_idx = 0
        header_idx: int | None = None
        for idx, row in enumerate(self._rows):
            if isinstance(row, _AgentRow) and row.agent.agent_id == parent_agent_id:
                parent_idx = idx
            elif isinstance(row, _SubAgentHeaderRow) and row.parent_agent_id == parent_agent_id:
                header_idx = idx
            elif (
                sub_tool_call_id is not None
                and isinstance(row, _SubAgentRow)
                and row.parent_agent_id == parent_agent_id
                and row.subagent.tool_call_id == sub_tool_call_id
            ):
                return idx
        if prefer_header and header_idx is not None:
            return header_idx
        return parent_idx

    def _is_header_row(self, index: int) -> bool:
        if not self._rows or index >= len(self._rows):
            return False
        return isinstance(self._rows[index], _SubAgentHeaderRow)

    def _rebuild_rows(self) -> None:
        rows: list[_Row] = []
        for agent in self._agents:
            rows.append(_AgentRow(agent=agent))
            if agent.agent_id not in self._expanded:
                continue
            tree = self._subagents.get(agent.agent_id)
            is_loading = agent.agent_id in self._loading
            if is_loading:
                rows.append(_SubAgentHeaderRow(parent_agent_id=agent.agent_id, loading=True))
                continue
            running = tuple(tree.running) if tree is not None else ()
            rows.append(_SubAgentHeaderRow(parent_agent_id=agent.agent_id, count=len(running)))
            for sub in running:
                rows.append(_SubAgentRow(parent_agent_id=agent.agent_id, subagent=sub))
        self._rows = tuple(rows)

    def _refresh_table(self) -> None:
        self.update(self._build_table())

    def _build_table(self) -> Table:
        # Keep rows in sync with ``_agents`` defensively — some legacy
        # tests poke ``_agents`` directly without going through
        # :meth:`set_agents`, and we don't want to silently render an
        # empty table in that case.
        if not self._rows and self._agents:
            self._rebuild_rows()
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
        for index, row in enumerate(self._rows):
            is_selected = index == self._selected_index
            indicator = Text("▎ ", style=f"bold {BLUE}") if is_selected else Text("  ")
            if isinstance(row, _AgentRow):
                agent = row.agent
                row_style = _row_style(agent, selected=is_selected)
                base_name = _display_name(agent, self._agents)
                running_count = self._running_subagent_count(agent.agent_id)
                display_name = _agent_display(
                    base_name,
                    expanded=agent.agent_id in self._expanded,
                    running_count=running_count,
                )
                status_text, status_style = _status_display(agent)
                br_style = PURPLE if is_selected else FG4
                table.add_row(
                    indicator,
                    Text(display_name, style=f"bold {FG}" if is_selected else FG2),
                    Text(status_text, style=status_style),
                    Text(_format_idle(agent.idle_seconds), style=FG4),
                    Text(agent.branch or "─", style=br_style, overflow="ellipsis"),
                    style=row_style,
                )
            elif isinstance(row, _SubAgentHeaderRow):
                table.add_row(*_render_subagent_header_row(row, is_selected=is_selected))
            else:
                table.add_row(*_render_subagent_row(row.subagent, is_selected=is_selected))
        return table

    def _running_subagent_count(self, agent_id: str) -> int | None:
        if agent_id not in self._expanded:
            return None
        if agent_id in self._loading:
            return None
        tree = self._subagents.get(agent_id)
        return 0 if tree is None else len(tree.running)


def _agent_display(base_name: str, *, expanded: bool, running_count: int | None) -> str:
    glyph = "▾ " if expanded else "▸ "
    if running_count is not None and running_count > 0:
        return f"{glyph}{base_name}  ·{running_count}"
    return f"{glyph}{base_name}"


def _render_subagent_header_row(
    row: _SubAgentHeaderRow, *, is_selected: bool
) -> tuple[Text, Text, Text, Text, Text]:
    indicator = Text("▎ ", style=f"bold {BLUE}") if is_selected else Text("  ")
    if row.loading:
        label = Text("    loading active sub-agents…", style=FG4)
        return (indicator, label, Text(""), Text(""), Text(""))
    if row.count == 0:
        label = Text("    no active sub-agents", style=FG4)
        return (indicator, label, Text(""), Text(""), Text(""))
    label = Text()
    label.append("    ", style=FG4)
    label.append("active sub-agents ", style=f"bold {FG3}")
    label.append(f"({row.count})", style=FG4)
    return (indicator, label, Text(""), Text(""), Text(""))


def _render_subagent_row(
    subagent: DashboardSubAgentView, *, is_selected: bool
) -> tuple[Text, Text, Text, Text, Text]:
    # Only active sub-agents are rendered — completed ones are
    # filtered out upstream in :meth:`AgentListPanel._rebuild_rows`.
    status_label = "running"
    status_style = GREEN
    glyph = "↳"
    glyph_color = AQUA
    duration = _format_subagent_duration(subagent)
    label = Text()
    label.append("    ", style=FG4)
    label.append(f"{glyph} ", style=f"bold {glyph_color}")
    label.append(subagent.display_name, style=FG2 if subagent.is_running else FG3)
    call_suffix = _shorten_tool_call_id(subagent.tool_call_id)
    if call_suffix:
        label.append(f"  {call_suffix}", style=FG4)
    indicator = Text("▎ ", style=f"bold {BLUE}") if is_selected else Text("  ")
    return (
        indicator,
        label,
        Text(status_label, style=status_style),
        Text(duration, style=FG4),
        Text("", style=FG4),
    )


def _shorten_tool_call_id(tool_call_id: str) -> str:
    # Copilot CLI ids look like ``call_xx3xSlNrWPvwRsjbTuFv5cN1``.
    # The prefix is noise in the dashboard; trim to the last 6 chars.
    if not tool_call_id:
        return ""
    tail = tool_call_id.rsplit("_", 1)[-1] if "_" in tool_call_id else tool_call_id
    return f"#{tail[-6:]}"


def _format_subagent_duration(subagent: DashboardSubAgentView) -> str:
    if subagent.completed_at is not None:
        elapsed = (subagent.completed_at - subagent.started_at).total_seconds()
    else:
        now = datetime.now(UTC)
        started = subagent.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        elapsed = (now - started).total_seconds()
    if elapsed < 0:
        elapsed = 0
    if elapsed < 60:
        return f"{int(elapsed)}s"
    if elapsed < 3600:
        return f"{int(elapsed // 60)}m"
    return f"{int(elapsed // 3600)}h"


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
        # The task_title is the high-level work ("Investigating cache
        # bug"). Only render it on the header when it genuinely differs
        # from the tool-level activity we show below — otherwise we
        # duplicate the same text twice on consecutive lines.
        if item.task_title and item.task_title != item.current_activity:
            result.append(f"  {item.task_title}", style=FG3)
        result.append("\n")

        # ── activity line (pulled from former ActivityPanel) ──
        activity = item.current_activity or operator_status.reason
        if activity:
            result.append("  ")
            result.append("» ", style=f"bold {AQUA}")
            result.append(activity, style=FG1)
            result.append("\n")

        # ── attention line — always visible when the agent needs it ──
        # The activity line above may be populated (e.g. "Reviewing layout"),
        # which used to mask the operator-visible reason ("waiting for
        # confirmation", "merge conflict", "runaway cost"). Surface the
        # reason on its own line so the user can act without scrolling
        # through the recent events list.
        if item.needs_attention and item.attention_reason and item.attention_reason != activity:
            result.append("  ")
            result.append("! ", style=f"bold {status_style}")
            result.append(item.attention_reason, style=status_style)
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

    def set_subagent(self, subagent: DashboardSubAgentView | None) -> None:
        """Render sub-agent focus: prompt (input) + result (output)."""
        result = Text()
        _section_header(result, "sub-agent detail")
        if subagent is None:
            result.append("  no sub-agent selected\n", style=FG4)
            self.update(result)
            return

        is_running = subagent.is_running
        is_failure = subagent.success is False
        glyph = "▶" if is_running else ("✗" if is_failure else "✓")
        glyph_color = AQUA if is_running else (SEVERITY_ERROR if is_failure else GREEN)
        status_label = "running" if is_running else ("failed" if is_failure else "completed")
        status_style = glyph_color

        result.append(f"  {glyph} ", style=f"bold {glyph_color}")
        name = subagent.task_name or subagent.display_name
        result.append(name, style=f"bold {FG}")
        result.append("  ")
        result.append(status_label, style=status_style)
        result.append("\n")

        if subagent.description:
            result.append("  ")
            result.append("» ", style=f"bold {AQUA}")
            result.append(subagent.description, style=FG1)
            result.append("\n")

        result.append("\n")
        _field_row(result, "type", subagent.agent_type or subagent.agent_name, PURPLE)
        _field_row(result, "id", subagent.tool_call_id[:16], FG4)
        _field_row(result, "duration", _format_subagent_duration(subagent), FG2)

        if subagent.prompt:
            result.append("\n")
            result.append("  input\n", style=f"bold {FG4}")
            snippet = _truncate(subagent.prompt, 800)
            for line in snippet.splitlines() or [snippet]:
                result.append(f"  {line}\n", style=FG2)

        if subagent.result_content:
            result.append("\n")
            # For background agents the parent's "result" is only a
            # launch acknowledgement — the real agent output lives in
            # the sub-agent's own session. Label it so operators don't
            # mistake the ack for the actual result.
            is_background = (subagent.mode or "").lower() == "background"
            if is_background and is_running:
                header = "  launch ack (background — output lives in sub-session)\n"
            elif is_background:
                header = "  launch ack\n"
            else:
                header = "  output\n"
            result.append(header, style=f"bold {FG4}")
            snippet = _truncate(subagent.result_content, 1200)
            for line in snippet.splitlines() or [snippet]:
                result.append(f"  {line}\n", style=FG1)

        self.update(result)


def _field_row(text: Text, label: str, value: str | None, style: str) -> None:
    """Render a single labeled field row. Skip if value is empty."""
    if not value or value == "-":
        return
    text.append(f"  {label:<10}", style=FG4)
    text.append(f"{value}\n", style=style)


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


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
