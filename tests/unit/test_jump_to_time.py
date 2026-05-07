"""Tests for the jump-to-time replay clock modal and its parser."""

from __future__ import annotations

import asyncio
import unittest
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from textual.app import App, ComposeResult
from textual.pilot import Pilot
from textual.widgets import Input

from muxdeck.screens.jump_to_time import JumpToTimeScreen, parse_time_input

_START = datetime(2025, 6, 1, 9, 0, 0, tzinfo=UTC)
_END = _START + timedelta(hours=8)
_CLOCK = _START + timedelta(hours=2)


class _Harness(App[None]):
    def compose(self) -> ComposeResult:
        return iter(())


@dataclass(frozen=True, slots=True)
class _DismissResult:
    dismissed: bool
    value: datetime | None


async def _run_with_action(
    action: Callable[[Pilot[None]], Awaitable[None]],
) -> _DismissResult:
    sentinel = object()
    captured: list[object] = [sentinel]

    def _capture(value: datetime | None) -> None:
        captured[0] = value

    app = _Harness()
    async with app.run_test() as pilot:
        screen = JumpToTimeScreen(clock=_CLOCK, start=_START, end=_END)
        await app.push_screen(screen, callback=_capture)
        await pilot.pause()
        await action(pilot)
        await pilot.pause()
    raw = captured[0]
    if raw is sentinel:
        return _DismissResult(dismissed=False, value=None)
    if raw is None:
        return _DismissResult(dismissed=True, value=None)
    assert isinstance(raw, datetime)
    return _DismissResult(dismissed=True, value=raw)


class ParseTimeInputTests(unittest.TestCase):
    """Pure-function coverage for :func:`parse_time_input`."""

    def test_empty_string_returns_none(self) -> None:
        assert parse_time_input("", clock=_CLOCK, start=_START, end=_END) is None

    def test_whitespace_only_returns_none(self) -> None:
        assert parse_time_input("   \t  ", clock=_CLOCK, start=_START, end=_END) is None

    def test_strips_leading_and_trailing_whitespace_for_absolute(self) -> None:
        result = parse_time_input("  10:30  ", clock=_CLOCK, start=_START, end=_END)
        assert result == _START.replace(hour=10, minute=30, second=0, microsecond=0)

    def test_absolute_hh_mm(self) -> None:
        result = parse_time_input("12:05", clock=_CLOCK, start=_START, end=_END)
        assert result == _START.replace(hour=12, minute=5, second=0, microsecond=0)

    def test_absolute_hh_mm_ss(self) -> None:
        result = parse_time_input("12:05:42", clock=_CLOCK, start=_START, end=_END)
        assert result == _START.replace(hour=12, minute=5, second=42, microsecond=0)

    def test_absolute_single_digit_hour(self) -> None:
        result = parse_time_input("9:00", clock=_CLOCK, start=_START, end=_END)
        assert result == _START.replace(hour=9, minute=0, second=0, microsecond=0)

    def test_absolute_midnight_zeroes(self) -> None:
        result = parse_time_input("00:00:00", clock=_CLOCK, start=_START, end=_END)
        assert result == _START.replace(hour=0, minute=0, second=0, microsecond=0)

    def test_absolute_max_valid_components(self) -> None:
        result = parse_time_input("23:59:59", clock=_CLOCK, start=_START, end=_END)
        assert result == _START.replace(hour=23, minute=59, second=59, microsecond=0)

    def test_absolute_hour_out_of_range_returns_none(self) -> None:
        assert parse_time_input("24:00", clock=_CLOCK, start=_START, end=_END) is None

    def test_absolute_minute_out_of_range_returns_none(self) -> None:
        assert parse_time_input("12:60", clock=_CLOCK, start=_START, end=_END) is None

    def test_absolute_seconds_out_of_range_returns_none(self) -> None:
        assert parse_time_input("12:00:60", clock=_CLOCK, start=_START, end=_END) is None

    def test_delta_positive_seconds(self) -> None:
        result = parse_time_input("+30s", clock=_CLOCK, start=_START, end=_END)
        assert result == _CLOCK + timedelta(seconds=30)

    def test_delta_negative_seconds(self) -> None:
        result = parse_time_input("-30s", clock=_CLOCK, start=_START, end=_END)
        assert result == _CLOCK - timedelta(seconds=30)

    def test_delta_positive_minutes(self) -> None:
        result = parse_time_input("+5m", clock=_CLOCK, start=_START, end=_END)
        assert result == _CLOCK + timedelta(minutes=5)

    def test_delta_negative_minutes(self) -> None:
        result = parse_time_input("-15m", clock=_CLOCK, start=_START, end=_END)
        assert result == _CLOCK - timedelta(minutes=15)

    def test_delta_positive_hours(self) -> None:
        result = parse_time_input("+2h", clock=_CLOCK, start=_START, end=_END)
        assert result == _CLOCK + timedelta(hours=2)

    def test_delta_negative_hours(self) -> None:
        result = parse_time_input("-1h", clock=_CLOCK, start=_START, end=_END)
        assert result == _CLOCK - timedelta(hours=1)

    def test_delta_strips_whitespace(self) -> None:
        result = parse_time_input("  +30s  ", clock=_CLOCK, start=_START, end=_END)
        assert result == _CLOCK + timedelta(seconds=30)

    def test_delta_zero_amount(self) -> None:
        result = parse_time_input("+0s", clock=_CLOCK, start=_START, end=_END)
        assert result == _CLOCK

    def test_delta_unknown_unit_returns_none(self) -> None:
        assert parse_time_input("+5d", clock=_CLOCK, start=_START, end=_END) is None

    def test_delta_missing_sign_returns_none(self) -> None:
        # "30s" matches neither the delta regex (needs sign) nor the HMS regex.
        assert parse_time_input("30s", clock=_CLOCK, start=_START, end=_END) is None

    def test_garbage_input_returns_none(self) -> None:
        assert parse_time_input("not-a-time", clock=_CLOCK, start=_START, end=_END) is None

    def test_partial_hms_returns_none(self) -> None:
        assert parse_time_input("12", clock=_CLOCK, start=_START, end=_END) is None

    def test_hms_with_extra_components_returns_none(self) -> None:
        assert parse_time_input("12:00:00:00", clock=_CLOCK, start=_START, end=_END) is None


