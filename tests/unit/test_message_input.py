"""Tests for the SendMessageScreen modal."""

from __future__ import annotations

import dataclasses

from muxdeck.screens.message_input import (
    MessageResult,
    SendMessageScreen,
)


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
