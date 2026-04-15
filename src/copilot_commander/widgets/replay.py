from __future__ import annotations

from collections.abc import Sequence

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import ListItem, ListView, Static

from copilot_commander.controllers import (
    ReplayJumpMarkerView,
    ReplayStateView,
    ReplayTranscriptEntryView,
)
from copilot_commander.theme import BLUE, FG, FG1, FG4, YELLOW


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
        yield ListView(id="replay-marker-list")

    def set_markers(self, markers: Sequence[ReplayJumpMarkerView]) -> None:
        list_view = self.query_one(ListView)
        list_view.clear()
        self._marker_ordinals = []
        for index, marker in enumerate(markers):
            line = Text()
            line.append(f"{marker.timestamp[11:19]} ", style=FG4)
            line.append(f"{marker.kind:<6.6} ", style=f"bold {YELLOW}")
            line.append(marker.label, style=FG1)
            list_view.append(ListItem(Static(line)))
            self._marker_ordinals.append(index)
        if self._marker_ordinals:
            list_view.index = (
                0
                if list_view.index is None
                else min(list_view.index, len(self._marker_ordinals) - 1)
            )

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

    def on_mount(self) -> None:
        pass  # borderless

    def compose(self) -> ComposeResult:
        yield ListView(id="replay-transcript-list")

    def set_transcript(self, transcript: Sequence[ReplayTranscriptEntryView]) -> None:
        list_view = self.query_one(ListView)
        list_view.clear()
        self._ordinals = []
        selected_index = 0
        for index, entry in enumerate(transcript):
            line = Text()
            if entry.is_selected:
                line.append("▸ ", style=f"bold {BLUE}")
            else:
                line.append("  ")
            line.append(f"{entry.timestamp[11:19]} ", style=FG4)
            line.append(f"{entry.kind:<8.8} ", style=f"bold {YELLOW}")
            line.append(f"{entry.label:<16.16} ", style=FG1)
            preview = entry.lines[0] if entry.lines else ""
            line.append(preview, style=FG4)
            list_view.append(ListItem(Static(line)))
            self._ordinals.append(entry.ordinal)
            if entry.is_selected:
                selected_index = index
        if self._ordinals:
            list_view.index = selected_index

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

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        del event
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
        for label, value in (
            ("session", state.session_id),
            ("agent", state.agent_id),
            ("task", state.task_title or "-"),
            ("entries", str(len(state.transcript))),
            ("markers", str(len(state.jump_markers))),
        ):
            if line.plain:
                line.append(" │ ", style=FG4)
            line.append(f"{label} ", style=FG4)
            line.append(str(value), style=FG1)
        self.update(line)


class ReplayDetailPanel(Static):
    def set_entry(self, entry: ReplayTranscriptEntryView | None) -> None:
        if entry is None:
            self.update(Text("No entry selected", style=FG4))
            return
        header = Text()
        header.append(f"#{entry.ordinal} ", style=f"bold {BLUE}")
        header.append(f"{entry.kind} ", style=f"bold {YELLOW}")
        header.append(entry.timestamp, style=FG4)
        if entry.severity:
            header.append(f" [{entry.severity}]", style=FG4)
        header.append(f"\n{entry.label}", style=FG1)
        if entry.lines:
            header.append("\n")
            header.append("\n".join(entry.lines), style=FG)
        self.update(header)


__all__ = [
    "ReplayDetailPanel",
    "ReplayMarkerListPanel",
    "ReplaySummaryPanel",
    "ReplayTranscriptPanel",
]
