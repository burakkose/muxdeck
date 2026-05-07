"""Tests for the move-window and rename-window modals."""

from __future__ import annotations

import asyncio
import unittest

from textual.app import App, ComposeResult
from textual.widgets import Button, Input, Static

from muxdeck.screens.window_input import (
    MoveWindowResult,
    MoveWindowScreen,
    RenameWindowResult,
    RenameWindowScreen,
)
from muxdeck.services.action_service import WindowChoice


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


class RenameWindowScreenTests(unittest.TestCase):
    def test_compose_focuses_input_and_prefills_current_name(self) -> None:
        async def scenario() -> tuple[bool, str]:
            app = _Harness()
            async with app.run_test() as pilot:
                screen = RenameWindowScreen("Planner", current_name="editor")
                await app.push_screen(screen)
                await pilot.pause()
                input_widget = app.screen.query_one("#window-input-value", Input)
                # Cancel and confirm buttons exist; query_one raises if missing.
                app.screen.query_one("#btn-window-cancel", Button)
                app.screen.query_one("#btn-window-confirm", Button)
                return input_widget.has_focus, input_widget.value

        focused, prefill = asyncio.run(scenario())
        assert focused is True
        assert prefill == "editor"

    def test_blank_current_name_uses_empty_default(self) -> None:
        async def scenario() -> str:
            app = _Harness()
            async with app.run_test() as pilot:
                screen = RenameWindowScreen("Planner")
                await app.push_screen(screen)
                await pilot.pause()
                return app.screen.query_one("#window-input-value", Input).value

        assert asyncio.run(scenario()) == ""

    def test_escape_dismisses_with_none(self) -> None:
        async def scenario() -> RenameWindowResult | None:
            sentinel = object()
            captured: list[object] = [sentinel]

            def _capture(value: RenameWindowResult | None) -> None:
                captured[0] = value

            app = _Harness()
            async with app.run_test() as pilot:
                screen = RenameWindowScreen("Planner", current_name="editor")
                await app.push_screen(screen, callback=_capture)
                await pilot.pause()
                await pilot.press("escape")
                await pilot.pause()
            raw = captured[0]
            assert raw is None or isinstance(raw, RenameWindowResult)
            return raw if isinstance(raw, RenameWindowResult) else None

        assert asyncio.run(scenario()) is None

    def test_cancel_button_dismisses_with_none(self) -> None:
        async def scenario() -> RenameWindowResult | None:
            sentinel = object()
            captured: list[object] = [sentinel]

            def _capture(value: RenameWindowResult | None) -> None:
                captured[0] = value

            app = _Harness()
            async with app.run_test() as pilot:
                screen = RenameWindowScreen("Planner", current_name="editor")
                await app.push_screen(screen, callback=_capture)
                await pilot.pause()
                await pilot.click("#btn-window-cancel")
                await pilot.pause()
            raw = captured[0]
            return raw if isinstance(raw, RenameWindowResult) else None

        assert asyncio.run(scenario()) is None

    def test_submit_with_text_dismisses_with_result(self) -> None:
        async def scenario() -> RenameWindowResult | None:
            sentinel = object()
            captured: list[object] = [sentinel]

            def _capture(value: RenameWindowResult | None) -> None:
                captured[0] = value

            app = _Harness()
            async with app.run_test() as pilot:
                screen = RenameWindowScreen("Planner", current_name="editor")
                await app.push_screen(screen, callback=_capture)
                await pilot.pause()
                input_widget = app.screen.query_one("#window-input-value", Input)
                input_widget.value = "  ops-room  "
                await pilot.press("enter")
                await pilot.pause()
            raw = captured[0]
            return raw if isinstance(raw, RenameWindowResult) else None

        assert asyncio.run(scenario()) == RenameWindowResult(name="ops-room")

    def test_confirm_button_submits(self) -> None:
        async def scenario() -> RenameWindowResult | None:
            sentinel = object()
            captured: list[object] = [sentinel]

            def _capture(value: RenameWindowResult | None) -> None:
                captured[0] = value

            app = _Harness()
            async with app.run_test() as pilot:
                screen = RenameWindowScreen("Planner")
                await app.push_screen(screen, callback=_capture)
                await pilot.pause()
                app.screen.query_one("#window-input-value", Input).value = "fresh"
                await pilot.click("#btn-window-confirm")
                await pilot.pause()
            raw = captured[0]
            return raw if isinstance(raw, RenameWindowResult) else None

        assert asyncio.run(scenario()) == RenameWindowResult(name="fresh")

    def test_blank_submit_keeps_screen_open_and_shows_status(self) -> None:
        async def scenario() -> tuple[bool, bool, bool, str]:
            sentinel = object()
            captured: list[object] = [sentinel]

            def _capture(value: RenameWindowResult | None) -> None:
                captured[0] = value

            app = _Harness()
            async with app.run_test() as pilot:
                screen = RenameWindowScreen("Planner")
                await app.push_screen(screen, callback=_capture)
                await pilot.pause()
                # Submit with whitespace-only value
                app.screen.query_one("#window-input-value", Input).value = "   "
                await pilot.press("enter")
                await pilot.pause()
                still_modal = isinstance(app.screen, RenameWindowScreen)
                refocused = app.screen.query_one("#window-input-value", Input).has_focus
                never_dismissed = captured[0] is sentinel
                status_widget = app.screen.query_one("#window-input-status", Static)
                status_text = str(status_widget.renderable)
            return still_modal, refocused, never_dismissed, status_text

        still_modal, refocused, never_dismissed, status_text = asyncio.run(scenario())
        assert still_modal is True
        assert refocused is True
        assert never_dismissed is True
        assert "required" in status_text


