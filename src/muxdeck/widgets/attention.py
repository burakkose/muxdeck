from __future__ import annotations

from collections.abc import Sequence

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static

from muxdeck.controllers.attention_controller import (
    AttentionFilterState,
    AttentionItemView,
    AttentionSelectedItemView,
    AttentionSummaryView,
)
from muxdeck.services.operator_status_service import OperatorStatusKind
from muxdeck.theme import (
    ATTENTION_ROW_BG,
    FG,
    FG1,
    FG2,
    FG3,
    FG4,
    ORANGE,
    RED,
    SELECTED_ROW_BG,
    YELLOW,
)
from muxdeck.widgets.common import format_short_timestamp, format_timestamp

_SEVERITY_STYLES: dict[str, str] = {
    "info": FG2,
    "warning": f"bold {ORANGE}",
    "error": f"bold {RED}",
}


def _append_section_title(text: Text, title: str) -> None:
    # Graphite redesign: section titles read as muted-label headings,
    # not loud BLUE banners. Colour stays reserved for state.
    text.append(f" {title}\n", style=f"bold {FG3}")


def _status_style(kind: OperatorStatusKind) -> str:
    lookup = {
        OperatorStatusKind.WORKING: FG3,
        OperatorStatusKind.WAITING_INPUT: f"bold {ORANGE}",
        OperatorStatusKind.BLOCKED: f"bold {RED}",
        OperatorStatusKind.REVIEW_READY: f"bold {ORANGE}",
        OperatorStatusKind.FAILED: f"bold {RED}",
        OperatorStatusKind.TERMINATED: f"bold {YELLOW}",
        OperatorStatusKind.STALE: f"bold {YELLOW}",
        OperatorStatusKind.COMPLETED: FG4,
    }
    return lookup[kind]


class AttentionSummaryBar(Static):
    def set_state(self, summary: AttentionSummaryView, filters: AttentionFilterState) -> None:
        content = Text()
        content.append(" attention ", style=FG4)
        content.append(str(summary.total_items), style=f"bold {FG}")
        content.append(" active", style=FG3)
        content.append("  │  ", style=FG4)
        content.append("unread ", style=FG3)
        content.append(str(summary.unread_items), style=f"bold {ORANGE}")
        content.append("  │  ", style=FG4)
        content.append("critical ", style=FG3)
        content.append(str(summary.critical_items), style=f"bold {RED}")
        if filters.unread_only:
            content.append("  │  ", style=FG4)
            # The unread filter is a state of the *view*, not a state
            # of the inbox. Tone it down so the unread count above
            # remains the loudest cell.
            content.append("filtered unread", style=f"bold {FG2}")
        self.update(content)


class AttentionListPanel(Static, can_focus=True):
    class AttentionSelected(Message):
        def __init__(self, agent_id: str) -> None:
            super().__init__()
            self.agent_id = agent_id

    def __init__(self, *, widget_id: str | None = None, classes: str | None = None) -> None:
        super().__init__(id=widget_id, classes=classes)
        self._items: tuple[AttentionItemView, ...] = ()
        self._selected_index = 0

    def set_items(
        self,
        items: Sequence[AttentionItemView],
        *,
        selected_agent_id: str | None,
    ) -> None:
        self._items = tuple(items)
        if not self._items:
            self._selected_index = 0
            self.update(Text(" no unread attention items", style=FG4))
            return
        self._selected_index = next(
            (index for index, item in enumerate(self._items) if item.agent_id == selected_agent_id),
            min(self._selected_index, len(self._items) - 1),
        )
        self._refresh_table()
        self._post_selection(self._selected_index)

    def move_cursor(self, delta: int) -> None:
        if not self._items:
            return
        self._selected_index = max(0, min(len(self._items) - 1, self._selected_index + delta))
        self.focus()
        self._refresh_table()
        self._post_selection(self._selected_index)

    def focus_list(self) -> None:
        self.focus()

    def _post_selection(self, index: int) -> None:
        self.post_message(self.AttentionSelected(self._items[index].agent_id))

    def _refresh_table(self) -> None:
        table = self._build_table()
        total = len(self._items)
        win_start = self._window_start
        win_end = self._window_end
        parts: list[RenderableType] = []
        if win_start > 0:
            parts.append(Text(f"  ↑ {win_start} more above", style=FG4))
        parts.append(table)
        remaining_below = total - win_end
        if remaining_below > 0:
            parts.append(Text(f"  ↓ {remaining_below} more below", style=FG4))
        self.update(Group(*parts) if len(parts) > 1 else parts[0])

    @property
    def _window_start(self) -> int:
        start, _ = self._visible_window()
        return start

    @property
    def _window_end(self) -> int:
        _, end = self._visible_window()
        return end

    def _visible_window(self) -> tuple[int, int]:
        """Return ``(start, end)`` indices for the rows to render.

        Keeps the highlighted row inside the visible window so cursor
        movement remains meaningful when the inbox is taller than the
        panel viewport. The viewport is read from the parent container
        because :class:`Static` sizes itself to its content (``height:
        auto``); without windowing the bottom rows would be clipped and
        ``j/k`` would silently move the cursor off-screen.
        """
        total = len(self._items)
        if total == 0:
            return 0, 0
        parent = self.parent
        viewport_widget = parent if isinstance(parent, Widget) else self
        # ``- 3`` reserves space for the table header and the optional
        # ``↑/↓ N more`` indicator rows. Even on a partially mounted
        # widget where ``size.height`` is ``0``, the ``max(..., 5)``
        # fallback guarantees at least five rows are kept visible.
        viewport = viewport_widget.size.height - 3
        visible = max(viewport, 5)
        if total <= visible:
            return 0, total
        half = visible // 2
        start = self._selected_index - half
        start = max(0, min(start, total - visible))
        return start, start + visible

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
        table.add_column("sev", width=4, no_wrap=True)
        table.add_column("state", width=8, no_wrap=True)
        table.add_column("agent", min_width=8, ratio=2, overflow="ellipsis")
        table.add_column("branch", min_width=6, ratio=1, overflow="ellipsis")
        table.add_column("reason", min_width=18, ratio=3, overflow="ellipsis")
        win_start, win_end = self._visible_window()
        for index in range(win_start, win_end):
            item = self._items[index]
            is_selected = index == self._selected_index
            row_style = f"on {SELECTED_ROW_BG}" if is_selected else ""
            if item.unread and not is_selected:
                row_style = f"on {ATTENTION_ROW_BG}"
            unread_marker = "●" if item.unread else " "
            table.add_row(
                # Unread dot already lives in a row that paints its own
                # ATTENTION_ROW_BG surface; an extra coloured dot is
                # overkill. Keep the dot in primary text so the row's
                # raised background carries the "unread" signal.
                Text(unread_marker, style=f"bold {FG}" if item.unread else FG4),
                Text(item.severity[:4], style=_SEVERITY_STYLES[item.severity]),
                Text(item.operator_status.label, style=_status_style(item.operator_status.kind)),
                Text(item.agent_name, style=f"bold {FG}" if is_selected else FG),
                Text(item.branch or "-", style=FG1),
                Text(item.message, style=FG2, overflow="ellipsis"),
                style=row_style,
            )
        return table


