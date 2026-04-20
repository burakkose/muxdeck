"""Reactive busy indicator surfaced through the footer."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from textual.worker import WorkerState

from muxdeck.screens.base import ShellScreen


def _make_event(state: WorkerState) -> SimpleNamespace:
    return SimpleNamespace(worker=SimpleNamespace(state=state))


def test_active_worker_counter_increments_and_decrements() -> None:
    screen = ShellScreen.__new__(ShellScreen)
    screen._active_workers = 0
    screen._sync_busy_indicator = MagicMock()  # type: ignore[method-assign]

    ShellScreen.on_worker_state_changed(screen, _make_event(WorkerState.RUNNING))
    assert screen._active_workers == 1

    ShellScreen.on_worker_state_changed(screen, _make_event(WorkerState.RUNNING))
    assert screen._active_workers == 2

    ShellScreen.on_worker_state_changed(screen, _make_event(WorkerState.SUCCESS))
    assert screen._active_workers == 1

    ShellScreen.on_worker_state_changed(screen, _make_event(WorkerState.ERROR))
    assert screen._active_workers == 0

    # Counter never drops below zero even on spurious terminal events.
    ShellScreen.on_worker_state_changed(screen, _make_event(WorkerState.CANCELLED))
    assert screen._active_workers == 0

    # Sync indicator is called for each tracked transition.
    assert screen._sync_busy_indicator.call_count == 5


def test_pending_state_does_not_change_counter() -> None:
    screen = ShellScreen.__new__(ShellScreen)
    screen._active_workers = 0
    screen._sync_busy_indicator = MagicMock()  # type: ignore[method-assign]

    ShellScreen.on_worker_state_changed(screen, _make_event(WorkerState.PENDING))
    assert screen._active_workers == 0
    screen._sync_busy_indicator.assert_not_called()
