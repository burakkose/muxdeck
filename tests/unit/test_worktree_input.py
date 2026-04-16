"""Tests for worktree input modals."""

from __future__ import annotations

import dataclasses
import inspect

from copilot_commander.screens.worktree_input import (
    AttachWorktreeResult,
    AttachWorktreeScreen,
    CreateWorktreeResult,
    CreateWorktreeScreen,
)


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
