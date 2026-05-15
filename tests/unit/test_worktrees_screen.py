"""Screen-level tests for ``WorktreesScreen`` actions and callbacks."""

# ruff: noqa: SLF001

from __future__ import annotations

import asyncio
import unittest
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from textual.app import App, ComposeResult
from textual.worker import Worker, WorkerState

from muxdeck.app import MuxdeckRuntime
from muxdeck.controllers.worktree_controller import (
    WorktreeActionView,
    WorktreeDetailView,
    WorktreeStartAgentIntent,
    WorktreeSummaryView,
)
from muxdeck.exceptions import DomainValidationError, PersistenceError
from muxdeck.screens.worktree_input import (
    AttachWorktreeResult,
    CreateWorktreeResult,
    LaunchAgentResult,
)
from muxdeck.screens.worktrees import (
    _DETAIL_WORKER_NAME,
    _WORKER_NAME,
    WorktreesScreen,
    _build_delete_worktree_message,
    _LoadedWorktreesState,
)
from muxdeck.services.action_service import ActionModelHint, ActionResult
from muxdeck.widgets.worktrees import (
    ConflictPanel,
    StartIntentPanel,
    WorktreeDetailPanel,
    WorktreeListPanel,
)


def _summary(
    *,
    worktree_id: str = "wt-1",
    is_main: bool = False,
    path: str = "/repo/wt-1",
    branch: str = "feature/x",
) -> WorktreeSummaryView:
    return WorktreeSummaryView(
        worktree_id=worktree_id,
        repo_root="/repo",
        path=path,
        branch=branch,
        base_branch="main",
        is_main_worktree=is_main,
        is_dirty=False,
        ahead_count=0,
        behind_count=0,
        locked=False,
        assigned_agent_id=None,
        assigned_agent_name=None,
        provenance=None,
        active_session_count=0,
        context_count=0,
        has_conflicts=False,
    )


def _detail(*, summary: WorktreeSummaryView | None = None) -> WorktreeDetailView:
    return WorktreeDetailView(
        summary=summary or _summary(),
        conflicts=(),
        active_session_ids=(),
        pane_targets=(),
    )


def _intent() -> WorktreeStartAgentIntent:
    return WorktreeStartAgentIntent(
        worktree_id="wt-1",
        repo_root="/repo",
        worktree_path="/repo/wt-1",
        branch="feature/x",
        suggested_session_name="muxdeck",
        suggested_window_name="copilot-x",
        prompt="",
    )


def _action_view() -> WorktreeActionView:
    return WorktreeActionView(
        action="created",
        message="created worktree wt-1",
        worktree=_detail(),
        conflicts=(),
    )


@dataclass(slots=True)
class _RecordingWorktreeService:
    summaries: tuple[WorktreeSummaryView, ...] = ()
    detail: WorktreeDetailView | None = None
    intent: WorktreeStartAgentIntent | None = None
    create_result: WorktreeActionView | Exception | None = None
    attach_result: WorktreeActionView | Exception | None = None
    remove_result: WorktreeActionView | Exception | None = None
    prune_result: WorktreeActionView | Exception | None = None
    create_calls: list[tuple[str, str | None]] = field(default_factory=list)
    attach_calls: list[Path] = field(default_factory=list)
    remove_calls: list[tuple[str, bool]] = field(default_factory=list)
    prune_calls: list[tuple[str, bool]] = field(default_factory=list)

    def list_worktrees(self) -> tuple[WorktreeSummaryView, ...]:
        return self.summaries

    def get_worktree_detail(self, worktree_id: str) -> WorktreeDetailView | None:
        del worktree_id
        return self.detail

    def start_agent_intent(
        self,
        worktree_id: str,
        *,
        prompt: str | None = None,
        model: str | None = None,
        target_session_name: str | None = None,
        window_name: str | None = None,
    ) -> WorktreeStartAgentIntent:
        del worktree_id, prompt, model, target_session_name, window_name
        return self.intent or _intent()

    def create_worktree(self, repo_root: Path, *, task_title: str | None) -> WorktreeActionView:
        self.create_calls.append((str(repo_root), task_title))
        if isinstance(self.create_result, Exception):
            raise self.create_result
        assert self.create_result is not None
        return self.create_result

    def attach_worktree(self, path: Path) -> WorktreeActionView:
        self.attach_calls.append(path)
        if isinstance(self.attach_result, Exception):
            raise self.attach_result
        assert self.attach_result is not None
        return self.attach_result

    def remove_worktree(self, worktree_id: str, *, force: bool) -> WorktreeActionView:
        self.remove_calls.append((worktree_id, force))
        if isinstance(self.remove_result, Exception):
            raise self.remove_result
        assert self.remove_result is not None
        return self.remove_result

    def prune_worktrees(self, repo_root: str, *, dry_run: bool) -> WorktreeActionView:
        self.prune_calls.append((repo_root, dry_run))
        if isinstance(self.prune_result, Exception):
            raise self.prune_result
        assert self.prune_result is not None
        return self.prune_result


@dataclass(slots=True)
class _RecordingActions:
    start_result: ActionResult = field(
        default_factory=lambda: ActionResult(success=True, message="agent started")
    )
    open_terminal_result: ActionResult = field(
        default_factory=lambda: ActionResult(success=True, message="terminal opened")
    )
    start_calls: list[dict[str, Any]] = field(default_factory=list)
    open_terminal_calls: list[dict[str, Any]] = field(default_factory=list)

    def start_agent(
        self,
        *,
        cwd: Path,
        model: str | None,
        window_name: str,
        target_session: str | None,
        prompt: str | None,
    ) -> ActionResult:
        self.start_calls.append(
            {
                "cwd": cwd,
                "model": model,
                "window_name": window_name,
                "target_session": target_session,
                "prompt": prompt,
            }
        )
        return self.start_result

    def open_terminal(self, *, cwd: Path, window_name: str) -> ActionResult:
        self.open_terminal_calls.append({"cwd": cwd, "window_name": window_name})
        return self.open_terminal_result


class _MinimalGeneral:
    log_preview_lines = 8


class _MinimalConfig:
    general = _MinimalGeneral()


