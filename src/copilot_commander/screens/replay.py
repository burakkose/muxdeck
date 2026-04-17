from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, cast

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.timer import Timer
from textual.widgets import Input, ListView
from textual.worker import Worker, WorkerState

from copilot_commander.adapters.diff_adapter import DiffAdapter, DiffPort
from copilot_commander.bindings import REPLAY_BINDINGS, REPLAY_HINTS
from copilot_commander.controllers import ReplayStateView, ReplayTranscriptEntryView
from copilot_commander.controllers.replay_controller import (
    ReplayExportFormat,
    ReplayPresentation,
)
from copilot_commander.screens.base import ShellScreen
from copilot_commander.services.playback_controller import PlaybackState
from copilot_commander.widgets.replay import (
    ReplayDetailPanel,
    ReplayDiffPanel,
    ReplayFilterBar,
    ReplayMarkerListPanel,
    ReplayProgressBar,
    ReplaySummaryPanel,
    ReplayTranscriptPanel,
)

if TYPE_CHECKING:
    from copilot_commander.app import CommanderApp, CommanderRuntime


class ReplayScreen(ShellScreen):
    SCREEN_TITLE = "REPLAY"
    BINDINGS = REPLAY_BINDINGS
    FOOTER_HINTS = REPLAY_HINTS

    def __init__(
        self,
        runtime: CommanderRuntime,
        *,
        diff_adapter: DiffPort | None = None,
    ) -> None:
        super().__init__(runtime)
        self._session_id: str | None = None
        self._session_ids: tuple[str, ...] = ()
        self._selected_index: int | None = None
        self._state: ReplayStateView | None = None
        self._export_format: ReplayExportFormat = "text"
        self._filter_text: str = ""
        self._presentation: ReplayPresentation = "parsed"
        self._follow_latest: bool = True
        self._refreshing: bool = False
        self._filter_debounce_timer: object | None = None
        self._load_version: int = 0
        self._playback: PlaybackState | None = None
        self._playback_timer: Timer | None = None
        self._playback_last_tick: float | None = None
        self._diff_adapter: DiffPort = diff_adapter or DiffAdapter()
        self._diff_version: int = 0
        self._diff_cache: dict[tuple[int, str], str] = {}

    def compose_body(self) -> ComposeResult:
        with Vertical(id="replay-root"):
            yield ReplaySummaryPanel(id="replay-summary", classes="muted")
            yield ReplayProgressBar(id="replay-progress", classes="muted")
            yield ReplayFilterBar(id="replay-filter-row")
            with Horizontal(id="replay-main", classes="frame"):
                yield ReplayMarkerListPanel(widget_id="replay-markers", classes="divider-right")
                yield ReplayTranscriptPanel(widget_id="replay-transcript", classes="section")
            yield ReplayDetailPanel(id="replay-detail", classes="frame")
            yield ReplayDiffPanel(id="replay-diff", classes="frame")

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
        if self._session_ids:
            self._load_version += 1
            session_ids = self._session_ids
            first_load = self._state is None
            if first_load:
                self.begin_loading(
                    self.query_one(ReplayMarkerListPanel),
                    self.query_one(ReplayTranscriptPanel),
                    self.query_one(ReplayDetailPanel),
                )
                self.set_status("loading multi-session replay…")
            selected_index = self._selected_index
            filter_text = self._filter_text
            presentation = self._presentation
            follow_latest = self._follow_latest

            def _load_multi() -> ReplayStateView:
                replay = self.runtime.replay_worker or self.runtime.replay
                return replay.load_multi_state(
                    session_ids,
                    selected_index=selected_index,
                    filter_text=filter_text,
                    presentation=presentation,
                    follow_latest=follow_latest,
                )

            self.run_worker(_load_multi, thread=True, exclusive=True, name="replay_load")
            return
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
        first_load = self._state is None
        if first_load:
            self.begin_loading(
                self.query_one(ReplayMarkerListPanel),
                self.query_one(ReplayTranscriptPanel),
                self.query_one(ReplayDetailPanel),
            )
            self.set_status("loading replay…")
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
        if event.worker.name == "replay_diff":
            if event.state != WorkerState.SUCCESS:
                return
            result = cast(
                "tuple[int, tuple[int, str], str] | None",
                event.worker.result,
            )
            if result is None:
                return
            version, cache_key, text = result
            if version != self._diff_version:
                return
            self._diff_cache[cache_key] = text
            self._refresh_panels()
            return
        if event.worker.name != "replay_load":
            return
        if event.state == WorkerState.ERROR:
            self.end_loading(
                self.query_one(ReplayMarkerListPanel),
                self.query_one(ReplayTranscriptPanel),
                self.query_one(ReplayDetailPanel),
            )
            self._state = None
            self._refresh_panels()
            self.set_status("replay load failed — no session data available")
            return
        if event.state != WorkerState.SUCCESS:
            return
        self.end_loading(
            self.query_one(ReplayMarkerListPanel),
            self.query_one(ReplayTranscriptPanel),
            self.query_one(ReplayDetailPanel),
        )
        state: ReplayStateView = event.worker.result
        self._state = state
        self._selected_index = state.selected_index
        session_id = self._session_id
        if session_id is not None:
            self.commander_app.remember_session_selection(session_id)
        self._initialize_playback(state)
        self._refresh_panels()
        status = (
            f"session {session_id} | "
            f"{len(state.transcript)}/{state.total_entries} entries | "
            f"files {state.files_touched} | tools {state.tool_calls} | "
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
        self._session_ids = ()
        self._selected_index = None if self._follow_latest else 0
        self.refresh_data()

    def action_open_multi_session_picker(self) -> None:
        prefill = ", ".join(self._session_ids) if self._session_ids else (self._session_id or "")
        from copilot_commander.screens.multi_session_picker import MultiSessionPickerScreen

        def _on_done(result: tuple[str, ...] | None) -> None:
            if result is None or not result:
                self.set_status("multi-session picker cancelled")
                return
            self._session_ids = result
            self._session_id = result[0]
            self._selected_index = None
            self._state = None
            self.refresh_data()
            self.set_status(f"merging {len(result)} session(s)")

        self.app.push_screen(MultiSessionPickerScreen(prefill=prefill), _on_done)

    def action_jump_next_marker(self) -> None:
        self._jump_from_state("marker", self.runtime.replay.jump_to_next_marker)

    def action_jump_previous_marker(self) -> None:
        self._jump_from_state("marker", self.runtime.replay.jump_to_previous_marker)

    def action_jump_next_activity(self) -> None:
        self._jump_from_state("activity", self.runtime.replay.jump_to_next_activity)

    def action_jump_next_problem(self) -> None:
        self._jump_from_state("problem", self.runtime.replay.jump_to_next_problem)

    def action_jump_next_file_edit(self) -> None:
        self._jump_from_state("file edit", self.runtime.replay.jump_to_next_file_edit)

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

    def action_playback_toggle(self) -> None:
        self._with_playback(
            lambda state, pb: self.runtime.replay.playback_toggle(state, pb),
            label_when_paused="paused",
            label_when_playing="playing",
        )

    def action_playback_step_prev(self) -> None:
        self._with_playback(
            lambda state, pb: self.runtime.replay.playback_step(state, pb, direction=-1),
            label_when_paused="step prev",
            label_when_playing="step prev",
        )

    def action_playback_step_next(self) -> None:
        self._with_playback(
            lambda state, pb: self.runtime.replay.playback_step(state, pb, direction=1),
            label_when_paused="step next",
            label_when_playing="step next",
        )

    def action_playback_speed_up(self) -> None:
        self._with_playback(
            lambda state, pb: self.runtime.replay.playback_cycle_speed(state, pb, direction=1),
            label_when_paused="speed",
            label_when_playing="speed",
        )

    def action_playback_speed_down(self) -> None:
        self._with_playback(
            lambda state, pb: self.runtime.replay.playback_cycle_speed(state, pb, direction=-1),
            label_when_paused="speed",
            label_when_playing="speed",
        )

    def action_playback_jump_to_time(self) -> None:
        if self._state is None or self._playback is None:
            self.set_status("playback unavailable")
            return
        from copilot_commander.screens.jump_to_time import JumpToTimeScreen

        playback = self._playback

        def _on_done(target: datetime | None) -> None:
            if target is None or self._state is None or self._playback is None:
                self.set_status("jump cancelled")
                return
            new_state, new_pb = self.runtime.replay.playback_jump_to(
                self._state, self._playback, target
            )
            self._apply_playback_result(new_state, new_pb)
            self.set_status(f"jumped to {new_pb.clock.isoformat(timespec='seconds')}")

        self.app.push_screen(
            JumpToTimeScreen(
                clock=playback.clock,
                start=playback.start,
                end=playback.end,
            ),
            _on_done,
        )

    def _initialize_playback(self, state: ReplayStateView) -> None:
        playback = self.runtime.replay.initial_playback(state)
        self._playback = playback
        if playback is None:
            self._stop_playback_timer()
            return
        # Render the playback view immediately so the progress bar shows
        # an accurate paused position even before the user starts the
        # clock.
        self._state = self.runtime.replay.apply_playback(state, playback)
        self._selected_index = self._state.selected_index

    def _with_playback(
        self,
        action: Callable[
            [ReplayStateView, PlaybackState],
            tuple[ReplayStateView, PlaybackState],
        ],
        *,
        label_when_paused: str,
        label_when_playing: str,
    ) -> None:
        if self._state is None or self._playback is None:
            self.set_status("playback unavailable")
            return
        new_state, new_pb = action(self._state, self._playback)
        self._apply_playback_result(new_state, new_pb)
        label = label_when_playing if new_pb.mode == "playing" else label_when_paused
        self.set_status(f"{label} | {new_pb.speed.label}")

    def _apply_playback_result(
        self,
        state: ReplayStateView,
        playback: PlaybackState,
    ) -> None:
        self._state = state
        self._playback = playback
        self._selected_index = state.selected_index
        self._release_follow_latest()
        self._refresh_panels()
        if playback.mode == "playing":
            self._start_playback_timer()
        else:
            self._stop_playback_timer()

    def _start_playback_timer(self) -> None:
        if self._playback_timer is not None:
            return
        self._playback_last_tick = monotonic()
        self._playback_timer = self.set_interval(0.1, self._on_playback_tick)

    def _stop_playback_timer(self) -> None:
        if self._playback_timer is not None:
            self._playback_timer.stop()
            self._playback_timer = None
        self._playback_last_tick = None

    def _on_playback_tick(self) -> None:
        if self._state is None or self._playback is None:
            self._stop_playback_timer()
            return
        if self._playback.mode != "playing":
            self._stop_playback_timer()
            return
        from copilot_commander.services import playback_controller as playback_module

        now = monotonic()
        previous = self._playback_last_tick if self._playback_last_tick is not None else now
        elapsed_seconds = max(0.0, now - previous)
        self._playback_last_tick = now
        next_pb = playback_module.advance(self._playback, timedelta(seconds=elapsed_seconds))
        if next_pb is self._playback:
            return
        new_state = self.runtime.replay.apply_playback(self._state, next_pb)
        self._state = new_state
        self._playback = next_pb
        self._selected_index = new_state.selected_index
        self._refresh_panels()
        if next_pb.mode == "paused":
            self._stop_playback_timer()
            self.set_status("playback ended")

    def on_hide(self) -> None:
        self._stop_playback_timer()

    def _refresh_panels(self) -> None:
        summary = self.query_one(ReplaySummaryPanel)
        markers = self.query_one(ReplayMarkerListPanel)
        transcript = self.query_one(ReplayTranscriptPanel)
        detail = self.query_one(ReplayDetailPanel)
        progress = self.query_one(ReplayProgressBar)
        diff_panel = self.query_one(ReplayDiffPanel)
        summary.set_state(self._state)
        if self._state is None:
            markers.set_markers((), selected_index=None)
            transcript.set_transcript(())
            detail.set_entry(None)
            progress.set_state(None, ())
            diff_panel.set_entry_diff(None, None)
            return
        markers.set_markers(self._state.jump_markers, selected_index=self._state.selected_index)
        transcript.set_transcript(self._state.transcript)
        entry = self._selected_entry()
        detail.set_entry(entry)
        progress.set_state(self._state.playback, self._state.jump_markers)
        self._refresh_diff_panel(entry)

    def _refresh_diff_panel(self, entry: ReplayTranscriptEntryView | None) -> None:
        diff_panel = self.query_one(ReplayDiffPanel)
        state = self._state
        if entry is None or not entry.file_path or state is None or state.worktree_path is None:
            diff_panel.set_entry_diff(entry, None)
            return
        cache_key = (entry.ordinal, entry.file_path)
        cached = self._diff_cache.get(cache_key)
        if cached is not None:
            diff_panel.set_entry_diff(entry, cached)
            return
        diff_panel.set_entry_diff(entry, "")
        self._diff_version += 1
        version = self._diff_version
        repo_path = Path(state.worktree_path)
        path = entry.file_path
        adapter = self._diff_adapter

        def _resolve() -> tuple[int, tuple[int, str], str]:
            text = adapter.diff_for_path(repo_path, path, before=None, after=None)
            return version, cache_key, text

        self.run_worker(_resolve, thread=True, exclusive=False, name="replay_diff")

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
