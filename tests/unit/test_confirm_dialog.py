# ruff: noqa: E402

"""Tests for confirmation dialog."""

from __future__ import annotations

import asyncio
import sys
import unittest
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from textual.app import App, ComposeResult
from textual.pilot import Pilot
from textual.widgets import Button

from muxdeck.screens.confirm_dialog import ConfirmScreen


class _Harness(App[None]):
    def compose(self) -> ComposeResult:
        return iter(())


@dataclass(frozen=True, slots=True)
class _DismissResult:
    dismissed: bool
    value: bool | None


async def _run_with_action(
    action: Callable[[Pilot[None]], Awaitable[None]],
) -> _DismissResult:
    sentinel = object()
    captured: list[object] = [sentinel]

    def _capture(value: bool | None) -> None:
        captured[0] = value

    app = _Harness()
    async with app.run_test() as pilot:
        screen = ConfirmScreen(message="Delete it?", title="Danger")
        await app.push_screen(screen, callback=_capture)
        await pilot.pause()
        await action(pilot)
        await pilot.pause()
    raw = captured[0]
    if raw is sentinel:
        return _DismissResult(dismissed=False, value=None)
    assert isinstance(raw, bool)
    return _DismissResult(dismissed=True, value=raw)


def test_confirm_screen_init() -> None:
    screen = ConfirmScreen(message="Delete everything?", title="Danger")
    assert screen._message == "Delete everything?"
    assert screen._title == "Danger"


def test_confirm_screen_default_title() -> None:
    screen = ConfirmScreen(message="Are you sure?")
    assert screen._title == "Confirm"


def test_confirm_screen_has_arrow_bindings() -> None:
    """Left/right and h/l should be bound for button navigation."""
    binding_keys: set[str] = set()
    for binding in ConfirmScreen.BINDINGS:
        # Bindings can be tuples or Binding objects
        key = binding[0] if isinstance(binding, tuple) else binding.key
        for k in key.split(","):
            binding_keys.add(k.strip())
    assert "left" in binding_keys
    assert "right" in binding_keys
    assert "h" in binding_keys
    assert "l" in binding_keys


def test_confirm_screen_has_enter_binding() -> None:
    """Enter should activate the focused button."""
    binding_keys: set[str] = set()
    for binding in ConfirmScreen.BINDINGS:
        key = binding[0] if isinstance(binding, tuple) else binding.key
        for k in key.split(","):
            binding_keys.add(k.strip())
    assert "enter" in binding_keys


def test_confirm_screen_has_escape_yn_bindings() -> None:
    """Basic keyboard shortcuts must be present."""
    actions: set[str] = set()
    for binding in ConfirmScreen.BINDINGS:
        action = binding[1] if isinstance(binding, tuple) else binding.action
        actions.add(action)
    assert "cancel" in actions
    assert "confirm" in actions


class ConfirmScreenBehaviourTests(unittest.TestCase):
    def test_compose_focuses_no_button_by_default(self) -> None:
        async def scenario() -> bool:
            app = _Harness()
            async with app.run_test() as pilot:
                await app.push_screen(ConfirmScreen("ok?"))
                await pilot.pause()
                no_button = app.screen.query_one("#btn-no", Button)
                return no_button.has_focus

        assert asyncio.run(scenario()) is True

    def test_escape_dismisses_with_false(self) -> None:
        async def action(pilot: Pilot[None]) -> None:
            await pilot.press("escape")

        result = asyncio.run(_run_with_action(action))
        assert result.dismissed is True
        assert result.value is False

    def test_yes_key_dismisses_with_true(self) -> None:
        async def action(pilot: Pilot[None]) -> None:
            await pilot.press("y")

        result = asyncio.run(_run_with_action(action))
        assert result.dismissed is True
        assert result.value is True

    def test_no_key_dismisses_with_false(self) -> None:
        async def action(pilot: Pilot[None]) -> None:
            await pilot.press("n")

        result = asyncio.run(_run_with_action(action))
        assert result.dismissed is True
        assert result.value is False

    def test_yes_button_click_dismisses_with_true(self) -> None:
        async def action(pilot: Pilot[None]) -> None:
            await pilot.click("#btn-yes")

        result = asyncio.run(_run_with_action(action))
        assert result.dismissed is True
        assert result.value is True

    def test_no_button_click_dismisses_with_false(self) -> None:
        async def action(pilot: Pilot[None]) -> None:
            await pilot.click("#btn-no")

        result = asyncio.run(_run_with_action(action))
        assert result.dismissed is True
        assert result.value is False

    def test_focus_yes_then_press_focused_dismisses_with_true(self) -> None:
        async def action(pilot: Pilot[None]) -> None:
            # Right arrow focuses Yes; Enter activates the focused button.
            await pilot.press("right")
            await pilot.press("enter")

        result = asyncio.run(_run_with_action(action))
        assert result.dismissed is True
        assert result.value is True

    def test_focus_no_then_press_focused_dismisses_with_false(self) -> None:
        async def action(pilot: Pilot[None]) -> None:
            # Right then left to exercise both focus actions; final focus is No.
            await pilot.press("right")
            await pilot.press("left")
            await pilot.press("enter")

        result = asyncio.run(_run_with_action(action))
        assert result.dismissed is True
        assert result.value is False

    def test_press_focused_no_op_when_focus_is_not_a_button(self) -> None:
        # If nothing focusable is a Button, action_press_focused must safely
        # do nothing rather than raise. Drop focus, then press enter.
        async def scenario() -> bool:
            app = _Harness()
            async with app.run_test() as pilot:
                screen = ConfirmScreen("ok?")
                await app.push_screen(screen)
                await pilot.pause()
                screen.set_focus(None)
                await pilot.pause()
                # Directly invoke the action to exercise the non-Button branch
                # without depending on key routing.
                screen.action_press_focused()
                await pilot.pause()
                return isinstance(app.screen, ConfirmScreen)

        assert asyncio.run(scenario()) is True
