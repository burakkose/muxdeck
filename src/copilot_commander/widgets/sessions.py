"""Widgets for the Sessions browser screen."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.table import Table
from rich.text import Text
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Static

from copilot_commander.theme import (
    AQUA,
    BLUE,
    FG,
    FG4,
    GREEN,
    ORANGE,
    RED,
    YELLOW,
)

if TYPE_CHECKING:
    from copilot_commander.controllers.sessions_controller import (
        SessionDetailView,
        SessionListItemView,
    )


_STATUS_COLORS = {
    "active": GREEN,
    "unclosed": RED,
    "completed": FG4,
}


class SessionSelected(Message):
    """Emitted when a session is selected in the list."""

    def __init__(self, session_id: str) -> None:
        super().__init__()
        self.session_id = session_id


class SessionListPanel(Static, can_focus=True):
    """Table listing all discovered Copilot CLI sessions."""

    selected_index: reactive[int] = reactive(0)

    def __init__(self, *, widget_id: str | None = None, **kwargs: object) -> None:
        super().__init__(id=widget_id, **kwargs)
        self._items: tuple[SessionListItemView, ...] = ()
        self._selected_session_id: str | None = None

    def set_sessions(
        self,
        items: tuple[SessionListItemView, ...],
        *,
        selected_session_id: str | None = None,
        notify: bool = True,
    ) -> None:
        self._items = items
        if selected_session_id is not None:
            self._selected_session_id = selected_session_id
            idx = next(
                (i for i, s in enumerate(items) if s.session_id == selected_session_id),
                0,
            )
            self.selected_index = idx
        elif self.selected_index >= len(items):
            self.selected_index = max(0, len(items) - 1)
        self._render_table()
        if notify and self._items:
            item = self._items[self.selected_index]
            self.post_message(SessionSelected(item.session_id))

    def focus_list(self) -> None:
        self.focus()

    def move_cursor(self, delta: int) -> str | None:
        if not self._items:
            return None
        new_index = max(0, min(len(self._items) - 1, self.selected_index + delta))
        self.selected_index = new_index
        self._render_table()
        item = self._items[new_index]
        self.post_message(SessionSelected(item.session_id))
        return item.session_id

    def get_selected_id(self) -> str | None:
        if not self._items or self.selected_index >= len(self._items):
            return None
        return self._items[self.selected_index].session_id

    def _render_table(self) -> None:
        table = Table(
            box=None,
            expand=True,
            show_header=True,
            header_style=f"bold {FG4}",
            padding=(0, 1),
        )
        table.add_column("", width=2, no_wrap=True)  # status glyph
        table.add_column("Summary", ratio=3, no_wrap=True)
        table.add_column("Repository", ratio=2, no_wrap=True)
        table.add_column("Branch", ratio=1, no_wrap=True)
        table.add_column("Updated", width=10, no_wrap=True)
        table.add_column("CPs", width=4, justify="right")
        table.add_column("State", width=10, no_wrap=True)

        for idx, item in enumerate(self._items):
            is_selected = idx == self.selected_index
            color = _STATUS_COLORS.get(item.status, FG4)

            row_style = f"bold {FG}" if is_selected else FG4
            pointer = "▸" if is_selected else " "
            summary_text = Text()
            if item.origin == "windows":
                summary_text.append("[win] ", style=f"bold {BLUE}")
            summary_text.append(item.summary[:50], style=row_style)

            table.add_row(
                Text(f"{pointer}{item.status_glyph}", style=row_style),
                summary_text,
                Text(item.repository, style=f"{AQUA}" if is_selected else FG4),
                Text(item.branch[:20], style=f"{YELLOW}" if is_selected else FG4),
                Text(item.updated, style=row_style),
                Text(str(item.checkpoint_count), style=row_style),
                Text(item.status, style=f"bold {color}"),
            )

        self.update(table)


class SessionDetailPanel(Static):
    """Detail panel showing selected session metadata."""

    def __init__(self, *, widget_id: str | None = None, **kwargs: object) -> None:
        super().__init__(id=widget_id, **kwargs)

    def set_detail(self, detail: SessionDetailView | None) -> None:
        if detail is None:
            self.update(Text("No session selected", style=FG4))
            return

        content = Text()
        color = _STATUS_COLORS.get(detail.status, FG4)

        content.append(f" {detail.status_glyph} {detail.status.upper()} ", style=f"bold {color}")
        content.append("  ")
        if detail.origin == "windows":
            content.append("[win] ", style=f"bold {BLUE}")
        content.append(detail.summary, style=f"bold {FG}")
        content.append("\n\n")

        # Session ID — prominent for copy
        content.append("  Session ID  ", style=f"bold {FG4}")
        content.append(detail.session_id, style=f"bold {ORANGE}")
        content.append("\n")

        content.append("  Repository  ", style=f"bold {FG4}")
        content.append(detail.repository, style=AQUA)
        content.append("  ")
        content.append(detail.branch, style=YELLOW)
        content.append("\n")

        content.append("  CWD         ", style=f"bold {FG4}")
        content.append(detail.cwd, style=FG)
        content.append("\n")

        content.append("  Created     ", style=f"bold {FG4}")
        content.append(detail.created_at, style=FG)
        content.append("  Updated  ", style=f"bold {FG4}")
        content.append(detail.updated_at, style=FG)
        content.append("\n")

        content.append("  Last Event  ", style=f"bold {FG4}")
        content.append(detail.last_event_type, style=FG)
        content.append("  at  ", style=FG4)
        content.append(detail.last_event_at, style=FG)
        content.append("\n")

        content.append("  Checkpoints ", style=f"bold {FG4}")
        content.append(str(detail.checkpoint_count), style=BLUE)
        content.append("\n\n")

        if detail.is_resumable:
            content.append("  Resume: ", style=f"bold {FG4}")
            content.append(detail.resume_command, style=f"bold {GREEN}")
            content.append("\n")
            content.append("  Press ", style=FG4)
            content.append("r", style=f"bold {AQUA}")
            content.append(" to resume in a new tmux window", style=FG4)
        else:
            content.append("  Session completed cleanly", style=FG4)

        self.update(content)


class SessionSummaryBar(Static):
    """Summary bar showing session counts."""

    def __init__(self, *, widget_id: str | None = None, **kwargs: object) -> None:
        super().__init__(id=widget_id, **kwargs)

    def set_counts(
        self,
        total: int,
        active: int,
        unclosed: int,
        completed: int,
    ) -> None:
        text = Text()
        text.append(f" {total} ", style=f"bold {FG}")
        text.append("sessions  ", style=FG4)
        text.append(f"🟢 {active} ", style=f"bold {GREEN}")
        text.append("active  ", style=FG4)
        text.append(f"🔴 {unclosed} ", style=f"bold {RED}")
        text.append("unclosed  ", style=FG4)
        text.append(f"⚪ {completed} ", style=FG4)
        text.append("completed", style=FG4)
        self.update(text)


__all__ = [
    "SessionDetailPanel",
    "SessionListPanel",
    "SessionSelected",
    "SessionSummaryBar",
]
