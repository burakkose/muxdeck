from __future__ import annotations

from collections.abc import Sequence

from rich.table import Table
from rich.text import Text
from textual.message import Message
from textual.widgets import Static

from copilot_commander.controllers import (
    AttentionInboxRowView,
    AttentionInboxSummaryView,
    DashboardHealthSummary,
)
from copilot_commander.domain.enums import AgentStatus
from copilot_commander.theme import FG, FG1, FG2, FG3, FG4, ORANGE, SEVERITY_ERROR
from copilot_commander.widgets.common import format_short_timestamp, format_timestamp, status_glyph
from copilot_commander.widgets.dashboard import _format_idle

_SEVERITY_STYLES: dict[str, str] = {
    "warning": f"bold {ORANGE}",
    "error": f"bold {SEVERITY_ERROR}",
}

_STATE_STYLES: dict[str, str] = {
    "new": f"bold {ORANGE}",
    "read": FG3,
    "acked": FG4,
}


def _severity_label(row: AttentionInboxRowView) -> str:
    return "crit" if row.severity == "error" else "warn"


def _attention_state(row: AttentionInboxRowView) -> tuple[str, str]:
    if row.is_acknowledged:
        return ("acked", _STATE_STYLES["acked"])
    if row.is_unread:
        return ("new", _STATE_STYLES["new"])
    return ("read", _STATE_STYLES["read"])


def _status_label(status: AgentStatus) -> str:
    if status is AgentStatus.WAITING_INPUT:
        return "input"
    if status is AgentStatus.BLOCKED:
        return "blocked"
    if status in {AgentStatus.ERROR, AgentStatus.DEAD}:
        return "error"
    return status.value.replace("_", " ")


class AttentionInboxListPanel(Static, can_focus=True):
    class RowSelected(Message):
        def __init__(self, alert_key: str) -> None:
            super().__init__()
            self.alert_key = alert_key

    def __init__(self, *, widget_id: str | None = None, classes: str | None = None) -> None:
        super().__init__(id=widget_id, classes=classes)
        self._rows: tuple[AttentionInboxRowView, ...] = ()
        self._selected_index = 0

    def set_rows(
        self,
        rows: Sequence[AttentionInboxRowView],
        *,
        selected_alert_key: str | None,
    ) -> None:
        self._rows = tuple(rows)
        if not self._rows:
            self._selected_index = 0
            self.update(Text(" no active attention items", style=FG4))
            return
        self._selected_index = next(
            (index for index, row in enumerate(self._rows) if row.alert_key == selected_alert_key),
            min(self._selected_index, len(self._rows) - 1),
        )
        self._refresh_table()
        self._post_selection(self._selected_index)

    def move_cursor(self, delta: int) -> None:
        if not self._rows:
            return
        self._selected_index = max(0, min(len(self._rows) - 1, self._selected_index + delta))
        self.focus()
        self._refresh_table()
        self._post_selection(self._selected_index)

    def focus_list(self) -> None:
        self.focus()

    def _post_selection(self, index: int | None) -> None:
        if index is None or index >= len(self._rows):
            return
        self.post_message(self.RowSelected(self._rows[index].alert_key))

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
        table.add_column("state", width=5, no_wrap=True)
        table.add_column("sev", width=4, no_wrap=True)
        table.add_column("agent", min_width=10, no_wrap=True, ratio=1)
        table.add_column("idle", width=6, no_wrap=True)
        table.add_column("seen", width=8, no_wrap=True)
        table.add_column("summary", min_width=20, ratio=3, overflow="ellipsis")
        for index, row in enumerate(self._rows):
            selected = index == self._selected_index
            state_text, state_style = _attention_state(row)
            severity_style = _SEVERITY_STYLES.get(row.severity, FG3)
            message = f"{row.agent_name}: {row.message}"
            table.add_row(
                status_glyph(row.agent_status, selected=selected),
                Text(state_text, style=state_style),
                Text(_severity_label(row), style=severity_style),
                Text(row.agent_name, style=f"bold {FG}" if selected else FG1),
                Text(_format_idle(row.idle_seconds), style=FG2),
                Text(format_short_timestamp(row.occurred_at), style=FG4),
                Text(message, style=FG2, overflow="ellipsis"),
            )
        return table


class AttentionInboxSummaryPanel(Static):
    def set_state(
        self,
        summary: AttentionInboxSummaryView,
        health: DashboardHealthSummary,
    ) -> None:
        result = Text()
        result.append(" inbox\n", style=f"bold {FG}")
        for label, value, style in (
            ("health", health.message, FG2),
            ("items", str(summary.total_rows), FG1),
            ("unread", str(summary.unread_rows), ORANGE),
            ("acked", str(summary.acknowledged_rows), FG4),
            ("critical", str(summary.critical_rows), SEVERITY_ERROR),
            ("warning", str(summary.warning_rows), ORANGE),
        ):
            result.append(f" {label:<8}", style=FG4)
            result.append(f"{value}\n", style=f"bold {style}" if label != "health" else style)
        self.update(result)


class AttentionInboxDetailPanel(Static):
    def set_row(self, row: AttentionInboxRowView | None) -> None:
        result = Text()
        result.append(" triage\n", style=f"bold {FG}")
        if row is None:
            result.append(" no alert selected", style=FG4)
            self.update(result)
            return
        state_text, state_style = _attention_state(row)
        severity_style = _SEVERITY_STYLES.get(row.severity, FG3)
        result.append(f" {row.agent_name}", style=f"bold {FG}")
        result.append("  ")
        result.append(_severity_label(row), style=severity_style)
        result.append("  ")
        result.append(state_text, style=state_style)
        result.append("\n")
        for label, value, style in (
            ("message", row.message, FG2),
            ("status", _status_label(row.agent_status), FG1),
            ("branch", row.branch or "-", FG1),
            ("idle", _format_idle(row.idle_seconds), FG2),
            ("activity", row.current_activity or "-", FG2),
            ("reason", row.attention_reason or "-", FG2),
            ("event", row.last_event_kind or "-", FG4),
            ("seen", format_timestamp(row.occurred_at), FG4),
        ):
            if value == "-":
                continue
            result.append(f" {label:<8}", style=FG4)
            result.append(f"{value}\n", style=style)
        self.update(result)


__all__ = [
    "AttentionInboxDetailPanel",
    "AttentionInboxListPanel",
    "AttentionInboxSummaryPanel",
]
