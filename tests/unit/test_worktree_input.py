"""Tests for worktree input modals."""

from __future__ import annotations

import dataclasses
import inspect
from typing import cast

from muxdeck.controllers import WorktreeStartAgentIntent
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