class JumpToTimeScreenTests(unittest.TestCase):
    """Behavioural tests for the jump-to-time modal screen."""

    def test_compose_mounts_dialog_widgets_and_focuses_input(self) -> None:
        async def scenario() -> bool:
            app = _Harness()
            async with app.run_test() as pilot:
                screen = JumpToTimeScreen(clock=_CLOCK, start=_START, end=_END)
                await app.push_screen(screen)
                await pilot.pause()
                input_widget = app.screen.query_one("#jump-input", Input)
                # Both buttons must exist; query_one raises if missing.
                app.screen.query_one("#btn-cancel")
                app.screen.query_one("#btn-jump")
                return input_widget.has_focus

        assert asyncio.run(scenario()) is True

    def test_escape_dismisses_with_none(self) -> None:
        async def action(pilot: Pilot[None]) -> None:
            await pilot.press("escape")

        result = asyncio.run(_run_with_action(action))
        assert result.dismissed is True
        assert result.value is None

    def test_cancel_button_dismisses_with_none(self) -> None:
        async def action(pilot: Pilot[None]) -> None:
            await pilot.click("#btn-cancel")

        result = asyncio.run(_run_with_action(action))
        assert result.dismissed is True
        assert result.value is None

    def test_submit_with_valid_absolute_time_dismisses(self) -> None:
        async def action(pilot: Pilot[None]) -> None:
            pilot.app.screen.query_one("#jump-input", Input).value = "10:30:15"
            await pilot.press("enter")

        result = asyncio.run(_run_with_action(action))
        assert result.dismissed is True
        assert result.value == _START.replace(hour=10, minute=30, second=15, microsecond=0)

    def test_submit_with_delta_dismisses(self) -> None:
        async def action(pilot: Pilot[None]) -> None:
            pilot.app.screen.query_one("#jump-input", Input).value = "+45m"
            await pilot.click("#btn-jump")

        result = asyncio.run(_run_with_action(action))
        assert result.dismissed is True
        assert result.value == _CLOCK + timedelta(minutes=45)

    def test_submit_with_invalid_input_keeps_screen_open(self) -> None:
        async def scenario() -> tuple[bool, bool, bool]:
            sentinel = object()
            captured: list[object] = [sentinel]

            def _capture(value: datetime | None) -> None:
                captured[0] = value

            app = _Harness()
            async with app.run_test() as pilot:
                screen = JumpToTimeScreen(clock=_CLOCK, start=_START, end=_END)
                await app.push_screen(screen, callback=_capture)
                await pilot.pause()
                input_widget = app.screen.query_one("#jump-input", Input)
                input_widget.value = "totally not a time"
                await pilot.press("enter")
                await pilot.pause()
                still_modal = isinstance(app.screen, JumpToTimeScreen)
                refocused = app.screen.query_one("#jump-input", Input).has_focus
                never_dismissed = captured[0] is sentinel
            return still_modal, refocused, never_dismissed

        still_modal, refocused, never_dismissed = asyncio.run(scenario())
        assert still_modal is True
        assert refocused is True
        assert never_dismissed is True