def _runtime_with(
    *,
    worktrees: _RecordingWorktreeService,
    actions: _RecordingActions | None,
) -> MuxdeckRuntime:
    return cast(
        MuxdeckRuntime,
        type(
            "_FakeRuntime",
            (),
            {
                "config": _MinimalConfig(),
                "worktrees": worktrees,
                "sync_worktrees": worktrees,
                "actions": actions,
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

    def remember_worktree_selection(self, worktree_id: str) -> None:
        self.remembered.append(worktree_id)


class WorktreesActionTests(unittest.TestCase):
    def test_action_launch_agent_no_selection_sets_status(self) -> None:
        async def scenario() -> str:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.action_launch_agent()
                await pilot.pause()
                return screen._status

        assert "no worktree" in asyncio.run(scenario())

    def test_action_create_worktree_no_detail_sets_status(self) -> None:
        async def scenario() -> str:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.action_create_worktree()
                await pilot.pause()
                return screen._status

        assert "no repo selected" in asyncio.run(scenario())

    def test_action_open_git_terminal_no_detail_sets_status(self) -> None:
        async def scenario() -> str:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.action_open_git_terminal()
                await pilot.pause()
                return screen._status

        assert "no worktree selected" in asyncio.run(scenario())

    def test_action_open_git_terminal_no_actions_service_returns_error(self) -> None:
        async def scenario() -> str:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=None)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._detail = _detail()
                screen.action_open_git_terminal()
                await pilot.pause()
                return screen._status

        assert "action service unavailable" in asyncio.run(scenario())

    def test_action_open_git_terminal_calls_action_service(self) -> None:
        async def scenario() -> tuple[str, dict[str, Any]]:
            wts = _RecordingWorktreeService()
            actions = _RecordingActions()
            runtime = _runtime_with(worktrees=wts, actions=actions)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._detail = _detail()
                screen.action_open_git_terminal()
                await pilot.pause()
                assert actions.open_terminal_calls
                return screen._status, actions.open_terminal_calls[-1]

        status, call = asyncio.run(scenario())
        assert call["cwd"] == Path("/repo/wt-1")
        assert call["window_name"].startswith("git-")
        assert "✓" in status

    def test_action_copy_details_no_selection_sets_status(self) -> None:
        async def scenario() -> str:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.action_copy_details()
                await pilot.pause()
                return screen._status

        assert "no worktree selected" in asyncio.run(scenario())

    def test_action_delete_worktree_no_selection_sets_status(self) -> None:
        async def scenario() -> str:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.action_delete_worktree()
                await pilot.pause()
                return screen._status

        assert "no worktree" in asyncio.run(scenario())

    def test_action_delete_worktree_no_detail_sets_status(self) -> None:
        async def scenario() -> str:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._selected_worktree_id = "wt-1"
                screen.action_delete_worktree()
                await pilot.pause()
                return screen._status

        assert "no worktree detail" in asyncio.run(scenario())

    def test_action_delete_worktree_main_blocks(self) -> None:
        async def scenario() -> str:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._selected_worktree_id = "wt-main"
                screen._detail = _detail(
                    summary=_summary(worktree_id="wt-main", is_main=True),
                )
                screen.action_delete_worktree()
                await pilot.pause()
                return screen._status

        assert "cannot delete the main worktree" in asyncio.run(scenario())

    def test_on_delete_confirmed_cancelled_sets_status(self) -> None:
        async def scenario() -> str:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._on_delete_confirmed(False)
                await pilot.pause()
                return screen._status

        assert "delete cancelled" in asyncio.run(scenario())

    def test_on_delete_confirmed_failure_sets_failure_status(self) -> None:
        async def scenario() -> str:
            wts = _RecordingWorktreeService(remove_result=DomainValidationError("locked"))
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._selected_worktree_id = "wt-1"
                screen._detail = _detail()
                screen._on_delete_confirmed(True)
                await pilot.pause()
                return screen._status

        assert "delete failed" in asyncio.run(scenario())

    def test_action_prune_no_worktrees_sets_status(self) -> None:
        async def scenario() -> str:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._worktrees = ()
                screen.action_prune_worktrees()
                await pilot.pause()
                return screen._status

        assert "no worktrees" in asyncio.run(scenario())

    def test_action_prune_no_detail_sets_status(self) -> None:
        async def scenario() -> str:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._worktrees = (_summary(),)
                screen._detail = None
                screen.action_prune_worktrees()
                await pilot.pause()
                return screen._status

        assert "no worktree detail" in asyncio.run(scenario())

    def test_on_prune_confirmed_cancelled_sets_status(self) -> None:
        async def scenario() -> str:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._on_prune_confirmed(False)
                await pilot.pause()
                return screen._status

        assert "prune cancelled" in asyncio.run(scenario())

    def test_on_create_worktree_result_none_sets_cancelled(self) -> None:
        async def scenario() -> str:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._on_create_worktree_result(None)
                await pilot.pause()
                return screen._status

        assert "create cancelled" in asyncio.run(scenario())

    def test_on_create_worktree_result_failure(self) -> None:
        async def scenario() -> str:
            wts = _RecordingWorktreeService(create_result=DomainValidationError("invalid path"))
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._on_create_worktree_result(
                    CreateWorktreeResult(
                        repo_root="/repo",
                        task_title="bug",
                    )
                )
                await pilot.pause()
                return screen._status

        assert "create failed" in asyncio.run(scenario())

    def test_on_attach_worktree_result_none_sets_cancelled(self) -> None:
        async def scenario() -> str:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._on_attach_worktree_result(None)
                await pilot.pause()
                return screen._status

        assert "select existing cancelled" in asyncio.run(scenario())

    def test_on_attach_worktree_result_failure(self) -> None:
        async def scenario() -> str:
            wts = _RecordingWorktreeService(attach_result=DomainValidationError("not a repo"))
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._on_attach_worktree_result(AttachWorktreeResult(path="/repo/other"))
                await pilot.pause()
                return screen._status

        assert "attach failed" in asyncio.run(scenario())

    def test_on_launch_agent_result_none_returns_silently(self) -> None:
        async def scenario() -> str:
            wts = _RecordingWorktreeService()
            actions = _RecordingActions()
            runtime = _runtime_with(worktrees=wts, actions=actions)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                # None means the launch screen was dismissed.
                screen._on_launch_agent_result(None)
                await pilot.pause()
                return screen._status

        # Status is whatever it was before the no-op (default "ready"
        # or "loading worktrees…"); we only assert no exception.
        asyncio.run(scenario())

    def test_on_launch_agent_result_cancelled_sets_status(self) -> None:
        async def scenario() -> str:
            wts = _RecordingWorktreeService()
            actions = _RecordingActions()
            runtime = _runtime_with(worktrees=wts, actions=actions)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._on_launch_agent_result(
                    LaunchAgentResult(
                        confirmed=False,
                        selected_worktree_id="wt-1",
                        prompt="",
                        model=None,
                        target_session_name="muxdeck",
                        window_name="copilot-x",
                    )
                )
                await pilot.pause()
                return screen._status

        assert "launch cancelled" in asyncio.run(scenario())

    def test_on_launch_agent_result_no_actions_service(self) -> None:
        async def scenario() -> str:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=None)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._on_launch_agent_result(
                    LaunchAgentResult(
                        confirmed=True,
                        selected_worktree_id="wt-1",
                        prompt="hi",
                        model=None,
                        target_session_name="muxdeck",
                        window_name="copilot-x",
                    )
                )
                await pilot.pause()
                return screen._status

        assert "action service unavailable" in asyncio.run(scenario())

    def test_on_launch_agent_result_calls_start_agent(self) -> None:
        async def scenario() -> tuple[str, dict[str, Any]]:
            wts = _RecordingWorktreeService(intent=_intent())
            actions = _RecordingActions()
            runtime = _runtime_with(worktrees=wts, actions=actions)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._on_launch_agent_result(
                    LaunchAgentResult(
                        confirmed=True,
                        selected_worktree_id="wt-1",
                        prompt="ship the bug",
                        model="claude",
                        target_session_name="muxdeck",
                        window_name="copilot-x",
                    )
                )
                await pilot.pause()
                assert actions.start_calls
                return screen._status, actions.start_calls[-1]

        status, call = asyncio.run(scenario())
        assert call["cwd"] == Path("/repo/wt-1")
        assert call["window_name"] == "copilot-x"
        assert "✓" in status

    def test_action_cursor_down_and_up_no_op(self) -> None:
        async def scenario() -> None:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.action_cursor_down()
                screen.action_cursor_up()
                await pilot.pause()

        asyncio.run(scenario())

    def test_aliases_call_through_to_launch_agent(self) -> None:
        async def scenario() -> tuple[str, str]:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.action_execute_start()
                first = screen._status
                screen.action_preview_start_agent()
                second = screen._status
                return first, second

        first, second = asyncio.run(scenario())
        assert first == second  # both go through the same path

    def test_set_status_during_loading_stashes_pending(self) -> None:
        async def scenario() -> str | None:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._loading = True
                screen.set_status("✓ created worktree wt-1")
                return screen._pending_status_after_refresh

        assert "created worktree" in (asyncio.run(scenario()) or "")


# Touch unused refs so they don't trip ruff.
_ = (_action_view, _RecordingActions)


def _worker_event(
    *, name: str, state: WorkerState, result: object | None = None
) -> Worker.StateChanged:
    """Build a ``Worker.StateChanged`` stand-in usable from a screen handler.

    The ``on_worker_state_changed`` handler only ever reads
    ``event.worker.name``, ``event.worker.state``, ``event.worker.result``
    and ``event.state``. Mirror that surface with ``SimpleNamespace`` so we
    can drive the worker callback without having to schedule a real worker.
    """
    return cast(
        Worker.StateChanged,
        SimpleNamespace(
            state=state,
            worker=SimpleNamespace(name=name, state=state, result=result),
        ),
    )


class WorktreesWorkerCallbackTests(unittest.TestCase):
    """Drive ``on_worker_state_changed`` through its branches directly."""

    def test_unknown_worker_name_is_ignored(self) -> None:
        async def scenario() -> str:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.set_status("baseline")
                screen.on_worker_state_changed(
                    _worker_event(name="other_worker", state=WorkerState.SUCCESS)
                )
                await pilot.pause()
                return screen._status

        # A worker we don't own should leave the status untouched.
        assert asyncio.run(scenario()) == "baseline"

    def test_load_worker_error_surfaces_status_and_clears_loading(self) -> None:
        async def scenario() -> tuple[str, bool]:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._loading = True
                screen.on_worker_state_changed(
                    _worker_event(name=_WORKER_NAME, state=WorkerState.ERROR)
                )
                await pilot.pause()
                return screen._status, screen._loading

        status, loading = asyncio.run(scenario())
        assert "load failed" in status
        assert loading is False

    def test_load_worker_cancelled_clears_loading_without_status(self) -> None:
        async def scenario() -> tuple[str, bool]:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.set_status("snapshot")
                screen._loading = True
                screen.on_worker_state_changed(
                    _worker_event(name=_WORKER_NAME, state=WorkerState.CANCELLED)
                )
                await pilot.pause()
                return screen._status, screen._loading

        status, loading = asyncio.run(scenario())
        # Cancelled doesn't update the status, just clears the loading flag.
        assert status == "snapshot"
        assert loading is False

    def test_load_worker_success_with_none_result_only_clears_loading(self) -> None:
        async def scenario() -> bool:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._loading = True
                screen.on_worker_state_changed(
                    _worker_event(name=_WORKER_NAME, state=WorkerState.SUCCESS, result=None)
                )
                await pilot.pause()
                return screen._loading

        # Worker handed back nothing; nothing to apply but loading must end.
        assert asyncio.run(scenario()) is False

    def test_load_worker_success_applies_state_and_remembers_selection(self) -> None:
        async def scenario() -> tuple[tuple[str, ...], list[str], str]:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._loading = True
                summaries = (
                    _summary(worktree_id="wt-a"),
                    _summary(worktree_id="wt-b"),
                )
                state = _LoadedWorktreesState(
                    worktrees=summaries,
                    detail=_detail(summary=summaries[0]),
                    start_intent=_intent(),
                    selected_worktree_id="wt-a",
                )
                screen.on_worker_state_changed(
                    _worker_event(name=_WORKER_NAME, state=WorkerState.SUCCESS, result=state)
                )
                await pilot.pause()
                return (
                    tuple(item.worktree_id for item in screen._worktrees),
                    list(app.remembered),
                    screen._status,
                )

        ids, remembered, status = asyncio.run(scenario())
        assert ids == ("wt-a", "wt-b")
        assert remembered[-1] == "wt-a"
        assert "2 worktrees" in status

    def test_load_worker_success_empty_state_sets_no_worktrees_message(self) -> None:
        async def scenario() -> str:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._loading = True
                state = _LoadedWorktreesState(
                    worktrees=(),
                    detail=None,
                    start_intent=None,
                    selected_worktree_id=None,
                )
                screen.on_worker_state_changed(
                    _worker_event(name=_WORKER_NAME, state=WorkerState.SUCCESS, result=state)
                )
                await pilot.pause()
                return screen._status

        assert "no worktrees discovered" in asyncio.run(scenario())

    def test_load_worker_success_uses_pending_status_when_set(self) -> None:
        async def scenario() -> str:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._loading = True
                screen._pending_status_after_refresh = "✓ created worktree wt-a"
                state = _LoadedWorktreesState(
                    worktrees=(_summary(worktree_id="wt-a"),),
                    detail=None,
                    start_intent=None,
                    selected_worktree_id=None,
                )
                screen.on_worker_state_changed(
                    _worker_event(name=_WORKER_NAME, state=WorkerState.SUCCESS, result=state)
                )
                await pilot.pause()
                return screen._status

        # The post-action message survives the refresh that would have
        # otherwise overwritten it with the default count summary.
        assert "created worktree wt-a" in asyncio.run(scenario())

    def test_detail_worker_success_updates_panels_for_active_selection(self) -> None:
        async def scenario() -> tuple[str | None, str | None]:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._selected_worktree_id = "wt-1"
                detail = _detail()
                intent = _intent()
                screen.on_worker_state_changed(
                    _worker_event(
                        name=_DETAIL_WORKER_NAME,
                        state=WorkerState.SUCCESS,
                        result=("wt-1", detail, intent),
                    )
                )
                await pilot.pause()
                detail_id = None if screen._detail is None else screen._detail.summary.worktree_id
                intent_id = (
                    None if screen._start_intent is None else screen._start_intent.worktree_id
                )
                return detail_id, intent_id

        detail_id, intent_id = asyncio.run(scenario())
        assert detail_id == "wt-1"
        assert intent_id == "wt-1"

    def test_detail_worker_drops_stale_results(self) -> None:
        async def scenario() -> tuple[WorktreeDetailView | None, WorktreeStartAgentIntent | None]:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._selected_worktree_id = "wt-current"
                screen._detail = None
                screen._start_intent = None
                stale_detail = _detail(summary=_summary(worktree_id="wt-stale"))
                screen.on_worker_state_changed(
                    _worker_event(
                        name=_DETAIL_WORKER_NAME,
                        state=WorkerState.SUCCESS,
                        result=("wt-stale", stale_detail, _intent()),
                    )
                )
                await pilot.pause()
                return screen._detail, screen._start_intent

        # The user moved on to a different selection; the stale worker
        # result is dropped on the floor instead of overwriting it.
        detail, intent = asyncio.run(scenario())
        assert detail is None
        assert intent is None

    def test_detail_worker_non_success_event_is_ignored(self) -> None:
        async def scenario() -> WorktreeDetailView | None:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._detail = _detail()
                screen.on_worker_state_changed(
                    _worker_event(name=_DETAIL_WORKER_NAME, state=WorkerState.ERROR)
                )
                await pilot.pause()
                return screen._detail

        # Detail worker failures are silent — we keep the previous detail.
        detail = asyncio.run(scenario())
        assert detail is not None

    def test_detail_worker_with_none_payload_returns_silently(self) -> None:
        async def scenario() -> WorktreeDetailView | None:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._detail = _detail()
                screen.on_worker_state_changed(
                    _worker_event(
                        name=_DETAIL_WORKER_NAME,
                        state=WorkerState.SUCCESS,
                        result=None,
                    )
                )
                await pilot.pause()
                return screen._detail

        detail = asyncio.run(scenario())
        # Empty payload is a no-op — the previous selection survives.
        assert detail is not None


class WorktreesDetailWorkerRaceTests(unittest.TestCase):
    """Exercise the actual ``_load`` body that the worker runs.

    The other detail-worker tests only fire pre-baked
    ``Worker.StateChanged`` events; they never run the closure, so a
    crashing ``_load`` would still pass them. These tests were added in
    response to a real production regression where deleting the
    selected worktree mid-fetch surfaced ``PersistenceError`` from the
    worker thread and tore down the app.
    """

    @staticmethod
    def _capture_load(screen: WorktreesScreen) -> Callable[[], object]:
        """Intercept ``screen.run_worker`` and return the captured callable.

        ``_update_selected_detail`` builds the ``_load`` closure inline
        and hands it straight to ``run_worker``. We replace ``run_worker``
        with a recorder so we can invoke ``_load`` synchronously and
        observe whether it raises — Textual purges completed workers from
        ``app.workers`` immediately, so polling that list is unreliable.
        """
        captured: list[Callable[[], object]] = []

        def _record(fn: Callable[[], object], *a: object, **kw: object) -> None:
            captured.append(fn)

        screen.run_worker = _record  # type: ignore[assignment, method-assign]
        screen._update_selected_detail()
        assert captured, "_update_selected_detail did not schedule a worker"
        return captured[-1]

    def test_detail_worker_returns_empty_payload_when_worktree_deleted_midflight(
        self,
    ) -> None:
        """Reproduces the production crash: worktree row vanishes after
        the worker is scheduled but before ``get_worktree_detail`` runs.

        Before the fix, the worker raised ``PersistenceError`` and Textual
        propagated it as a fatal error. The fix swallows the error inside
        ``_load`` and returns an empty payload that ``on_worker_state_changed``
        treats as a no-op.
        """
        from muxdeck.exceptions import PersistenceError

        intent_calls: list[str] = []

        class _MissingDetailService(_RecordingWorktreeService):
            def get_worktree_detail(self, worktree_id: str) -> WorktreeDetailView | None:
                raise PersistenceError(f"unknown worktree: {worktree_id}")

            def start_agent_intent(  # type: ignore[override]
                self,
                worktree_id: str,
                *,
                prompt: str | None = None,
                model: str | None = None,
                target_session_name: str | None = None,
                window_name: str | None = None,
            ) -> WorktreeStartAgentIntent:
                intent_calls.append(worktree_id)
                return _intent()

        async def scenario() -> object:
            wts = _MissingDetailService()
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._selected_worktree_id = "wt-deleted"
                load = self._capture_load(screen)
                # Run the closure exactly the way the worker thread would.
                # Before the fix this raised PersistenceError; with the
                # fix it must return an empty payload.
                return load()

        result = asyncio.run(scenario())
        assert result == ("wt-deleted", None, None), (
            f"expected empty payload after delete-race, got {result!r}"
        )
        # When the detail lookup fails, we must NOT proceed to fetch
        # the start-intent (which would raise the same error). The
        # short-circuit is part of the contract.
        assert intent_calls == [], (
            f"start_agent_intent should not be called after detail failure, got {intent_calls!r}"
        )

    def test_detail_worker_returns_partial_payload_when_intent_lookup_fails(
        self,
    ) -> None:
        """If the worktree is removed between the detail fetch and the
        start-intent fetch, we still surface the detail and just drop
        the intent. This shouldn't crash the worker either.
        """
        from muxdeck.exceptions import PersistenceError

        captured_detail = _detail(summary=_summary(worktree_id="wt-1"))

        class _MissingIntentService(_RecordingWorktreeService):
            def start_agent_intent(  # type: ignore[override]
                self,
                worktree_id: str,
                *,
                prompt: str | None = None,
                model: str | None = None,
                target_session_name: str | None = None,
                window_name: str | None = None,
            ) -> WorktreeStartAgentIntent:
                raise PersistenceError(f"unknown worktree: {worktree_id}")

        async def scenario() -> object:
            wts = _MissingIntentService(detail=captured_detail)
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._selected_worktree_id = "wt-1"
                load = self._capture_load(screen)
                return load()

        result = asyncio.run(scenario())
        assert isinstance(result, tuple)
        target_id, detail, intent = cast(
            tuple[str, WorktreeDetailView | None, WorktreeStartAgentIntent | None], result
        )
        assert target_id == "wt-1"
        # The detail loaded successfully — only the intent disappeared.
        assert detail is captured_detail
        assert intent is None

    def test_detail_worker_returns_full_payload_in_happy_path(self) -> None:
        """Sanity check: when nothing fails the worker still returns
        both the detail and the intent. Without this, a future change
        that always returns ``None`` would silently pass the race tests.
        """
        captured_detail = _detail(summary=_summary(worktree_id="wt-1"))
        captured_intent = _intent()

        async def scenario() -> object:
            wts = _RecordingWorktreeService(
                detail=captured_detail,
                intent=captured_intent,
            )
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._selected_worktree_id = "wt-1"
                load = self._capture_load(screen)
                return load()

        result = asyncio.run(scenario())
        assert isinstance(result, tuple)
        target_id, detail, intent = cast(
            tuple[str, WorktreeDetailView | None, WorktreeStartAgentIntent | None], result
        )
        assert target_id == "wt-1"
        assert detail is captured_detail
        assert intent is captured_intent


class WorktreesLoadWorkerResilienceTests(unittest.TestCase):
    """Exercise the ``_load`` body inside ``refresh_data``.

    A regression bug let a single exception from ``get_worktree_detail``
    or ``start_agent_intent`` blank the entire worktree list because the
    worker errored out before ``_apply_loaded_state`` ever ran. These
    tests pin the contract that the list survives partial failures.
    """

    @staticmethod
    def _capture_load(screen: WorktreesScreen) -> Callable[[], _LoadedWorktreesState]:
        """Intercept ``screen.run_worker`` and return the captured ``_load`` callable."""
        captured: list[Callable[[], object]] = []

        def _record(fn: Callable[[], object], *a: object, **kw: object) -> None:
            captured.append(fn)

        screen.run_worker = _record  # type: ignore[assignment, method-assign]
        screen.refresh_data()
        assert captured, "refresh_data did not schedule a worker"
        return cast(Callable[[], _LoadedWorktreesState], captured[-1])

    def test_load_worker_returns_full_state_in_happy_path(self) -> None:
        """Sanity check: when nothing fails the worker returns the list,
        the auto-selected detail, and the start intent. Pins that
        future broad-except changes don't hide a real regression."""
        summary = _summary(worktree_id="wt-1")
        captured_detail = _detail(summary=summary)
        captured_intent = _intent()

        async def scenario() -> _LoadedWorktreesState:
            wts = _RecordingWorktreeService(
                summaries=(summary,),
                detail=captured_detail,
                intent=captured_intent,
            )
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                load = self._capture_load(screen)
                return load()

        state = asyncio.run(scenario())
        assert state.worktrees == (summary,)
        assert state.detail is captured_detail
        assert state.start_intent is captured_intent
        assert state.selected_worktree_id == "wt-1"
        assert state.warning_message is None

    def test_load_worker_keeps_list_when_get_worktree_detail_raises(self) -> None:
        """The reproducer for the user-reported bug: a single exception
        from ``get_worktree_detail`` previously took down the whole
        worker, leaving the list panel empty even though
        ``list_worktrees()`` succeeded. The fix degrades gracefully:
        list survives, detail/intent are cleared, warning is set."""
        summary_a = _summary(worktree_id="wt-a", path="/repo/wt-a")
        summary_b = _summary(worktree_id="wt-b", path="/repo/wt-b")
        intent_calls: list[str] = []

        class _DetailRaisingService(_RecordingWorktreeService):
            def get_worktree_detail(self, worktree_id: str) -> WorktreeDetailView | None:
                raise PersistenceError(f"unknown worktree: {worktree_id}")

            def start_agent_intent(  # type: ignore[override]
                self,
                worktree_id: str,
                *,
                prompt: str | None = None,
                model: str | None = None,
                target_session_name: str | None = None,
                window_name: str | None = None,
            ) -> WorktreeStartAgentIntent:
                intent_calls.append(worktree_id)
                return _intent()

        async def scenario() -> _LoadedWorktreesState:
            wts = _DetailRaisingService(summaries=(summary_a, summary_b))
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                load = self._capture_load(screen)
                return load()

        state = asyncio.run(scenario())
        assert state.worktrees == (summary_a, summary_b), "list must survive a detail failure"
        assert state.selected_worktree_id == "wt-a"
        assert state.detail is None
        assert state.start_intent is None
        assert state.warning_message is not None
        assert "detail unavailable" in state.warning_message
        # Skipping intent after detail failure mirrors _update_selected_detail
        # and prevents redundant log spam from the same root cause.
        assert intent_calls == [], (
            f"start_agent_intent should not be called after detail failure, got {intent_calls!r}"
        )

    def test_load_worker_keeps_list_and_detail_when_intent_raises(self) -> None:
        """If only the start-intent lookup fails we still surface the
        list and the loaded detail. The intent panel is cleared and
        the warning explains what's missing."""
        summary_a = _summary(worktree_id="wt-a", path="/repo/wt-a")
        summary_b = _summary(worktree_id="wt-b", path="/repo/wt-b")
        captured_detail = _detail(summary=summary_a)

        class _IntentRaisingService(_RecordingWorktreeService):
            def start_agent_intent(  # type: ignore[override]
                self,
                worktree_id: str,
                *,
                prompt: str | None = None,
                model: str | None = None,
                target_session_name: str | None = None,
                window_name: str | None = None,
            ) -> WorktreeStartAgentIntent:
                raise PersistenceError(f"intent gone: {worktree_id}")

        async def scenario() -> _LoadedWorktreesState:
            wts = _IntentRaisingService(
                summaries=(summary_a, summary_b),
                detail=captured_detail,
            )
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                load = self._capture_load(screen)
                return load()

        state = asyncio.run(scenario())
        assert state.worktrees == (summary_a, summary_b)
        assert state.detail is captured_detail
        assert state.start_intent is None
        assert state.warning_message is not None
        assert "intent unavailable" in state.warning_message

    def test_load_worker_keeps_list_when_detail_raises_unexpected_exception(
        self,
    ) -> None:
        """The boundary catch must absorb non-``MuxdeckError`` exceptions
        too — e.g. ``OSError`` from a stale Windows drive mount or
        ``RuntimeError`` from a corrupted git index. Without this, the
        worker dies and the user is back to an empty list with no list
        rendered at all."""
        summary = _summary(worktree_id="wt-only")

        class _OSErrorService(_RecordingWorktreeService):
            def get_worktree_detail(self, worktree_id: str) -> WorktreeDetailView | None:
                raise OSError("transport endpoint is not connected")

        async def scenario() -> _LoadedWorktreesState:
            wts = _OSErrorService(summaries=(summary,))
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                load = self._capture_load(screen)
                return load()

        state = asyncio.run(scenario())
        assert state.worktrees == (summary,)
        assert state.detail is None
        assert state.warning_message is not None
        assert "transport endpoint" in state.warning_message

    def test_load_worker_empty_list_skips_enrichment_calls(self) -> None:
        """When ``list_worktrees()`` returns nothing we must NOT call
        detail/intent (there's no selection). The state has no warning
        because there's no failure — just nothing to show."""
        detail_calls: list[str] = []
        intent_calls: list[str] = []

        class _RecordingService(_RecordingWorktreeService):
            def get_worktree_detail(self, worktree_id: str) -> WorktreeDetailView | None:
                detail_calls.append(worktree_id)
                return None

            def start_agent_intent(  # type: ignore[override]
                self,
                worktree_id: str,
                *,
                prompt: str | None = None,
                model: str | None = None,
                target_session_name: str | None = None,
                window_name: str | None = None,
            ) -> WorktreeStartAgentIntent:
                intent_calls.append(worktree_id)
                return _intent()

        async def scenario() -> _LoadedWorktreesState:
            wts = _RecordingService(summaries=())
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                load = self._capture_load(screen)
                return load()

        state = asyncio.run(scenario())
        assert state.worktrees == ()
        assert state.selected_worktree_id is None
        assert state.detail is None
        assert state.start_intent is None
        assert state.warning_message is None
        assert detail_calls == []
        assert intent_calls == []

    def test_apply_loaded_state_surfaces_warning_message(self) -> None:
        """``_apply_loaded_state`` must prefer the warning message over
        the default ``"N worktrees loaded"`` count so the user notices
        when detail/intent silently failed."""
        summary = _summary(worktree_id="wt-1")

        async def scenario() -> str:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._loading = True
                state = _LoadedWorktreesState(
                    worktrees=(summary,),
                    detail=None,
                    start_intent=None,
                    selected_worktree_id="wt-1",
                    warning_message="worktree detail unavailable: simulated",
                )
                screen.on_worker_state_changed(
                    _worker_event(name=_WORKER_NAME, state=WorkerState.SUCCESS, result=state)
                )
                await pilot.pause()
                return screen._status

        assert "detail unavailable" in asyncio.run(scenario())

    def test_apply_loaded_state_pending_status_overrides_warning(self) -> None:
        """The user-action pending status (e.g. ``✓ created …``) is
        what triggered the refresh and must outrank the warning so
        the user sees the result of THEIR action, not a worker hint."""
        summary = _summary(worktree_id="wt-1")

        async def scenario() -> str:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._loading = True
                screen._pending_status_after_refresh = "✓ created /repo/wt-1"
                state = _LoadedWorktreesState(
                    worktrees=(summary,),
                    detail=None,
                    start_intent=None,
                    selected_worktree_id="wt-1",
                    warning_message="worktree detail unavailable: simulated",
                )
                screen.on_worker_state_changed(
                    _worker_event(name=_WORKER_NAME, state=WorkerState.SUCCESS, result=state)
                )
                await pilot.pause()
                return screen._status

        status = asyncio.run(scenario())
        assert status == "✓ created /repo/wt-1"

    def test_load_worker_through_full_pipeline_renders_list_on_partial_failure(
        self,
    ) -> None:
        """End-to-end: run the actual worker via ``app.run_test`` and
        assert the list panel displays both rows when detail loading
        raises. This is the test that would have caught the original
        production regression — the unit-level closure tests above
        could pass even if the worker plumbing itself was broken."""
        summary_a = _summary(worktree_id="wt-a", path="/repo/wt-a")
        summary_b = _summary(worktree_id="wt-b", path="/repo/wt-b")

        class _DetailRaisingService(_RecordingWorktreeService):
            def get_worktree_detail(self, worktree_id: str) -> WorktreeDetailView | None:
                raise PersistenceError(f"unknown worktree: {worktree_id}")

        async def scenario() -> tuple[int, bool, str]:
            wts = _DetailRaisingService(summaries=(summary_a, summary_b))
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                # Wait for the load worker to finish (worker is threaded;
                # poll with a deterministic upper bound rather than a
                # fixed sleep).
                for _ in range(60):
                    await pilot.pause()
                    if screen._loaded_once and not screen._loading:
                        break
                panel = screen.query_one(WorktreeListPanel)
                return (len(panel._worktrees), screen._loaded_once, screen._status)

        rendered, loaded_once, status = asyncio.run(scenario())
        assert rendered == 2, "list panel must render both rows even on detail failure"
        assert loaded_once is True
        assert "detail unavailable" in status


class WorktreesRefreshFlowTests(unittest.TestCase):
    """Cover refresh coalescing and on_show plumbing."""

    def test_on_show_after_first_skip_refreshes(self) -> None:
        async def scenario() -> int:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                refreshes: list[bool] = []

                original_refresh = screen.refresh_data

                def _track() -> None:
                    refreshes.append(True)
                    original_refresh()

                screen.refresh_data = _track  # type: ignore[method-assign]
                # First show after mount should swallow the redundant refresh.
                screen.on_show()
                first = len(refreshes)
                # A subsequent show triggers a fresh refresh.
                screen.on_show()
                second = len(refreshes)
                await pilot.pause()
                return second - first

        assert asyncio.run(scenario()) == 1

    def test_refresh_data_while_loading_marks_pending_only(self) -> None:
        async def scenario() -> tuple[bool, int]:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                # Simulate an in-flight refresh and verify a second one
                # only flags the pending bit instead of starting another.
                screen._loading = True
                before = len(list(screen.workers))
                screen.refresh_data()
                after = len(list(screen.workers))
                return screen._refresh_pending, after - before

        pending, started = asyncio.run(scenario())
        assert pending is True
        assert started == 0

    def test_schedule_pending_refresh_runs_when_pending(self) -> None:
        async def scenario() -> int:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
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

        # Pending bit clears and the queued refresh actually runs.
        assert asyncio.run(scenario()) >= 1

    def test_schedule_pending_refresh_no_op_when_not_pending(self) -> None:
        async def scenario() -> int:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
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


class WorktreesPushScreenTests(unittest.TestCase):
    """Verify action_* methods that open modal screens push them onto the app."""

    def test_action_create_worktree_with_detail_pushes_screen(self) -> None:
        async def scenario() -> str:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._detail = _detail()
                screen.action_create_worktree()
                await pilot.pause()
                return type(app.screen).__name__

        # CreateWorktreeScreen modal is on top of the WorktreesScreen.
        assert asyncio.run(scenario()) == "CreateWorktreeScreen"

    def test_action_attach_worktree_pushes_attach_screen(self) -> None:
        async def scenario() -> str:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.action_attach_worktree()
                await pilot.pause()
                return type(app.screen).__name__

        assert asyncio.run(scenario()) == "AttachWorktreeScreen"

    def test_action_delete_worktree_pushes_confirm_for_non_main(self) -> None:
        async def scenario() -> str:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._selected_worktree_id = "wt-1"
                screen._detail = _detail()
                screen.action_delete_worktree()
                await pilot.pause()
                return type(app.screen).__name__

        assert asyncio.run(scenario()) == "ConfirmScreen"

    def test_action_prune_worktrees_pushes_confirm(self) -> None:
        async def scenario() -> str:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._worktrees = (_summary(),)
                screen._detail = _detail()
                screen.action_prune_worktrees()
                await pilot.pause()
                return type(app.screen).__name__

        assert asyncio.run(scenario()) == "ConfirmScreen"

    def test_action_launch_agent_with_selection_pushes_launch_screen(self) -> None:
        async def scenario() -> str:
            wts = _RecordingWorktreeService(intent=_intent())
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._selected_worktree_id = "wt-1"
                screen._start_intent = _intent()
                screen.action_launch_agent()
                await pilot.pause()
                return type(app.screen).__name__

        assert asyncio.run(scenario()) == "LaunchAgentScreen"


class WorktreesActionResultTests(unittest.TestCase):
    """Drive the ``_on_*`` callbacks for confirmed flows that hit the runtime."""

    def test_on_create_worktree_result_success_runs_refresh_and_status(self) -> None:
        async def scenario() -> tuple[str, list[tuple[str, str | None]]]:
            wts = _RecordingWorktreeService(create_result=_action_view())
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._on_create_worktree_result(
                    CreateWorktreeResult(repo_root="/repo", task_title="bug")
                )
                await pilot.pause()
                return screen._status, list(wts.create_calls)

        status, calls = asyncio.run(scenario())
        assert "✓ created worktree wt-1" in status
        assert calls == [("/repo", "bug")]

    def test_on_attach_worktree_result_success_runs_refresh_and_status(self) -> None:
        async def scenario() -> tuple[str, list[Path]]:
            wts = _RecordingWorktreeService(attach_result=_action_view())
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._on_attach_worktree_result(AttachWorktreeResult(path="/repo/wt-1"))
                await pilot.pause()
                return screen._status, list(wts.attach_calls)

        status, calls = asyncio.run(scenario())
        assert status.startswith("✓")
        # ``attach_worktree`` receives the path verbatim from the modal result.
        assert calls == [cast(Path, "/repo/wt-1")]

    def test_on_delete_confirmed_no_selection_returns_silently(self) -> None:
        async def scenario() -> str:
            wts = _RecordingWorktreeService(remove_result=_action_view())
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.set_status("baseline")
                screen._selected_worktree_id = None
                screen._on_delete_confirmed(True)
                await pilot.pause()
                return screen._status

        # Confirmed delete with no selection is a noop — status stays put.
        assert asyncio.run(scenario()) == "baseline"

    def test_on_delete_confirmed_success_drops_local_state_and_refreshes(self) -> None:
        async def scenario() -> tuple[str, list[tuple[str, bool]], tuple[str, ...]]:
            wts = _RecordingWorktreeService(remove_result=_action_view())
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._selected_worktree_id = "wt-1"
                screen._detail = _detail()
                screen._worktrees = (_summary(worktree_id="wt-1"), _summary(worktree_id="wt-2"))
                screen._on_delete_confirmed(True)
                await pilot.pause()
                return (
                    screen._status,
                    list(wts.remove_calls),
                    tuple(item.worktree_id for item in screen._worktrees),
                )

        status, calls, ids = asyncio.run(scenario())
        assert status.startswith("✓")
        assert calls == [("wt-1", False)]
        # The deleted row is dropped immediately so the list never shows
        # a phantom entry between the action and the refresh worker.
        # Note: refresh worker may then re-populate from the fake's empty
        # snapshot, so we just assert the deleted row no longer appears.
        assert "wt-1" not in ids

    def test_on_delete_confirmed_failure_uses_failure_status(self) -> None:
        async def scenario() -> str:
            wts = _RecordingWorktreeService(remove_result=RuntimeError("locked"))
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._selected_worktree_id = "wt-1"
                screen._detail = _detail()
                screen._on_delete_confirmed(True)
                await pilot.pause()
                return screen._status

        assert "delete failed" in asyncio.run(scenario())

    def test_on_prune_confirmed_no_detail_returns_silently(self) -> None:
        async def scenario() -> str:
            wts = _RecordingWorktreeService(prune_result=_action_view())
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.set_status("baseline")
                screen._detail = None
                screen._on_prune_confirmed(True)
                await pilot.pause()
                return screen._status

        # No detail → early return, status untouched.
        assert asyncio.run(scenario()) == "baseline"

    def test_on_prune_confirmed_success_runs_refresh(self) -> None:
        async def scenario() -> tuple[str, list[tuple[str, bool]]]:
            wts = _RecordingWorktreeService(prune_result=_action_view())
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._detail = _detail()
                screen._on_prune_confirmed(True)
                await pilot.pause()
                return screen._status, list(wts.prune_calls)

        status, calls = asyncio.run(scenario())
        assert status.startswith("✓")
        assert calls == [("/repo", False)]

    def test_on_prune_confirmed_failure_sets_failure_status(self) -> None:
        async def scenario() -> str:
            wts = _RecordingWorktreeService(prune_result=DomainValidationError("boom"))
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._detail = _detail()
                screen._on_prune_confirmed(True)
                await pilot.pause()
                return screen._status

        assert "prune failed" in asyncio.run(scenario())

    def test_action_copy_details_with_selection_copies_panels(self) -> None:
        async def scenario() -> str:
            wts = _RecordingWorktreeService(detail=_detail(), intent=_intent())
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                # Pre-populate the list so ``selected_worktree_id`` returns
                # a real id, then make sure the detail panel is fed.
                screen._worktrees = (_summary(),)
                screen.query_one(WorktreeListPanel).set_worktrees(
                    screen._worktrees,
                    selected_worktree_id="wt-1",
                    notify=False,
                )
                screen._detail = _detail()
                screen.action_copy_details()
                await pilot.pause()
                return screen._status

        # The base ``copy_rendered_text`` either copies or reports
        # "no … available" — both branches go through the happy line range.
        status = asyncio.run(scenario())
        assert "worktree details" in status

    def test_action_copy_details_no_detail_loaded_sets_status(self) -> None:
        async def scenario() -> str:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._worktrees = (_summary(),)
                screen.query_one(WorktreeListPanel).set_worktrees(
                    screen._worktrees,
                    selected_worktree_id="wt-1",
                    notify=False,
                )
                screen._detail = None
                screen.action_copy_details()
                await pilot.pause()
                return screen._status

        assert "no worktree detail loaded" in asyncio.run(scenario())


class WorktreesSelectionTests(unittest.TestCase):
    """Cover the panel→screen selection pipeline."""

    def test_on_worktree_list_panel_worktree_selected_updates_state(self) -> None:
        async def scenario() -> str | None:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                message = WorktreeListPanel.WorktreeSelected("wt-2")
                screen.on_worktree_list_panel_worktree_selected(message)
                await pilot.pause()
                return screen._selected_worktree_id

        assert asyncio.run(scenario()) == "wt-2"

    def test_on_worktree_list_panel_same_id_is_noop(self) -> None:
        async def scenario() -> tuple[str | None, str | None]:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._selected_worktree_id = "wt-1"
                before = screen._detail_timer
                message = WorktreeListPanel.WorktreeSelected("wt-1")
                screen.on_worktree_list_panel_worktree_selected(message)
                await pilot.pause()
                return (
                    "noop" if screen._detail_timer is before else "rescheduled",
                    screen._selected_worktree_id,
                )

        marker, current = asyncio.run(scenario())
        assert marker == "noop"
        assert current == "wt-1"

    def test_update_selected_detail_with_no_selection_clears_panels(self) -> None:
        async def scenario() -> tuple[WorktreeDetailView | None, WorktreeStartAgentIntent | None]:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._detail = _detail()
                screen._start_intent = _intent()
                screen._selected_worktree_id = None
                screen._update_selected_detail()
                await pilot.pause()
                return screen._detail, screen._start_intent

        detail, intent = asyncio.run(scenario())
        assert detail is None
        assert intent is None

    def test_update_selected_detail_with_selection_starts_worker(self) -> None:
        async def scenario() -> int:
            wts = _RecordingWorktreeService(detail=_detail(), intent=_intent())
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._selected_worktree_id = "wt-1"
                screen._update_selected_detail()
                await pilot.pause()
                # The remembered list grows once for each detail load.
                return app.remembered.count("wt-1")

        assert asyncio.run(scenario()) >= 1


class WorktreesDropLocalStateTests(unittest.TestCase):
    def test_drop_worktree_from_local_state_removes_row_and_clears_panels(self) -> None:
        async def scenario() -> tuple[tuple[str, ...], str | None]:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._worktrees = (
                    _summary(worktree_id="wt-1"),
                    _summary(worktree_id="wt-2"),
                )
                screen._detail = _detail()
                screen._start_intent = _intent()
                screen._selected_worktree_id = "wt-1"
                screen._drop_worktree_from_local_state("wt-1")
                await pilot.pause()
                return (
                    tuple(item.worktree_id for item in screen._worktrees),
                    screen._selected_worktree_id,
                )

        ids, selected = asyncio.run(scenario())
        assert ids == ("wt-2",)
        assert selected is None


class WorktreesLaunchModelHintTests(unittest.TestCase):
    """Direct coverage for the ``_launch_model_hint`` helper."""

    def test_returns_default_when_no_actions_service(self) -> None:
        async def scenario() -> ActionModelHint:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=None)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                return screen._launch_model_hint()

        hint = asyncio.run(scenario())
        assert hint.configured_model is None

    def test_returns_default_when_loader_missing(self) -> None:
        @dataclass(slots=True)
        class _ActionsNoLoader:
            pass

        async def scenario() -> ActionModelHint:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=cast(Any, _ActionsNoLoader()))
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                return screen._launch_model_hint()

        # Without a callable ``launch_model_hint`` we fall back to defaults.
        hint = asyncio.run(scenario())
        assert hint.configured_model is None

    def test_returns_default_when_loader_returns_invalid_model(self) -> None:
        @dataclass(slots=True)
        class _BadHint:
            configured_model: object
            message: object

        @dataclass(slots=True)
        class _ActionsWithBadLoader:
            payload: _BadHint

            def launch_model_hint(self) -> _BadHint:
                return self.payload

        async def scenario() -> ActionModelHint:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(
                worktrees=wts,
                actions=cast(
                    Any,
                    _ActionsWithBadLoader(_BadHint(configured_model=42, message="ok")),
                ),
            )
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                return screen._launch_model_hint()

        # configured_model must be ``str | None``; a ``42`` triggers fallback.
        hint = asyncio.run(scenario())
        assert hint.configured_model is None

    def test_returns_default_when_loader_returns_invalid_message(self) -> None:
        @dataclass(slots=True)
        class _BadHint:
            configured_model: str | None
            message: object

        @dataclass(slots=True)
        class _ActionsWithBadLoader:
            def launch_model_hint(self) -> _BadHint:
                return _BadHint(configured_model="claude", message=12345)

        async def scenario() -> ActionModelHint:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=cast(Any, _ActionsWithBadLoader()))
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                return screen._launch_model_hint()

        # Non-string ``message`` falls back to the default hint object.
        hint = asyncio.run(scenario())
        assert hint.configured_model is None

    def test_returns_loader_value_when_valid(self) -> None:
        @dataclass(slots=True)
        class _GoodHint:
            configured_model: str | None
            message: str

        @dataclass(slots=True)
        class _ActionsWithLoader:
            def launch_model_hint(self) -> _GoodHint:
                return _GoodHint(configured_model="claude", message="pick a model")

        async def scenario() -> ActionModelHint:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=cast(Any, _ActionsWithLoader()))
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                return screen._launch_model_hint()

        hint = asyncio.run(scenario())
        assert hint.configured_model == "claude"
        assert hint.message == "pick a model"


