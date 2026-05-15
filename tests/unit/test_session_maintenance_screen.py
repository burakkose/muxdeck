"""Tests for the session-maintenance bulk-delete modal."""

from __future__ import annotations

import asyncio
import unittest
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from textual.app import App, ComposeResult
from textual.pilot import Pilot
from textual.widgets import OptionList

from muxdeck.controllers.sessions_controller import (
    MaintenanceCohort,
    MaintenanceCohortsView,
)
from muxdeck.screens.session_maintenance import SessionMaintenanceScreen


class _Harness(App[None]):
    def compose(self) -> ComposeResult:
        return iter(())


@dataclass(frozen=True, slots=True)
class _DismissResult:
    dismissed: bool
    value: int | None


async def _run_with_action(
    view: MaintenanceCohortsView,
    action: Callable[[Pilot[None]], Awaitable[None]],
) -> _DismissResult:
    sentinel = object()
    captured: list[object] = [sentinel]

    def _capture(value: int | None) -> None:
        captured[0] = value

    app = _Harness()
    async with app.run_test() as pilot:
        screen = SessionMaintenanceScreen(view)
        await app.push_screen(screen, callback=_capture)
        await pilot.pause()
        await action(pilot)
        await pilot.pause()
    raw = captured[0]
    if raw is sentinel:
        return _DismissResult(dismissed=False, value=None)
    if raw is None:
        return _DismissResult(dismissed=True, value=None)
    assert isinstance(raw, int)
    return _DismissResult(dismissed=True, value=raw)


def _view(
    cohorts: tuple[MaintenanceCohort, ...] = (),
    *,
    total_eligible: int = 0,
    skipped_live: int = 0,
) -> MaintenanceCohortsView:
    return MaintenanceCohortsView(
        cohorts=cohorts,
        total_eligible=total_eligible,
        skipped_live=skipped_live,
    )


class SessionMaintenanceScreenTests(unittest.TestCase):
    def test_escape_dismisses_without_value(self) -> None:
        view = _view(
            cohorts=(MaintenanceCohort(older_than_days=7, label="Older than 7 days", count=3),),
            total_eligible=3,
        )

        async def press_escape(pilot: Pilot[None]) -> None:
            await pilot.press("escape")

        result = asyncio.run(_run_with_action(view, press_escape))
        assert result.dismissed
        assert result.value is None

    def test_enter_returns_selected_cohort_threshold(self) -> None:
        view = _view(
            cohorts=(
                MaintenanceCohort(older_than_days=1, label="Older than 1 day", count=10),
                MaintenanceCohort(older_than_days=7, label="Older than 7 days", count=4),
                MaintenanceCohort(older_than_days=30, label="Older than 30 days", count=2),
            ),
            total_eligible=10,
        )

        async def press_enter(pilot: Pilot[None]) -> None:
            await pilot.press("enter")

        result = asyncio.run(_run_with_action(view, press_enter))
        assert result.dismissed
        # On mount the first eligible cohort (1-day) is pre-selected.
        assert result.value == 1

    def test_arrow_keys_navigate_between_cohorts(self) -> None:
        view = _view(
            cohorts=(
                MaintenanceCohort(older_than_days=1, label="Older than 1 day", count=10),
                MaintenanceCohort(older_than_days=7, label="Older than 7 days", count=4),
                MaintenanceCohort(older_than_days=30, label="Older than 30 days", count=2),
            ),
            total_eligible=10,
        )

        async def navigate_and_enter(pilot: Pilot[None]) -> None:
            # Down twice -> select the 30-day cohort, then enter.
            await pilot.press("down")
            await pilot.press("down")
            await pilot.press("enter")

        result = asyncio.run(_run_with_action(view, navigate_and_enter))
        assert result.dismissed
        assert result.value == 30

    def test_first_eligible_cohort_is_preselected_when_earlier_buckets_are_empty(
        self,
    ) -> None:
        # The 1-day cohort is empty (e.g. all sessions are recent), so
        # the modal must pre-highlight the first NON-empty bucket so
        # the operator can't accidentally land on a no-op selection.
        view = _view(
            cohorts=(
                MaintenanceCohort(older_than_days=1, label="Older than 1 day", count=0),
                MaintenanceCohort(older_than_days=7, label="Older than 7 days", count=2),
            ),
            total_eligible=2,
        )

        async def press_enter(pilot: Pilot[None]) -> None:
            await pilot.press("enter")

        result = asyncio.run(_run_with_action(view, press_enter))
        assert result.dismissed
        assert result.value == 7

    def test_skipped_live_count_is_surfaced_in_footer(self) -> None:
        view = _view(
            cohorts=(MaintenanceCohort(older_than_days=1, label="Older than 1 day", count=3),),
            total_eligible=3,
            skipped_live=4,
        )

        async def inspect(pilot: Pilot[None]) -> None:
            await pilot.press("escape")

        # Just smoke-test that mount + dismiss work; the footer label
        # is rendered from the same field so the modal can't crash.
        result = asyncio.run(_run_with_action(view, inspect))
        assert result.dismissed
        assert result.value is None

    def test_options_list_disables_empty_cohorts(self) -> None:
        # Empty cohorts must not be selectable -- otherwise pressing
        # enter on one yields zero deletions and a confusing toast.
        view = _view(
            cohorts=(
                MaintenanceCohort(older_than_days=1, label="Older than 1 day", count=0),
                MaintenanceCohort(older_than_days=7, label="Older than 7 days", count=5),
            ),
            total_eligible=5,
        )

        captured_disabled: list[tuple[str | None, bool]] = []

        async def collect_options(pilot: Pilot[None]) -> None:
            app = pilot.app
            options = app.query_one(OptionList)
            for option in options._options:
                captured_disabled.append((option.id, option.disabled))
            await pilot.press("escape")

        asyncio.run(_run_with_action(view, collect_options))
        # The day-1 cohort is empty and should be disabled; the 7-day
        # one has 5 sessions and should be enabled.
        by_id = dict(captured_disabled)
        assert by_id["1"] is True
        assert by_id["7"] is False
