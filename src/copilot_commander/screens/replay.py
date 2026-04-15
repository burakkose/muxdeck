from __future__ import annotations

from typing import TYPE_CHECKING, cast

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import ListView

from copilot_commander.bindings import REPLAY_BINDINGS, REPLAY_HINTS
from copilot_commander.controllers import ReplayStateView, ReplayTranscriptEntryView
from copilot_commander.controllers.replay_controller import ReplayExportFormat
from copilot_commander.screens.base import ShellScreen
from copilot_commander.widgets.replay import (
    ReplayDetailPanel,
    ReplayMarkerListPanel,
    ReplaySummaryPanel,
    ReplayTranscriptPanel,
)

if TYPE_CHECKING:
    from copilot_commander.app import CommanderApp, CommanderRuntime


class ReplayScreen(ShellScreen):
    SCREEN_TITLE = "REPLAY"
    BINDINGS = REPLAY_BINDINGS
    FOOTER_HINTS = REPLAY_HINTS

    def __init__(self, runtime: CommanderRuntime) -> None:
        super().__init__(runtime)
        self._session_id: str | None = None
        self._selected_index: int | None = None
        self._state: ReplayStateView | None = None
        self._export_format: ReplayExportFormat = "text"

    def compose_body(self) -> ComposeResult:
        with Vertical(id="replay-root"):
            yield ReplaySummaryPanel(id="replay-summary", classes="muted")
            with Horizontal(id="replay-main", classes="frame"):
                yield ReplayMarkerListPanel(widget_id="replay-markers", classes="divider-right")
                yield ReplayTranscriptPanel(widget_id="replay-transcript", classes="section")
            yield ReplayDetailPanel(id="replay-detail", classes="frame")

    def on_mount(self) -> None:
        self.refresh_data()
        self.call_after_refresh(self.query_one(ReplayTranscriptPanel).focus_list)

    def on_show(self) -> None:
        self.refresh_data()

    @property
    def commander_app(self) -> CommanderApp:
        return cast("CommanderApp", self.app)

    def refresh_data(self) -> None:
        resolved_session_id = self.commander_app.resolve_replay_session_id(self._session_id)
        self._session_id = resolved_session_id
        if resolved_session_id is None:
            self._state = None
            self.query_one(ReplaySummaryPanel).set_state(None)
            self.query_one(ReplayMarkerListPanel).set_markers(())
            self.query_one(ReplayTranscriptPanel).set_transcript(())
            self.query_one(ReplayDetailPanel).set_entry(None)
            self.set_status("no replayable sessions")
            return
        selected_index = 0 if self._selected_index is None else self._selected_index
        self._state = self.runtime.replay.load_state(
            session_id=resolved_session_id,
            selected_index=selected_index,
        )
        self.commander_app.remember_session_selection(resolved_session_id)
        self.query_one(ReplaySummaryPanel).set_state(self._state)
        self.query_one(ReplayMarkerListPanel).set_markers(self._state.jump_markers)
        self.query_one(ReplayTranscriptPanel).set_transcript(self._state.transcript)
        self.query_one(ReplayDetailPanel).set_entry(self._selected_entry())
        self.set_status(
            f"session {resolved_session_id} | "
            f"{len(self._state.transcript)} entries | export {self._export_format}"
        )

    def on_replay_marker_list_panel_marker_selected(
        self,
        message: ReplayMarkerListPanel.MarkerSelected,
    ) -> None:
        if self._state is None:
            return
        self._state = self.runtime.replay.jump_to_marker(self._state, message.marker_ordinal)
        self._selected_index = self._state.selected_index
        self.query_one(ReplayTranscriptPanel).set_transcript(self._state.transcript)
        self.query_one(ReplayDetailPanel).set_entry(self._selected_entry())
        self.set_status(f"jumped to marker {message.marker_ordinal}")

    def on_replay_transcript_panel_transcript_selected(
        self,
        message: ReplayTranscriptPanel.TranscriptSelected,
    ) -> None:
        if self._state is None or self._session_id is None:
            return
        self._selected_index = message.transcript_index
        self.refresh_data()

    def action_cursor_down(self) -> None:
        self._active_list().move_cursor(1)

    def action_cursor_up(self) -> None:
        self._active_list().move_cursor(-1)

    def action_focus_markers(self) -> None:
        self.query_one(ReplayMarkerListPanel).focus_list()
        self.set_status("marker focus")

    def action_focus_transcript(self) -> None:
        self.query_one(ReplayTranscriptPanel).focus_list()
        self.set_status("transcript focus")

    def action_cycle_export_format(self) -> None:
        self._export_format = "json" if self._export_format == "text" else "text"
        if self._state is None:
            self.set_status(f"export {self._export_format}")
            return
        intent = self.runtime.replay.build_export_intent(
            self._state,
            export_format=self._export_format,
        )
        self.set_status(f"export {intent.format} -> {intent.filename_hint}")

    def action_load_latest(self) -> None:
        self._session_id = None
        self._selected_index = 0
        self.refresh_data()

    def _selected_entry(self) -> ReplayTranscriptEntryView | None:
        if self._state is None:
            return None
        return next((entry for entry in self._state.transcript if entry.is_selected), None)

    def _active_list(self) -> ReplayMarkerListPanel | ReplayTranscriptPanel:
        focused = self.focused
        if isinstance(focused, ListView) and focused.id == "replay-marker-list":
            return self.query_one(ReplayMarkerListPanel)
        return self.query_one(ReplayTranscriptPanel)