class WorktreesRefreshAfterActionTests(unittest.TestCase):
    def test_refresh_after_action_with_worktree_uses_returned_id(self) -> None:
        async def scenario() -> tuple[str | None, str | None]:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                # Stub refresh_data so the background worker doesn't reset
                # the selection back to ``None`` from the empty fake snapshot.
                screen.refresh_data = lambda: None  # type: ignore[method-assign]
                action_view = WorktreeActionView(
                    action="created",
                    message="created worktree wt-9",
                    worktree=_detail(summary=_summary(worktree_id="wt-9")),
                    conflicts=(),
                )
                screen._refresh_after_worktree_action(action_view)
                await pilot.pause()
                return screen._selected_worktree_id, screen._pending_status_after_refresh

        selected, pending = asyncio.run(scenario())
        assert selected == "wt-9"
        assert pending is not None
        assert "created worktree wt-9" in pending

    def test_refresh_after_action_with_no_worktree_keeps_selection(self) -> None:
        async def scenario() -> str | None:
            wts = _RecordingWorktreeService()
            runtime = _runtime_with(worktrees=wts, actions=_RecordingActions())
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = WorktreesScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._selected_worktree_id = "wt-2"
                screen.refresh_data = lambda: None  # type: ignore[method-assign]
                action_view = WorktreeActionView(
                    action="pruned",
                    message="pruned 0 worktrees",
                    worktree=None,
                    conflicts=(),
                )
                screen._refresh_after_worktree_action(action_view)
                await pilot.pause()
                return screen._selected_worktree_id

        # When the action_view carries no worktree, the prior selection survives.
        assert asyncio.run(scenario()) == "wt-2"


