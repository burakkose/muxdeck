from __future__ import annotations

from collections.abc import Sequence

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import Static

from copilot_commander.controllers import OperationsActionPreview
from copilot_commander.controllers.dashboard_controller import DashboardAgentListItemView
from copilot_commander.domain.enums import AgentStatus
from copilot_commander.services.operations_service import OperationAuditEntry
from copilot_commander.theme import (
    FG,
    FG1,
    FG2,
    FG3,
    FG4,
    ORANGE,
    SELECTED_ROW_BG,
    SEVERITY_ERROR,
    YELLOW,
)
from copilot_commander.widgets.common import format_short_timestamp, status_glyph


def _append_section_title(text: Text, title: str) -> None:
    text.append(f" {title}\n", style=f"bold {FG1}")


def _format_idle(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60}m"


def _status_text(agent: DashboardAgentListItemView) -> tuple[str, str]:
    if agent.is_potentially_stuck:
        return ("stuck", f"bold {YELLOW}")
    if agent.status is AgentStatus.WAITING_INPUT:
        return ("input", f"bold {ORANGE}")
    if agent.status is AgentStatus.DEAD:
        return ("terminated", f"bold {YELLOW}")
    if agent.status in {AgentStatus.BLOCKED, AgentStatus.ERROR}:
        return ("review", f"bold {SEVERITY_ERROR}")
    if agent.needs_attention:
        return ("review", f"bold {ORANGE}")
    return (agent.status.value.replace("_", " "), FG3)


class OperationsAgentListPanel(Static, can_focus=True):
    """Bulk-operations agent list with selection state and cursor."""

    def __init__(self, *, widget_id: str | None = None, classes: str | None = None) -> None:
        super().__init__(id=widget_id, classes=classes)
        self._agents: tuple[DashboardAgentListItemView, ...] = ()
        self._selected_agent_ids: tuple[str, ...] = ()
        self._cursor_index = 0

    @property
    def current_agent_id(self) -> str | None:
        if not self._agents:
            return None
        return self._agents[self._cursor_index].agent_id

    @property
    def agents(self) -> tuple[DashboardAgentListItemView, ...]:
        return self._agents

    def compose(self) -> ComposeResult:
        yield Static()

    def set_agents(
        self,
        agents: Sequence[DashboardAgentListItemView],
        *,
        selected_agent_ids: Sequence[str],
        cursor_agent_id: str | None,
    ) -> None:
        self._agents = tuple(agents)
        self._selected_agent_ids = tuple(selected_agent_ids)
        if not self._agents:
            self._cursor_index = 0
            self.update(Text(" no agents available", style=FG4))
            return
        requested_index = next(
            (
                index
                for index, agent in enumerate(self._agents)
                if agent.agent_id == cursor_agent_id
            ),
            self._cursor_index,
        )
        self._cursor_index = min(requested_index, len(self._agents) - 1)
        self.update(self._build_table())

    def move_cursor(self, delta: int) -> None:
        if not self._agents:
            return
        self._cursor_index = max(0, min(len(self._agents) - 1, self._cursor_index + delta))
        self.update(self._build_table())
        self.focus()

    def focus_list(self) -> None:
        self.focus()

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
        table.add_column("sel", width=3, no_wrap=True)
        table.add_column("", width=1, no_wrap=True)
        table.add_column("name", min_width=8, no_wrap=True, ratio=2)
        table.add_column("state", width=8, no_wrap=True)
        table.add_column("idle", width=6, no_wrap=True)
        table.add_column("branch", min_width=6, no_wrap=True, ratio=2, overflow="ellipsis")
        selected_lookup = set(self._selected_agent_ids)
        for index, agent in enumerate(self._agents):
            is_cursor = index == self._cursor_index
            mark = "[x]" if agent.agent_id in selected_lookup else "[ ]"
            status_text, status_style = _status_text(agent)
            row_style = f"on {SELECTED_ROW_BG}" if is_cursor else ""
            table.add_row(
                Text(mark, style=f"bold {FG1}" if agent.agent_id in selected_lookup else FG4),
                status_glyph(agent.status, selected=is_cursor),
                Text(agent.name, style=f"bold {FG}" if is_cursor else FG1),
                Text(status_text, style=status_style),
                Text(_format_idle(agent.idle_seconds), style=FG2),
                Text(agent.branch or "-", style=FG2),
                style=row_style,
            )
        return table


class OperationsSelectionPanel(Static):
    def set_selection(
        self,
        selected_agents: Sequence[DashboardAgentListItemView],
        *,
        total_agents: int,
    ) -> None:
        text = Text()
        _append_section_title(text, "selection")
        text.append(f" {len(selected_agents)} selected", style=f"bold {FG}")
        text.append(f" / {total_agents} visible\n", style=FG2)
        if not selected_agents:
            text.append(" press space to select agents", style=FG4)
            self.update(text)
            return
        for agent in selected_agents[:6]:
            text.append(f" {agent.name}", style=f"bold {FG1}")
            if agent.branch:
                text.append(f" · {agent.branch}", style=FG3)
            text.append("\n")
        self.update(text)


class BulkActionPreviewPanel(Static):
    def set_preview(self, preview: OperationsActionPreview | None) -> None:
        text = Text()
        _append_section_title(text, "preview")
        if preview is None:
            text.append(" choose an action to build a preview", style=FG4)
            self.update(text)
            return
        text.append(f" {preview.summary}\n", style=f"bold {FG}")
        text.append(f" {preview.confirmation_message}\n", style=FG2)
        text.append(
            " confirm required\n" if preview.requires_confirmation else " ready to execute\n",
            style=FG3,
        )
        for target in preview.targets[:6]:
            text.append(f" • {target.name}", style=FG1)
            if target.branch:
                text.append(f" · {target.branch}", style=FG3)
            text.append("\n")
        self.update(text)


class OperationsHistoryPanel(Static):
    def set_entries(self, entries: Sequence[OperationAuditEntry]) -> None:
        text = Text()
        _append_section_title(text, "history")
        if not entries:
            text.append(" no operator actions recorded", style=FG4)
            self.update(text)
            return
        for entry in entries[:8]:
            style = f"bold {FG1}" if entry.success else f"bold {SEVERITY_ERROR}"
            text.append(f" {format_short_timestamp(entry.occurred_at)} ", style=FG4)
            text.append(entry.action, style=style)
            text.append(f" {entry.agent_name}\n", style=FG2)
            text.append(f"   {entry.message}\n", style=FG3)
        self.update(text)


__all__ = [
    "BulkActionPreviewPanel",
    "OperationsAgentListPanel",
    "OperationsHistoryPanel",
    "OperationsSelectionPanel",
]
