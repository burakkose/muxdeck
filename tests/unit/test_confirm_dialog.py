"""Tests for confirmation dialog."""

from __future__ import annotations

from copilot_commander.screens.confirm_dialog import ConfirmScreen


def test_confirm_screen_init() -> None:
    screen = ConfirmScreen(message="Delete everything?", title="Danger")
    assert screen._message == "Delete everything?"
    assert screen._title == "Danger"


def test_confirm_screen_default_title() -> None:
    screen = ConfirmScreen(message="Are you sure?")
    assert screen._title == "Confirm"
