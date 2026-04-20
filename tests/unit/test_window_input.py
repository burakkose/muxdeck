"""Tests for the move-window modal."""

from __future__ import annotations

import asyncio
import unittest

from textual.app import App, ComposeResult
from textual.widgets import Input

from copilot_commander.screens.window_input import MoveWindowResult, MoveWindowScreen
from copilot_commander.services.action_service import WindowChoice


class _Harness(App[None]):
    def compose(self) -> ComposeResult:
        return iter(())


class MoveWindowScreenTests(unittest.TestCase):
    def test_arrow_keys_select_existing_window(self) -> None:
        async def scenario() -> MoveWindowResult | None:
            result: MoveWindowResult | None = None

            def _capture(value: MoveWindowResult | None) -> None:
                nonlocal result
                result = value

            app = _Harness()
            async with app.run_test() as pilot:
                screen = MoveWindowScreen(
                    "Planner",
                    current_window_name="editor",
                    choices=(
                        WindowChoice(
                            session_name="muxdeck",
                            window_id="@2",
                            window_name="editor",
                            pane_count=1,
                        ),
                        WindowChoice(
                            session_name="muxdeck",
                            window_id="@3",
                            window_name="review",
                            pane_count=2,
                        ),
                    ),
                )
                await app.push_screen(screen, callback=_capture)
                await pilot.pause()

                input_widget = app.screen.query_one("#window-input-value", Input)
                assert input_widget.has_focus is True

                await pilot.press("down")
                await pilot.pause()
                assert input_widget.value == "muxdeck:review"

                await pilot.press("enter")
                await pilot.pause()

            return result

        result = asyncio.run(scenario())

        assert result == MoveWindowResult(target_window="@3", new_window_name=None)

    def test_blank_enter_moves_to_selected_window(self) -> None:
        async def scenario() -> MoveWindowResult | None:
            result: MoveWindowResult | None = None

            def _capture(value: MoveWindowResult | None) -> None:
                nonlocal result
                result = value

            app = _Harness()
            async with app.run_test() as pilot:
                screen = MoveWindowScreen(
                    "Planner",
                    choices=(
                        WindowChoice(
                            session_name="muxdeck",
                            window_id="@3",
                            window_name="review",
                            pane_count=2,
                        ),
                    ),
                )
                await app.push_screen(screen, callback=_capture)
                await pilot.pause()

                await pilot.press("enter")
                await pilot.pause()

            return result

        result = asyncio.run(scenario())

        assert result == MoveWindowResult(target_window="@3", new_window_name=None)

    def test_typed_name_creates_new_window(self) -> None:
        async def scenario() -> MoveWindowResult | None:
            result: MoveWindowResult | None = None

            def _capture(value: MoveWindowResult | None) -> None:
                nonlocal result
                result = value

            app = _Harness()
            async with app.run_test() as pilot:
                screen = MoveWindowScreen(
                    "Planner",
                    choices=(
                        WindowChoice(
                            session_name="muxdeck",
                            window_id="@2",
                            window_name="editor",
                            pane_count=1,
                        ),
                    ),
                )
                await app.push_screen(screen, callback=_capture)
                await pilot.pause()

                input_widget = app.screen.query_one("#window-input-value", Input)
                input_widget.value = "ops"
                await pilot.press("enter")
                await pilot.pause()

            return result

        result = asyncio.run(scenario())

        assert result == MoveWindowResult(target_window=None, new_window_name="ops")
