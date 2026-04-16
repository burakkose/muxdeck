from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, cast

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Input, ListView
from textual.worker import Worker, WorkerState

from copilot_commander.bindings import REPLAY_BINDINGS, REPLAY_HINTS
from copilot_commander.controllers import ReplayStateView, ReplayTranscriptEntryView
from copilot_commander.controllers.replay_controller import (
    ReplayExportFormat,
    ReplayPresentation,
)
from copilot_commander.screens.base import ShellScreen
from copilot_commander.widgets.replay import (
    ReplayDetailPanel,
    ReplayFilterBar,
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
        self._filter_text: str = ""
        self._presentation: ReplayPresentation = "parsed"
        self._follow_latest: bool = True
        self._refreshing: bool = False
        self._filter_debounce_timer: object | None = None
        self._load_version: int = 0

    def compose_body(self) -> ComposeResult:
        with Vertical(id="replay-root"):
            yield ReplaySummaryPanel(id="replay-summary", classes="muted")
            yield ReplayFilterBar(id="replay-filter-row")
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
        self._refreshing = True
        try:
            self._refresh_data_inner()
        finally:
            self._refreshing = False

    def _refresh_data_inner(self) -> None:
        filter_bar = self.query_one(ReplayFilterBar)
        filter_bar.set_query(self._filter_text)
        resolved_session_id = self.commander_app.resolve_replay_session_id(self._session_id)
        self._session_id = resolved_session_id
        if resolved_session_id is None:
            self._state = None
            self.query_one(ReplaySummaryPanel).set_state(None)
            self.query_one(ReplayMarkerListPanel).set_markers((), selected_index=None)
            self.query_one(ReplayTranscriptPanel).set_transcript(())
            self.query_one(ReplayDetailPanel).set_entry(None)
            self.set_status("no replayable sessions")
            return
        # Offload the heavy load_state call to a worker thread so the UI
        # stays responsive.  A version token prevents stale results from
        # overwriting newer ones when the user types faster than the worker.
        self._load_version += 1
        session_id = resolved_session_id
        selected_index = self._selected_index
        filter_text = self._filter_text
        presentation = self._presentation
        follow_latest = self._follow_latest

        def _load() -> ReplayStateView:
            # Use thread-safe replay controller for worker thread access
            replay = self.runtime.replay_worker or self.runtime.replay
            return replay.load_state(
                session_id=session_id,
                selected_index=selected_index,
                filter_text=filter_text,
                presentation=presentation,
                follow_latest=follow_latest,
            )

        self.run_worker(_load, thread=True, exclusive=True, name="replay_load")

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name != "replay_load":
            return
        if event.state == WorkerState.ERROR:
            self._state = None
            self._refresh_panels()
            self.set_status("replay load failed — no session data available")
            return
        if event.state != WorkerState.SUCCESS:
            return
        state: ReplayStateView = event.worker.result
        self._state = state
        self._selected_index = state.selected_index
        session_id = self._session_id
        if session_id is not None:
            self.commander_app.remember_session_selection(session_id)
        self._refresh_panels()
        status = (
            f"session {session_id} | "
            f"{len(state.transcript)}/{state.total_entries} entries | "
            f"{self._presentation} | follow {'on' if self._follow_latest else 'off'} | "
            f"export {self._export_format}"
        )
        if not state.transcript and self._filter_text.strip():
            status = f"no replay matches for {self._filter_text.strip()}"
        self.set_status(status)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "replay-filter-input":
            return
        self._filter_text = event.value
        if self._filter_debounce_timer is not None:
            self._filter_debounce_timer.stop()  # type: ignore[union-attr]
        self._filter_debounce_timer = self.set_timer(0.3, self.refresh_data)

    def on_replay_marker_list_panel_marker_selected(
        self,
        message: ReplayMarkerListPanel.MarkerSelected,
    ) -> None:
        if self._state is None:
            return
        self._state = self.runtime.replay.jump_to_marker(self._state, message.marker_ordinal)
        self._selected_index = self._state.selected_index
        self._release_follow_latest()
        self._refresh_panels()
        marker = self._state.jump_markers[message.marker_ordinal]
        self.set_status(f"jumped to {marker.kind}: {marker.label}")

    def on_replay_transcript_panel_transcript_selected(
        self,
        message: ReplayTranscriptPanel.TranscriptSelected,
    ) -> None:
        if self._state is None or self._session_id is None:
            return
        if self._refreshing:
            return
        if self._selected_index == message.transcript_index:
            return
        self._selected_index = message.transcript_index
        if self._follow_latest and message.transcript_index != self._latest_visible_index():
            self._follow_latest = False
        self.refresh_data()

    def action_cursor_down(self) -> None:
        self._active_list().move_cursor(1)

    def action_cursor_up(self) -> None:
        self._active_list().move_cursor(-1)

    def action_focus_filter(self) -> None:
        self.query_one(ReplayFilterBar).focus_input()
        self.set_status("filter transcript")

    def action_escape_filter(self) -> None:
        """Return focus to the active list (ESC from filter)."""
        self._active_list().focus_list()

    def action_focus_markers(self) -> None:
        self.query_one(ReplayMarkerListPanel).focus_list()
        self.set_status("marker focus")

    def action_focus_transcript(self) -> None:
        self.query_one(ReplayTranscriptPanel).focus_list()
        self.set_status("transcript focus")

    def action_toggle_presentation(self) -> None:
        self._presentation = "raw" if self._presentation == "parsed" else "parsed"
        self.refresh_data()
        self.set_status(f"view {self._presentation}")

    def action_toggle_follow_latest(self) -> None:
        self._follow_latest = not self._follow_latest
        if self._follow_latest:
            self._selected_index = None
        self.refresh_data()
        self.set_status(f"follow latest {'on' if self._follow_latest else 'off'}")

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
        self._selected_index = None if self._follow_latest else 0
        self.refresh_data()

    def action_jump_next_marker(self) -> None:
        self._jump_from_state("marker", self.runtime.replay.jump_to_next_marker)

    def action_jump_previous_marker(self) -> None:
        self._jump_from_state("marker", self.runtime.replay.jump_to_previous_marker)

    def action_jump_next_activity(self) -> None:
        self._jump_from_state("activity", self.runtime.replay.jump_to_next_activity)

    def action_jump_next_problem(self) -> None:
        self._jump_from_state("problem", self.runtime.replay.jump_to_next_problem)

    def _jump_from_state(
        self,
        label: str,
        jump: Callable[[ReplayStateView], ReplayStateView | None],
    ) -> None:
        if self._state is None:
            return
        next_state = jump(self._state)
        if next_state is None:
            self.set_status(f"no {label} markers")
            return
        self._state = next_state
        self._selected_index = next_state.selected_index
        self._release_follow_latest()
        self._refresh_panels()
        entry = self._selected_entry()
        target = entry.label if entry is not None else label
        self.set_status(f"jumped to {label}: {target}")

    def _refresh_panels(self) -> None:
        summary = self.query_one(ReplaySummaryPanel)
        markers = self.query_one(ReplayMarkerListPanel)
        transcript = self.query_one(ReplayTranscriptPanel)
        detail = self.query_one(ReplayDetailPanel)
        summary.set_state(self._state)
        if self._state is None:
            markers.set_markers((), selected_index=None)
            transcript.set_transcript(())
            detail.set_entry(None)
            return
        markers.set_markers(self._state.jump_markers, selected_index=self._state.selected_index)
        transcript.set_transcript(self._state.transcript)
        detail.set_entry(self._selected_entry())

    def _release_follow_latest(self) -> None:
        if self._follow_latest:
            self._follow_latest = False

    def _latest_visible_index(self) -> int | None:
        if self._state is None or not self._state.transcript:
            return None
        return self._state.transcript[-1].ordinal

    def _selected_entry(self) -> ReplayTranscriptEntryView | None:
        if self._state is None:
            return None
        return next((entry for entry in self._state.transcript if entry.is_selected), None)

    def _active_list(self) -> ReplayMarkerListPanel | ReplayTranscriptPanel:
        focused = self.focused
        if isinstance(focused, ListView) and focused.id == "replay-marker-list":
            return self.query_one(ReplayMarkerListPanel)
        return self.query_one(ReplayTranscriptPanel)
