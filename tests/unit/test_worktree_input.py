"""Tests for worktree input modals."""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import unittest
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import cast

from textual.app import App, ComposeResult
from textual.pilot import Pilot
from textual.widgets import Input, Static

from muxdeck.controllers import (
    WorktreeActionView,
    WorktreeDetailView,
    WorktreeStartAgentIntent,
    WorktreeSummaryView,
)
from muxdeck.exceptions import DomainValidationError, PersistenceError
from muxdeck.screens.worktree_input import (
    AttachWorktreeResult,
    AttachWorktreeScreen,
    CreateWorktreeResult,
    CreateWorktreeScreen,
    LaunchAgentResult,
    LaunchAgentScreen,
    LaunchWorktreeController,
)
from muxdeck.services.action_service import ActionModelHint


class TestCreateWorktreeResult:
    def test_fields(self) -> None:
        result = CreateWorktreeResult(repo_root="/repo", task_title="Ship worktrees")
        assert result.repo_root == "/repo"
        assert result.task_title == "Ship worktrees"

    def test_frozen(self) -> None:
        result = CreateWorktreeResult(repo_root="/repo", task_title="Ship worktrees")
        assert dataclasses.is_dataclass(result)
        try:
            result.task_title = "nope"  # type: ignore[misc]
            raise AssertionError("Expected FrozenInstanceError")  # pragma: no cover
        except dataclasses.FrozenInstanceError:
            pass

    def test_slots(self) -> None:
        assert hasattr(CreateWorktreeResult, "__slots__")


class TestAttachWorktreeResult:
    def test_fields(self) -> None:
        result = AttachWorktreeResult(path="/repo/worktrees/api")
        assert result.path == "/repo/worktrees/api"

    def test_frozen(self) -> None:
        result = AttachWorktreeResult(path="/repo/worktrees/api")
        assert dataclasses.is_dataclass(result)
        try:
            result.path = "/repo/worktrees/other"  # type: ignore[misc]
            raise AssertionError("Expected FrozenInstanceError")  # pragma: no cover
        except dataclasses.FrozenInstanceError:
            pass

    def test_slots(self) -> None:
        assert hasattr(AttachWorktreeResult, "__slots__")


class TestLaunchAgentResult:
    def test_fields(self) -> None:
        result = LaunchAgentResult(
            confirmed=True,
            selected_worktree_id="worktree-1",
            target_session_name="muxdeck",
            window_name="planner",
            prompt="Continue work",
            model="gpt-5.4",
        )
        assert result.confirmed is True
        assert result.selected_worktree_id == "worktree-1"
        assert result.model == "gpt-5.4"

    def test_slots(self) -> None:
        assert hasattr(LaunchAgentResult, "__slots__")


class TestCreateWorktreeScreen:
    def test_screen_init(self) -> None:
        screen = CreateWorktreeScreen(repo_root="/repo")
        assert screen._repo_root == "/repo"

    def test_compose_is_generator(self) -> None:
        screen = CreateWorktreeScreen(repo_root="/repo")
        assert inspect.isgeneratorfunction(screen.compose)


class TestAttachWorktreeScreen:
    def test_compose_is_generator(self) -> None:
        screen = AttachWorktreeScreen()
        assert inspect.isgeneratorfunction(screen.compose)


class _FakeWorktrees:
    def create_worktree(self, *_: object, **__: object) -> object:  # pragma: no cover - UI stub
        raise AssertionError("not used in this test")

    def attach_worktree(self, *_: object, **__: object) -> object:  # pragma: no cover - UI stub
        raise AssertionError("not used in this test")

    def start_agent_intent(self, worktree_id: str, **_: object) -> WorktreeStartAgentIntent:
        return WorktreeStartAgentIntent(
            worktree_id=worktree_id,
            repo_root="/repo",
            worktree_path="/repo/worktrees/ui",
            branch="task/ui",
            suggested_session_name="muxdeck",
            suggested_window_name="ui",
            prompt="Continue work for task/ui",
            model=None,
        )


