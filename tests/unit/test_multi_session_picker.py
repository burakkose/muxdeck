"""Tests for the multi-session picker modal."""

from __future__ import annotations

import asyncio
import unittest
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from textual.app import App, ComposeResult
from textual.pilot import Pilot
from textual.widgets import Input

from muxdeck.screens.multi_session_picker import MultiSessionPickerScreen


class _Harness(App[None]):
    def compose(self) -> ComposeResult:
        return iter(())


@dataclass(frozen=True, slots=True)
class _DismissResult:
    dismissed: bool
    value: tuple[str, ...] | None


async def _run_with_action(
    action: Callable[[Pilot[None]], Awaitable[None]],
    *,
    prefill: str = "",
) -> _DismissResult:
    sentinel = object()
    captured: list[object] = [sentinel]

    def _capture(value: tuple[str, ...] | None) -> None:
        captured[0] = value

    app = _Harness()
    async with app.run_test() as pilot:
        screen = MultiSessionPickerScreen(prefill=prefill)
        await app.push_screen(screen, callback=_capture)
        await pilot.pause()
        await action(pilot)
        await pilot.pause()
    raw = captured[0]
    if raw is sentinel:
        return _DismissResult(dismissed=False, value=None)
    if raw is None:
        return _DismissResult(dismissed=True, value=None)
    assert isinstance(raw, tuple)
    return _DismissResult(dismissed=True, value=raw)


class MultiSessionPickerScreenTests(unittest.TestCase):
    def test_compose_mounts_dialog_widgets_and_focuses_input(self) -> None:
        async def scenario() -> tuple[bool, str]:
            app = _Harness()
            async with app.run_test() as pilot:
                screen = MultiSessionPickerScreen(prefill="abc, def")
                await app.push_screen(screen)
                await pilot.pause()
                input_widget = app.screen.query_one("#multi-input", Input)
                # Cancel and Merge buttons exist; query_one raises if missing.
                app.screen.query_one("#btn-cancel")
                app.screen.query_one("#btn-merge")
                return input_widget.has_focus, input_widget.value

        focused, prefill_value = asyncio.run(scenario())
        assert focused is True
        assert prefill_value == "abc, def"

    def test_default_prefill_is_blank(self) -> None:
        async def scenario() -> str:
            app = _Harness()
            async with app.run_test() as pilot:
                screen = MultiSessionPickerScreen()
                await app.push_screen(screen)
                await pilot.pause()
                return app.screen.query_one("#multi-input", Input).value

        assert asyncio.run(scenario()) == ""

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

    def test_submit_with_single_id_dismisses_with_tuple(self) -> None:
        async def action(pilot: Pilot[None]) -> None:
            pilot.app.screen.query_one("#multi-input", Input).value = "session-a"
            await pilot.press("enter")

        result = asyncio.run(_run_with_action(action))
        assert result.dismissed is True
        assert result.value == ("session-a",)

    def test_submit_trims_and_drops_empties(self) -> None:
        async def action(pilot: Pilot[None]) -> None:
            pilot.app.screen.query_one("#multi-input", Input).value = " s-1 , , s-2 ,, s-3 ,"
            await pilot.click("#btn-merge")

        result = asyncio.run(_run_with_action(action))
        assert result.dismissed is True
        assert result.value == ("s-1", "s-2", "s-3")

    def test_blank_input_keeps_screen_open(self) -> None:
        async def scenario() -> tuple[bool, bool, bool]:
            sentinel = object()
            captured: list[object] = [sentinel]

            def _capture(value: tuple[str, ...] | None) -> None:
                captured[0] = value

            app = _Harness()
            async with app.run_test() as pilot:
                screen = MultiSessionPickerScreen()
                await app.push_screen(screen, callback=_capture)
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                still_modal = isinstance(app.screen, MultiSessionPickerScreen)
                refocused = app.screen.query_one("#multi-input", Input).has_focus
                never_dismissed = captured[0] is sentinel
            return still_modal, refocused, never_dismissed

        still_modal, refocused, never_dismissed = asyncio.run(scenario())
        assert still_modal is True
        assert refocused is True
        assert never_dismissed is True

    def test_only_separators_keeps_screen_open(self) -> None:
        async def scenario() -> bool:
            sentinel = object()
            captured: list[object] = [sentinel]

            def _capture(value: tuple[str, ...] | None) -> None:
                captured[0] = value

            app = _Harness()
            async with app.run_test() as pilot:
                screen = MultiSessionPickerScreen()
                await app.push_screen(screen, callback=_capture)
                await pilot.pause()
                app.screen.query_one("#multi-input", Input).value = "   ,  ,, "
                await pilot.press("enter")
                await pilot.pause()
                return captured[0] is sentinel

        assert asyncio.run(scenario()) is True
