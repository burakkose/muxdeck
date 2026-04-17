from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta

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
    return f"bold {YELLOW}"


class ReplayFilterBar(Vertical):
    def compose(self) -> ComposeResult:
        yield Input(
            placeholder=(
                "/ search  kind:event severity:error agent:foo "
                "marker:activity since:14:30 until:15:00"
            ),
            id="replay-filter-input",
        )

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

    def action_cycle_export_format(self) -> None:
        self._invoke_screen_action("cycle_export_format")

    def action_load_latest(self) -> None:
        self._invoke_screen_action("load_latest")

    def action_chip_errors_only(self) -> None:
        self._invoke_screen_action("chip_errors_only")

    def action_chip_activity(self) -> None:
        self._invoke_screen_action("chip_activity")

    def action_chip_tool_calls(self) -> None:
        self._invoke_screen_action("chip_tool_calls")

    def action_chip_clear(self) -> None:
        self._invoke_screen_action("chip_clear")

    def action_toggle_insights(self) -> None:
        self._invoke_screen_action("toggle_insights")


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


def _format_duration(value: timedelta) -> str:
    total = int(value.total_seconds())
    if total < 0:
        total = 0
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{seconds:02d}s"
    if minutes:
        return f"{minutes}m{seconds:02d}s"
    return f"{seconds}s"


class ReplayInsightsPanel(Static):
    """Read-only summary of replay insights (i to toggle)."""

    def set_state(self, state: ReplayStateView | None) -> None:
        if state is None or state.insights is None:
            self.update(Text("No insights available", style=FG4))
            return
        insights = state.insights
        body = Text()
        body.append("Insights\n", style=f"bold {AQUA}")
        body.append("duration ", style=FG4)
        body.append(_format_duration(insights.total_duration), style=FG1)
        body.append("  longest streak ", style=FG4)
        body.append(_format_duration(insights.longest_activity_streak), style=FG1)
        body.append("  errors ", style=FG4)
        body.append(str(insights.error_count), style=FG1)
        body.append("  files ", style=FG4)
        body.append(str(insights.files_touched), style=FG1)
        body.append("  idle gaps ", style=FG4)
        body.append(str(len(insights.idle_gaps)), style=FG1)
        if insights.idle_gaps:
            body.append("\n")
            for gap in insights.idle_gaps[:3]:
                body.append("  · ", style=FG4)
                body.append(gap.start.isoformat(timespec="seconds"), style=FG4)
                body.append(" → ", style=FG4)
                body.append(gap.end.isoformat(timespec="seconds"), style=FG4)
                body.append("  ", style=FG4)
                body.append(_format_duration(gap.duration), style=YELLOW)
                body.append("\n")
            if len(insights.idle_gaps) > 3:
                body.append(
                    f"  · …{len(insights.idle_gaps) - 3} more\n",
                    style=FG4,
                )
        if insights.top_error_clusters:
            body.append("Top errors\n", style=f"bold {ORANGE}")
            for cluster in insights.top_error_clusters:
                body.append(f"  x{cluster.count} ", style=f"bold {SEVERITY_ERROR}")
                body.append(cluster.canonical, style=FG1)
                body.append("\n")
        self.update(body)


__all__ = [
    "ReplayDetailPanel",
    "ReplayFilterBar",
    "ReplayInsightsPanel",
    "ReplayMarkerListPanel",
    "ReplaySummaryPanel",
    "ReplayTranscriptPanel",
]