class TestLaunchAgentScreen:
    def test_compose_is_generator(self) -> None:
        screen = LaunchAgentScreen(
            cast(LaunchWorktreeController, _FakeWorktrees()),
            intent=WorktreeStartAgentIntent(
                worktree_id="worktree-1",
                repo_root="/repo",
                worktree_path="/repo/worktrees/ui",
                branch="task/ui",
                suggested_session_name="muxdeck",
                suggested_window_name="ui",
                prompt="Continue work for task/ui",
                model=None,
            ),
            model_hint=ActionModelHint(
                configured_model="gpt-5.4",
                message=(
                    "Configured model: gpt-5.4. "
                    "Model availability depends on your Copilot account/provider. "
                    "Enter a model manually or leave it blank to use Copilot's default."
                ),
            ),
        )
        assert inspect.isgeneratorfunction(screen.compose)


# ── Behavioural screen tests below ───────────────────────────────────


class _Harness(App[None]):
    def compose(self) -> ComposeResult:
        return iter(())


def _intent(worktree_id: str = "worktree-1") -> WorktreeStartAgentIntent:
    return WorktreeStartAgentIntent(
        worktree_id=worktree_id,
        repo_root="/repo",
        worktree_path=f"/repo/worktrees/{worktree_id}",
        branch=f"task/{worktree_id}",
        suggested_session_name="muxdeck",
        suggested_window_name=worktree_id,
        prompt=f"Continue work for task/{worktree_id}",
        model=None,
    )


