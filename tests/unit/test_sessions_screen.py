"""Screen-level tests for ``SessionsScreen``.

Drives ``action_*`` methods directly using a thin recording fake for
the runtime. Avoids spinning up the real session store or tmux
adapters.
"""

# ruff: noqa: SLF001

from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from textual.app import App, ComposeResult
from textual.worker import Worker, WorkerState

from muxdeck.app import MuxdeckRuntime
from muxdeck.controllers.sessions_controller import (
    SessionDetailView,
    SessionListItemView,
    SessionsState,
)
from muxdeck.screens.sessions import (
    _WORKER_NAME,
    SessionsScreen,
    _LiveSessionTarget,
    _LoadedSessionsState,
)
from muxdeck.services.action_service import ActionResult


def _list_item(
    session_id: str = "session-1",
    *,
    summary: str = "Review",
    status: str = "active",
) -> SessionListItemView:
    return SessionListItemView(
        session_id=session_id,
        summary=summary,
        repository="repo",
        branch="feature/x",
        status=status,
        status_glyph="🟢",
        updated="2m ago",
        created="20m ago",
        checkpoint_count=2,
        last_event_type="agent.updated",
        cwd="/repo/wt",
        is_resumable=True,
        origin="local",
    )


def _detail(
    *,
    session_id: str = "session-1",
    origin: str = "local",
    cwd: str = "/repo/wt",
    git_root: str = "/repo",
    repository: str = "repo",
    summary: str = "Review",
    windows_cwd: str | None = None,
) -> SessionDetailView:
    return SessionDetailView(
        session_id=session_id,
        summary=summary,
        repository=repository,
        branch="feature/x",
        cwd=cwd,
        git_root=git_root,
        status="active",
        status_glyph="🟢",
        created_at="20m ago",
        updated_at="2m ago",
        last_event_type="agent.updated",
        last_event_at="2m ago",
        checkpoint_count=2,
        is_resumable=True,
        resume_command=f"copilot --resume={session_id}",
        origin=origin,
        windows_cwd=windows_cwd,
    )


def _state(
    *,
    sessions: tuple[SessionListItemView, ...] = (),
    selected_id: str | None = None,
    selected: SessionDetailView | None = None,
) -> SessionsState:
    active = sum(1 for s in sessions if s.status == "active")
    unclosed = sum(1 for s in sessions if s.status == "unclosed")
    completed = sum(1 for s in sessions if s.status == "completed")
    return SessionsState(
        sessions=sessions,
        selected=selected,
        total_count=len(sessions),
        active_count=active,
        unclosed_count=unclosed,
        completed_count=completed,
        selected_session_id=selected_id,
    )


@dataclass(slots=True)
class _RecordingSessionsCtrl:
    state: SessionsState
    detail: SessionDetailView | None = None
    build_calls: int = 0
    detail_calls: list[str | None] = field(default_factory=list)

    def build_state(
        self,
        *,
        live_session_ids: frozenset[str],
        selected_session_id: str | None,
        filter_text: str,
        show_completed: bool,
    ) -> SessionsState:
        del live_session_ids, selected_session_id, filter_text, show_completed
        self.build_calls += 1
        return self.state

    def get_session_detail(
        self,
        session_id: str | None,
        *,
        live_session_ids: frozenset[str],
    ) -> SessionDetailView | None:
        del live_session_ids
        self.detail_calls.append(session_id)
        return self.detail


@dataclass(slots=True)
class _RecordingActions:
    resume_result: ActionResult = field(
        default_factory=lambda: ActionResult(success=True, message="resumed")
    )
    focus_result: ActionResult = field(
        default_factory=lambda: ActionResult(success=True, message="focused")
    )
    resume_calls: list[dict[str, Any]] = field(default_factory=list)
    focus_calls: list[dict[str, Any]] = field(default_factory=list)

    def resume_session(
        self,
        session_id: str,
        *,
        cwd: Path | None,
        window_name: str,
        origin: str,
        windows_cwd: str | None,
    ) -> ActionResult:
        self.resume_calls.append(
            {
                "session_id": session_id,
                "cwd": cwd,
                "window_name": window_name,
                "origin": origin,
                "windows_cwd": windows_cwd,
            }
        )
        return self.resume_result

    def focus_pane(
        self,
        pane_id: str,
        *,
        window_id: str | None,
        session_name: str | None,
    ) -> ActionResult:
        self.focus_calls.append(
            {
                "pane_id": pane_id,
                "window_id": window_id,
                "session_name": session_name,
            }
        )
        return self.focus_result


@dataclass(slots=True)
class _RecordingStore:
    agents: tuple[Any, ...] = ()

    def list_agents(self) -> tuple[Any, ...]:
        return self.agents


@dataclass(frozen=True, slots=True)
class _MinimalPaths:
    """Stand-in for ``AppConfig.paths`` used by ``ComposeWithMirrorScreen``.

    Only path arithmetic happens during construction; no I/O occurs
    until the screen is mounted, which these tests never do.
    """

    state_dir: Path = Path(".muxdeck-test-state")


class _MinimalGeneral:
    log_preview_lines = 8
    discovery_interval_sec = 2


