"""Widgets for the Sessions browser screen."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

from muxdeck.theme import (
    AQUA,
    BLUE,
    FG,
    FG4,
    GREEN,
    ORANGE,
    RED,
    YELLOW,
)
from muxdeck.ui_preferences import UiDensity, UiPreferences, resolve_ui_preferences
from muxdeck.widgets.common import item_separator, pipe_separator, ui_symbol

if TYPE_CHECKING:
    from muxdeck.controllers.sessions_controller import (
        SessionDetailView,
        SessionListItemView,
    )


_STATUS_COLORS = {
    "active": GREEN,
    "unclosed": RED,
    "completed": FG4,
}

_STATUS_GLYPHS = {
    "active": ("🟢", "o"),
    "unclosed": ("🔴", "!"),
    "completed": ("⚪", "v"),
}


def _session_status_glyph(status: str, *, preferences: UiPreferences) -> str:
    rich, ascii_safe = _STATUS_GLYPHS.get(status, ("⚫", "?"))
    if preferences.glyphs.value == "ascii":
        return ascii_safe
    return rich


def _append_chip(
    text: Text,
    label: str,
    value: str,
    *,
    value_style: str,
    preferences: UiPreferences,
) -> None:
    if text.plain:
        text.append(pipe_separator(preferences), style=FG4)
    text.append(f"{label} ", style=FG4)
    text.append(value, style=value_style)


def _append_action(text: Text, key: str, label: str, *, enabled: bool = True) -> None:
    if text.plain:
        text.append("  ")
    key_style = f"bold {AQUA}" if enabled else FG4
    label_style = FG if enabled else FG4
    text.append(key, style=key_style)
    text.append(f" {label}", style=label_style)


class SessionSelected(Message):
    """Emitted when a session is selected in the list."""

    def __init__(self, session_id: str) -> None:
        super().__init__()
        self.session_id = session_id


class SessionListPanel(Static, can_focus=True):
    """Table listing all discovered Copilot CLI sessions.

    Rendering is optimised for rapid cursor movement: per-row cells are
    pre-built in two variants (selected / unselected) when the list is
    set, so ``_render_table`` only has to assemble a ``rich.Table`` out
    of already-constructed ``Text`` objects instead of re-allocating 7
    columns x N rows of styled text on every keystroke.
    """

    selected_index: reactive[int] = reactive(0)

    def __init__(
        self,
        *,
        widget_id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(id=widget_id, classes=classes)
        self._items: tuple[SessionListItemView, ...] = ()
        self._selected_session_id: str | None = None
        # Parallel to ``_items``: each entry is a 2-tuple of (unselected,
        # selected) row cells. Populated by ``_rebuild_row_cache``.
        self._row_cache: tuple[tuple[tuple[Text, ...], tuple[Text, ...]], ...] = ()
        # Draw coalescing: rapid cursor moves merge into one paint per
        # Textual frame instead of one rebuild per keystroke.
        self._render_pending: bool = False

    def set_sessions(
        self,
        items: tuple[SessionListItemView, ...],
        *,
        selected_session_id: str | None = None,
        notify: bool = True,
    ) -> None:
        self._items = items
        self._rebuild_row_cache()
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
        if new_index == self.selected_index:
            # Already at the edge — no repaint needed at all.
            item = self._items[new_index]
            return item.session_id
        self.selected_index = new_index
        self._schedule_render()
        item = self._items[new_index]
        self.post_message(SessionSelected(item.session_id))
        return item.session_id

    def get_selected_id(self) -> str | None:
        if not self._items or self.selected_index >= len(self._items):
            return None
        return self._items[self.selected_index].session_id

    def _schedule_render(self) -> None:
        """Coalesce multiple rapid render requests into one paint.

        Holding ``j`` fires ``move_cursor`` many times per Textual
        frame; without coalescing we'd rebuild the table once per
        keystroke. ``call_after_refresh`` defers the actual render to
        the next compositor tick so successive moves collapse into a
        single repaint at the final index.
        """
        if self._render_pending:
            return
        self._render_pending = True
        self.call_after_refresh(self._flush_render)

    def _flush_render(self) -> None:
        self._render_pending = False
        self._render_table()

    def _rebuild_row_cache(self) -> None:
        """Pre-build row cells for every item in both selection states.

        Each item contributes two 7-tuples of ``Text`` — one for the
        unselected look and one for the selected look. During cursor
        movement ``_render_table`` just picks the right variant per row
        rather than re-allocating ``Text`` and re-running the style
        logic N times.
        """
        cache: list[tuple[tuple[Text, ...], tuple[Text, ...]]] = []
        preferences = resolve_ui_preferences(self)
        for item in self._items:
            cache.append(
                (
                    _build_row_cells(item, selected=False, preferences=preferences),
                    _build_row_cells(item, selected=True, preferences=preferences),
                )
            )
        self._row_cache = tuple(cache)

    def _render_table(self) -> None:
        preferences = resolve_ui_preferences(self)
        comfortable = preferences.density is UiDensity.COMFORTABLE
        table = Table(
            box=None,
            expand=True,
            show_header=True,
            header_style=f"bold {FG4}",
            padding=(0, 1),
        )
        table.add_column("", width=2, no_wrap=True)  # status glyph
        table.add_column("Summary", ratio=4 if comfortable else 3, no_wrap=True)
        table.add_column("Repository", ratio=2, no_wrap=True)
        table.add_column("Branch", ratio=1, no_wrap=True)
        table.add_column("Updated", width=10, no_wrap=True)
        table.add_column("CPs", width=4, justify="right")
        table.add_column("State", width=10, no_wrap=True)

        selected_idx = self.selected_index
        win_start, win_end = self._visible_window(comfortable=comfortable)
        for idx in range(win_start, win_end):
            variants = self._row_cache[idx]
            cells = variants[1] if idx == selected_idx else variants[0]
            table.add_row(*cells)

        total = len(self._row_cache)
        parts: list[RenderableType] = []
        if win_start > 0:
            parts.append(Text(f"  ↑ {win_start} more above", style=FG4))
        parts.append(table)
        remaining_below = total - win_end
        if remaining_below > 0:
            parts.append(Text(f"  ↓ {remaining_below} more below", style=FG4))

        self.update(Group(*parts) if len(parts) > 1 else parts[0])

    def _visible_window(self, *, comfortable: bool) -> tuple[int, int]:
        """Return ``(start, end)`` indices for the rows to render.

        Keeps the selected row inside the visible window so cursor
        movement remains meaningful when the list is taller than the
        panel viewport. The viewport is read from the parent container
        because :class:`Static` sizes itself to its content (``height:
        auto``), which would otherwise grow without bound.
        """
        total = len(self._row_cache)
        if total == 0:
            return 0, 0
        parent = self.parent
        viewport_widget = parent if isinstance(parent, Widget) else self
        # ``- 3`` reserves space for the table header and the optional
        # ``↑/↓ N more`` indicator rows. Even on a partially mounted
        # widget where ``size.height`` is ``0``, the ``max(..., 5)``
        # fallback guarantees at least five rows are kept visible so
        # ``j/k`` cannot park the cursor permanently outside the window.
        viewport = viewport_widget.size.height - 3
        row_lines = 2 if comfortable else 1
        visible = max(viewport // row_lines, 5)
        if total <= visible:
            return 0, total
        half = visible // 2
        start = self.selected_index - half
        start = max(0, min(start, total - visible))
        return start, start + visible


def _build_row_cells(
    item: SessionListItemView,
    *,
    selected: bool,
    preferences: UiPreferences,
) -> tuple[Text, ...]:
    """Construct the 7 ``Text`` cells for a single row.

    Pulled out of ``_render_table`` so row cells can be pre-built once
    per ``set_sessions`` call and re-used on every cursor move.
    """
    comfortable = preferences.density is UiDensity.COMFORTABLE
    color = _STATUS_COLORS.get(item.status, FG4)
    row_style = f"bold {FG}" if selected else FG4
    pointer = ui_symbol("collapsed", preferences=preferences) if selected else " "
    summary_text = Text()
    if item.origin == "windows":
        summary_text.append("[win] ", style=f"bold {BLUE}")
    summary_text.append(item.summary[: 64 if comfortable else 50], style=row_style)
    if comfortable:
        detail_parts = [item.session_id[:12], item.last_event_type]
        cwd_tail = item.cwd.rstrip("/").split("/")[-1] if item.cwd else ""
        if cwd_tail:
            detail_parts.append(cwd_tail)
        summary_text.append("\n  ", style=FG4)
        summary_text.append(item_separator(preferences).join(detail_parts[:3]), style=FG4)
    return (
        Text(
            f"{pointer}{_session_status_glyph(item.status, preferences=preferences)}",
            style=row_style,
        ),
        summary_text,
        Text(item.repository, style=AQUA if selected else FG4),
        Text(item.branch[:20], style=YELLOW if selected else FG4),
        Text(item.updated, style=row_style),
        Text(str(item.checkpoint_count), style=row_style),
        Text(item.status, style=f"bold {color}"),
    )


class SessionDetailPanel(Static):
    """Detail panel showing selected session metadata."""

    def __init__(self, *, widget_id: str | None = None, classes: str | None = None) -> None:
        super().__init__(id=widget_id, classes=classes)

    def set_detail(self, detail: SessionDetailView | None) -> None:
        preferences = resolve_ui_preferences(self)
        if detail is None:
            self.update(Text("No session selected", style=FG4))
            return

        content = Text()
        color = _STATUS_COLORS.get(detail.status, FG4)

        status_glyph = _session_status_glyph(detail.status, preferences=preferences)
        content.append(f" {status_glyph} {detail.status.upper()} ", style=f"bold {color}")
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
        content.append("\n")

        content.append("  Usage       ", style=f"bold {FG4}")
        content.append(
            detail.usage_summary,
            style=AQUA if detail.usage_available else FG4,
        )
        content.append("\n")
        if detail.premium_requests is not None:
            content.append("  Premium     ", style=f"bold {FG4}")
            content.append(detail.premium_requests, style=BLUE)
            content.append("\n")
        content.append("\n")

        if detail.is_resumable:
            content.append("  Resume: ", style=f"bold {FG4}")
            content.append(detail.resume_command, style=f"bold {GREEN}")
            content.append("\n")
            content.append("  Press ", style=FG4)
            content.append("R", style=f"bold {AQUA}")
            content.append(" to resume in a new tmux window", style=FG4)
        else:
            content.append("  Session completed cleanly", style=FG4)

        self.update(content)


class SessionSummaryBar(Static):
    """Summary bar showing session counts."""

    def __init__(self, *, widget_id: str | None = None, classes: str | None = None) -> None:
        super().__init__(id=widget_id, classes=classes)

    def set_counts(
        self,
        total: int,
        active: int,
        unclosed: int,
        completed: int,
    ) -> None:
        preferences = resolve_ui_preferences(self)
        text = Text()
        text.append(f" {total} ", style=f"bold {FG}")
        text.append("sessions  ", style=FG4)
        text.append(
            f"{_session_status_glyph('active', preferences=preferences)} {active} ",
            style=f"bold {GREEN}",
        )
        text.append("active  ", style=FG4)
        text.append(
            f"{_session_status_glyph('unclosed', preferences=preferences)} {unclosed} ",
            style=f"bold {RED}",
        )
        text.append("unclosed  ", style=FG4)
        text.append(
            f"{_session_status_glyph('completed', preferences=preferences)} {completed} ",
            style=FG4,
        )
        text.append("completed", style=FG4)
        self.update(text)

    def show_loading(
        self,
        *,
        filter_text: str,
        show_completed: bool,
    ) -> None:
        preferences = resolve_ui_preferences(self)
        text = Text()
        text.append(" loading sessions… ", style=f"bold {AQUA}")
        text.append("discovering local session state", style=FG4)
        if filter_text.strip():
            text.append(pipe_separator(preferences), style=FG4)
            text.append("filter ", style=FG4)
            text.append(filter_text.strip(), style=YELLOW)
        text.append(pipe_separator(preferences), style=FG4)
        text.append("completed ", style=FG4)
        text.append("shown" if show_completed else "hidden", style=FG)
        self.update(text)


class SessionActionBar(Static):
    """Visible session actions + filter state."""

    def __init__(self, *, widget_id: str | None = None, classes: str | None = None) -> None:
        super().__init__(id=widget_id, classes=classes)

    def show_loading(
        self,
        *,
        filter_text: str,
        show_completed: bool,
    ) -> None:
        text = Text()
        text.append(" preparing actions… ", style=f"bold {AQUA}")
        text.append("Replay opens the selected session once discovery finishes", style=FG4)
        text.append("\n")
        _append_action(text, "↵", "replay", enabled=False)
        _append_action(text, "l", "live mirror", enabled=False)
        _append_action(text, "R", "resume", enabled=False)
        _append_action(text, "p", "focus pane", enabled=False)
        _append_action(text, "y", "copy command", enabled=False)
        text.append("  ")
        text.append("x", style=f"bold {AQUA}")
        text.append(f" {'show' if not show_completed else 'hide'} completed", style=FG)
        if filter_text.strip():
            text.append("  ")
            text.append("filter ", style=FG4)
            text.append(filter_text.strip(), style=YELLOW)
        self.update(text)

    def set_state(
        self,
        detail: SessionDetailView | None,
        *,
        has_live_pane: bool,
        filter_text: str,
        show_completed: bool,
    ) -> None:
        preferences = resolve_ui_preferences(self)
        if detail is None:
            text = Text()
            text.append(" no session selected ", style=f"bold {FG}")
            if filter_text.strip():
                text.append(pipe_separator(preferences), style=FG4)
                text.append("filter ", style=FG4)
                text.append(filter_text.strip(), style=YELLOW)
            text.append("\n")
            _append_action(text, "↵", "replay", enabled=False)
            _append_action(text, "l", "live mirror", enabled=False)
            _append_action(text, "R", "resume", enabled=False)
            _append_action(text, "p", "focus pane", enabled=False)
            _append_action(text, "y", "copy command", enabled=False)
            self.update(text)
            return
        status_style = _STATUS_COLORS.get(detail.status, FG4)
        text = Text()
        _append_chip(
            text,
            "selected",
            detail.session_id[:12],
            value_style=f"bold {ORANGE}",
            preferences=preferences,
        )
        _append_chip(
            text,
            "status",
            detail.status,
            value_style=f"bold {status_style}",
            preferences=preferences,
        )
        _append_chip(text, "repo", detail.repository, value_style=AQUA, preferences=preferences)
        _append_chip(text, "branch", detail.branch, value_style=YELLOW, preferences=preferences)
        _append_chip(
            text,
            "checkpoints",
            str(detail.checkpoint_count),
            value_style=BLUE,
            preferences=preferences,
        )
        _append_chip(
            text,
            "usage",
            detail.usage_badge,
            value_style=AQUA if detail.usage_available else FG4,
            preferences=preferences,
        )
        if detail.premium_requests is not None:
            _append_chip(
                text,
                "premium",
                detail.premium_requests,
                value_style=BLUE,
                preferences=preferences,
            )
        if detail.origin == "windows":
            _append_chip(text, "host", "windows", value_style=BLUE, preferences=preferences)
        if filter_text.strip():
            _append_chip(
                text,
                "filter",
                filter_text.strip(),
                value_style=YELLOW,
                preferences=preferences,
            )
        _append_chip(
            text,
            "completed",
            "shown" if show_completed else "hidden",
            value_style=FG,
            preferences=preferences,
        )
        text.append("\n")
        _append_action(text, "↵", "replay")
        _append_action(text, "l", "live mirror", enabled=has_live_pane)
        _append_action(text, "R", "resume", enabled=detail.is_resumable)
        _append_action(text, "p", "focus pane", enabled=has_live_pane)
        _append_action(text, "y", "copy command")
        self.update(text)


__all__ = [
    "SessionActionBar",
    "SessionDetailPanel",
    "SessionListPanel",
    "SessionSelected",
    "SessionSummaryBar",
]
