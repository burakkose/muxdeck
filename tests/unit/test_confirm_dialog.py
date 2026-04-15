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