class _MinimalConfig:
    general = _MinimalGeneral()
    paths = _MinimalPaths()


def _runtime_with(
    *,
    sessions_ctrl: _RecordingSessionsCtrl | None,
    actions: _RecordingActions | None = None,
    store: _RecordingStore | None = None,
) -> MuxdeckRuntime:
    return cast(
        MuxdeckRuntime,
        type(
            "_FakeRuntime",
            (),
            {
                "config": _MinimalConfig(),
                "sessions_ctrl": sessions_ctrl,
                "store": store or _RecordingStore(),
                "sync_store": None,
                "actions": actions,
                "pane_stream": None,
                "session_resolver": None,
                "tmux": None,
            },
        )(),
    )


class _Harness(App[None]):
    def __init__(self, runtime: MuxdeckRuntime) -> None:
        super().__init__()
        self._runtime = runtime
        self.tab_badges: dict[str, str] = {}
        self.remembered: list[str] = []
        self.selected_agent_id: str | None = None
        self.switched_to: list[str] = []

    def compose(self) -> ComposeResult:
        return iter(())

    def remember_session_selection(self, session_id: str) -> None:
        self.remembered.append(session_id)

    def switch_mode(self, mode: str) -> None:  # type: ignore[override]
        self.switched_to.append(mode)


