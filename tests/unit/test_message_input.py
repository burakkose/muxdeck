"""Tests for the SendMessageScreen modal."""

from __future__ import annotations

import asyncio
import dataclasses
import unittest
from collections.abc import Awaitable, Callable

from textual.app import App, ComposeResult
from textual.pilot import Pilot
from textual.widgets import Input

from muxdeck.screens.message_input import (
    MessageResult,
    SendMessageScreen,
)


class _Harness(App[None]):
    def compose(self) -> ComposeResult:
        return iter(())


@dataclasses.dataclass(frozen=True, slots=True)
class _DismissResult:
    dismissed: bool
    value: MessageResult | None


async def _run_with_action(
    action: Callable[[Pilot[None]], Awaitable[None]],
) -> _DismissResult:
    sentinel = object()
    captured: list[object] = [sentinel]

    def _capture(value: MessageResult | None) -> None:
        captured[0] = value

    app = _Harness()
    async with app.run_test() as pilot:
        screen = SendMessageScreen(agent_name="planner", pane_id="%9")
        await app.push_screen(screen, callback=_capture)
        await pilot.pause()
        await action(pilot)
        await pilot.pause()
    raw = captured[0]
    if raw is sentinel:
        return _DismissResult(dismissed=False, value=None)
    if raw is None:
        return _DismissResult(dismissed=True, value=None)
    assert isinstance(raw, MessageResult)
    return _DismissResult(dismissed=True, value=raw)


class TestMessageResult:
    def test_fields(self) -> None:
        result = MessageResult(text="hello", pane_id="%1")
        assert result.text == "hello"
        assert result.pane_id == "%1"

    def test_frozen(self) -> None:
        result = MessageResult(text="hello", pane_id="%1")
        assert dataclasses.is_dataclass(result)
        try:
            result.text = "nope"  # type: ignore[misc]
            raise AssertionError("Expected FrozenInstanceError")  # pragma: no cover
        except dataclasses.FrozenInstanceError:
            pass

    def test_slots(self) -> None:
        assert hasattr(MessageResult, "__slots__")


class TestSendMessageScreen:
    def test_screen_init(self) -> None:
        screen = SendMessageScreen(agent_name="tachyon", pane_id="%5")
        assert screen._agent_name == "tachyon"
        assert screen._pane_id == "%5"

    def test_compose_is_generator(self) -> None:
        screen = SendMessageScreen(agent_name="agent-1", pane_id="%2")
        # compose() uses Textual context managers that require a running app,
        # so we verify it is a generator (callable composable) without
        # iterating into the app-dependent context.
        import inspect

        assert inspect.isgeneratorfunction(screen.compose)


class SendMessageScreenBehaviourTests(unittest.TestCase):
    def test_compose_focuses_input_and_mounts_buttons(self) -> None:
        async def scenario() -> bool:
            app = _Harness()
            async with app.run_test() as pilot:
                screen = SendMessageScreen(agent_name="planner", pane_id="%9")
                await app.push_screen(screen)
                await pilot.pause()
                input_widget = app.screen.query_one("#message-input", Input)
                # Both buttons must exist; query_one raises if missing.
                app.screen.query_one("#btn-cancel")
                app.screen.query_one("#btn-send")
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

    def test_send_via_enter_returns_typed_text(self) -> None:
        async def action(pilot: Pilot[None]) -> None:
            pilot.app.screen.query_one("#message-input", Input).value = "  hi there  "
            await pilot.press("enter")

        result = asyncio.run(_run_with_action(action))
        assert result.dismissed is True
        assert result.value == MessageResult(text="hi there", pane_id="%9")

    def test_send_button_returns_typed_text(self) -> None:
        async def action(pilot: Pilot[None]) -> None:
            pilot.app.screen.query_one("#message-input", Input).value = "hello"
            await pilot.click("#btn-send")

        result = asyncio.run(_run_with_action(action))
        assert result.dismissed is True
        assert result.value == MessageResult(text="hello", pane_id="%9")

    def test_blank_send_keeps_screen_open(self) -> None:
        async def scenario() -> tuple[bool, bool, bool]:
            sentinel = object()
            captured: list[object] = [sentinel]

            def _capture(value: MessageResult | None) -> None:
                captured[0] = value

            app = _Harness()
            async with app.run_test() as pilot:
                screen = SendMessageScreen(agent_name="planner", pane_id="%9")
                await app.push_screen(screen, callback=_capture)
                await pilot.pause()
                app.screen.query_one("#message-input", Input).value = "   "
                await pilot.press("enter")
                await pilot.pause()
                still_modal = isinstance(app.screen, SendMessageScreen)
                refocused = app.screen.query_one("#message-input", Input).has_focus
                never_dismissed = captured[0] is sentinel
            return still_modal, refocused, never_dismissed

        still_modal, refocused, never_dismissed = asyncio.run(scenario())
        assert still_modal is True
        assert refocused is True
        assert never_dismissed is True
