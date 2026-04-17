from __future__ import annotations

from collections.abc import Sequence

from rich.syntax import Syntax
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Input, ListItem, ListView, Static

from copilot_commander.bindings import REPLAY_BINDINGS
from copilot_commander.controllers import (
    ReplayJumpMarkerView,
    ReplayStateView,
    ReplayTranscriptEntryView,
)
from copilot_commander.theme import AQUA, BLUE, FG, FG1, FG4, GREEN, ORANGE, SEVERITY_ERROR, YELLOW


def _marker_style(kind: str) -> str:
    if kind == "error":
        return f"bold {SEVERITY_ERROR}"
    if kind == "blocking":
        return f"bold {ORANGE}"
    if kind == "activity":
        return f"bold {GREEN}"
    if kind == "boundary":
        return f"bold {AQUA}"
    if kind == "file_edit":
        return f"bold {BLUE}"
    if kind == "tool_call":
        return f"bold {AQUA}"
    return f"bold {YELLOW}"


class ReplayFilterBar(Vertical):
    def compose(self) -> ComposeResult:
        yield Input(placeholder="/ search transcript", id="replay-filter-input")

    def set_query(self, value: str) -> None:
        self.query_one(Input).value = value

    def focus_input(self) -> None:
        self.query_one(Input).focus()


class ReplayBoundListView(ListView):
    BINDINGS = REPLAY_BINDINGS

    def _invoke_screen_action(self, action_name: str) -> None:
        handler = getattr(self.screen, f"action_{action_name}", None)
        if callable(handler):
            handler()

    def action_cursor_down(self) -> None:
        self._invoke_screen_action("cursor_down")

    def action_cursor_up(self) -> None:
        self._invoke_screen_action("cursor_up")

    def action_focus_filter(self) -> None:
        self._invoke_screen_action("focus_filter")

    def action_focus_markers(self) -> None:
        self._invoke_screen_action("focus_markers")

    def action_focus_transcript(self) -> None:
        self._invoke_screen_action("focus_transcript")

    def action_toggle_presentation(self) -> None:
        self._invoke_screen_action("toggle_presentation")

    def action_toggle_follow_latest(self) -> None:
        self._invoke_screen_action("toggle_follow_latest")

    def action_jump_next_marker(self) -> None:
        self._invoke_screen_action("jump_next_marker")

    def action_jump_previous_marker(self) -> None:
        self._invoke_screen_action("jump_previous_marker")

    def action_jump_next_activity(self) -> None:
        self._invoke_screen_action("jump_next_activity")

    def action_jump_next_problem(self) -> None:
        self._invoke_screen_action("jump_next_problem")

    def action_jump_next_file_edit(self) -> None:
        self._invoke_screen_action("jump_next_file_edit")

    def action_cycle_export_format(self) -> None:
        self._invoke_screen_action("cycle_export_format")

    def action_load_latest(self) -> None:
        self._invoke_screen_action("load_latest")


class ReplayMarkerListPanel(Vertical):
    class MarkerSelected(Message):
        def __init__(self, marker_ordinal: int) -> None:
            super().__init__()
            self.marker_ordinal = marker_ordinal

    def __init__(self, *, widget_id: str | None = None, classes: str | None = None) -> None:
        super().__init__(id=widget_id, classes=classes)
        self._marker_ordinals: list[int] = []

    def on_mount(self) -> None:
        pass  # borderless

    def compose(self) -> ComposeResult:
        yield ReplayBoundListView(id="replay-marker-list")

    def set_markers(
        self,
        markers: Sequence[ReplayJumpMarkerView],
        *,
        selected_index: int | None,
    ) -> None:
        list_view = self.query_one(ListView)
        list_view.clear()
        self._marker_ordinals = []
        highlighted_marker = 0
        for index, marker in enumerate(markers):
            line = Text()
            line.append(f"{marker.timestamp[11:19]} ", style=FG4)
            line.append(f"{marker.kind:<8.8} ", style=_marker_style(marker.kind))
            line.append(marker.label, style=FG1)
            list_view.append(ListItem(Static(line)))
            self._marker_ordinals.append(index)
            if selected_index is not None and marker.index <= selected_index:
                highlighted_marker = index
        if self._marker_ordinals:
            list_view.index = highlighted_marker

    def move_cursor(self, delta: int) -> None:
        if not self._marker_ordinals:
            return
        list_view = self.query_one(ListView)
        current = list_view.index if list_view.index is not None else 0
        list_view.index = max(0, min(len(self._marker_ordinals) - 1, current + delta))
        list_view.focus()
        self._post_selection(list_view.index)

    def focus_list(self) -> None:
        self.query_one(ListView).focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        del event
        self._post_selection(self.query_one(ListView).index)

    def _post_selection(self, index: int | None) -> None:
        if index is None or index >= len(self._marker_ordinals):
            return
        self.post_message(self.MarkerSelected(self._marker_ordinals[index]))


