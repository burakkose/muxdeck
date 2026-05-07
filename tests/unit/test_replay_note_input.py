"""Tests for the replay-note input modal."""

from __future__ import annotations

import asyncio
import unittest
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from textual.app import App, ComposeResult
from textual.pilot import Pilot
from textual.widgets import Input

from muxdeck.screens.replay_note_input import ReplayNoteInputScreen


class _Harness(App[None]):
    def compose(self) -> ComposeResult:
        return iter(())


@dataclass(frozen=True, slots=True)
class _DismissResult:
    dismissed: bool
    value: str | None


async def _run_with_action(
    action: Callable[[Pilot[None]], Awaitable[None]],
    *,
    ordinal: int = 7,
    initial: str = "",
) -> _DismissResult:
    sentinel = object()
    captured: list[object] = [sentinel]

    def _capture(value: str | None) -> None:
        captured[0] = value

    app = _Harness()
    async with app.run_test() as pilot:
        screen = ReplayNoteInputScreen(ordinal, initial=initial)
        await app.push_screen(screen, callback=_capture)
        await pilot.pause()
        await action(pilot)
        await pilot.pause()
    raw = captured[0]
    if raw is sentinel:
        return _DismissResult(dismissed=False, value=None)
    if raw is None:
        return _DismissResult(dismissed=True, value=None)
    assert isinstance(raw, str)
    return _DismissResult(dismissed=True, value=raw)


class ReplayNoteInputScreenTests(unittest.TestCase):
    def test_compose_focuses_input_and_uses_initial(self) -> None:
        async def scenario() -> tuple[bool, str]:
            app = _Harness()
            async with app.run_test() as pilot:
                screen = ReplayNoteInputScreen(3, initial="seed text")
                await app.push_screen(screen)
                await pilot.pause()
                input_widget = app.screen.query_one("#note-input", Input)
                # Save and cancel buttons must be present.
                app.screen.query_one("#note-cancel")
                app.screen.query_one("#note-save")
                return input_widget.has_focus, input_widget.value

        focused, prefill = asyncio.run(scenario())
        assert focused is True
        assert prefill == "seed text"

    def test_default_initial_is_blank(self) -> None:
        async def scenario() -> str:
            app = _Harness()
            async with app.run_test() as pilot:
                screen = ReplayNoteInputScreen(1)
                await app.push_screen(screen)
                await pilot.pause()
                return app.screen.query_one("#note-input", Input).value

        assert asyncio.run(scenario()) == ""

    def test_escape_dismisses_with_none(self) -> None:
        async def action(pilot: Pilot[None]) -> None:
            await pilot.press("escape")

        result = asyncio.run(_run_with_action(action))
        assert result.dismissed is True
        assert result.value is None

    def test_cancel_button_dismisses_with_none(self) -> None:
        async def action(pilot: Pilot[None]) -> None:
            await pilot.click("#note-cancel")

        result = asyncio.run(_run_with_action(action))
        assert result.dismissed is True
        assert result.value is None

    def test_submit_with_text_dismisses_with_value(self) -> None:
        async def action(pilot: Pilot[None]) -> None:
            pilot.app.screen.query_one("#note-input", Input).value = "  important  "
            await pilot.press("enter")

        result = asyncio.run(_run_with_action(action))
        assert result.dismissed is True
        assert result.value == "important"

    def test_save_button_dismisses_with_value(self) -> None:
        async def action(pilot: Pilot[None]) -> None:
            pilot.app.screen.query_one("#note-input", Input).value = "save me"
            await pilot.click("#note-save")

        result = asyncio.run(_run_with_action(action))
        assert result.dismissed is True
        assert result.value == "save me"

    def test_blank_submit_dismisses_with_none(self) -> None:
        # Per ``_save``: a whitespace-only body dismisses with None (the user
        # essentially cleared the entry).
        async def action(pilot: Pilot[None]) -> None:
            pilot.app.screen.query_one("#note-input", Input).value = "   "
            await pilot.press("enter")

        result = asyncio.run(_run_with_action(action))
        assert result.dismissed is True
        assert result.value is None
