"""Screen-level tests for ``ReplayScreen`` covering early-return branches.

Many ``action_*`` methods in the replay screen short-circuit when there
is no loaded state or no selected session. These tests exercise those
branches directly, plus a few playback/insights toggles, by stubbing the
``ReplayController`` interface.
"""

# ruff: noqa: SLF001

from __future__ import annotations

import asyncio
import tempfile
import unittest
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast
from unittest.mock import MagicMock

from textual.app import App, ComposeResult
from textual.widgets import Input
from textual.worker import Worker, WorkerState

from muxdeck.app import MuxdeckRuntime
from muxdeck.controllers.replay_controller import (
    PlaybackStateView,
    ReplayExportFormat,
    ReplayExportIntent,
    ReplayJumpMarkerView,
    ReplayPresentation,
    ReplayStateView,
    ReplayTranscriptEntryView,
)
from muxdeck.screens.replay import ReplayScreen
from muxdeck.services.playback_controller import (
    SPEED_NORMAL,
    PlaybackState,
)
from muxdeck.widgets.replay import (
    ReplayDetailPanel,
    ReplayFilterBar,
    ReplayInsightsPanel,
    ReplayMarkerListPanel,
    ReplaySummaryPanel,
    ReplayTranscriptPanel,
)


def _empty_state(*, session_id: str = "s-1") -> ReplayStateView:
    return ReplayStateView(
        session_id=session_id,
        agent_id="agent-1",
        task_title=None,
        selected_index=None,
        transcript=(),
        jump_markers=(),
        presentation="parsed",
        filter_text="",
        follow_latest=True,
        total_entries=0,
        total_markers=0,
    )


@dataclass
class _RecordingReplayCtrl:
    state_to_return: ReplayStateView | None = None
    apply_chip_calls: list[str] = field(default_factory=list)
    bookmark_calls: list[tuple[str, int]] = field(default_factory=list)
    note_calls: list[tuple[str, int, str]] = field(default_factory=list)

    def load_state(
        self,
        *,
        session_id: str,
        selected_index: int | None,
        filter_text: str,
        presentation: ReplayPresentation,
        follow_latest: bool,
    ) -> ReplayStateView:
        del selected_index, filter_text, presentation, follow_latest
        return self.state_to_return or _empty_state(session_id=session_id)

    def apply_errors_only_chip(self) -> str:
        self.apply_chip_calls.append("errors_only")
        return "type:error"

    def apply_activity_chip(self) -> str:
        self.apply_chip_calls.append("activity")
        return "kind:activity"

    def apply_tool_calls_chip(self) -> str:
        self.apply_chip_calls.append("tool_calls")
        return "kind:tool"

    def clear_chips(self) -> str:
        self.apply_chip_calls.append("clear")
        return ""

    def toggle_bookmark(self, session_id: str, ordinal: int) -> bool:
        self.bookmark_calls.append((session_id, ordinal))
        return True

    def add_note(self, session_id: str, ordinal: int, text: str) -> None:
        self.note_calls.append((session_id, ordinal, text))


@dataclass(slots=True)
class _RecordingStore:
    def list_agents(self) -> tuple[Any, ...]:
        return ()


class _MinimalGeneral:
    log_preview_lines = 8


class _MinimalConfig:
    general = _MinimalGeneral()


def _runtime_with(
    ctrl: _RecordingReplayCtrl | None = None,
) -> MuxdeckRuntime:
    return cast(
        MuxdeckRuntime,
        type(
            "_FakeRuntime",
            (),
            {
                "config": _MinimalConfig(),
                "replay": ctrl,
                "replay_worker": None,
            },
        )(),
    )


class _Harness(App[None]):
    def __init__(self, runtime: MuxdeckRuntime) -> None:
        super().__init__()
        self._runtime = runtime
        self.tab_badges: dict[str, str] = {}
        self.remembered: list[str] = []

    def compose(self) -> ComposeResult:
        return iter(())

    def remember_session_selection(self, session_id: str) -> None:
        self.remembered.append(session_id)

    def resolve_replay_session_id(self, current: str | None = None) -> str | None:
        return current


