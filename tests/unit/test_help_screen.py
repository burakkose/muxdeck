"""Tests for the HelpScreen behaviour."""

from __future__ import annotations

import asyncio
import unittest
from collections.abc import Callable
from typing import ClassVar, cast

from textual.app import App, ComposeResult
from textual.screen import Screen
from textual.widgets import Input, Static

from muxdeck.app import MuxdeckRuntime
from muxdeck.screens.help import HelpScreen
from muxdeck.widgets.common import KeyHintFooter


class _Harness(App[None]):
    MODES: ClassVar[dict[str, str | Callable[[], Screen[object]]]] = {
        "help": "help",
    }

    def __init__(self, runtime: MuxdeckRuntime) -> None:
        super().__init__()
        self._runtime = runtime
        self._switched_to: str | None = None

    def compose(self) -> ComposeResult:
        return iter(())

    def switch_mode(self, mode: str) -> object:  # type: ignore[override]
        # Override to record without dispatching to a real mode stack.
        self._switched_to = mode
        return None


def _runtime() -> MuxdeckRuntime:
    return cast(
        MuxdeckRuntime,
        type("_FakeRuntime", (), {})(),
    )


class HelpScreenTests(unittest.TestCase):
    def test_compose_renders_default_help_content(self) -> None:
        async def scenario() -> tuple[str, str]:
            runtime = _runtime()
            app = _Harness(runtime)
            async with app.run_test(size=(140, 60)) as pilot:
                screen = HelpScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                # Force a refresh so help-content has populated text.
                screen.refresh_data()
                await pilot.pause()
                content = str(app.screen.query_one("#help-content", Static).renderable)
                status = app.screen.query_one(KeyHintFooter).status
            return content, status

        content, status = asyncio.run(scenario())
        assert "Muxdeck" in content
        # When no filter text is set, default global hints render.
        assert "Global" in content
        assert status == "operator reference"

    def test_filter_input_narrows_help_content(self) -> None:
        async def scenario() -> tuple[str, str]:
            runtime = _runtime()
            app = _Harness(runtime)
            async with app.run_test(size=(140, 60)) as pilot:
                screen = HelpScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                # Type into the filter input so on_input_changed fires
                # _filter_text update + refresh_data with a query.
                filter_input = app.screen.query_one("#help-filter-input", Input)
                filter_input.value = "filter"
                await pilot.pause()
                content = str(app.screen.query_one("#help-content", Static).renderable)
                status = app.screen.query_one(KeyHintFooter).status
            return content, status

        content, status = asyncio.run(scenario())
        # The status describes the search outcome with a count.
        assert "help search" in status
        # And content should not include the "no help matches" hint when there
        # is at least one match for "filter".
        assert "filter" in content.lower()

    def test_filter_with_no_matches_renders_empty_state(self) -> None:
        async def scenario() -> tuple[str, str]:
            runtime = _runtime()
            app = _Harness(runtime)
            async with app.run_test(size=(140, 60)) as pilot:
                screen = HelpScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                filter_input = app.screen.query_one("#help-filter-input", Input)
                filter_input.value = "xyzzy-no-such-binding"
                await pilot.pause()
                content = str(app.screen.query_one("#help-content", Static).renderable)
                status = app.screen.query_one(KeyHintFooter).status
            return content, status

        content, status = asyncio.run(scenario())
        assert "No help matches" in content
        assert "0 matches" in status

    def test_action_focus_filter_focuses_input_and_updates_status(self) -> None:
        async def scenario() -> tuple[bool, str]:
            runtime = _runtime()
            app = _Harness(runtime)
            async with app.run_test(size=(140, 60)) as pilot:
                screen = HelpScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.action_focus_filter()
                await pilot.pause()
                focused = app.screen.query_one("#help-filter-input", Input).has_focus
                status = app.screen.query_one(KeyHintFooter).status
            return focused, status

        focused, status = asyncio.run(scenario())
        assert focused is True
        assert status == "search help"

    def test_escape_filter_blurs_focused_input(self) -> None:
        async def scenario() -> tuple[bool, str]:
            runtime = _runtime()
            app = _Harness(runtime)
            async with app.run_test(size=(140, 60)) as pilot:
                screen = HelpScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                # Focus the input first, then escape.
                screen.action_focus_filter()
                await pilot.pause()
                screen.action_escape_filter()
                await pilot.pause()
                still_focused = app.screen.query_one("#help-filter-input", Input).has_focus
                status = app.screen.query_one(KeyHintFooter).status
            return still_focused, status

        still_focused, status = asyncio.run(scenario())
        assert still_focused is False
        assert status == "operator reference"

    def test_escape_filter_clears_filter_text(self) -> None:
        async def scenario() -> tuple[str, str]:
            runtime = _runtime()
            app = _Harness(runtime)
            async with app.run_test(size=(140, 60)) as pilot:
                screen = HelpScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                # Set a filter, then move focus elsewhere, then escape.
                filter_input = app.screen.query_one("#help-filter-input", Input)
                filter_input.value = "filter"
                await pilot.pause()
                # Drop focus so escape takes the "clear filter" branch.
                screen.set_focus(None)
                await pilot.pause()
                screen.action_escape_filter()
                await pilot.pause()
                value_after = app.screen.query_one("#help-filter-input", Input).value
                status = app.screen.query_one(KeyHintFooter).status
            return value_after, status

        value_after, status = asyncio.run(scenario())
        assert value_after == ""
        assert status == "operator reference"

    def test_escape_with_no_filter_and_no_focus_switches_to_dashboard(self) -> None:
        async def scenario() -> str | None:
            runtime = _runtime()
            app = _Harness(runtime)
            async with app.run_test(size=(140, 60)) as pilot:
                screen = HelpScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.set_focus(None)
                await pilot.pause()
                screen.action_escape_filter()
                await pilot.pause()
            return app._switched_to

        assert asyncio.run(scenario()) == "dashboard"

    def test_apply_ui_preferences_returns_true(self) -> None:
        async def scenario() -> bool:
            runtime = _runtime()
            app = _Harness(runtime)
            async with app.run_test(size=(140, 60)) as pilot:
                screen = HelpScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                # Touching apply_ui_preferences after mount triggers refresh
                # and returns True.
                return screen.apply_ui_preferences()

        assert asyncio.run(scenario()) is True

    def test_on_input_changed_ignores_other_inputs(self) -> None:
        # Input.Changed events from any input that isn't ``#help-filter-input``
        # must be ignored — _filter_text stays unchanged.
        async def scenario() -> str:
            runtime = _runtime()
            app = _Harness(runtime)
            async with app.run_test(size=(140, 60)) as pilot:
                screen = HelpScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen._filter_text = "preserved"
                # Build a synthetic Input.Changed event from a foreign input.
                foreign = Input(id="other-input")
                event = Input.Changed(input=foreign, value="ignored")
                screen.on_input_changed(event)
                await pilot.pause()
                return screen._filter_text

        assert asyncio.run(scenario()) == "preserved"