class AttentionDetailPanel(Static):
    def set_item(self, selected: AttentionSelectedItemView | None) -> None:
        content = Text()
        _append_section_title(content, "triage")
        if selected is None:
            content.append(" no attention item selected", style=FG4)
            self.update(content)
            return
        item = selected.item
        content.append(f" {item.agent_name}", style=f"bold {FG}")
        content.append("  ")
        content.append(
            item.operator_status.headline,
            style=_status_style(item.operator_status.kind),
        )
        content.append("\n")
        fields: list[tuple[str, str, str]] = [
            ("reason", item.message, _status_style(item.operator_status.kind)),
            # ``unread`` is metadata about *this* item, not a state of
            # the agent. Demote to primary text bold so colour stays
            # reserved for severity / status above.
            ("unread", "yes" if item.unread else "no", f"bold {FG}" if item.unread else FG4),
            ("task", item.task_title or "", FG2),
            ("branch", item.branch or "", FG1),
            ("worktree", item.worktree_name or "", FG2),
            # Pane id is metadata, not navigation. Keep it grey.
            ("pane", item.pane_id, FG2),
            ("seen", format_timestamp(item.occurred_at), FG4),
            ("event", selected.agent.latest_event_kind or "", FG4),
            ("level", selected.agent.latest_event_severity or "", FG4),
        ]
        for label, value, style in fields:
            if not value:
                continue
            content.append(f" {label:<8}", style=FG3)
            content.append(f"{value}\n", style=style)
        if selected.agent.recent_events:
            content.append(" recent\n", style=FG3)
            for event in selected.agent.recent_events[-4:]:
                content.append(" ")
                content.append(event, style=FG2)
                content.append("\n")
        self.update(content)


class AttentionActivityPanel(Static):
    def set_items(self, items: Sequence[AttentionItemView]) -> None:
        content = Text()
        _append_section_title(content, "queue")
        if not items:
            content.append(" inbox clear", style=FG4)
            self.update(content)
            return
        for index, item in enumerate(items[:6]):
            if index:
                content.append("\n")
            content.append(f" {format_short_timestamp(item.occurred_at)} ", style=FG4)
            if item.unread:
                # The "new" tag accompanies an attention-state colour
                # immediately to its right; an extra BLUE pill made
                # the row look like a multi-colour ribbon.
                content.append("new ", style=f"bold {FG}")
            content.append(f"{item.agent_name}: ", style=f"bold {FG1}")
            content.append(
                item.operator_status.headline,
                style=_status_style(item.operator_status.kind),
            )
        self.update(content)


__all__ = [
    "AttentionActivityPanel",
    "AttentionDetailPanel",
    "AttentionListPanel",
    "AttentionSummaryBar",
]