class ReplayTranscriptPanel(Vertical):
    class TranscriptSelected(Message):
        def __init__(self, transcript_index: int) -> None:
            super().__init__()
            self.transcript_index = transcript_index

    def __init__(self, *, widget_id: str | None = None, classes: str | None = None) -> None:
        super().__init__(id=widget_id, classes=classes)
        self._ordinals: list[int] = []
        self._rebuilding: bool = False

    def on_mount(self) -> None:
        pass  # borderless

    def compose(self) -> ComposeResult:
        yield ReplayBoundListView(id="replay-transcript-list")

    def set_transcript(self, transcript: Sequence[ReplayTranscriptEntryView]) -> None:
        self._rebuilding = True
        try:
            list_view = self.query_one(ListView)
            list_view.clear()
            self._ordinals = []
            selected_index = 0
            for index, entry in enumerate(transcript):
                line = Text()
                caret_style = f"bold {BLUE}" if entry.is_selected else FG4
                line.append("▸ " if entry.is_selected else "  ", style=caret_style)
                line.append(f"{entry.timestamp[11:19]} ", style=FG4)
                entry_style = _marker_style(entry.marker_kind or entry.severity or entry.kind)
                line.append(f"{(entry.marker_kind or entry.kind):<8.8} ", style=entry_style)
                line.append(f"{entry.label:<24.24} ", style=FG1)
                preview = entry.lines[0] if entry.lines else ""
                line.append(preview, style=FG4)
                list_view.append(ListItem(Static(line)))
                self._ordinals.append(entry.ordinal)
                if entry.is_selected:
                    selected_index = index
            if self._ordinals:
                list_view.index = selected_index
        finally:
            self._rebuilding = False

    def move_cursor(self, delta: int) -> None:
        if not self._ordinals:
            return
        list_view = self.query_one(ListView)
        current = list_view.index if list_view.index is not None else 0
        list_view.index = max(0, min(len(self._ordinals) - 1, current + delta))
        list_view.focus()
        self._post_selection(list_view.index)

    def focus_list(self) -> None:
        self.query_one(ListView).focus()

    def last_transcript_index(self) -> int | None:
        return self._ordinals[-1] if self._ordinals else None

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        del event
        if self._rebuilding:
            return
        self._post_selection(self.query_one(ListView).index)

    def _post_selection(self, index: int | None) -> None:
        if index is None or index >= len(self._ordinals):
            return
        self.post_message(self.TranscriptSelected(self._ordinals[index]))


class ReplaySummaryPanel(Static):
    def set_state(self, state: ReplayStateView | None) -> None:
        if state is None:
            self.update(Text("No replayable sessions", style=FG4))
            return
        line = Text()
        items = (
            ("session", state.session_id, FG1),
            ("agent", state.agent_id, FG1),
            ("task", state.task_title or "-", FG1),
            ("entries", f"{len(state.transcript)}/{state.total_entries}", FG1),
            ("markers", f"{len(state.jump_markers)}/{state.total_markers}", FG1),
            ("files", str(state.files_touched), BLUE if state.files_touched else FG4),
            ("tools", str(state.tool_calls), AQUA if state.tool_calls else FG4),
            ("view", state.presentation, AQUA),
            (
                "follow",
                "on" if state.follow_latest else "off",
                GREEN if state.follow_latest else FG4,
            ),
        )
        for label, value, style in items:
            if line.plain:
                line.append(" │ ", style=FG4)
            line.append(f"{label} ", style=FG4)
            line.append(str(value), style=style)
        if state.filter_text.strip():
            line.append(" │ ", style=FG4)
            line.append("filter ", style=FG4)
            line.append(state.filter_text.strip(), style=YELLOW)
        self.update(line)


class ReplayDetailPanel(Static):
    def set_entry(self, entry: ReplayTranscriptEntryView | None) -> None:
        if entry is None:
            self.update(Text("No entry selected", style=FG4))
            return
        header = Text()
        header.append(f"#{entry.ordinal} ", style=f"bold {BLUE}")
        header.append(f"{entry.kind} ", style=_marker_style(entry.marker_kind or entry.kind))
        header.append(entry.timestamp, style=FG4)
        if entry.severity:
            header.append(f" [{entry.severity}]", style=_marker_style(entry.severity))
        header.append(f"\n{entry.label}", style=FG1)
        if entry.lines:
            header.append("\n")
            header.append("\n".join(entry.lines), style=FG)
        self.update(header)


class ReplayDiffPanel(Static):
    """Render a unified diff for the selected file-mutation entry.

    The screen owns diff resolution (so it can run off the UI thread)
    and feeds the result to :meth:`set_entry_diff`. The widget is
    purely presentation: it picks a placeholder when the entry has no
    file mutation or when the diff text is empty/whitespace.
    """

    def set_entry_diff(
        self,
        entry: ReplayTranscriptEntryView | None,
        diff_text: str | None,
    ) -> None:
        if entry is None:
            self.update(Text("No entry selected", style=FG4))
            return
        if not entry.file_path:
            self.update(Text("non-file entry", style=FG4))
            return
        if diff_text is None or not diff_text.strip():
            self.update(Text(f"no diff available for {entry.file_path}", style=FG4))
            return
        self.update(Syntax(diff_text, "diff", theme="ansi_dark", word_wrap=False))


__all__ = [
    "ReplayDetailPanel",
    "ReplayDiffPanel",
    "ReplayFilterBar",
    "ReplayMarkerListPanel",
    "ReplaySummaryPanel",
    "ReplayTranscriptPanel",
]