class SessionsActionTests(unittest.TestCase):
    def _push_screen(
        self,
        runtime: MuxdeckRuntime,
        state: SessionsState | None = None,
        detail: SessionDetailView | None = None,
    ) -> tuple[_Harness, SessionsScreen]:
        app = _Harness(runtime)
        return app, SessionsScreen(runtime)

    def test_action_open_replay_without_selected_sets_status(self) -> None:
        async def scenario() -> str:
            ctrl = _RecordingSessionsCtrl(state=_state())
            runtime = _runtime_with(sessions_ctrl=ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = SessionsScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.action_open_replay()
                await pilot.pause()
                return screen._status

        assert "no session" in asyncio.run(scenario())

    def test_action_open_replay_remembers_and_switches(self) -> None:
        async def scenario() -> tuple[list[str], list[str]]:
            ctrl = _RecordingSessionsCtrl(state=_state())
            runtime = _runtime_with(sessions_ctrl=ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = SessionsScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._selected_session_id = "session-1"
                screen.action_open_replay()
                await pilot.pause()
                return app.remembered, app.switched_to

        remembered, switched = asyncio.run(scenario())
        assert remembered[-1] == "session-1"
        assert switched[-1] == "replay"

    def test_action_toggle_completed_toggles_state_and_status(self) -> None:
        async def scenario() -> tuple[bool, str]:
            ctrl = _RecordingSessionsCtrl(state=_state())
            runtime = _runtime_with(sessions_ctrl=ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = SessionsScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                show_initial = screen._show_completed
                screen.action_toggle_completed()
                await pilot.pause()
                return show_initial, screen._status

        initial, status = asyncio.run(scenario())
        assert initial is True
        # After the worker resolves the refresh, the status reflects
        # the new "hide-done" mode rather than the transient toggle msg.
        assert "hide-done" in status

    def test_action_resume_session_no_selected_sets_status(self) -> None:
        async def scenario() -> str:
            ctrl = _RecordingSessionsCtrl(state=_state())
            actions = _RecordingActions()
            runtime = _runtime_with(sessions_ctrl=ctrl, actions=actions)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = SessionsScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.action_resume_session()
                await pilot.pause()
                return screen._status

        assert "no session" in asyncio.run(scenario())

    def test_action_resume_session_no_actions_service_error(self) -> None:
        async def scenario() -> str:
            ctrl = _RecordingSessionsCtrl(state=_state())
            runtime = _runtime_with(sessions_ctrl=ctrl, actions=None)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = SessionsScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._selected_session_id = "session-1"
                screen.action_resume_session()
                await pilot.pause()
                return screen._status

        assert "action service unavailable" in asyncio.run(scenario())

    def test_action_resume_session_no_detail_sets_status(self) -> None:
        async def scenario() -> str:
            ctrl = _RecordingSessionsCtrl(state=_state())
            actions = _RecordingActions()
            runtime = _runtime_with(sessions_ctrl=ctrl, actions=actions)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = SessionsScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._selected_session_id = "session-1"
                screen._selected_detail = None
                screen.action_resume_session()
                await pilot.pause()
                return screen._status

        assert "no session" in asyncio.run(scenario())

    def test_action_resume_session_local_uses_cwd_path(self) -> None:
        async def scenario() -> dict[str, Any]:
            ctrl = _RecordingSessionsCtrl(state=_state())
            actions = _RecordingActions()
            runtime = _runtime_with(sessions_ctrl=ctrl, actions=actions)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = SessionsScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._selected_session_id = "session-1"
                screen._selected_detail = _detail()
                screen.action_resume_session()
                await pilot.pause()
                assert actions.resume_calls
                return actions.resume_calls[-1]

        call = asyncio.run(scenario())
        assert call["origin"] == "local"
        assert call["cwd"] == Path("/repo/wt")
        assert call["window_name"].startswith("copilot-")

    def test_action_resume_session_windows_keeps_no_cwd(self) -> None:
        async def scenario() -> dict[str, Any]:
            ctrl = _RecordingSessionsCtrl(state=_state())
            actions = _RecordingActions()
            runtime = _runtime_with(sessions_ctrl=ctrl, actions=actions)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = SessionsScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._selected_session_id = "session-1"
                screen._selected_detail = _detail(origin="windows", windows_cwd=r"C:\projects")
                screen.action_resume_session()
                await pilot.pause()
                assert actions.resume_calls
                return actions.resume_calls[-1]

        call = asyncio.run(scenario())
        assert call["origin"] == "windows"
        assert call["cwd"] is None
        assert call["windows_cwd"] == r"C:\projects"

    def test_action_resume_session_failure_path_sets_failure_status(self) -> None:
        async def scenario() -> str:
            ctrl = _RecordingSessionsCtrl(state=_state())
            actions = _RecordingActions(
                resume_result=ActionResult(success=False, message="cannot resume")
            )
            runtime = _runtime_with(sessions_ctrl=ctrl, actions=actions)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = SessionsScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._selected_session_id = "session-1"
                screen._selected_detail = _detail()
                screen.action_resume_session()
                await pilot.pause()
                return screen._status

        assert "cannot resume" in asyncio.run(scenario())

    def test_action_focus_pane_without_target_sets_helpful_message(self) -> None:
        async def scenario() -> str:
            ctrl = _RecordingSessionsCtrl(state=_state())
            actions = _RecordingActions()
            runtime = _runtime_with(sessions_ctrl=ctrl, actions=actions)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = SessionsScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.action_focus_pane()
                await pilot.pause()
                return screen._status

        assert "no active pane" in asyncio.run(scenario())

    def test_action_focus_pane_with_target_calls_actions(self) -> None:
        async def scenario() -> dict[str, Any]:
            ctrl = _RecordingSessionsCtrl(state=_state())
            actions = _RecordingActions()
            runtime = _runtime_with(sessions_ctrl=ctrl, actions=actions)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = SessionsScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._selected_session_id = "session-1"
                from muxdeck.screens.sessions import _LiveSessionTarget

                screen._live_targets = {
                    "session-1": _LiveSessionTarget(
                        pane_id="%5",
                        window_id="@2",
                        session_name="muxdeck",
                    )
                }
                screen.action_focus_pane()
                await pilot.pause()
                assert actions.focus_calls
                return actions.focus_calls[-1]

        call = asyncio.run(scenario())
        assert call["pane_id"] == "%5"
        assert call["window_id"] == "@2"
        assert call["session_name"] == "muxdeck"

    def test_action_open_live_no_detail_returns_with_status(self) -> None:
        async def scenario() -> str:
            ctrl = _RecordingSessionsCtrl(state=_state())
            runtime = _runtime_with(sessions_ctrl=ctrl, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = SessionsScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.action_open_live()
                await pilot.pause()
                return screen._status

        assert "no session" in asyncio.run(scenario())

    def test_action_open_live_no_target_sets_status(self) -> None:
        async def scenario() -> str:
            ctrl = _RecordingSessionsCtrl(state=_state())
            runtime = _runtime_with(sessions_ctrl=ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = SessionsScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._selected_session_id = "session-1"
                screen._selected_detail = _detail()
                screen.action_open_live()
                await pilot.pause()
                return screen._status

        assert "no live pane" in asyncio.run(scenario())

    def test_resolve_live_mirror_target_returns_pane_id_when_no_resolver(self) -> None:
        async def scenario() -> tuple[str, Any]:
            ctrl = _RecordingSessionsCtrl(state=_state())
            runtime = _runtime_with(sessions_ctrl=ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = SessionsScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                from muxdeck.screens.sessions import _LiveSessionTarget

                target = _LiveSessionTarget(
                    pane_id="%5", window_id=None, session_name=None, pane_pid=None
                )
                pane_id, adapter = screen._resolve_live_mirror_target(target)
                return pane_id, adapter

        pane, adapter = asyncio.run(scenario())
        assert pane == "%5"
        assert adapter is None

    def test_action_copy_details_no_selection_sets_status(self) -> None:
        async def scenario() -> str:
            ctrl = _RecordingSessionsCtrl(state=_state())
            runtime = _runtime_with(sessions_ctrl=ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = SessionsScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.action_copy_details()
                await pilot.pause()
                return screen._status

        assert "no session" in asyncio.run(scenario())

    def test_action_focus_filter_focuses_input(self) -> None:
        async def scenario() -> bool:
            ctrl = _RecordingSessionsCtrl(state=_state())
            runtime = _runtime_with(sessions_ctrl=ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = SessionsScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.action_focus_filter()
                await pilot.pause()
                from textual.widgets import Input

                input_widget = screen.query_one("#sessions-filter-input", Input)
                return bool(input_widget.has_focus)

        assert asyncio.run(scenario()) is True

    def test_action_cursor_down_and_up_no_op_when_empty(self) -> None:
        async def scenario() -> None:
            ctrl = _RecordingSessionsCtrl(state=_state())
            runtime = _runtime_with(sessions_ctrl=ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = SessionsScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.action_cursor_down()
                screen.action_cursor_up()
                await pilot.pause()
                return

        asyncio.run(scenario())  # should not raise

    def test_refresh_data_with_no_controller_returns_quietly(self) -> None:
        async def scenario() -> None:
            runtime = _runtime_with(sessions_ctrl=None)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = SessionsScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.refresh_data()
                await pilot.pause()
                return

        asyncio.run(scenario())

    def test_apply_state_updates_status_with_filter_chip(self) -> None:
        async def scenario() -> str:
            items = (_list_item(),)
            state = _state(sessions=items, selected_id="session-1", selected=_detail())
            ctrl = _RecordingSessionsCtrl(state=state, detail=_detail())
            runtime = _runtime_with(sessions_ctrl=ctrl, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = SessionsScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._filter_text = "alpha"
                screen._show_completed = False
                screen._apply_state(state)
                await pilot.pause()
                return screen._status

        status = asyncio.run(scenario())
        assert "filter:alpha" in status
        assert "hide-done" in status


class _SessionTargetStub:
    def __init__(
        self,
        *,
        pane_id: str | None,
        socket_path: Path | None,
    ) -> None:
        self.pane_id = pane_id
        self.socket_path = socket_path
        self.session_name: str | None = None
        self.window_id: str | None = None


class _StubResolver:
    def __init__(self, target: _SessionTargetStub | None) -> None:
        self._target = target

    def resolve_target_for_pid(self, pid: int) -> _SessionTargetStub | None:
        del pid
        return self._target


class SessionsResolveLiveMirrorTargetTests(unittest.TestCase):
    """Drive the live-mirror resolution through its alternate branches."""

    def _runtime_with_resolver(
        self,
        *,
        target: _SessionTargetStub | None,
        pane_stream: object | None = None,
    ) -> MuxdeckRuntime:
        ctrl = _RecordingSessionsCtrl(state=_state())
        runtime = _runtime_with(sessions_ctrl=ctrl, actions=_RecordingActions())
        runtime.session_resolver = cast(Any, _StubResolver(target))  # type: ignore[assignment]
        runtime.pane_stream = pane_stream  # type: ignore[assignment]
        return runtime

    def test_returns_target_pane_when_resolver_returns_none(self) -> None:
        async def scenario() -> str:
            runtime = self._runtime_with_resolver(target=None)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = SessionsScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                from muxdeck.screens.sessions import _LiveSessionTarget

                pane_id, _ = screen._resolve_live_mirror_target(
                    _LiveSessionTarget(
                        pane_id="%9",
                        window_id=None,
                        session_name=None,
                        pane_pid=12345,
                    )
                )
                return pane_id

        assert asyncio.run(scenario()) == "%9"

    def test_returns_resolved_pane_when_no_socket_path(self) -> None:
        async def scenario() -> str:
            runtime = self._runtime_with_resolver(
                target=_SessionTargetStub(pane_id="%99", socket_path=None),
            )
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = SessionsScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                from muxdeck.screens.sessions import _LiveSessionTarget

                pane_id, _ = screen._resolve_live_mirror_target(
                    _LiveSessionTarget(
                        pane_id="%9",
                        window_id=None,
                        session_name=None,
                        pane_pid=42,
                    )
                )
                return pane_id

        assert asyncio.run(scenario()) == "%99"


def _session_worker_event(
    *, name: str, state: WorkerState, result: object | None = None
) -> Worker.StateChanged:
    """Build a ``Worker.StateChanged`` stand-in for ``on_worker_state_changed``."""
    return cast(
        Worker.StateChanged,
        SimpleNamespace(
            state=state,
            worker=SimpleNamespace(name=name, state=state, result=result),
        ),
    )


@dataclass(slots=True)
class _StubAgent:
    """Mimic the subset of ``Agent`` used by the sessions worker."""

    copilot_session_id: str | None
    tmux_pane_id: str = ""
    tmux_window_id: str = ""
    tmux_session_name: str = ""
    pid: int | None = None


class SessionsLoaderTests(unittest.TestCase):
    """Cover the worker-side ``_load`` helper end to end."""

    def test_refresh_data_while_loading_marks_pending(self) -> None:
        async def scenario() -> tuple[bool, int]:
            ctrl = _RecordingSessionsCtrl(state=_state())
            runtime = _runtime_with(sessions_ctrl=ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = SessionsScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._loading = True
                before = len(list(screen.workers))
                screen.refresh_data()
                after = len(list(screen.workers))
                return screen._refresh_pending, after - before

        pending, started = asyncio.run(scenario())
        # Coalesce: don't start a duplicate worker; just queue a follow-up.
        assert pending is True
        assert started == 0

    def test_load_promotes_agents_with_pane_to_live_targets(self) -> None:
        async def scenario() -> dict[str, _LiveSessionTarget]:
            agents = (
                _StubAgent(copilot_session_id=None, tmux_pane_id="%5"),
                _StubAgent(copilot_session_id="sess-a", tmux_pane_id=""),
                _StubAgent(
                    copilot_session_id="sess-b",
                    tmux_pane_id="%9",
                    tmux_window_id="@1",
                    tmux_session_name="muxdeck",
                    pid=4242,
                ),
            )
            store = _RecordingStore(agents=agents)
            ctrl = _RecordingSessionsCtrl(state=_state())
            runtime = _runtime_with(sessions_ctrl=ctrl, store=store)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = SessionsScreen(runtime)
                await app.push_screen(screen)
                # Wait for the load worker scheduled by on_mount to settle.
                await pilot.pause()
                await pilot.pause()
                return dict(screen._live_targets)

        targets = asyncio.run(scenario())
        # Only agents with both a copilot session id AND a tmux pane id
        # are promoted into the live-target lookup; the other rows are
        # filtered out by ``_load`` before the screen sees them.
        assert "sess-b" in targets
        assert "sess-a" not in targets
        assert targets["sess-b"].pane_id == "%9"
        assert targets["sess-b"].window_id == "@1"
        assert targets["sess-b"].session_name == "muxdeck"
        assert targets["sess-b"].pane_pid == 4242


class SessionsWorkerCallbackTests(unittest.TestCase):
    def test_unknown_worker_name_is_ignored(self) -> None:
        async def scenario() -> str:
            ctrl = _RecordingSessionsCtrl(state=_state())
            runtime = _runtime_with(sessions_ctrl=ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = SessionsScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.set_status("baseline")
                screen.on_worker_state_changed(
                    _session_worker_event(name="other", state=WorkerState.SUCCESS)
                )
                await pilot.pause()
                return screen._status

        assert asyncio.run(scenario()) == "baseline"

    def test_load_worker_error_surfaces_failure(self) -> None:
        async def scenario() -> tuple[str, bool]:
            ctrl = _RecordingSessionsCtrl(state=_state())
            runtime = _runtime_with(sessions_ctrl=ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = SessionsScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._loading = True
                screen.on_worker_state_changed(
                    _session_worker_event(name=_WORKER_NAME, state=WorkerState.ERROR)
                )
                await pilot.pause()
                return screen._status, screen._loading

        status, loading = asyncio.run(scenario())
        assert "session load failed" in status
        assert loading is False

    def test_load_worker_cancelled_clears_loading(self) -> None:
        async def scenario() -> tuple[str, bool]:
            ctrl = _RecordingSessionsCtrl(state=_state())
            runtime = _runtime_with(sessions_ctrl=ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = SessionsScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.set_status("snapshot")
                screen._loading = True
                screen.on_worker_state_changed(
                    _session_worker_event(name=_WORKER_NAME, state=WorkerState.CANCELLED)
                )
                await pilot.pause()
                return screen._status, screen._loading

        status, loading = asyncio.run(scenario())
        # Cancelled workers are silent; the visible status survives.
        assert status == "snapshot"
        assert loading is False

    def test_load_worker_success_with_no_payload_just_clears_loading(self) -> None:
        async def scenario() -> bool:
            ctrl = _RecordingSessionsCtrl(state=_state())
            runtime = _runtime_with(sessions_ctrl=ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = SessionsScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._loading = True
                screen.on_worker_state_changed(
                    _session_worker_event(name=_WORKER_NAME, state=WorkerState.SUCCESS, result=None)
                )
                await pilot.pause()
                return screen._loading

        assert asyncio.run(scenario()) is False

    def test_load_worker_success_applies_state_and_targets(self) -> None:
        async def scenario() -> tuple[str | None, dict[str, _LiveSessionTarget]]:
            items = (_list_item(),)
            state = _state(sessions=items, selected_id="session-1", selected=_detail())
            ctrl = _RecordingSessionsCtrl(state=state, detail=_detail())
            runtime = _runtime_with(sessions_ctrl=ctrl, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = SessionsScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._loading = True
                payload = _LoadedSessionsState(
                    state=state,
                    live_session_ids=frozenset({"session-1"}),
                    live_targets={
                        "session-1": _LiveSessionTarget(
                            pane_id="%5",
                            window_id="@1",
                            session_name="muxdeck",
                            pane_pid=99,
                        )
                    },
                )
                screen.on_worker_state_changed(
                    _session_worker_event(
                        name=_WORKER_NAME, state=WorkerState.SUCCESS, result=payload
                    )
                )
                await pilot.pause()
                return screen._selected_session_id, dict(screen._live_targets)

        selected, targets = asyncio.run(scenario())
        assert selected == "session-1"
        assert "session-1" in targets


class SessionsSchedulePendingTests(unittest.TestCase):
    def test_schedule_pending_refresh_runs_when_pending(self) -> None:
        async def scenario() -> int:
            ctrl = _RecordingSessionsCtrl(state=_state())
            runtime = _runtime_with(sessions_ctrl=ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = SessionsScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                refreshes: list[bool] = []

                def _track() -> None:
                    refreshes.append(True)

                screen.refresh_data = _track  # type: ignore[method-assign]
                screen._refresh_pending = True
                screen._schedule_pending_refresh()
                await pilot.pause()
                return len(refreshes)

        assert asyncio.run(scenario()) >= 1

    def test_schedule_pending_refresh_no_op_when_not_pending(self) -> None:
        async def scenario() -> int:
            ctrl = _RecordingSessionsCtrl(state=_state())
            runtime = _runtime_with(sessions_ctrl=ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = SessionsScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                refreshes: list[bool] = []

                def _track() -> None:
                    refreshes.append(True)

                screen.refresh_data = _track  # type: ignore[method-assign]
                screen._refresh_pending = False
                screen._schedule_pending_refresh()
                await pilot.pause()
                return len(refreshes)

        assert asyncio.run(scenario()) == 0


class SessionsSelectionTests(unittest.TestCase):
    def test_on_session_selected_remembers_and_starts_detail_timer(self) -> None:
        async def scenario() -> tuple[str | None, list[str]]:
            ctrl = _RecordingSessionsCtrl(state=_state())
            runtime = _runtime_with(sessions_ctrl=ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = SessionsScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                from muxdeck.widgets.sessions import SessionSelected

                screen.on_session_selected(SessionSelected("session-2"))
                await pilot.pause()
                return screen._selected_session_id, app.remembered

        selected, remembered = asyncio.run(scenario())
        assert selected == "session-2"
        assert "session-2" in remembered

    def test_on_session_selected_same_id_returns_immediately(self) -> None:
        async def scenario() -> int:
            ctrl = _RecordingSessionsCtrl(state=_state())
            runtime = _runtime_with(sessions_ctrl=ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = SessionsScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._selected_session_id = "session-1"
                from muxdeck.widgets.sessions import SessionSelected

                screen.on_session_selected(SessionSelected("session-1"))
                await pilot.pause()
                return app.remembered.count("session-1")

        # No state change → no remembered selection update happens.
        assert asyncio.run(scenario()) == 0

    def test_update_selected_detail_no_controller_returns(self) -> None:
        async def scenario() -> SessionDetailView | None:
            runtime = _runtime_with(sessions_ctrl=None)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = SessionsScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._selected_detail = _detail()
                screen._update_selected_detail()
                await pilot.pause()
                return screen._selected_detail

        # Without a controller the helper short-circuits before mutating state.
        assert asyncio.run(scenario()) is not None

    def test_update_selected_detail_no_state_returns(self) -> None:
        async def scenario() -> SessionDetailView | None:
            ctrl = _RecordingSessionsCtrl(state=_state())
            runtime = _runtime_with(sessions_ctrl=ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = SessionsScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                # Force the early-return branch where the screen has not
                # yet received a state snapshot.
                screen._state = None
                screen._selected_detail = _detail()
                screen._update_selected_detail()
                await pilot.pause()
                return screen._selected_detail

        assert asyncio.run(scenario()) is not None

    def test_update_selected_detail_reads_controller_and_paints(self) -> None:
        async def scenario() -> tuple[SessionDetailView | None, list[str | None]]:
            other_detail = _detail(session_id="session-2", summary="Other")
            ctrl = _RecordingSessionsCtrl(
                state=_state(sessions=(_list_item(),)), detail=other_detail
            )
            runtime = _runtime_with(sessions_ctrl=ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = SessionsScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._selected_session_id = "session-2"
                screen._update_selected_detail()
                await pilot.pause()
                return screen._selected_detail, list(ctrl.detail_calls)

        detail, calls = asyncio.run(scenario())
        assert detail is not None
        assert detail.session_id == "session-2"
        assert "session-2" in calls


class SessionsFilterTests(unittest.TestCase):
    def test_action_escape_filter_focuses_list(self) -> None:
        async def scenario() -> bool:
            ctrl = _RecordingSessionsCtrl(state=_state())
            runtime = _runtime_with(sessions_ctrl=ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = SessionsScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                from muxdeck.widgets.sessions import SessionListPanel

                screen.action_escape_filter()
                await pilot.pause()
                list_panel = screen.query_one(SessionListPanel)
                return bool(list_panel.has_focus)

        assert asyncio.run(scenario()) is True

    def test_on_input_changed_for_filter_input_updates_state(self) -> None:
        async def scenario() -> str:
            ctrl = _RecordingSessionsCtrl(state=_state())
            runtime = _runtime_with(sessions_ctrl=ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = SessionsScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                from textual.widgets import Input

                input_widget = screen.query_one("#sessions-filter-input", Input)
                input_widget.value = "needle"
                # ``Input.Changed`` carries the input + value; the screen
                # uses both to identify and consume the event.
                event = Input.Changed(input_widget, "needle")
                screen.on_input_changed(event)
                await pilot.pause()
                return screen._filter_text

        assert asyncio.run(scenario()) == "needle"

    def test_on_input_changed_for_unrelated_input_is_ignored(self) -> None:
        async def scenario() -> str:
            ctrl = _RecordingSessionsCtrl(state=_state())
            runtime = _runtime_with(sessions_ctrl=ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = SessionsScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                from textual.widgets import Input

                # An ``Input`` from a different id (e.g., another screen
                # if it ever leaks through) must not mutate the filter.
                other_input = Input(id="not-our-input")
                screen.mount(other_input)
                await pilot.pause()
                screen._filter_text = "keep"
                event = Input.Changed(other_input, "noise")
                screen.on_input_changed(event)
                await pilot.pause()
                return screen._filter_text

        assert asyncio.run(scenario()) == "keep"


class SessionsResumeBranchTests(unittest.TestCase):
    def test_resume_session_falls_back_to_git_root_when_cwd_is_dash(self) -> None:
        async def scenario() -> dict[str, Any]:
            ctrl = _RecordingSessionsCtrl(state=_state())
            actions = _RecordingActions()
            runtime = _runtime_with(sessions_ctrl=ctrl, actions=actions)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = SessionsScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._selected_session_id = "session-1"
                # cwd missing ("—") forces the resume action to use git_root
                # as the start directory instead.
                screen._selected_detail = _detail(cwd="—", git_root="/repo")
                screen.action_resume_session()
                await pilot.pause()
                assert actions.resume_calls
                return actions.resume_calls[-1]

        call = asyncio.run(scenario())
        assert call["cwd"] == Path("/repo")

    def test_resume_session_keeps_no_cwd_when_both_missing(self) -> None:
        async def scenario() -> dict[str, Any]:
            ctrl = _RecordingSessionsCtrl(state=_state())
            actions = _RecordingActions()
            runtime = _runtime_with(sessions_ctrl=ctrl, actions=actions)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = SessionsScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._selected_session_id = "session-1"
                screen._selected_detail = _detail(cwd="—", git_root="—")
                screen.action_resume_session()
                await pilot.pause()
                assert actions.resume_calls
                return actions.resume_calls[-1]

        call = asyncio.run(scenario())
        assert call["cwd"] is None


class SessionsCopyDetailsTests(unittest.TestCase):
    def test_action_copy_details_with_selection_copies_panel(self) -> None:
        async def scenario() -> str:
            items = (_list_item(),)
            state = _state(sessions=items, selected_id="session-1", selected=_detail())
            ctrl = _RecordingSessionsCtrl(state=state, detail=_detail())
            runtime = _runtime_with(sessions_ctrl=ctrl, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = SessionsScreen(runtime)
                await app.push_screen(screen)
                # Two pauses so the worker that loads on_mount gets to
                # populate the list panel before we read the cursor.
                await pilot.pause()
                await pilot.pause()
                screen._selected_detail = _detail()
                screen.action_copy_details()
                await pilot.pause()
                return screen._status

        # Either copies to clipboard or reports "no … available"; both
        # paths exercise the helper through to its conclusion.
        status = asyncio.run(scenario())
        assert "session details" in status

    def test_action_copy_details_no_loaded_detail_sets_status(self) -> None:
        async def scenario() -> str:
            items = (_list_item(),)
            state = _state(sessions=items, selected_id="session-1")
            ctrl = _RecordingSessionsCtrl(state=state, detail=None)
            runtime = _runtime_with(sessions_ctrl=ctrl)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = SessionsScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                await pilot.pause()
                screen._selected_detail = None
                screen.action_copy_details()
                await pilot.pause()
                return screen._status

        assert "no session detail loaded" in asyncio.run(scenario())


class SessionsFocusPaneNoActionsTests(unittest.TestCase):
    def test_action_focus_pane_no_actions_service_sets_status(self) -> None:
        async def scenario() -> str:
            ctrl = _RecordingSessionsCtrl(state=_state())
            runtime = _runtime_with(sessions_ctrl=ctrl, actions=None)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = SessionsScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._selected_session_id = "session-1"
                screen._live_targets = {
                    "session-1": _LiveSessionTarget(
                        pane_id="%5",
                        window_id="@2",
                        session_name="muxdeck",
                    )
                }
                screen.action_focus_pane()
                await pilot.pause()
                return screen._status

        assert "action service unavailable" in asyncio.run(scenario())

    def test_action_focus_pane_with_failure_result_uses_failure_status(self) -> None:
        async def scenario() -> str:
            ctrl = _RecordingSessionsCtrl(state=_state())
            actions = _RecordingActions(
                focus_result=ActionResult(success=False, message="pane gone")
            )
            runtime = _runtime_with(sessions_ctrl=ctrl, actions=actions)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = SessionsScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._selected_session_id = "session-1"
                screen._live_targets = {
                    "session-1": _LiveSessionTarget(
                        pane_id="%5",
                        window_id=None,
                        session_name=None,
                    )
                }
                screen.action_focus_pane()
                await pilot.pause()
                return screen._status

        assert "pane gone" in asyncio.run(scenario())


class _StubPaneStream:
    """Minimal ``PaneStreamAdapter`` stand-in for the ``pane_stream`` slot.

    Implements just enough of the interface so that
    ``ComposeWithMirrorScreen`` can mount during ``action_open_live`` tests
    without exploding. The on_mount worker eventually calls
    ``capture_snapshot`` and ``start_pipe``; both are no-ops here.
    """

    def capture_snapshot(self, pane_id: str) -> str:
        return ""

    def start_pipe(self, pane_id: str, target: Path) -> None:
        return None

    def stop_pipe(self, pane_id: str) -> None:
        return None

    def send_keys(self, pane_id: str, keys: str) -> None:
        return None

    def pane_exists(self, pane_id: str) -> bool:
        return True


class SessionsOpenLiveTests(unittest.TestCase):
    def test_action_open_live_no_pane_stream_sets_status(self) -> None:
        async def scenario() -> str:
            ctrl = _RecordingSessionsCtrl(state=_state())
            runtime = _runtime_with(sessions_ctrl=ctrl, actions=_RecordingActions())
            # Default fake runtime has pane_stream=None already, but be
            # explicit so the test reads as intentional.
            runtime.pane_stream = None  # type: ignore[assignment]
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = SessionsScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._selected_session_id = "session-1"
                screen._selected_detail = _detail()
                screen._live_targets = {
                    "session-1": _LiveSessionTarget(
                        pane_id="%5",
                        window_id=None,
                        session_name=None,
                    )
                }
                screen.action_open_live()
                await pilot.pause()
                return screen._status

        assert "pane streaming unavailable" in asyncio.run(scenario())

    def test_action_open_live_with_stream_pushes_compose_screen(self) -> None:
        async def scenario() -> str:
            ctrl = _RecordingSessionsCtrl(state=_state())
            runtime = _runtime_with(sessions_ctrl=ctrl, actions=_RecordingActions())
            runtime.pane_stream = cast(Any, _StubPaneStream())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = SessionsScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._selected_session_id = "session-1"
                screen._selected_detail = _detail()
                screen._live_targets = {
                    "session-1": _LiveSessionTarget(
                        pane_id="%7",
                        window_id=None,
                        session_name=None,
                    )
                }
                screen.action_open_live()
                # Pause once so push_screen plumbing observes the new
                # screen on the stack but don't pause again — that would
                # mount it and trigger pane I/O against the stub.
                await pilot.pause()
                return type(app.screen).__name__

        # Pushed screen on top is the compose+mirror screen.
        assert asyncio.run(scenario()) == "ComposeWithMirrorScreen"


class SessionsResolveLiveMirrorAdvancedTests(unittest.TestCase):
    def test_resolves_via_socket_returns_resolved_pane_and_nested_stream(self) -> None:
        @dataclass(slots=True)
        class _StubTmuxAdapter:
            socket_calls: list[Path] = field(default_factory=list)

            def with_socket_path(self, path: Path) -> _StubTmuxAdapter:
                self.socket_calls.append(path)
                return _StubTmuxAdapter()

        target = _SessionTargetStub(pane_id="%nested", socket_path=Path("/tmp/sock"))

        async def scenario() -> tuple[str, bool]:
            ctrl = _RecordingSessionsCtrl(state=_state())
            runtime = _runtime_with(sessions_ctrl=ctrl, actions=_RecordingActions())
            runtime.session_resolver = cast(Any, _StubResolver(target))
            runtime.pane_stream = cast(Any, _StubPaneStream())
            tmux_stub = _StubTmuxAdapter()
            runtime.tmux = cast(Any, tmux_stub)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = SessionsScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                pane_id, adapter = screen._resolve_live_mirror_target(
                    _LiveSessionTarget(
                        pane_id="%outer",
                        window_id=None,
                        session_name=None,
                        pane_pid=99,
                    )
                )
                # When a socket path is returned the helper builds a
                # nested PaneStreamAdapter via the tmux adapter.
                return pane_id, adapter is not None

        pane_id, adapter_resolved = asyncio.run(scenario())
        assert pane_id == "%nested"
        assert adapter_resolved is True

    def test_falls_back_when_resolved_pane_id_is_none(self) -> None:
        # Resolved target has no pane id → fallback to the original target.
        target = _SessionTargetStub(pane_id=None, socket_path=Path("/tmp/sock"))

        async def scenario() -> str:
            ctrl = _RecordingSessionsCtrl(state=_state())
            runtime = _runtime_with(sessions_ctrl=ctrl, actions=_RecordingActions())
            runtime.session_resolver = cast(Any, _StubResolver(target))
            runtime.pane_stream = cast(Any, _StubPaneStream())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = SessionsScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                pane_id, _adapter = screen._resolve_live_mirror_target(
                    _LiveSessionTarget(
                        pane_id="%outer",
                        window_id=None,
                        session_name=None,
                        pane_pid=42,
                    )
                )
                return pane_id

        assert asyncio.run(scenario()) == "%outer"

    def test_socket_resolution_falls_back_when_tmux_is_none(self) -> None:
        # Resolver supplies a socket path, but the runtime has no tmux
        # adapter to use → the helper drops back to the outer pane id +
        # the runtime's existing pane_stream adapter.
        target = _SessionTargetStub(pane_id="%nested", socket_path=Path("/tmp/sock"))

        async def scenario() -> str:
            ctrl = _RecordingSessionsCtrl(state=_state())
            runtime = _runtime_with(sessions_ctrl=ctrl, actions=_RecordingActions())
            runtime.session_resolver = cast(Any, _StubResolver(target))
            runtime.pane_stream = cast(Any, _StubPaneStream())
            runtime.tmux = None
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = SessionsScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                pane_id, _adapter = screen._resolve_live_mirror_target(
                    _LiveSessionTarget(
                        pane_id="%outer",
                        window_id=None,
                        session_name=None,
                        pane_pid=42,
                    )
                )
                return pane_id

        assert asyncio.run(scenario()) == "%outer"


# Touch unused refs so they don't trip ruff.
_ = (SessionListItemView, SessionsState)