class BuildDeleteWorktreeMessageTests(unittest.TestCase):
    """Pure formatter for the delete-confirm dialog message.

    Regression: the dialog used to render the full path and wrap mid-segment
    (e.g. ``/mnt/q/src/CosmosDB.worktrees/agents-…`` clipped to
    ``…CosmosDB.worktree`` on the first line), making users think the path
    was wrong. The formatter now leads with the recognizable basename + branch
    and labels the full path on a second line.
    """

    def test_uses_basename_branch_and_labels_full_path(self) -> None:
        summary = _summary(
            worktree_id="wt-9",
            path=(
                "/mnt/q/src/CosmosDB.worktrees/"
                "agents-i-lost-the-agent-session-id-that-worked-9218e730"
            ),
            branch="agents/i-lost-the-agent-session-id-that-worked",
        )

        message = _build_delete_worktree_message(summary, "wt-9")

        first_line, _, rest = message.partition("\n")
        # The primary identifier is the basename, not the full path.
        assert first_line == (
            "Delete worktree "
            "'agents-i-lost-the-agent-session-id-that-worked-9218e730' "
            "(branch: agents/i-lost-the-agent-session-id-that-worked)?"
        )
        # Full path is explicitly labeled so wrapping is unambiguous.
        assert rest == (
            "Full path: /mnt/q/src/CosmosDB.worktrees/"
            "agents-i-lost-the-agent-session-id-that-worked-9218e730"
        )

    def test_falls_back_to_selected_id_when_path_missing(self) -> None:
        summary = _summary(worktree_id="wt-2", path="", branch="")

        message = _build_delete_worktree_message(summary, "wt-2")

        assert message == ("Delete worktree 'wt-2' (branch: unknown)?\nFull path: wt-2")

    def test_falls_back_to_unknown_when_everything_missing(self) -> None:
        summary = _summary(worktree_id="", path="", branch="")

        message = _build_delete_worktree_message(summary, None)

        assert message == ("Delete worktree 'unknown' (branch: unknown)?\nFull path: unknown")


# Touch unused refs so they don't trip ruff.
_unused_panels = (ConflictPanel, StartIntentPanel, WorktreeDetailPanel, ActionResult)