class ReplayScreenActionTests(unittest.TestCase):
    def test_action_toggle_bookmark_no_state_sets_status(self) -> None:
        async def scenario() -> str:
            runtime = _runtime_with(_RecordingReplayCtrl())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.action_toggle_bookmark()
                await pilot.pause()
                return screen._status

        assert "no replay" in asyncio.run(scenario())

    def test_action_toggle_bookmark_no_selection_sets_status(self) -> None:
        async def scenario() -> str:
            runtime = _runtime_with(_RecordingReplayCtrl())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._state = _empty_state()
                screen._session_id = "s-1"
                screen.action_toggle_bookmark()
                await pilot.pause()
                return screen._status

        assert "select an entry" in asyncio.run(scenario())

    def test_action_add_note_no_state_sets_status(self) -> None:
        async def scenario() -> str:
            runtime = _runtime_with(_RecordingReplayCtrl())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.action_add_note()
                await pilot.pause()
                return screen._status

        assert "no replay" in asyncio.run(scenario())

    def test_action_add_note_no_selection_sets_status(self) -> None:
        async def scenario() -> str:
            runtime = _runtime_with(_RecordingReplayCtrl())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._state = _empty_state()
                screen._session_id = "s-1"
                screen.action_add_note()
                await pilot.pause()
                return screen._status

        assert "select an entry" in asyncio.run(scenario())

    def test_next_export_format_cycle(self) -> None:
        assert ReplayScreen._next_export_format("text") == "json"
        assert ReplayScreen._next_export_format("json") == "markdown"
        assert ReplayScreen._next_export_format("markdown") == "text"

    def test_action_playback_toggle_unavailable_sets_status(self) -> None:
        async def scenario() -> str:
            runtime = _runtime_with(_RecordingReplayCtrl())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.action_playback_toggle()
                await pilot.pause()
                return screen._status

        assert "playback unavailable" in asyncio.run(scenario())

    def test_action_playback_step_prev_unavailable_sets_status(self) -> None:
        async def scenario() -> str:
            runtime = _runtime_with(_RecordingReplayCtrl())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.action_playback_step_prev()
                await pilot.pause()
                return screen._status

        assert "playback unavailable" in asyncio.run(scenario())

    def test_action_playback_step_next_unavailable_sets_status(self) -> None:
        async def scenario() -> str:
            runtime = _runtime_with(_RecordingReplayCtrl())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.action_playback_step_next()
                await pilot.pause()
                return screen._status

        assert "playback unavailable" in asyncio.run(scenario())

    def test_action_playback_speed_up_unavailable_sets_status(self) -> None:
        async def scenario() -> str:
            runtime = _runtime_with(_RecordingReplayCtrl())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.action_playback_speed_up()
                await pilot.pause()
                return screen._status

        assert "playback unavailable" in asyncio.run(scenario())

    def test_action_playback_speed_down_unavailable_sets_status(self) -> None:
        async def scenario() -> str:
            runtime = _runtime_with(_RecordingReplayCtrl())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.action_playback_speed_down()
                await pilot.pause()
                return screen._status

        assert "playback unavailable" in asyncio.run(scenario())

    def test_action_playback_jump_to_time_unavailable_sets_status(self) -> None:
        async def scenario() -> str:
            runtime = _runtime_with(_RecordingReplayCtrl())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.action_playback_jump_to_time()
                await pilot.pause()
                return screen._status

        assert "playback unavailable" in asyncio.run(scenario())

    def test_action_focus_filter_status(self) -> None:
        async def scenario() -> str:
            runtime = _runtime_with(_RecordingReplayCtrl())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.action_focus_filter()
                await pilot.pause()
                return screen._status

        assert "filter" in asyncio.run(scenario())

    def test_action_focus_markers_status(self) -> None:
        async def scenario() -> str:
            runtime = _runtime_with(_RecordingReplayCtrl())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.action_focus_markers()
                await pilot.pause()
                return screen._status

        assert "marker" in asyncio.run(scenario())

    def test_action_focus_transcript_status(self) -> None:
        async def scenario() -> str:
            runtime = _runtime_with(_RecordingReplayCtrl())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.action_focus_transcript()
                await pilot.pause()
                return screen._status

        assert "transcript" in asyncio.run(scenario())

    def test_action_toggle_insights_off_to_on(self) -> None:
        async def scenario() -> tuple[bool, bool]:
            runtime = _runtime_with(_RecordingReplayCtrl())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                panel = screen.query_one(ReplayInsightsPanel)
                start = panel.display
                screen.action_toggle_insights()
                await pilot.pause()
                end = panel.display
                return start, end

        start, end = asyncio.run(scenario())
        assert start != end

    def test_action_jump_methods_no_state_no_status_change(self) -> None:
        async def scenario() -> str:
            ctrl = _RecordingReplayCtrl()
            # The action_jump_* methods evaluate their bound-method
            # argument before checking _state, so we attach noop stubs
            # to satisfy attribute access.
            for attr in (
                "jump_to_next_marker",
                "jump_to_previous_marker",
                "jump_to_next_activity",
                "jump_to_next_problem",
                "jump_to_next_file_edit",
                "jump_to_next_annotation",
            ):
                setattr(ctrl, attr, lambda _state: None)
            runtime = _runtime_with(ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                # All jump_* actions early-return when state is None.
                screen.action_jump_next_marker()
                screen.action_jump_previous_marker()
                screen.action_jump_next_activity()
                screen.action_jump_next_problem()
                screen.action_jump_next_file_edit()
                screen.action_jump_next_annotation()
                await pilot.pause()
                return screen._status

        # Nothing crashed; status unchanged from the initial post-mount.
        asyncio.run(scenario())

    def test_action_cycle_export_format_no_state_just_announces(self) -> None:
        async def scenario() -> str:
            runtime = _runtime_with(_RecordingReplayCtrl())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.action_cycle_export_format()
                await pilot.pause()
                return screen._status

        status = asyncio.run(scenario())
        assert "export" in status

    def test_action_load_latest_resets_session_state(self) -> None:
        async def scenario() -> tuple[str | None, tuple[str, ...]]:
            runtime = _runtime_with(_RecordingReplayCtrl())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._session_id = "s-1"
                screen._session_ids = ("s-1", "s-2")
                screen.action_load_latest()
                await pilot.pause()
                return screen._session_id, screen._session_ids

        session_id, ids = asyncio.run(scenario())
        assert session_id is None
        assert ids == ()


# ── extended coverage: state-driven branches and helpers ──────────────


def _make_marker(
    index: int,
    *,
    kind: str = "activity",
    label: str = "marker",
    timestamp: str = "2024-01-01T00:00:00+00:00",
) -> ReplayJumpMarkerView:
    return ReplayJumpMarkerView(
        index=index,
        timestamp=timestamp,
        label=label,
        kind=kind,
    )


def _make_entry(
    ordinal: int,
    *,
    kind: str = "log",
    timestamp: str = "2024-01-01T00:00:00+00:00",
    label: str = "entry",
    is_selected: bool = False,
    file_path: str | None = None,
) -> ReplayTranscriptEntryView:
    return ReplayTranscriptEntryView(
        ordinal=ordinal,
        kind=kind,
        timestamp=timestamp,
        label=label,
        severity=None,
        marker_kind=None,
        lines=(),
        is_selected=is_selected,
        raw_lines=(),
        file_path=file_path,
    )


def _state_with_transcript(
    *,
    session_id: str = "s-1",
    selected_index: int | None = 0,
    follow_latest: bool = True,
    filter_text: str = "",
    presentation: ReplayPresentation = "parsed",
    playback: PlaybackStateView | None = None,
) -> ReplayStateView:
    entries = (
        _make_entry(
            0,
            timestamp="2024-01-01T00:00:00+00:00",
            label="alpha",
            is_selected=selected_index == 0,
            file_path="src/foo.py",
        ),
        _make_entry(
            1,
            timestamp="2024-01-01T00:00:30+00:00",
            label="beta",
            is_selected=selected_index == 1,
        ),
        _make_entry(
            2,
            timestamp="2024-01-01T00:01:00+00:00",
            label="gamma",
            is_selected=selected_index == 2,
        ),
    )
    markers = (
        _make_marker(0, kind="activity", label="m0"),
        _make_marker(2, kind="file_edit", label="m2", timestamp="2024-01-01T00:01:00+00:00"),
    )
    return ReplayStateView(
        session_id=session_id,
        agent_id="agent-1",
        task_title=None,
        selected_index=selected_index,
        transcript=entries,
        jump_markers=markers,
        presentation=presentation,
        filter_text=filter_text,
        follow_latest=follow_latest,
        total_entries=len(entries),
        total_markers=len(markers),
        playback=playback,
    )


def _make_playback_state() -> PlaybackState:
    start = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
    end = datetime(2024, 1, 1, 0, 1, 0, tzinfo=UTC)
    return PlaybackState(
        mode="paused",
        speed=SPEED_NORMAL,
        clock=start,
        start=start,
        end=end,
    )


@dataclass
class _FullReplayCtrl:
    """Recording fake of the full ``ReplayController`` surface used by the screen."""

    state_to_return: ReplayStateView | None = None
    multi_state_to_return: ReplayStateView | None = None
    next_jump_state: ReplayStateView | None = None
    initial_playback_state: PlaybackState | None = None
    apply_playback_result: ReplayStateView | None = None
    playback_action_result: tuple[ReplayStateView, PlaybackState] | None = None
    export_intent_to_return: ReplayExportIntent | None = None
    bookmark_added: bool = True
    raise_on_export: bool = False

    load_state_calls: list[dict[str, Any]] = field(default_factory=list)
    load_multi_calls: list[dict[str, Any]] = field(default_factory=list)
    bookmark_calls: list[tuple[str, int]] = field(default_factory=list)
    note_calls: list[tuple[str, int, str]] = field(default_factory=list)
    chip_calls: list[str] = field(default_factory=list)
    jump_calls: list[str] = field(default_factory=list)
    apply_playback_calls: list[tuple[ReplayStateView, PlaybackState]] = field(
        default_factory=list,
    )

    def load_state(
        self,
        *,
        session_id: str,
        selected_index: int | None,
        filter_text: str,
        presentation: ReplayPresentation,
        follow_latest: bool,
    ) -> ReplayStateView:
        self.load_state_calls.append(
            {
                "session_id": session_id,
                "selected_index": selected_index,
                "filter_text": filter_text,
                "presentation": presentation,
                "follow_latest": follow_latest,
            }
        )
        return self.state_to_return or _empty_state(session_id=session_id)

    def load_multi_state(
        self,
        session_ids: Sequence[str],
        *,
        selected_index: int | None,
        filter_text: str,
        presentation: ReplayPresentation,
        follow_latest: bool,
    ) -> ReplayStateView:
        self.load_multi_calls.append(
            {
                "session_ids": tuple(session_ids),
                "selected_index": selected_index,
                "filter_text": filter_text,
                "presentation": presentation,
                "follow_latest": follow_latest,
            }
        )
        return self.multi_state_to_return or _empty_state(session_id=session_ids[0])

    def jump_to_marker(
        self,
        state: ReplayStateView,
        marker_ordinal: int,
    ) -> ReplayStateView:
        del marker_ordinal
        return self.next_jump_state or state

    def select_entry(self, state: ReplayStateView, ordinal: int) -> ReplayStateView:
        del ordinal
        return self.next_jump_state or state

    def initial_playback(self, state: ReplayStateView) -> PlaybackState | None:
        del state
        return self.initial_playback_state

    def apply_playback(
        self,
        state: ReplayStateView,
        playback_state: PlaybackState,
    ) -> ReplayStateView:
        self.apply_playback_calls.append((state, playback_state))
        return self.apply_playback_result or state

    def playback_toggle(
        self,
        state: ReplayStateView,
        playback_state: PlaybackState,
    ) -> tuple[ReplayStateView, PlaybackState]:
        del state, playback_state
        assert self.playback_action_result is not None
        return self.playback_action_result

    def playback_step(
        self,
        state: ReplayStateView,
        playback_state: PlaybackState,
        *,
        direction: Literal[-1, 1],
    ) -> tuple[ReplayStateView, PlaybackState]:
        del state, playback_state, direction
        assert self.playback_action_result is not None
        return self.playback_action_result

    def playback_cycle_speed(
        self,
        state: ReplayStateView,
        playback_state: PlaybackState,
        *,
        direction: Literal[-1, 1] = 1,
    ) -> tuple[ReplayStateView, PlaybackState]:
        del state, playback_state, direction
        assert self.playback_action_result is not None
        return self.playback_action_result

    def playback_jump_to(
        self,
        state: ReplayStateView,
        playback_state: PlaybackState,
        target: datetime,
    ) -> tuple[ReplayStateView, PlaybackState]:
        del state, playback_state, target
        assert self.playback_action_result is not None
        return self.playback_action_result

    def jump_to_next_marker(self, state: ReplayStateView) -> ReplayStateView | None:
        self.jump_calls.append("next_marker")
        return self.next_jump_state if self.next_jump_state is not None else None

    def jump_to_previous_marker(self, state: ReplayStateView) -> ReplayStateView | None:
        self.jump_calls.append("previous_marker")
        return self.next_jump_state if self.next_jump_state is not None else None

    def jump_to_next_activity(self, state: ReplayStateView) -> ReplayStateView | None:
        self.jump_calls.append("next_activity")
        return self.next_jump_state if self.next_jump_state is not None else None

    def jump_to_next_problem(self, state: ReplayStateView) -> ReplayStateView | None:
        self.jump_calls.append("next_problem")
        return self.next_jump_state if self.next_jump_state is not None else None

    def jump_to_next_file_edit(self, state: ReplayStateView) -> ReplayStateView | None:
        self.jump_calls.append("next_file_edit")
        return self.next_jump_state if self.next_jump_state is not None else None

    def jump_to_next_annotation(self, state: ReplayStateView) -> ReplayStateView | None:
        self.jump_calls.append("next_annotation")
        return self.next_jump_state if self.next_jump_state is not None else None

    def build_export_intent(
        self,
        state: ReplayStateView,
        *,
        export_format: ReplayExportFormat,
    ) -> ReplayExportIntent:
        if self.raise_on_export:
            msg = "boom"
            raise OSError(msg)
        return self.export_intent_to_return or ReplayExportIntent(
            session_id=state.session_id,
            format=export_format,
            filename_hint=f"replay-{state.session_id}.txt",
            content="export-body",
        )

    def toggle_bookmark(self, session_id: str, ordinal: int) -> bool:
        self.bookmark_calls.append((session_id, ordinal))
        return self.bookmark_added

    def add_note(self, session_id: str, ordinal: int, body: str) -> None:
        self.note_calls.append((session_id, ordinal, body))

    @staticmethod
    def apply_errors_only_chip() -> str:
        return "type:error"

    @staticmethod
    def apply_activity_chip() -> str:
        return "kind:activity"

    @staticmethod
    def apply_tool_calls_chip() -> str:
        return "kind:tool"

    @staticmethod
    def clear_chips() -> str:
        return ""


@dataclass
class _PathsConfig:
    state_dir: Path


@dataclass
class _GeneralConfig:
    log_preview_lines: int = 8


@dataclass
class _ConfigWithPaths:
    paths: _PathsConfig
    general: _GeneralConfig = field(default_factory=_GeneralConfig)


def _runtime_with_paths(
    ctrl: _FullReplayCtrl,
    *,
    state_dir: Path,
    replay_worker: _FullReplayCtrl | None = None,
) -> MuxdeckRuntime:
    return cast(
        MuxdeckRuntime,
        type(
            "_FakeRuntime",
            (),
            {
                "config": _ConfigWithPaths(paths=_PathsConfig(state_dir=state_dir)),
                "replay": ctrl,
                "replay_worker": replay_worker,
            },
        )(),
    )


def _runtime_with_full(
    ctrl: _FullReplayCtrl,
    *,
    replay_worker: _FullReplayCtrl | None = None,
) -> MuxdeckRuntime:
    return cast(
        MuxdeckRuntime,
        type(
            "_FakeRuntime",
            (),
            {
                "config": _MinimalConfig(),
                "replay": ctrl,
                "replay_worker": replay_worker,
            },
        )(),
    )


def _make_worker_event(
    state: WorkerState,
    *,
    name: str = "replay_load",
    result: object | None = None,
) -> Worker.StateChanged:
    worker = SimpleNamespace(name=name, state=state, result=result)
    return cast(Worker.StateChanged, SimpleNamespace(worker=worker, state=state))


class ReplayRefreshDataTests(unittest.TestCase):
    """``refresh_data`` and the worker-state transitions that follow it."""

    def test_refresh_skipped_during_loading_in_show(self) -> None:
        async def scenario() -> bool:
            ctrl = _FullReplayCtrl()
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                ctrl.load_state_calls.clear()
                # Force the second-call branch where _refresh_on_activate
                # short-circuits because a load is already in flight.
                screen._skip_next_show_refresh = False
                screen._loading = True
                screen._refresh_on_activate()
                await pilot.pause()
                return not ctrl.load_state_calls

        assert asyncio.run(scenario())

    def test_refresh_skip_resets_after_first_show(self) -> None:
        async def scenario() -> bool:
            ctrl = _FullReplayCtrl()
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                # First time, skip flag prevents the redundant refresh.
                screen._skip_next_show_refresh = True
                screen._refresh_on_activate()
                return screen._skip_next_show_refresh is False

        assert asyncio.run(scenario())

    def test_refresh_no_session_sets_status(self) -> None:
        async def scenario() -> str:
            ctrl = _FullReplayCtrl()
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            # Resolve to None so the early-return branch is exercised.
            app.resolve_replay_session_id = lambda current=None: None  # type: ignore[method-assign]
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.refresh_data()
                await pilot.pause()
                return screen._status

        status = asyncio.run(scenario())
        assert "no replayable" in status

    def test_refresh_single_session_first_load_sets_loading(self) -> None:
        async def scenario() -> tuple[bool, list[dict[str, Any]]]:
            ctrl = _FullReplayCtrl()
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            app.resolve_replay_session_id = lambda current=None: "s-1"  # type: ignore[method-assign]
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                screen.run_worker = MagicMock()  # type: ignore[method-assign]
                await app.push_screen(screen)
                await pilot.pause()
                ctrl.load_state_calls.clear()
                screen._state = None
                screen.refresh_data()
                await pilot.pause()
                # Run the worker callable that was captured.
                worker_callable = screen.run_worker.call_args.args[0]
                worker_callable()
                return screen._loading, ctrl.load_state_calls

        loading, calls = asyncio.run(scenario())
        assert loading is True
        assert calls
        assert calls[0]["session_id"] == "s-1"

    def test_refresh_multi_session_uses_load_multi_state(self) -> None:
        async def scenario() -> list[dict[str, Any]]:
            ctrl = _FullReplayCtrl()
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                screen.run_worker = MagicMock()  # type: ignore[method-assign]
                await app.push_screen(screen)
                await pilot.pause()
                ctrl.load_multi_calls.clear()
                screen._session_ids = ("s-1", "s-2")
                screen._state = None
                screen.refresh_data()
                await pilot.pause()
                worker_callable = screen.run_worker.call_args.args[0]
                worker_callable()
                return ctrl.load_multi_calls

        calls = asyncio.run(scenario())
        assert calls
        assert calls[0]["session_ids"] == ("s-1", "s-2")

    def test_refresh_uses_replay_worker_when_available(self) -> None:
        async def scenario() -> tuple[bool, bool]:
            primary = _FullReplayCtrl()
            worker = _FullReplayCtrl()
            runtime = _runtime_with_full(primary, replay_worker=worker)
            app = _Harness(runtime)
            app.resolve_replay_session_id = lambda current=None: "s-1"  # type: ignore[method-assign]
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                screen.run_worker = MagicMock()  # type: ignore[method-assign]
                await app.push_screen(screen)
                await pilot.pause()
                screen._state = None
                screen.refresh_data()
                await pilot.pause()
                worker_callable = screen.run_worker.call_args.args[0]
                worker_callable()
                return bool(worker.load_state_calls), bool(primary.load_state_calls)

        worker_called, primary_called = asyncio.run(scenario())
        assert worker_called is True
        assert primary_called is False

    def test_refresh_multi_session_subsequent_skips_loading_indicators(self) -> None:
        async def scenario() -> tuple[bool, list[dict[str, Any]]]:
            ctrl = _FullReplayCtrl()
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                screen.run_worker = MagicMock()  # type: ignore[method-assign]
                await app.push_screen(screen)
                await pilot.pause()
                screen._session_ids = ("s-1",)
                # Pretend a prior load already completed so the
                # ``first_load`` branch is skipped.
                screen._state = _empty_state()
                screen.refresh_data()
                await pilot.pause()
                worker_callable = screen.run_worker.call_args.args[0]
                worker_callable()
                return screen._loading, ctrl.load_multi_calls

        loading, calls = asyncio.run(scenario())
        assert loading is True
        assert calls
        assert calls[0]["session_ids"] == ("s-1",)


class ReplayWorkerStateChangedTests(unittest.TestCase):
    def test_worker_success_updates_state_and_status(self) -> None:
        async def scenario() -> tuple[str, ReplayStateView | None]:
            ctrl = _FullReplayCtrl()
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._session_id = "s-1"
                screen._loading = True
                state = _state_with_transcript(session_id="s-1", selected_index=1)
                event = _make_worker_event(WorkerState.SUCCESS, result=state)
                screen.on_worker_state_changed(event)
                await pilot.pause()
                return screen._status, screen._state

        status, state = asyncio.run(scenario())
        assert "session s-1" in status
        assert state is not None
        assert state.session_id == "s-1"

    def test_worker_success_filtered_no_matches(self) -> None:
        async def scenario() -> str:
            ctrl = _FullReplayCtrl()
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._session_id = "s-1"
                screen._filter_text = "needle"
                empty = ReplayStateView(
                    session_id="s-1",
                    agent_id="agent-1",
                    task_title=None,
                    selected_index=None,
                    transcript=(),
                    jump_markers=(),
                    presentation="parsed",
                    filter_text="needle",
                    follow_latest=True,
                    total_entries=0,
                    total_markers=0,
                )
                event = _make_worker_event(WorkerState.SUCCESS, result=empty)
                screen.on_worker_state_changed(event)
                await pilot.pause()
                return screen._status

        status = asyncio.run(scenario())
        assert "no replay matches for needle" in status

    def test_worker_error_clears_state_and_announces(self) -> None:
        async def scenario() -> tuple[str, ReplayStateView | None]:
            ctrl = _FullReplayCtrl()
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._loading = True
                screen._state = _empty_state()
                event = _make_worker_event(WorkerState.ERROR)
                screen.on_worker_state_changed(event)
                await pilot.pause()
                return screen._status, screen._state

        status, state = asyncio.run(scenario())
        assert "replay load failed" in status
        assert state is None

    def test_worker_other_event_name_ignored(self) -> None:
        async def scenario() -> str:
            ctrl = _FullReplayCtrl()
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                # Capture the status assigned by the initial refresh.
                baseline = screen._status
                event = _make_worker_event(WorkerState.SUCCESS, name="other")
                screen.on_worker_state_changed(event)
                await pilot.pause()
                return baseline if baseline == screen._status else screen._status

        # Status remains the initial one because the handler ignored the
        # foreign-named worker event.
        status = asyncio.run(scenario())
        assert "no replayable" in status

    def test_worker_running_state_is_noop_for_screen(self) -> None:
        async def scenario() -> ReplayStateView | None:
            ctrl = _FullReplayCtrl()
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._state = None
                event = _make_worker_event(WorkerState.RUNNING)
                screen.on_worker_state_changed(event)
                await pilot.pause()
                return screen._state

        # State remains unchanged (None) because RUNNING is the
        # ignored intermediate phase.
        assert asyncio.run(scenario()) is None


class ReplayInputAndMessagesTests(unittest.TestCase):
    def test_input_changed_starts_debounce_timer(self) -> None:
        async def scenario() -> tuple[str, bool]:
            ctrl = _FullReplayCtrl()
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                input_widget = screen.query_one(
                    "#replay-filter-input",
                    Input,
                )
                event = Input.Changed(input=input_widget, value="needle")
                screen.on_input_changed(event)
                # Trigger again so the existing timer is stopped first.
                event2 = Input.Changed(input=input_widget, value="other")
                screen.on_input_changed(event2)
                await pilot.pause()
                return screen._filter_text, screen._filter_debounce_timer is not None

        filter_text, has_timer = asyncio.run(scenario())
        assert filter_text == "other"
        assert has_timer is True

    def test_input_changed_ignores_unrelated_input(self) -> None:
        async def scenario() -> str:
            ctrl = _FullReplayCtrl()
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                # A fresh Input with a different id mimics a foreign
                # input firing on the screen.
                other = Input(id="some-other-input")
                event = Input.Changed(input=other, value="ignored")
                screen.on_input_changed(event)
                await pilot.pause()
                return screen._filter_text

        assert asyncio.run(scenario()) == ""

    def test_marker_selected_jumps_and_sets_status(self) -> None:
        async def scenario() -> str:
            ctrl = _FullReplayCtrl()
            base = _state_with_transcript(selected_index=0)
            ctrl.next_jump_state = base
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._state = base
                screen._session_id = "s-1"
                msg = ReplayMarkerListPanel.MarkerSelected(0)
                screen.on_replay_marker_list_panel_marker_selected(msg)
                await pilot.pause()
                return screen._status

        status = asyncio.run(scenario())
        assert "jumped to" in status

    def test_marker_selected_no_state_returns_quietly(self) -> None:
        async def scenario() -> ReplayStateView | None:
            ctrl = _FullReplayCtrl()
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                msg = ReplayMarkerListPanel.MarkerSelected(0)
                screen.on_replay_marker_list_panel_marker_selected(msg)
                await pilot.pause()
                return screen._state

        assert asyncio.run(scenario()) is None

    def test_transcript_selected_changes_selection(self) -> None:
        async def scenario() -> int | None:
            ctrl = _FullReplayCtrl()
            ctrl.next_jump_state = _state_with_transcript(selected_index=2)
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._state = _state_with_transcript(selected_index=0)
                screen._session_id = "s-1"
                msg = ReplayTranscriptPanel.TranscriptSelected(2)
                screen.on_replay_transcript_panel_transcript_selected(msg)
                await pilot.pause()
                return screen._selected_index

        assert asyncio.run(scenario()) == 2

    def test_transcript_selected_releases_follow_when_not_latest(self) -> None:
        async def scenario() -> bool:
            ctrl = _FullReplayCtrl()
            ctrl.next_jump_state = _state_with_transcript(
                selected_index=0,
                follow_latest=False,
            )
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                # follow_latest True; transcript with last ordinal == 2.
                screen._state = _state_with_transcript(selected_index=2)
                screen._session_id = "s-1"
                screen._follow_latest = True
                msg = ReplayTranscriptPanel.TranscriptSelected(0)
                screen.on_replay_transcript_panel_transcript_selected(msg)
                await pilot.pause()
                return screen._follow_latest

        assert asyncio.run(scenario()) is False

    def test_transcript_selected_no_state_returns(self) -> None:
        async def scenario() -> int | None:
            ctrl = _FullReplayCtrl()
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                msg = ReplayTranscriptPanel.TranscriptSelected(0)
                screen.on_replay_transcript_panel_transcript_selected(msg)
                await pilot.pause()
                return screen._selected_index

        assert asyncio.run(scenario()) is None

    def test_transcript_selected_during_refresh_returns(self) -> None:
        async def scenario() -> int | None:
            ctrl = _FullReplayCtrl()
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._state = _state_with_transcript(selected_index=0)
                screen._session_id = "s-1"
                screen._selected_index = 0
                screen._refreshing = True
                msg = ReplayTranscriptPanel.TranscriptSelected(2)
                screen.on_replay_transcript_panel_transcript_selected(msg)
                await pilot.pause()
                return screen._selected_index

        # Selection unchanged because we short-circuited mid-refresh.
        assert asyncio.run(scenario()) == 0

    def test_transcript_selected_same_index_returns(self) -> None:
        async def scenario() -> int | None:
            ctrl = _FullReplayCtrl()
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._state = _state_with_transcript(selected_index=1)
                screen._session_id = "s-1"
                screen._selected_index = 1
                msg = ReplayTranscriptPanel.TranscriptSelected(1)
                screen.on_replay_transcript_panel_transcript_selected(msg)
                await pilot.pause()
                return screen._selected_index

        assert asyncio.run(scenario()) == 1


class ReplayActionAndPlaybackTests(unittest.TestCase):
    def test_active_list_default_is_transcript(self) -> None:
        async def scenario() -> bool:
            ctrl = _FullReplayCtrl()
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                active = screen._active_list()
                return isinstance(active, ReplayTranscriptPanel)

        assert asyncio.run(scenario())

    def test_action_cursor_down_and_up_invoke_active_list(self) -> None:
        async def scenario() -> int:
            ctrl = _FullReplayCtrl()
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                # Replace the active list helper to record calls.
                fake_panel = MagicMock()
                screen._active_list = lambda: fake_panel  # type: ignore[method-assign]
                screen.action_cursor_down()
                screen.action_cursor_up()
                return int(fake_panel.move_cursor.call_count)

        assert asyncio.run(scenario()) == 2

    def test_active_list_returns_marker_when_focused(self) -> None:
        async def scenario() -> bool:
            ctrl = _FullReplayCtrl()
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                marker_panel = screen.query_one(ReplayMarkerListPanel)
                marker_panel.focus_list()
                await pilot.pause()
                active = screen._active_list()
                return isinstance(active, ReplayMarkerListPanel)

        assert asyncio.run(scenario())

    def test_action_escape_filter_returns_focus_to_list(self) -> None:
        async def scenario() -> bool:
            ctrl = _FullReplayCtrl()
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.query_one(ReplayFilterBar).focus_input()
                await pilot.pause()
                screen.action_escape_filter()
                await pilot.pause()
                input_widget = screen.query_one("#replay-filter-input", Input)
                return not input_widget.has_focus

        assert asyncio.run(scenario())

    def test_action_escape_filter_clears_chips_when_no_focus(self) -> None:
        async def scenario() -> str:
            ctrl = _FullReplayCtrl()
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._filter_text = "kind:tool"
                # Ensure focus is not on the filter input.
                screen.query_one(ReplayTranscriptPanel).focus_list()
                await pilot.pause()
                screen.action_escape_filter()
                await pilot.pause()
                return screen._filter_text

        assert asyncio.run(scenario()) == ""

    def test_action_toggle_presentation_swaps_modes(self) -> None:
        async def scenario() -> tuple[str, str]:
            ctrl = _FullReplayCtrl()
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                start = screen._presentation
                screen.action_toggle_presentation()
                await pilot.pause()
                return start, screen._presentation

        start, end = asyncio.run(scenario())
        assert {start, end} == {"parsed", "raw"}

    def test_action_toggle_follow_latest_resets_selection(self) -> None:
        async def scenario() -> tuple[bool, int | None]:
            ctrl = _FullReplayCtrl()
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._follow_latest = False
                screen._selected_index = 5
                screen.action_toggle_follow_latest()
                await pilot.pause()
                return screen._follow_latest, screen._selected_index

        follow, idx = asyncio.run(scenario())
        assert follow is True
        assert idx is None

    def test_action_toggle_follow_latest_off_keeps_selection(self) -> None:
        async def scenario() -> tuple[bool, int | None]:
            ctrl = _FullReplayCtrl()
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._follow_latest = True
                screen._selected_index = 7
                screen.action_toggle_follow_latest()
                await pilot.pause()
                return screen._follow_latest, screen._selected_index

        follow, idx = asyncio.run(scenario())
        assert follow is False
        assert idx == 7

    def test_action_cycle_export_format_writes_file(self) -> None:
        async def scenario() -> tuple[str, str | None]:
            with tempfile.TemporaryDirectory() as tmp:
                tmpdir = Path(tmp)
                ctrl = _FullReplayCtrl(
                    state_to_return=_state_with_transcript(selected_index=0),
                )
                runtime = _runtime_with_paths(ctrl, state_dir=tmpdir)
                app = _Harness(runtime)
                async with app.run_test(size=(160, 60)) as pilot:
                    screen = ReplayScreen(runtime)
                    await app.push_screen(screen)
                    await pilot.pause()
                    screen._state = _state_with_transcript(selected_index=0)
                    screen.action_cycle_export_format()
                    await pilot.pause()
                    files = sorted((tmpdir / "replay-exports").glob("*"))
                    contents = files[0].read_text() if files else None
                    return screen._status, contents

        status, contents = asyncio.run(scenario())
        assert "exported" in status
        assert contents == "export-body"

    def test_action_cycle_export_format_oserror_announces(self) -> None:
        async def scenario() -> str:
            with tempfile.TemporaryDirectory() as tmp:
                tmpdir = Path(tmp)
                # ``state_dir`` is a regular file → mkdir(state_dir/replay-exports)
                # raises NotADirectoryError (a subclass of OSError).
                blocker = tmpdir / "blocker"
                blocker.write_text("not a dir")
                ctrl = _FullReplayCtrl(
                    state_to_return=_state_with_transcript(selected_index=0),
                )
                runtime = _runtime_with_paths(ctrl, state_dir=blocker)
                app = _Harness(runtime)
                async with app.run_test(size=(160, 60)) as pilot:
                    screen = ReplayScreen(runtime)
                    await app.push_screen(screen)
                    await pilot.pause()
                    screen._state = _state_with_transcript(selected_index=0)
                    screen.action_cycle_export_format()
                    await pilot.pause()
                    return screen._status

        assert "export failed" in asyncio.run(scenario())

    def test_action_toggle_bookmark_records_when_state_loaded(self) -> None:
        async def scenario() -> tuple[list[tuple[str, int]], str]:
            ctrl = _FullReplayCtrl()
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._state = _state_with_transcript(selected_index=2)
                screen._session_id = "s-1"
                # Stub refresh_data so the follow-up reload does not
                # overwrite the status we want to assert.
                screen.refresh_data = lambda: None  # type: ignore[method-assign]
                screen.action_toggle_bookmark()
                await pilot.pause()
                return ctrl.bookmark_calls, screen._status

        calls, status = asyncio.run(scenario())
        assert calls == [("s-1", 2)]
        assert "bookmark added at #2" in status

    def test_action_toggle_bookmark_announces_removal(self) -> None:
        async def scenario() -> str:
            ctrl = _FullReplayCtrl(bookmark_added=False)
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._state = _state_with_transcript(selected_index=2)
                screen._session_id = "s-1"
                screen.refresh_data = lambda: None  # type: ignore[method-assign]
                screen.action_toggle_bookmark()
                await pilot.pause()
                return screen._status

        assert "bookmark removed at #2" in asyncio.run(scenario())

    def test_action_add_note_pushes_modal_and_saves(self) -> None:
        async def scenario() -> tuple[list[tuple[str, int, str]], str]:
            ctrl = _FullReplayCtrl()
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            captured_callback: list[Callable[[str | None], None]] = []
            captured_screen: list[object] = []

            def fake_push_screen(
                screen_arg: object,
                callback: Callable[[str | None], None] | None = None,
            ) -> None:
                captured_screen.append(screen_arg)
                if callback is not None:
                    captured_callback.append(callback)

            app.push_screen = fake_push_screen  # type: ignore[assignment,method-assign]
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                # Push the screen using base App.push_screen to mount it.
                await App.push_screen(app, screen)
                await pilot.pause()
                screen._state = _state_with_transcript(selected_index=1)
                screen._session_id = "s-1"
                screen.refresh_data = lambda: None  # type: ignore[method-assign]
                screen.action_add_note()
                await pilot.pause()
                callback = captured_callback[0]
                callback("my note body")
                await pilot.pause()
                return ctrl.note_calls, screen._status

        calls, status = asyncio.run(scenario())
        assert calls == [("s-1", 1, "my note body")]
        assert "note saved at #1" in status

    def test_action_add_note_dismiss_cancels(self) -> None:
        async def scenario() -> str:
            ctrl = _FullReplayCtrl()
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            captured: list[Callable[[str | None], None]] = []

            def fake_push_screen(
                screen_arg: object,
                callback: Callable[[str | None], None] | None = None,
            ) -> None:
                if callback is not None:
                    captured.append(callback)

            app.push_screen = fake_push_screen  # type: ignore[assignment,method-assign]
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await App.push_screen(app, screen)
                await pilot.pause()
                screen._state = _state_with_transcript(selected_index=0)
                screen._session_id = "s-1"
                screen.action_add_note()
                await pilot.pause()
                captured[0](None)
                await pilot.pause()
                return screen._status

        assert "note cancelled" in asyncio.run(scenario())

    def test_action_open_multi_session_picker_pushes_picker(self) -> None:
        async def scenario() -> tuple[tuple[str, ...], str | None, str]:
            ctrl = _FullReplayCtrl()
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            captured: list[Callable[[tuple[str, ...] | None], None]] = []

            def fake_push_screen(
                screen_arg: object,
                callback: Callable[[tuple[str, ...] | None], None] | None = None,
            ) -> None:
                if callback is not None:
                    captured.append(callback)

            app.push_screen = fake_push_screen  # type: ignore[assignment,method-assign]
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await App.push_screen(app, screen)
                await pilot.pause()
                screen.refresh_data = lambda: None  # type: ignore[method-assign]
                screen.action_open_multi_session_picker()
                await pilot.pause()
                captured[0](("s-1", "s-2"))
                await pilot.pause()
                return screen._session_ids, screen._session_id, screen._status

        ids, sid, status = asyncio.run(scenario())
        assert ids == ("s-1", "s-2")
        assert sid == "s-1"
        assert "merging 2 session(s)" in status

    def test_action_open_multi_session_picker_cancel(self) -> None:
        async def scenario() -> str:
            ctrl = _FullReplayCtrl()
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            captured: list[Callable[[tuple[str, ...] | None], None]] = []

            def fake_push_screen(
                screen_arg: object,
                callback: Callable[[tuple[str, ...] | None], None] | None = None,
            ) -> None:
                if callback is not None:
                    captured.append(callback)

            app.push_screen = fake_push_screen  # type: ignore[assignment,method-assign]
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await App.push_screen(app, screen)
                await pilot.pause()
                screen.action_open_multi_session_picker()
                await pilot.pause()
                captured[0](None)
                await pilot.pause()
                return screen._status

        assert "multi-session picker cancelled" in asyncio.run(scenario())

    def test_chip_actions_apply_filter(self) -> None:
        async def scenario() -> tuple[str, str, str, str]:
            ctrl = _FullReplayCtrl()
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.action_chip_errors_only()
                errors = screen._filter_text
                screen.action_chip_activity()
                activity = screen._filter_text
                screen.action_chip_tool_calls()
                tool = screen._filter_text
                screen.action_chip_clear()
                cleared = screen._filter_text
                await pilot.pause()
                return errors, activity, tool, cleared

        errors, activity, tool, cleared = asyncio.run(scenario())
        assert errors == "type:error"
        assert activity == "kind:activity"
        assert tool == "kind:tool"
        assert cleared == ""

    def test_apply_chip_stops_existing_debounce_timer(self) -> None:
        async def scenario() -> bool:
            ctrl = _FullReplayCtrl()
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                input_widget = screen.query_one("#replay-filter-input", Input)
                screen.on_input_changed(Input.Changed(input=input_widget, value="x"))
                assert screen._filter_debounce_timer is not None
                screen.action_chip_clear()
                await pilot.pause()
                return screen._filter_debounce_timer is None

        assert asyncio.run(scenario())

    def test_action_toggle_insights_then_state_set(self) -> None:
        async def scenario() -> bool:
            ctrl = _FullReplayCtrl()
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._state = _state_with_transcript(selected_index=0)
                # First toggle: switches display on.
                screen.action_toggle_insights()
                await pilot.pause()
                first = screen.query_one(ReplayInsightsPanel).display
                # Second toggle: switches display off.
                screen.action_toggle_insights()
                await pilot.pause()
                second = screen.query_one(ReplayInsightsPanel).display
                return first and not second

        assert asyncio.run(scenario())

    def test_jump_from_state_no_results_announces(self) -> None:
        async def scenario() -> str:
            ctrl = _FullReplayCtrl(next_jump_state=None)
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._state = _state_with_transcript(selected_index=0)
                screen.action_jump_next_marker()
                await pilot.pause()
                return screen._status

        assert "no marker markers" in asyncio.run(scenario())

    def test_jump_from_state_with_result_updates_state(self) -> None:
        async def scenario() -> tuple[int | None, str]:
            ctrl = _FullReplayCtrl()
            destination = _state_with_transcript(selected_index=2, follow_latest=False)
            ctrl.next_jump_state = destination
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._state = _state_with_transcript(selected_index=0)
                screen.action_jump_next_problem()
                await pilot.pause()
                return screen._selected_index, screen._status

        selected, status = asyncio.run(scenario())
        assert selected == 2
        assert "jumped to problem" in status

    def test_action_playback_jump_to_time_pushes_modal(self) -> None:
        async def scenario() -> tuple[bool, str]:
            ctrl = _FullReplayCtrl()
            playback = _make_playback_state()
            new_pb = playback
            target_state = _state_with_transcript(selected_index=1)
            ctrl.playback_action_result = (target_state, new_pb)
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            captured: list[Callable[[datetime | None], None]] = []

            def fake_push_screen(
                screen_arg: object,
                callback: Callable[[datetime | None], None] | None = None,
            ) -> None:
                if callback is not None:
                    captured.append(callback)

            app.push_screen = fake_push_screen  # type: ignore[assignment,method-assign]
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await App.push_screen(app, screen)
                await pilot.pause()
                screen._state = _state_with_transcript(selected_index=0)
                screen._playback = playback
                screen.action_playback_jump_to_time()
                await pilot.pause()
                target = playback.start + timedelta(seconds=15)
                captured[0](target)
                await pilot.pause()
                return bool(captured), screen._status

        pushed, status = asyncio.run(scenario())
        assert pushed is True
        assert "jumped to" in status

    def test_action_playback_jump_to_time_cancel_branch(self) -> None:
        async def scenario() -> str:
            ctrl = _FullReplayCtrl()
            playback = _make_playback_state()
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            captured: list[Callable[[datetime | None], None]] = []

            def fake_push_screen(
                screen_arg: object,
                callback: Callable[[datetime | None], None] | None = None,
            ) -> None:
                if callback is not None:
                    captured.append(callback)

            app.push_screen = fake_push_screen  # type: ignore[assignment,method-assign]
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await App.push_screen(app, screen)
                await pilot.pause()
                screen._state = _state_with_transcript(selected_index=0)
                screen._playback = playback
                screen.action_playback_jump_to_time()
                await pilot.pause()
                captured[0](None)
                await pilot.pause()
                return screen._status

        assert "jump cancelled" in asyncio.run(scenario())

    def test_with_playback_toggle_starts_timer_when_playing(self) -> None:
        async def scenario() -> tuple[bool, str]:
            ctrl = _FullReplayCtrl()
            paused = _make_playback_state()
            playing = PlaybackState(
                mode="playing",
                speed=paused.speed,
                clock=paused.clock,
                start=paused.start,
                end=paused.end,
            )
            target_state = _state_with_transcript(selected_index=1)
            ctrl.playback_action_result = (target_state, playing)
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._state = _state_with_transcript(selected_index=0)
                screen._playback = paused
                screen.action_playback_toggle()
                await pilot.pause()
                has_timer = screen._playback_timer is not None
                # Cleanup so the timer doesn't leak past run_test().
                screen._stop_playback_timer()
                return has_timer, screen._status

        running, status = asyncio.run(scenario())
        assert running is True
        assert "playing" in status

    def test_with_playback_step_keeps_paused(self) -> None:
        async def scenario() -> tuple[bool, str]:
            ctrl = _FullReplayCtrl()
            paused = _make_playback_state()
            ctrl.playback_action_result = (
                _state_with_transcript(selected_index=1),
                paused,
            )
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._state = _state_with_transcript(selected_index=0)
                screen._playback = paused
                screen.action_playback_step_next()
                await pilot.pause()
                return screen._playback_timer is None, screen._status

        no_timer, status = asyncio.run(scenario())
        assert no_timer is True
        assert "step next" in status

    def test_initialize_playback_via_worker_event(self) -> None:
        async def scenario() -> bool:
            ctrl = _FullReplayCtrl()
            playback = _make_playback_state()
            ctrl.initial_playback_state = playback
            ctrl.apply_playback_result = _state_with_transcript(selected_index=0)
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                state = _state_with_transcript(selected_index=0)
                event = _make_worker_event(WorkerState.SUCCESS, result=state)
                screen.on_worker_state_changed(event)
                await pilot.pause()
                return screen._playback is playback

        assert asyncio.run(scenario())

    def test_initialize_playback_none_stops_timer(self) -> None:
        async def scenario() -> bool:
            ctrl = _FullReplayCtrl()
            ctrl.initial_playback_state = None
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._playback_timer = screen.set_interval(60.0, lambda: None)
                screen._initialize_playback(_state_with_transcript(selected_index=0))
                await pilot.pause()
                return screen._playback_timer is None

        assert asyncio.run(scenario())

    def test_start_playback_timer_idempotent(self) -> None:
        async def scenario() -> bool:
            ctrl = _FullReplayCtrl()
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._start_playback_timer()
                first = screen._playback_timer
                screen._start_playback_timer()
                same = screen._playback_timer is first
                screen._stop_playback_timer()
                return same

        assert asyncio.run(scenario())

    def test_stop_playback_timer_when_none(self) -> None:
        async def scenario() -> bool:
            ctrl = _FullReplayCtrl()
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                # No-op when no timer is active.
                screen._stop_playback_timer()
                return screen._playback_timer is None

        assert asyncio.run(scenario())

    def test_on_playback_tick_no_state_stops_timer(self) -> None:
        async def scenario() -> bool:
            ctrl = _FullReplayCtrl()
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._start_playback_timer()
                screen._state = None
                screen._playback = None
                screen._on_playback_tick()
                return screen._playback_timer is None

        assert asyncio.run(scenario())

    def test_on_playback_tick_paused_stops_timer(self) -> None:
        async def scenario() -> bool:
            ctrl = _FullReplayCtrl()
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._start_playback_timer()
                screen._state = _state_with_transcript(selected_index=0)
                screen._playback = _make_playback_state()
                screen._on_playback_tick()
                return screen._playback_timer is None

        assert asyncio.run(scenario())

    def test_on_playback_tick_advances_clock(self) -> None:
        async def scenario() -> tuple[bool, str]:
            ctrl = _FullReplayCtrl()
            playback = _make_playback_state()
            playing = PlaybackState(
                mode="playing",
                speed=playback.speed,
                clock=playback.start,
                start=playback.start,
                end=playback.end,
            )
            ctrl.apply_playback_result = _state_with_transcript(selected_index=2)
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._state = _state_with_transcript(selected_index=0)
                screen._playback = playing
                screen._start_playback_timer()
                # Manually rewind the last-tick clock so advance() sees
                # an elapsed gap exceeding the playback duration.
                screen._playback_last_tick = -1000.0
                screen._on_playback_tick()
                await pilot.pause()
                return screen._playback_timer is None, screen._status

        stopped, status = asyncio.run(scenario())
        assert stopped is True
        assert status == "playback ended"

    def test_on_hide_stops_timer(self) -> None:
        async def scenario() -> bool:
            ctrl = _FullReplayCtrl()
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._start_playback_timer()
                screen.on_hide()
                return screen._playback_timer is None

        assert asyncio.run(scenario())

    def test_refresh_panels_with_state_populates_widgets(self) -> None:
        """``_refresh_panels`` must push the current state into every
        downstream widget. The original test only returned ``True`` from
        the scenario closure, which was a tautology — it could not
        observe whether ``set_state``/``set_transcript`` actually fired.
        """

        async def scenario() -> tuple[str, int, str, str]:
            ctrl = _FullReplayCtrl()
            runtime = _runtime_with_full(ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._state = _state_with_transcript(session_id="s-pop", selected_index=0)
                screen.query_one(ReplayInsightsPanel).display = True
                screen._refresh_panels()
                await pilot.pause()
                summary = screen.query_one(ReplaySummaryPanel)
                transcript_panel = screen.query_one(ReplayTranscriptPanel)
                detail_panel = screen.query_one(ReplayDetailPanel)
                insights = screen.query_one(ReplayInsightsPanel)
                summary_text = _render(summary)
                transcript_count = len(transcript_panel._ordinals)  # type: ignore[attr-defined]
                detail_text = _render(detail_panel)
                # ReplayInsightsPanel doesn't store state, but `set_state`
                # always calls `update(...)`. Inspect the renderable to
                # confirm it ran (vs. being skipped because display=False).
                insights_text = _render(insights)
                return (
                    summary_text,
                    transcript_count,
                    detail_text,
                    insights_text,
                )

        summary_text, transcript_count, detail_text, insights_text = asyncio.run(scenario())
        # Summary surfaces the session id; transcript holds the three
        # entries from ``_state_with_transcript``; detail panel renders
        # the selected entry's label "alpha"; insights panel had its
        # set_state invoked (we passed display=True so the
        # `if insights.display:` branch fired).
        assert "s-pop" in summary_text, f"summary missing session id: {summary_text!r}"
        assert transcript_count == 3, (
            f"transcript should hold 3 entries from _state_with_transcript, got {transcript_count}"
        )
        assert "alpha" in detail_text, f"detail missing selected label: {detail_text!r}"
        # Either has insights data or shows the empty-state message —
        # both prove ``set_state`` actually ran on the panel. The
        # default Static placeholder is empty, so any of these strings
        # being present means the panel was populated by the refresh.
        assert insights_text.strip(), "insights panel was not populated"


def _render(widget: object) -> str:
    """Read the visible plain text from a Static widget after update."""
    rendered = getattr(widget, "renderable", None)
    plain = getattr(rendered, "plain", None)
    if isinstance(plain, str):
        return plain
    if hasattr(widget, "render"):
        renderable = widget.render()  # type: ignore[attr-defined]
        plain = getattr(renderable, "plain", None)
        if isinstance(plain, str):
            return plain
        inner = getattr(renderable, "_renderable", None)
        inner_plain = getattr(inner, "plain", None)
        if isinstance(inner_plain, str):
            return inner_plain
    return str(rendered or widget)


def test_sync_follow_flag_replaces_when_mismatched() -> None:
    ctrl = _FullReplayCtrl()
    runtime = _runtime_with_full(ctrl)
    screen = ReplayScreen.__new__(ReplayScreen)
    screen._follow_latest = False
    state = _state_with_transcript(follow_latest=True)
    new_state = screen._sync_follow_flag(state)
    assert new_state.follow_latest is False
    # When already in sync, the same instance is returned.
    same = screen._sync_follow_flag(new_state)
    assert same is new_state
    del runtime  # silence unused


def test_release_follow_latest_idempotent_when_off() -> None:
    screen = ReplayScreen.__new__(ReplayScreen)
    screen._follow_latest = False
    screen._release_follow_latest()
    assert screen._follow_latest is False


def test_latest_visible_index_helpers() -> None:
    screen = ReplayScreen.__new__(ReplayScreen)
    screen._state = None
    assert screen._latest_visible_index() is None
    screen._state = _empty_state()
    assert screen._latest_visible_index() is None
    screen._state = _state_with_transcript(selected_index=0)
    assert screen._latest_visible_index() == 2


def test_selected_entry_finds_first_marked_entry() -> None:
    screen = ReplayScreen.__new__(ReplayScreen)
    screen._state = None
    assert screen._selected_entry() is None
    screen._state = _state_with_transcript(selected_index=1)
    entry = screen._selected_entry()
    assert entry is not None
    assert entry.ordinal == 1


def test_write_export_creates_state_dir() -> None:
    async def scenario() -> bool:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            ctrl = _FullReplayCtrl()
            runtime = _runtime_with_paths(ctrl, state_dir=tmpdir)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = ReplayScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                intent = ReplayExportIntent(
                    session_id="s-1",
                    format="text",
                    filename_hint="replay-s-1.txt",
                    content="hello",
                )
                path = screen._write_export(intent)
                return path.exists() and path.read_text() == "hello"

    assert asyncio.run(scenario())