def _detail(worktree_id: str) -> WorktreeDetailView:
    summary = WorktreeSummaryView(
        worktree_id=worktree_id,
        repo_root="/repo",
        path=f"/repo/worktrees/{worktree_id}",
        branch=f"task/{worktree_id}",
        base_branch="main",
        is_main_worktree=False,
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
    return WorktreeDetailView(
        summary=summary,
        conflicts=(),
        active_session_ids=(),
        pane_targets=(),
    )


@dataclass(frozen=True, slots=True)
class _CreateDismiss:
    dismissed: bool
    value: CreateWorktreeResult | None


async def _run_create_action(
    action: Callable[[Pilot[None]], Awaitable[None]],
    *,
    repo_root: str = "/repo",
) -> _CreateDismiss:
    sentinel = object()
    captured: list[object] = [sentinel]

    def _capture(value: CreateWorktreeResult | None) -> None:
        captured[0] = value

    app = _Harness()
    async with app.run_test() as pilot:
        screen = CreateWorktreeScreen(repo_root=repo_root)
        await app.push_screen(screen, callback=_capture)
        await pilot.pause()
        await action(pilot)
        await pilot.pause()
    raw = captured[0]
    if raw is sentinel:
        return _CreateDismiss(dismissed=False, value=None)
    if raw is None:
        return _CreateDismiss(dismissed=True, value=None)
    assert isinstance(raw, CreateWorktreeResult)
    return _CreateDismiss(dismissed=True, value=raw)


@dataclass(frozen=True, slots=True)
class _AttachDismiss:
    dismissed: bool
    value: AttachWorktreeResult | None


async def _run_attach_action(
    action: Callable[[Pilot[None]], Awaitable[None]],
) -> _AttachDismiss:
    sentinel = object()
    captured: list[object] = [sentinel]

    def _capture(value: AttachWorktreeResult | None) -> None:
        captured[0] = value

    app = _Harness()
    async with app.run_test() as pilot:
        screen = AttachWorktreeScreen()
        await app.push_screen(screen, callback=_capture)
        await pilot.pause()
        await action(pilot)
        await pilot.pause()
    raw = captured[0]
    if raw is sentinel:
        return _AttachDismiss(dismissed=False, value=None)
    if raw is None:
        return _AttachDismiss(dismissed=True, value=None)
    assert isinstance(raw, AttachWorktreeResult)
    return _AttachDismiss(dismissed=True, value=raw)


class CreateWorktreeScreenBehaviourTests(unittest.TestCase):
    def test_compose_focuses_input_and_shows_repo(self) -> None:
        async def scenario() -> bool:
            app = _Harness()
            async with app.run_test() as pilot:
                screen = CreateWorktreeScreen(repo_root="/repo")
                await app.push_screen(screen)
                await pilot.pause()
                input_widget = app.screen.query_one("#create-worktree-title", Input)
                # Both buttons must exist.
                app.screen.query_one("#btn-cancel-create-worktree")
                app.screen.query_one("#btn-create-worktree")
                return input_widget.has_focus

        assert asyncio.run(scenario()) is True

    def test_escape_dismisses_with_none(self) -> None:
        async def action(pilot: Pilot[None]) -> None:
            await pilot.press("escape")

        result = asyncio.run(_run_create_action(action))
        assert result.dismissed is True
        assert result.value is None

    def test_cancel_button_dismisses_with_none(self) -> None:
        async def action(pilot: Pilot[None]) -> None:
            await pilot.click("#btn-cancel-create-worktree")

        result = asyncio.run(_run_create_action(action))
        assert result.dismissed is True
        assert result.value is None

    def test_submit_with_title_dismisses_with_result(self) -> None:
        async def action(pilot: Pilot[None]) -> None:
            pilot.app.screen.query_one("#create-worktree-title", Input).value = "  Plan migration  "
            await pilot.click("#btn-create-worktree")

        result = asyncio.run(_run_create_action(action))
        assert result.dismissed is True
        assert result.value == CreateWorktreeResult(
            repo_root="/repo",
            task_title="Plan migration",
        )

    def test_input_submitted_dismisses_with_result(self) -> None:
        async def action(pilot: Pilot[None]) -> None:
            pilot.app.screen.query_one("#create-worktree-title", Input).value = "Ship"
            await pilot.press("enter")

        result = asyncio.run(_run_create_action(action))
        assert result.dismissed is True
        assert result.value == CreateWorktreeResult(repo_root="/repo", task_title="Ship")

    def test_blank_title_keeps_screen_open(self) -> None:
        async def scenario() -> tuple[bool, bool, bool]:
            sentinel = object()
            captured: list[object] = [sentinel]

            def _capture(value: CreateWorktreeResult | None) -> None:
                captured[0] = value

            app = _Harness()
            async with app.run_test() as pilot:
                screen = CreateWorktreeScreen(repo_root="/repo")
                await app.push_screen(screen, callback=_capture)
                await pilot.pause()
                app.screen.query_one("#create-worktree-title", Input).value = "   "
                await pilot.press("enter")
                await pilot.pause()
                still_modal = isinstance(app.screen, CreateWorktreeScreen)
                refocused = app.screen.query_one("#create-worktree-title", Input).has_focus
                never_dismissed = captured[0] is sentinel
            return still_modal, refocused, never_dismissed

        still_modal, refocused, never_dismissed = asyncio.run(scenario())
        assert still_modal is True
        assert refocused is True
        assert never_dismissed is True


class AttachWorktreeScreenBehaviourTests(unittest.TestCase):
    def test_compose_focuses_input(self) -> None:
        async def scenario() -> bool:
            app = _Harness()
            async with app.run_test() as pilot:
                screen = AttachWorktreeScreen()
                await app.push_screen(screen)
                await pilot.pause()
                input_widget = app.screen.query_one("#attach-worktree-path", Input)
                app.screen.query_one("#btn-cancel-attach-worktree")
                app.screen.query_one("#btn-attach-worktree")
                return input_widget.has_focus

        assert asyncio.run(scenario()) is True

    def test_escape_dismisses_with_none(self) -> None:
        async def action(pilot: Pilot[None]) -> None:
            await pilot.press("escape")

        result = asyncio.run(_run_attach_action(action))
        assert result.dismissed is True
        assert result.value is None

    def test_cancel_button_dismisses_with_none(self) -> None:
        async def action(pilot: Pilot[None]) -> None:
            await pilot.click("#btn-cancel-attach-worktree")

        result = asyncio.run(_run_attach_action(action))
        assert result.dismissed is True
        assert result.value is None

    def test_submit_with_path_dismisses_with_result(self) -> None:
        async def action(pilot: Pilot[None]) -> None:
            pilot.app.screen.query_one(
                "#attach-worktree-path", Input
            ).value = "  /repo/worktrees/api  "
            await pilot.press("enter")

        result = asyncio.run(_run_attach_action(action))
        assert result.dismissed is True
        assert result.value == AttachWorktreeResult(path="/repo/worktrees/api")

    def test_attach_button_submits(self) -> None:
        async def action(pilot: Pilot[None]) -> None:
            pilot.app.screen.query_one("#attach-worktree-path", Input).value = "/repo/wt"
            await pilot.click("#btn-attach-worktree")

        result = asyncio.run(_run_attach_action(action))
        assert result.dismissed is True
        assert result.value == AttachWorktreeResult(path="/repo/wt")

    def test_blank_path_keeps_screen_open(self) -> None:
        async def scenario() -> tuple[bool, bool]:
            sentinel = object()
            captured: list[object] = [sentinel]

            def _capture(value: AttachWorktreeResult | None) -> None:
                captured[0] = value

            app = _Harness()
            async with app.run_test() as pilot:
                screen = AttachWorktreeScreen()
                await app.push_screen(screen, callback=_capture)
                await pilot.pause()
                app.screen.query_one("#attach-worktree-path", Input).value = "   "
                await pilot.press("enter")
                await pilot.pause()
                refocused = app.screen.query_one("#attach-worktree-path", Input).has_focus
                never_dismissed = captured[0] is sentinel
            return refocused, never_dismissed

        refocused, never_dismissed = asyncio.run(scenario())
        assert refocused is True
        assert never_dismissed is True


class _RecordingWorktrees:
    """LaunchWorktreeController stub that records calls and returns canned values."""

    def __init__(
        self,
        *,
        create_action: WorktreeActionView | Exception | None = None,
        attach_action: WorktreeActionView | Exception | None = None,
    ) -> None:
        self.create_calls: list[tuple[str, str | None]] = []
        self.attach_calls: list[str] = []
        self.intent_calls: list[tuple[str, str | None, str | None, str | None, str | None]] = []
        self._create_action = create_action
        self._attach_action = attach_action

    def create_worktree(
        self,
        cwd: str,
        /,
        *,
        task_title: str | None = None,
    ) -> WorktreeActionView:
        self.create_calls.append((cwd, task_title))
        if isinstance(self._create_action, Exception):
            raise self._create_action
        if self._create_action is None:
            raise AssertionError("no create_action configured")
        return self._create_action

    def attach_worktree(self, path: str, /) -> WorktreeActionView:
        self.attach_calls.append(path)
        if isinstance(self._attach_action, Exception):
            raise self._attach_action
        if self._attach_action is None:
            raise AssertionError("no attach_action configured")
        return self._attach_action

    def start_agent_intent(
        self,
        worktree_id: str,
        /,
        *,
        prompt: str | None = None,
        model: str | None = None,
        target_session_name: str | None = None,
        window_name: str | None = None,
    ) -> WorktreeStartAgentIntent:
        self.intent_calls.append((worktree_id, prompt, model, target_session_name, window_name))
        return _intent(worktree_id)


def _model_hint() -> ActionModelHint:
    return ActionModelHint(
        configured_model="gpt-5.4",
        message=("Configured model: gpt-5.4. Provide a model or leave blank for default."),
    )


class LaunchAgentScreenBehaviourTests(unittest.TestCase):
    def test_compose_prefills_inputs_and_focuses_name(self) -> None:
        async def scenario() -> tuple[bool, str, str, str, str]:
            worktrees = _RecordingWorktrees()
            app = _Harness()
            async with app.run_test(size=(140, 60)) as pilot:
                screen = LaunchAgentScreen(
                    cast(LaunchWorktreeController, worktrees),
                    intent=_intent(),
                    model_hint=_model_hint(),
                )
                await app.push_screen(screen)
                await pilot.pause()
                name = app.screen.query_one("#launch-agent-name", Input)
                session = app.screen.query_one("#launch-agent-session", Input)
                model = app.screen.query_one("#launch-agent-model", Input)
                prompt = app.screen.query_one("#launch-agent-prompt", Input)
                return (
                    name.has_focus,
                    name.value,
                    session.value,
                    model.value,
                    prompt.value,
                )

        focused, name_value, session_value, model_value, prompt_value = asyncio.run(scenario())
        assert focused is True
        assert name_value == "worktree-1"
        assert session_value == "muxdeck"
        # When intent.model is None, the configured model from hint is used.
        assert model_value == "gpt-5.4"
        assert prompt_value == "Continue work for task/worktree-1"

    def test_cancel_button_dismisses_with_unconfirmed_result(self) -> None:
        async def scenario() -> LaunchAgentResult:
            worktrees = _RecordingWorktrees()
            captured: list[LaunchAgentResult | None] = [None]

            def _capture(value: LaunchAgentResult | None) -> None:
                captured[0] = value

            app = _Harness()
            async with app.run_test(size=(140, 60)) as pilot:
                screen = LaunchAgentScreen(
                    cast(LaunchWorktreeController, worktrees),
                    intent=_intent(),
                    model_hint=_model_hint(),
                )
                await app.push_screen(screen, callback=_capture)
                await pilot.pause()
                await pilot.click("#btn-cancel-launch-agent")
                await pilot.pause()
            assert captured[0] is not None
            return captured[0]

        result = asyncio.run(scenario())
        assert result.confirmed is False
        assert result.selected_worktree_id == "worktree-1"

    def test_escape_dismisses_with_unconfirmed_result(self) -> None:
        async def scenario() -> LaunchAgentResult:
            worktrees = _RecordingWorktrees()
            captured: list[LaunchAgentResult | None] = [None]

            def _capture(value: LaunchAgentResult | None) -> None:
                captured[0] = value

            app = _Harness()
            async with app.run_test(size=(140, 60)) as pilot:
                screen = LaunchAgentScreen(
                    cast(LaunchWorktreeController, worktrees),
                    intent=_intent(),
                    model_hint=_model_hint(),
                )
                await app.push_screen(screen, callback=_capture)
                await pilot.pause()
                await pilot.press("escape")
                await pilot.pause()
            assert captured[0] is not None
            return captured[0]

        assert asyncio.run(scenario()).confirmed is False

    def test_submit_with_blank_name_keeps_screen_open(self) -> None:
        async def scenario() -> tuple[bool, str]:
            worktrees = _RecordingWorktrees()
            sentinel = object()
            captured: list[object] = [sentinel]

            def _capture(value: LaunchAgentResult | None) -> None:
                captured[0] = value

            app = _Harness()
            async with app.run_test(size=(140, 60)) as pilot:
                screen = LaunchAgentScreen(
                    cast(LaunchWorktreeController, worktrees),
                    intent=_intent(),
                    model_hint=_model_hint(),
                )
                await app.push_screen(screen, callback=_capture)
                await pilot.pause()
                app.screen.query_one("#launch-agent-name", Input).value = "   "
                await pilot.click("#btn-launch-agent")
                await pilot.pause()
                never_dismissed = captured[0] is sentinel
                status_text = str(app.screen.query_one("#launch-agent-status", Static).renderable)
            return never_dismissed, status_text

        never_dismissed, status_text = asyncio.run(scenario())
        assert never_dismissed is True
        assert "name is required" in status_text

    def test_submit_with_filled_inputs_returns_confirmed_result(self) -> None:
        async def scenario() -> LaunchAgentResult:
            worktrees = _RecordingWorktrees()
            captured: list[LaunchAgentResult | None] = [None]

            def _capture(value: LaunchAgentResult | None) -> None:
                captured[0] = value

            app = _Harness()
            async with app.run_test(size=(140, 60)) as pilot:
                screen = LaunchAgentScreen(
                    cast(LaunchWorktreeController, worktrees),
                    intent=_intent(),
                    model_hint=_model_hint(),
                )
                await app.push_screen(screen, callback=_capture)
                await pilot.pause()
                app.screen.query_one("#launch-agent-name", Input).value = "agent-name"
                app.screen.query_one("#launch-agent-session", Input).value = "ops"
                app.screen.query_one("#launch-agent-model", Input).value = "gpt-5.5"
                app.screen.query_one("#launch-agent-prompt", Input).value = "do work"
                await pilot.click("#btn-launch-agent")
                await pilot.pause()
            assert captured[0] is not None
            return captured[0]

        result = asyncio.run(scenario())
        assert result == LaunchAgentResult(
            confirmed=True,
            selected_worktree_id="worktree-1",
            target_session_name="ops",
            window_name="agent-name",
            prompt="do work",
            model="gpt-5.5",
        )

    def test_normalized_model_blank_falls_back_to_none(self) -> None:
        async def scenario() -> LaunchAgentResult:
            worktrees = _RecordingWorktrees()
            captured: list[LaunchAgentResult | None] = [None]

            def _capture(value: LaunchAgentResult | None) -> None:
                captured[0] = value

            app = _Harness()
            async with app.run_test(size=(140, 60)) as pilot:
                screen = LaunchAgentScreen(
                    cast(LaunchWorktreeController, worktrees),
                    intent=_intent(),
                    model_hint=_model_hint(),
                )
                await app.push_screen(screen, callback=_capture)
                await pilot.pause()
                # Clear the model field so empty string normalizes to None.
                app.screen.query_one("#launch-agent-model", Input).value = "  "
                await pilot.click("#btn-launch-agent")
                await pilot.pause()
            assert captured[0] is not None
            return captured[0]

        result = asyncio.run(scenario())
        assert result.confirmed is True
        assert result.model is None

    def test_create_worktree_action_success_updates_intent_and_status(self) -> None:
        async def scenario() -> tuple[str, list[tuple[str, str | None]], LaunchAgentResult]:
            detail = _detail("new-worktree")
            create_action = WorktreeActionView(
                action="create",
                message="created task/new-worktree",
                worktree=detail,
                conflicts=(),
            )
            worktrees = _RecordingWorktrees(create_action=create_action)
            captured: list[LaunchAgentResult | None] = [None]

            def _capture(value: LaunchAgentResult | None) -> None:
                captured[0] = value

            app = _Harness()
            async with app.run_test(size=(140, 60)) as pilot:
                screen = LaunchAgentScreen(
                    cast(LaunchWorktreeController, worktrees),
                    intent=_intent(),
                    model_hint=_model_hint(),
                )
                await app.push_screen(screen, callback=_capture)
                await pilot.pause()
                # Drive the create-worktree sub-flow by invoking the action method
                # directly and then dispatching a successful result.
                screen._on_create_worktree_result(
                    CreateWorktreeResult(repo_root="/repo", task_title="ship")
                )
                await pilot.pause()
                status_text = str(app.screen.query_one("#launch-agent-status", Static).renderable)
                # Submit via Launch button to capture the chosen worktree.
                await pilot.click("#btn-launch-agent")
                await pilot.pause()
            assert captured[0] is not None
            return status_text, worktrees.create_calls, captured[0]

        status_text, create_calls, result = asyncio.run(scenario())
        assert "✓" in status_text
        assert "created" in status_text
        assert create_calls == [("/repo", "ship")]
        # After refresh_intent, the worktree id should be the new worktree.
        assert result.selected_worktree_id == "new-worktree"

    def test_create_worktree_action_cancel_sets_status(self) -> None:
        async def scenario() -> str:
            worktrees = _RecordingWorktrees()
            app = _Harness()
            async with app.run_test(size=(140, 60)) as pilot:
                screen = LaunchAgentScreen(
                    cast(LaunchWorktreeController, worktrees),
                    intent=_intent(),
                    model_hint=_model_hint(),
                )
                await app.push_screen(screen)
                await pilot.pause()
                screen._on_create_worktree_result(None)
                await pilot.pause()
                return str(app.screen.query_one("#launch-agent-status", Static).renderable)

        assert "create cancelled" in asyncio.run(scenario())

    def test_create_worktree_action_domain_failure_sets_status(self) -> None:
        async def scenario() -> str:
            worktrees = _RecordingWorktrees(
                create_action=DomainValidationError("invalid title"),
            )
            app = _Harness()
            async with app.run_test(size=(140, 60)) as pilot:
                screen = LaunchAgentScreen(
                    cast(LaunchWorktreeController, worktrees),
                    intent=_intent(),
                    model_hint=_model_hint(),
                )
                await app.push_screen(screen)
                await pilot.pause()
                screen._on_create_worktree_result(
                    CreateWorktreeResult(repo_root="/repo", task_title="bad")
                )
                await pilot.pause()
                return str(app.screen.query_one("#launch-agent-status", Static).renderable)

        rendered = asyncio.run(scenario())
        assert "create failed" in rendered
        assert "invalid title" in rendered

    def test_create_worktree_action_returns_no_worktree(self) -> None:
        async def scenario() -> str:
            empty_action = WorktreeActionView(
                action="create",
                message="nothing happened",
                worktree=None,
                conflicts=(),
            )
            worktrees = _RecordingWorktrees(create_action=empty_action)
            app = _Harness()
            async with app.run_test(size=(140, 60)) as pilot:
                screen = LaunchAgentScreen(
                    cast(LaunchWorktreeController, worktrees),
                    intent=_intent(),
                    model_hint=_model_hint(),
                )
                await app.push_screen(screen)
                await pilot.pause()
                screen._on_create_worktree_result(
                    CreateWorktreeResult(repo_root="/repo", task_title="ship")
                )
                await pilot.pause()
                return str(app.screen.query_one("#launch-agent-status", Static).renderable)

        assert "no worktree returned" in asyncio.run(scenario())

    def test_attach_worktree_action_success_updates_intent(self) -> None:
        async def scenario() -> tuple[str, LaunchAgentResult]:
            detail = _detail("attached-worktree")
            attach_action = WorktreeActionView(
                action="attach",
                message="attached worktree",
                worktree=detail,
                conflicts=(),
            )
            worktrees = _RecordingWorktrees(attach_action=attach_action)
            captured: list[LaunchAgentResult | None] = [None]

            def _capture(value: LaunchAgentResult | None) -> None:
                captured[0] = value

            app = _Harness()
            async with app.run_test(size=(140, 60)) as pilot:
                screen = LaunchAgentScreen(
                    cast(LaunchWorktreeController, worktrees),
                    intent=_intent(),
                    model_hint=_model_hint(),
                )
                await app.push_screen(screen, callback=_capture)
                await pilot.pause()
                screen._on_attach_worktree_result(
                    AttachWorktreeResult(path="/repo/worktrees/attached")
                )
                await pilot.pause()
                status_text = str(app.screen.query_one("#launch-agent-status", Static).renderable)
                await pilot.click("#btn-launch-agent")
                await pilot.pause()
            assert captured[0] is not None
            return status_text, captured[0]

        status, result = asyncio.run(scenario())
        assert "✓" in status
        assert result.selected_worktree_id == "attached-worktree"

    def test_attach_worktree_action_cancel_sets_status(self) -> None:
        async def scenario() -> str:
            worktrees = _RecordingWorktrees()
            app = _Harness()
            async with app.run_test(size=(140, 60)) as pilot:
                screen = LaunchAgentScreen(
                    cast(LaunchWorktreeController, worktrees),
                    intent=_intent(),
                    model_hint=_model_hint(),
                )
                await app.push_screen(screen)
                await pilot.pause()
                screen._on_attach_worktree_result(None)
                await pilot.pause()
                return str(app.screen.query_one("#launch-agent-status", Static).renderable)

        assert "select existing cancelled" in asyncio.run(scenario())

    def test_attach_worktree_action_persistence_failure_sets_status(self) -> None:
        async def scenario() -> str:
            worktrees = _RecordingWorktrees(
                attach_action=PersistenceError("disk full"),
            )
            app = _Harness()
            async with app.run_test(size=(140, 60)) as pilot:
                screen = LaunchAgentScreen(
                    cast(LaunchWorktreeController, worktrees),
                    intent=_intent(),
                    model_hint=_model_hint(),
                )
                await app.push_screen(screen)
                await pilot.pause()
                screen._on_attach_worktree_result(AttachWorktreeResult(path="/missing"))
                await pilot.pause()
                return str(app.screen.query_one("#launch-agent-status", Static).renderable)

        rendered = asyncio.run(scenario())
        assert "attach failed" in rendered
        assert "disk full" in rendered

    def test_attach_worktree_action_returns_no_worktree(self) -> None:
        async def scenario() -> str:
            empty_action = WorktreeActionView(
                action="attach",
                message="empty",
                worktree=None,
                conflicts=(),
            )
            worktrees = _RecordingWorktrees(attach_action=empty_action)
            app = _Harness()
            async with app.run_test(size=(140, 60)) as pilot:
                screen = LaunchAgentScreen(
                    cast(LaunchWorktreeController, worktrees),
                    intent=_intent(),
                    model_hint=_model_hint(),
                )
                await app.push_screen(screen)
                await pilot.pause()
                screen._on_attach_worktree_result(AttachWorktreeResult(path="/repo/wt"))
                await pilot.pause()
                return str(app.screen.query_one("#launch-agent-status", Static).renderable)

        assert "no worktree returned" in asyncio.run(scenario())

    def test_create_button_pushes_create_screen_then_cancel_returns(self) -> None:
        async def scenario() -> str:
            worktrees = _RecordingWorktrees()
            app = _Harness()
            async with app.run_test(size=(140, 60)) as pilot:
                screen = LaunchAgentScreen(
                    cast(LaunchWorktreeController, worktrees),
                    intent=_intent(),
                    model_hint=_model_hint(),
                )
                await app.push_screen(screen)
                await pilot.pause()
                # Drive _on_create_button → action_create_worktree → push_screen.
                screen._on_create_button()
                await pilot.pause()
                # The new top screen is the CreateWorktreeScreen.
                assert isinstance(app.screen, CreateWorktreeScreen)
                # Cancel sub-screen returns to LaunchAgentScreen with "cancelled" status.
                await pilot.press("escape")
                await pilot.pause()
                return str(app.screen.query_one("#launch-agent-status", Static).renderable)

        assert "create cancelled" in asyncio.run(scenario())

    def test_attach_button_pushes_attach_screen_then_cancel_returns(self) -> None:
        async def scenario() -> str:
            worktrees = _RecordingWorktrees()
            app = _Harness()
            async with app.run_test(size=(140, 60)) as pilot:
                screen = LaunchAgentScreen(
                    cast(LaunchWorktreeController, worktrees),
                    intent=_intent(),
                    model_hint=_model_hint(),
                )
                await app.push_screen(screen)
                await pilot.pause()
                screen._on_attach_button()
                await pilot.pause()
                assert isinstance(app.screen, AttachWorktreeScreen)
                await pilot.press("escape")
                await pilot.pause()
                return str(app.screen.query_one("#launch-agent-status", Static).renderable)

        assert "select existing cancelled" in asyncio.run(scenario())

    def test_input_submitted_in_any_input_triggers_submit(self) -> None:
        async def scenario() -> LaunchAgentResult:
            worktrees = _RecordingWorktrees()
            captured: list[LaunchAgentResult | None] = [None]

            def _capture(value: LaunchAgentResult | None) -> None:
                captured[0] = value

            app = _Harness()
            async with app.run_test(size=(140, 60)) as pilot:
                screen = LaunchAgentScreen(
                    cast(LaunchWorktreeController, worktrees),
                    intent=_intent(),
                    model_hint=_model_hint(),
                )
                await app.push_screen(screen, callback=_capture)
                await pilot.pause()
                # Submit via Input.Submitted (Enter) on the prompt input,
                # exercising the catch-all `@on(Input.Submitted)` handler.
                prompt_input = app.screen.query_one("#launch-agent-prompt", Input)
                prompt_input.focus()
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
            assert captured[0] is not None
            return captured[0]

        result = asyncio.run(scenario())
        assert result.confirmed is True