class MoveWindowScreenAdditionalTests(unittest.TestCase):
    def test_compose_with_no_choices_renders_empty_state(self) -> None:
        async def scenario() -> str:
            app = _Harness()
            async with app.run_test() as pilot:
                screen = MoveWindowScreen("Planner")
                await app.push_screen(screen)
                await pilot.pause()
                static = app.screen.query_one("#window-choice-list", Static)
                return str(static.renderable)

        rendered = asyncio.run(scenario())
        assert "No other windows" in rendered

    def test_blank_submit_with_no_choices_shows_status(self) -> None:
        async def scenario() -> tuple[bool, str]:
            sentinel = object()
            captured: list[object] = [sentinel]

            def _capture(value: MoveWindowResult | None) -> None:
                captured[0] = value

            app = _Harness()
            async with app.run_test() as pilot:
                screen = MoveWindowScreen("Planner")
                await app.push_screen(screen, callback=_capture)
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                never_dismissed = captured[0] is sentinel
                status_text = str(app.screen.query_one("#window-input-status", Static).renderable)
            return never_dismissed, status_text

        never_dismissed, status_text = asyncio.run(scenario())
        assert never_dismissed is True
        assert "required" in status_text

    def test_cancel_button_dismisses_with_none(self) -> None:
        async def scenario() -> MoveWindowResult | None:
            sentinel = object()
            captured: list[object] = [sentinel]

            def _capture(value: MoveWindowResult | None) -> None:
                captured[0] = value

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
                await pilot.click("#btn-window-cancel")
                await pilot.pause()
            raw = captured[0]
            return raw if isinstance(raw, MoveWindowResult) else None

        assert asyncio.run(scenario()) is None

    def test_escape_dismisses_with_none(self) -> None:
        async def scenario() -> MoveWindowResult | None:
            sentinel = object()
            captured: list[object] = [sentinel]

            def _capture(value: MoveWindowResult | None) -> None:
                captured[0] = value

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
                await pilot.press("escape")
                await pilot.pause()
            raw = captured[0]
            return raw if isinstance(raw, MoveWindowResult) else None

        assert asyncio.run(scenario()) is None

    def test_arrow_up_wraps_to_last_choice(self) -> None:
        async def scenario() -> str:
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
                        WindowChoice(
                            session_name="muxdeck",
                            window_id="@3",
                            window_name="review",
                            pane_count=2,
                        ),
                        WindowChoice(
                            session_name="muxdeck",
                            window_id="@4",
                            window_name="ops",
                            pane_count=3,
                        ),
                    ),
                )
                await app.push_screen(screen)
                await pilot.pause()
                await pilot.press("up")
                await pilot.pause()
                return app.screen.query_one("#window-input-value", Input).value

        # Pressing up from index 0 wraps to last entry.
        assert asyncio.run(scenario()) == "muxdeck:ops"

    def test_typed_existing_window_id_resolves_to_target(self) -> None:
        async def scenario() -> MoveWindowResult | None:
            sentinel = object()
            captured: list[object] = [sentinel]

            def _capture(value: MoveWindowResult | None) -> None:
                captured[0] = value

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
                # Type the bare window name "review" — should match the second
                # choice and dismiss with target_window=@3.
                input_widget = app.screen.query_one("#window-input-value", Input)
                input_widget.value = "review"
                await pilot.press("enter")
                await pilot.pause()
            raw = captured[0]
            return raw if isinstance(raw, MoveWindowResult) else None

        assert asyncio.run(scenario()) == MoveWindowResult(
            target_window="@3",
            new_window_name=None,
        )

    def test_status_label_reflects_current_text(self) -> None:
        async def scenario() -> tuple[str, str]:
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
                await app.push_screen(screen)
                await pilot.pause()
                # Empty input shows "selected ..." status because @2 is selected.
                empty_status = str(app.screen.query_one("#window-input-status", Static).renderable)
                # Typing a new (non-matching) value flips status to "create".
                input_widget = app.screen.query_one("#window-input-value", Input)
                input_widget.value = "brand-new-window"
                await pilot.pause()
                new_status = str(app.screen.query_one("#window-input-status", Static).renderable)
            return empty_status, new_status

        empty_status, new_status = asyncio.run(scenario())
        assert "selected" in empty_status
        assert "create new window" in new_status
        assert "brand-new-window" in new_status

    def test_choices_with_more_than_visible_window_render_truncation_marker(
        self,
    ) -> None:
        async def scenario() -> str:
            app = _Harness()
            async with app.run_test() as pilot:
                choices = tuple(
                    WindowChoice(
                        session_name="muxdeck",
                        window_id=f"@{i}",
                        window_name=f"win-{i}",
                        pane_count=1,
                    )
                    for i in range(1, 9)
                )
                screen = MoveWindowScreen("Planner", choices=choices)
                await app.push_screen(screen)
                await pilot.pause()
                # Move several times to push the visible window past index 0
                # so the leading "…" marker appears.
                for _ in range(6):
                    await pilot.press("down")
                await pilot.pause()
                return str(app.screen.query_one("#window-choice-list", Static).renderable)

        rendered = asyncio.run(scenario())
        assert "…" in rendered

    def test_current_window_marker_appears_in_choices(self) -> None:
        async def scenario() -> str:
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
                    ),
                )
                await app.push_screen(screen)
                await pilot.pause()
                return str(app.screen.query_one("#window-choice-list", Static).renderable)

        rendered = asyncio.run(scenario())
        assert "current" in rendered

    def test_window_with_no_name_uses_window_id_as_typed_value(self) -> None:
        async def scenario() -> str:
            app = _Harness()
            async with app.run_test() as pilot:
                screen = MoveWindowScreen(
                    "Planner",
                    choices=(
                        WindowChoice(
                            session_name="muxdeck",
                            window_id="@2",
                            window_name=None,
                            pane_count=1,
                        ),
                        WindowChoice(
                            session_name="muxdeck",
                            window_id="@3",
                            window_name=None,
                            pane_count=1,
                        ),
                    ),
                )
                await app.push_screen(screen)
                await pilot.pause()
                await pilot.press("down")
                await pilot.pause()
                return app.screen.query_one("#window-input-value", Input).value

        assert asyncio.run(scenario()) == "muxdeck:@3"
