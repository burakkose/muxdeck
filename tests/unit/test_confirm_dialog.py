# ruff: noqa: E402

"""Tests for confirmation dialog."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from copilot_commander.screens.confirm_dialog import ConfirmScreen


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
